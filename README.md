<p align="center">
  <a href="https://github.com/lupaxa-workstation-toolbox">
    <img src="https://raw.githubusercontent.com/the-lupaxa-project/brand-assets/master/logos/organisations/workstation-toolbox/readme-logo.png" alt="Workstation Toolbox" />
  </a>
</p>

<h1 align="center">File Recipe</h1>

File Recipe plans a filesystem change, shows every operation, then runs it. A recipe is TOML. The plan lists the files it will touch, the destinations, and any conflict, before a user file is modified.

It works offline. It does not send telemetry, open a network connection, run a shell, or evaluate Python from a recipe.

This README is the usage guide and the developer guide.

## What it Does

- Load a versioned TOML recipe from a path, the project, your user directory, or the built-in examples.
- Discover files inside the input roots. Symlinks are not followed unless the recipe says so.
- Match names, sizes, times, types, and bounded content with declarative conditions.
- Render destinations from a small template language.
- Detect collisions, cycles, and overlapping paths.
- Preview or save the plan, then execute it with a journal.
- Undo, redo, and recover from that journal.
- Write text, JSON, JSON Lines, CSV, Markdown, and HTML reports.

> **Note:** A plan is not a transaction. A rename on the same filesystem is atomic. A cross-device move is a
> copy, a verification, then a delete. File Recipe does not claim the whole workflow is atomic when the
> filesystem cannot make it so. Another process can still change a file between the check and the write. The tool
> checks again immediately before each modification and aborts when the snapshot no longer matches.

## Requirements

- Python 3.11 or newer
- macOS, Linux, or Windows

## Install

```bash
pip install lupaxa-file-recipe
```

From a checkout:

```bash
make init
make install-dev
```

The console script is `file-recipe`. `python -m lupaxa.file_recipe` runs the same entry point.

## Quick Start

```bash
file-recipe init
file-recipe create sort-notes
file-recipe validate --recipe sort-notes
file-recipe preview --recipe sort-notes
file-recipe run --recipe sort-notes --dry-run
file-recipe run --recipe sort-notes --yes
```

`preview` and `plan` do not move, copy, or delete the files you named. `run` asks for confirmation when it will
modify files. `--yes` answers that question for work that is not destructive. Permanent delete and overwrite
still need `settings.allow_destructive = true` in the recipe, or `--allow-destructive` on the command.

A non-interactive run that would modify files exits with code 2 unless you pass `--yes` or `--dry-run`. It does not wait on a prompt.

## Recipe Format

A recipe is TOML with `version = 1`.

```toml
version = 1

[metadata]
name = "sort-notes"
description = "Move text files into a folder."
author = "The Lupaxa Project"
tags = ["notes"]

[input]
paths = ["."]
recursive = false

[output]
directory = "sorted"
collision = "error"

[rules]
mode = "first"

[[rules.items]]
name = "notes"
glob = "*.txt"

[rules.items.action]
type = "move"
destination = "{output}/notes/{filename}"
```

Metadata (`name`, `description`, `author`, `tags`, `docs`, `id`) is recorded on the plan. It does not choose files. `name` is required and is available to templates as `{recipe}`.

Unknown keys warn. `--strict` turns those warnings into an error.

Search order for a name:

1. A path, when the argument contains a slash or ends in `.toml`.
2. `./.file-recipe/recipes/<name>.toml` and `./recipes/<name>.toml`. Two project files with the same name is an error.
3. The user recipe directory.
4. The built-in examples.

## Operations

| Operation    | Effect                                                                                                                   |
| ------------ | ------------------------------------------------------------------------------------------------------------------------ |
| `rename`     | Change the file name in its current directory.                                                                           |
| `move`       | Move a file. Across devices this copies, verifies, then deletes the source.                                              |
| `copy`       | Copy a file and check the destination size.                                                                              |
| `delete`     | Quarantine the file. `permanent = true` deletes it, and that must be authorised.                                         |
| `quarantine` | Move the file aside and keep its original path for restore.                                                              |
| `mkdir`      | Create a directory from a template.                                                                                      |
| `metadata`   | Set mtime, atime, mode, or the read-only bit where the platform allows it.                                               |
| `hash`       | Stream SHA-256, SHA-512, or BLAKE2b and record the digest.                                                               |
| `dedupe`     | Group identical files. Keep `first`, `newest`, or `oldest`. Then `report`, `quarantine`, `move`, or authorised `delete`. |
| `text`       | Rewrite text through a temporary file in the same directory.                                                             |

`file-recipe operations` prints this list.

Ownership and extended attributes are rejected. The tool does not pretend it can set them.

Text actions need an explicit `encoding`. Binary files (a NUL in the first 8 KiB) are skipped unless `binary = true`. When undo is enabled, the original bytes are copied to the quarantine area and checked before the replacement.

## Matching

A rule selects files with `glob`, `globs`, `regex`, or `exact`. With none of those, the rule matches every file and the plan warns you.

`case_sensitive` defaults to true. `full_path` matches the relative path instead of the file name. `*.tar.gz` matches that compound suffix.

Discovery defaults:

- Stay inside the input roots.
- Do not follow symlinks.
- Skip hidden names.
- Skip the output directory, the journal, quarantine, temp, and `.file-recipe`.
- Order by relative path. `--order` can be `filename`, `relative`, `size`, `mtime`, or `ctime`.
- Do not include directories unless `include_directories` is set.

`follow_symlinks = true` rejects a link that leaves the root and rejects a cycle. `same_filesystem = true` skips other devices and reports them.

Pass paths on standard input with `--input -`. One path per line.

## Conditions

Conditions are tables, not expressions.

```toml
[rules.items.when]
all = [
  { size_min = 1 },
  { any = [{ glob = "*.txt" }, { glob = "*.md" }] },
]
not = { empty = true }
```

Supported tests include size, mtime, ctime, atime, type, permissions, read-only, empty, symlink, and owner (not on Windows). Content tests are `contains`, `not_contains`, `content_regex`, `hash`, `mime`, and `binary`.

Content reads stop at `limits.max_scan_bytes` (default 8 MiB). A larger file that a content rule must read is a limit error, not a silent miss. MIME comes from the standard library guess. When that guess is empty, the file does not match.

Rule mode is `first` (default), `all`, or `exclusive`. Exclusive mode conflicts when two rules select the same file.

## Templates

Templates substitute fields. They do not read attributes and they do not run code.

| Field                                          | Value                                                               |
| ---------------------------------------------- | ------------------------------------------------------------------- |
| `{filename}`                                   | File name                                                           |
| `{stem}`                                       | Name without the extension                                          |
| `{extension}`                                  | Extension without the leading dot. `archive.tar.gz` yields `tar.gz` |
| `{parent}`                                     | Parent directory name                                               |
| `{relative_path}`                              | Path relative to the input root                                     |
| `{year}` `{month}` `{day}` `{hour}` `{minute}` | From mtime, unless `date_source` is `ctime` or `now`                |
| `{size}`                                       | Size in bytes                                                       |
| `{counter}`                                    | Per-rule counter in discovery order                                 |
| `{hash}`                                       | Hash when a template asks for it                                    |
| `{recipe}` `{rule}`                            | Recipe and rule names                                               |
| `{output}`                                     | Output directory from the recipe or `--output`                      |

Formats: `{counter:04d}`, `{month:02d}`, `{stem:lower}`, `{stem:upper}`, `{hash:8}`.

`[variables]` and `--var key=value` supply extra names. Unknown names, reserved names (`filename`, `stem`, and the
other fields above), `..`, and absolute paths are rejected. `settings.allow_absolute = true` permits an absolute
destination. Values longer than 4096 characters are rejected.

Timezone is `UTC` by default, or `local`, or an offset such as `+10:00`.

## Multi-step Rules

Steps run in order. Each step sees the path planned by the step before it.

```toml
[[rules.items]]
name = "shots"
glob = "*.jpg"

[[rules.items.steps]]
type = "rename"
template = "{stem}_{counter:04d}.{extension}"

[[rules.items.steps]]
type = "move"
destination = "{output}/{year}/{filename}"
```

A step may set `when` with the same condition tables. Intermediate destinations go through the same collision checks.

## Chaining

```toml
[[recipes]]
name = "downloads"

[[recipes]]
name = "archive-prep"
```

Later recipes see the planned result of earlier ones. A cycle is an error. The default maximum depth is 5 (`settings.max_chain_depth`). Variables are shared only when the link says `inherit_variables = true`, and only for names the child declares.

One plan and one journal cover the chain. The run is not a success when a required stage fails.

`settings.on_error` is `stop` (default), `continue`, or `rollback`. Stop and rollback undo reversible operations from the failed run. Continue skips operations that depend on the failure and finishes with a partial result.

## Planning

`preview` prints the plan. `plan --plan plan.json` writes it. Neither command modifies the input files.

The plan records schema version `1.0`, the recipe name and digest, resolved variables, source snapshots, operations, destinations, conflicts, estimates, and a timestamp.

Detail is `--detail summary`, `detailed`, `conflicts`, or `warnings`.

A saved plan is untrusted. `run --plan plan.json` loads it and checks it again. If the recipe file digest changed,
or a source size or mtime changed, the run stops with exit code 10 and does not apply the plan. Set
`settings.snapshot_hash = true` when you also want content hashes in that check. The default check is size and
mtime, which stays cheap.

## Collisions

The default policy is `error`.

| Policy                | Behaviour                                                                                    |
| --------------------- | -------------------------------------------------------------------------------------------- |
| `error`               | The plan conflicts and `run` does not start.                                                 |
| `skip`                | The extra operation is skipped and the reason is recorded.                                   |
| `rename`              | `report.pdf` becomes `report_001.pdf`, then `report_002.pdf`.                                |
| `overwrite`           | Allowed only with destructive authorisation. The existing file is backed up when undo is on. |
| `quarantine-existing` | The existing file is quarantined, then the new file is written.                              |

Two operations that write the same destination conflict even under overwrite. A file written onto a directory
conflicts. Names that differ only by case or Unicode normalisation conflict, so a plan made on macOS still reports
a Windows collision. A rename cycle conflicts. A destination inside its own source conflicts.

Windows reserved names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`) are rejected on every platform.

## Dry Run

```bash
file-recipe run --recipe sort-notes --dry-run
```

Dry-run validates the recipe and the current files, prints the plan, and does not write a journal or change those files.

## Journals

Every modifying run writes a journal under the platform data directory:

- macOS: `~/Library/Application Support/file-recipe/journals`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/file-recipe/journals`
- Windows: `%APPDATA%\file-recipe\journals`

`FILE_RECIPE_DATA` overrides that root. The journal is replaced atomically. A record is stored as `prepared` before the irreversible step.

The journal stores the execution id, recipe name and digest, times, roots, variables, operations, snapshots, states, errors, and backup paths.

`file-recipe history` lists journals. `file-recipe status` shows the latest one, or `file-recipe status <id>`.

A truncated or unknown journal is reported and left as it is. File Recipe does not rewrite it to “fix” it.

## Undo and Redo

```bash
file-recipe undo last
file-recipe undo last --dry-run
file-recipe redo last
```

Undo walks completed operations in reverse dependency order. It can reverse a rename or move, remove an unchanged copy, remove an empty directory it created, restore a quarantine or a verified backup, and put metadata back.

It will not overwrite a file that changed after the run. A partial undo is reported. A permanent delete with undo disabled has no backup and cannot be reversed.

Redo checks the files again, then writes a new journal linked to the original. It does not skip the safety checks.

## Recovery

```bash
file-recipe recover last
file-recipe recover last --resume
file-recipe recover last --rollback
file-recipe recover last --resolved
```

Recovery looks at the filesystem, not only the journal. A prepared move where both the source and the destination
exist is ambiguous and needs a person. Resume continues pending work and recognises an operation whose end state
already matches. Rollback undoes completed reversible work. `--resolved` records that you handled it outside the
tool. It does not change files.

## Configuration

User config is `$FILE_RECIPE_CONFIG`, or `~/.config/file-recipe/config.toml`, or `%APPDATA%\file-recipe\config.toml` on Windows.

```toml
recipe_dir = "/path/to/recipes"
journal_dir = "/path/to/journals"
quarantine_dir = "/path/to/quarantine"
temp_dir = "/path/to/tmp"
strict = false
workers = 1
colour = true
collision = "error"
follow_symlinks = false
```

Precedence is the command line, then `--config`, then the recipe, then this file, then built-in defaults.

`allow_destructive` is not a config key. A user config file cannot turn permanent delete or overwrite on. The recipe has to set `settings.allow_destructive`, or you pass `--allow-destructive`.

Invalid values are errors.

## Command Line

```text
file-recipe init
file-recipe create NAME
file-recipe list
file-recipe show --recipe NAME
file-recipe validate --recipe NAME
file-recipe explain --recipe NAME
file-recipe preview --recipe NAME
file-recipe plan --recipe NAME --plan plan.json
file-recipe run --recipe NAME --yes
file-recipe history
file-recipe status [ID]
file-recipe undo [ID]
file-recipe redo [ID]
file-recipe recover [ID]
file-recipe report [ID]
file-recipe operations
file-recipe variables --recipe NAME
file-recipe config
file-recipe version
```

Shared options, where they apply: `--recipe`, `--input`, `--output`, `--recursive`, `--include`, `--exclude`,
`--var`, `--config`, `--format`, `--json`, `--quiet`, `--verbose`, `--no-colour`, `--yes`, `--force`,
`--dry-run`, `--strict`, `--workers`, `--timeout`.

`--no-color` is accepted as well. `NO_COLOR` also disables colour. Machine-readable stdout has no colour and no progress. Diagnostics go to stderr. Sizes in JSON are bytes.

`--workers` must be at least 1 and must not exceed `limits.max_workers` (default 8). Mutations stay in dependency order. Independent hash operations may use the pool when `--workers` is greater than 1.

## JSON Output

JSON documents use `"schema_version": "1.0"`. `--json` selects JSON. `--format` also accepts `text`, `jsonl`, `csv`, `markdown`, and `html`.

HTML is one self-contained file. It does not load remote assets. Text is escaped.

## Exit Codes

| Code | Meaning                                |
| ---- | -------------------------------------- |
| 0    | Success                                |
| 1    | General failure                        |
| 2    | Invalid arguments or an invalid recipe |
| 3    | The plan has a conflict                |
| 4    | A filesystem operation failed          |
| 5    | Verification failed                    |
| 6    | The run or the undo was partial        |
| 7    | Recovery needs a decision              |
| 8    | Undo failed                            |
| 9    | A resource limit was hit               |
| 10   | The plan is stale                      |

## Security Limits

File Recipe will not:

- Pass a path to a shell.
- Evaluate recipe text as code.
- Follow `..` out of a rendered destination.
- Follow a symlink out of an input root.
- Scan the output directory, the journal, or quarantine as if they were new input.

Limits, all optional except the scan default:

- `max_files` (default 100000)
- `max_total_bytes`
- `max_file_bytes`
- `max_scan_bytes` (default 8388608)
- `max_seconds`, or `--timeout`
- `max_workers` (default 8)
- `max_path_length`
- `max_chain_depth` (default 5)

Hitting a limit exits 9 and names the limit.

Quarantine keeps the original path in a sidecar. Cleanup is explicit. The tool does not delete old quarantine by age on its own. `settings.quarantine_days` is recorded for you to act on.

## Cross-platform Behaviour

Paths use the local separator. Templates use `/` and are converted when the destination is resolved.

Case-only and normalisation collisions are reported even on a case-sensitive disk. Permissions that the platform
cannot set fail the operation. A cross-device move is copy, verify, delete. Timestamp checks use nanoseconds when
the system provides them. Hard links are reported because more than one name points at the same bytes.

An operation the platform cannot perform fails at validation when that is knowable, and otherwise when the plan is built or when it runs. File Recipe does not substitute a different operation quietly.

## Troubleshooting

- Exit 2 on `run` with no prompt: the session is not a terminal. Pass `--yes` or `--dry-run`.
- Exit 3: read the conflict lines. Change the destination or the collision policy. Overwrite is not implied by `--yes`.
- Exit 10: a file or the recipe changed after `plan`. Build the plan again.
- Exit 7: `recover` found an ambiguous operation. Inspect it, then `--resume`, `--rollback`, or `--resolved`.
- A text rule skipped a file: it looked binary. Set `binary = true` only when you mean that.
- Undo refused a file: the file changed after the run, or the backup is missing.

## Example Recipes

These ship with the package and validate against version 1:

| Name                | What it Does                                                     |
| ------------------- | ---------------------------------------------------------------- |
| `downloads`         | Sort archives, documents, and images into kind and date folders. |
| `photos`            | File images by year and month.                                   |
| `documents`         | File documents by year, skipping empty files.                    |
| `temp-cleanup`      | Quarantine `*.tmp`, `*.temp`, and `*~`.                          |
| `duplicates`        | Quarantine duplicate bytes and keep the first file.              |
| `archive-prep`      | Gather archives into one folder.                                 |
| `rename-and-move`   | Rename with a counter, then move by year.                        |
| `safe-text-replace` | Replace `TODO` with `DONE` in UTF-8 text.                        |
| `chained`           | Run `downloads`, then `archive-prep`, on one plan.               |

```bash
file-recipe explain --recipe downloads
file-recipe preview --recipe downloads --input ~/Downloads
```

## Development

`make init` clones makefile-skills into `.makefiles/`. That directory is gitignored. `make install-dev` installs the `dev` extra: pytest, Ruff, mypy, Hatch, and bump-my-version.

```bash
make check
make test
make lint
make format
```

`make check` is what CI runs: Ruff, mypy, and pytest. Tests use temporary directories and synthetic files. They do not use the network and they do not touch unrelated files.

The version string is `__version__` in `src/lupaxa/file_recipe/version.py`. Hatch reads that file. The first version is `0.0.0`. Version tests check the semantic shape. They do not pin a literal version.

`make help` lists every target.

<a href="https://github.com/the-lupaxa-project">
  <img src="https://raw.githubusercontent.com/the-lupaxa-project/brand-assets/master/logos/components/footer-for-child-orgs.svg" alt="The Lupaxa Project Footer" width="100%" />
</a>
