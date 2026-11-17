"""Command implementations for the file-recipe CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO, cast

from lupaxa.file_recipe.config import Settings, load_config_file, resolve_settings, user_config
from lupaxa.file_recipe.engine import (
    execute_plan,
    list_journals,
    load_journal,
    recover_journal,
    redo_journal,
    undo_journal,
)
from lupaxa.file_recipe.errors import RecipeError
from lupaxa.file_recipe.models import OPERATION_CATALOGUE, Plan
from lupaxa.file_recipe.planner import Request, build_plan
from lupaxa.file_recipe.recipe import (
    Recipe,
    coerce_parameters,
    list_recipes,
    load_named,
    recipe_from_text,
)
from lupaxa.file_recipe.report import render_journal, render_plan
from lupaxa.file_recipe.version import get_version

COMMANDS = (
    "init",
    "create",
    "list",
    "show",
    "validate",
    "explain",
    "preview",
    "plan",
    "run",
    "history",
    "status",
    "undo",
    "redo",
    "recover",
    "report",
    "operations",
    "variables",
    "config",
    "version",
)


def dispatch(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Run the selected command."""
    result = globals()[f"cmd_{args.command}"](args, stdin, stdout)
    return cast("int", result)


def cmd_init(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Create the project recipe directory."""
    del stdin
    root = Path.cwd() / ".file-recipe" / "recipes"
    root.mkdir(parents=True, exist_ok=True)
    config = Path.cwd() / ".file-recipe" / "config.toml"
    if not config.exists() or args.force:
        config.write_text("# Project notes for file-recipe.\n", encoding="utf-8")
    _emit(stdout, args, {"schema_version": "1.0", "created": str(root)})
    return 0


def cmd_create(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Write a starter recipe."""
    del stdin
    directory = Path.cwd() / ".file-recipe" / "recipes"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{args.name}.toml"
    if path.exists() and not args.force:
        raise RecipeError(f"recipe already exists: {path}")
    path.write_text(_starter(str(args.name)), encoding="utf-8")
    _emit(stdout, args, {"schema_version": "1.0", "recipe": str(path)})
    return 0


def cmd_list(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """List available recipes."""
    del stdin
    rows = [
        {"name": name, "path": str(path), "tier": tier}
        for name, path, tier in list_recipes(Path.cwd(), _settings(args).recipe_dir)
    ]
    if _fmt(args) == "json":
        _emit(stdout, args, {"schema_version": "1.0", "recipes": rows})
    else:
        for row in rows:
            print(f"{row['name']}\t{row['tier']}\t{row['path']}", file=stdout)
    return 0


def cmd_show(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Print a recipe."""
    del stdin
    recipe = _load(args)
    print(recipe.source, file=stdout, end="" if recipe.source.endswith("\n") else "\n")
    return 0


def cmd_validate(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Validate a recipe without touching files."""
    del stdin
    recipe = _load(args)
    _emit(
        stdout,
        args,
        {"schema_version": "1.0", "name": recipe.name, "valid": True, "warnings": recipe.warnings},
    )
    return 0


def cmd_explain(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Explain what a recipe will consider."""
    del stdin
    recipe = _load(args)
    lines = [f"Recipe {recipe.name}: {recipe.metadata.get('description', '')}"]
    for rule in (recipe.body.get("rules") or {}).get("items") or []:
        kind = (rule.get("action") or {}).get("type") or "steps"
        lines.append(f"- {rule['name']} selects files and runs {kind}")
    for item in recipe.body.get("recipes") or []:
        lines.append(f"- then runs {item['name']}")
    print("\n".join(lines), file=stdout)
    return 0


def cmd_preview(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Preview a plan. No user file is modified."""
    return _show_plan(args, stdin, stdout, write=False)


def cmd_plan(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Write a plan. Input files are not modified."""
    return _show_plan(args, stdin, stdout, write=True)


def cmd_run(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Execute a new or saved plan."""
    plan = _plan(args, stdin)
    if plan.conflicts:
        print(render_plan(plan, "text"), file=sys.stderr, end="")
        return 3
    mutating = any(not op.skipped and op.kind not in {"hash", "report"} for op in plan.operations)
    if mutating and not args.dry_run and not args.yes and not _confirm(stdin):
        raise RecipeError("confirmation required; pass --yes or --dry-run")
    result = execute_plan(plan, dry_run=args.dry_run)
    if not args.quiet:
        if args.dry_run:
            print(render_plan(plan, _fmt(args), colour=_colour(args)), file=stdout, end="")
        else:
            print(render_journal(result, _fmt(args)), file=stdout, end="")
    return 0


def cmd_history(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """List journals."""
    del stdin
    rows = []
    for path in list_journals(_settings(args).journal_dir):
        data = load_journal(path)
        rows.append(
            {
                "id": data["id"],
                "state": data.get("state"),
                "recipe": data.get("recipe_name"),
                "path": str(path),
            }
        )
    _emit(stdout, args, {"schema_version": "1.0", "journals": rows})
    return 0


def cmd_status(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Show one journal."""
    del stdin
    print(render_journal(_journal(args), _fmt(args)), file=stdout, end="")
    return 0


def cmd_undo(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Undo a journal."""
    del stdin
    print(
        render_journal(undo_journal(_journal(args), dry_run=args.dry_run), _fmt(args)),
        file=stdout,
        end="",
    )
    return 0


def cmd_redo(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Redo an undone journal."""
    del stdin
    print(render_journal(redo_journal(_journal(args)), _fmt(args)), file=stdout, end="")
    return 0


def cmd_recover(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Inspect or continue a journal."""
    del stdin
    mode = "inspect"
    if args.resume:
        mode = "resume"
    elif args.rollback:
        mode = "rollback"
    elif args.resolved:
        mode = "resolved"
    print(render_journal(recover_journal(_journal(args), mode), _fmt(args)), file=stdout, end="")
    return 0


def cmd_report(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Render a journal or a plan."""
    del stdin
    if args.plan and Path(args.plan).is_file() and args.command == "report":
        plan = Plan.from_dict(json.loads(Path(args.plan).read_text(encoding="utf-8")))
        print(render_plan(plan, _fmt(args)), file=stdout, end="")
        return 0
    print(render_journal(_journal(args), _fmt(args)), file=stdout, end="")
    return 0


def cmd_operations(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """List supported operations."""
    del stdin
    rows = [{"name": name, "summary": summary} for name, summary in OPERATION_CATALOGUE]
    _emit(stdout, args, {"schema_version": "1.0", "operations": rows})
    return 0


def cmd_variables(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Show resolved variables."""
    del stdin
    resolved = coerce_parameters(_load(args), _vars(args))
    _emit(stdout, args, {"schema_version": "1.0", "variables": resolved})
    return 0


def cmd_config(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Show effective configuration."""
    del stdin
    settings = _settings(args)
    _emit(
        stdout,
        args,
        {
            "schema_version": "1.0",
            "journal_dir": str(settings.journal_dir),
            "quarantine_dir": str(settings.quarantine_dir),
            "temp_dir": str(settings.temp_dir),
            "recipe_dir": str(settings.recipe_dir),
            "strict": settings.strict,
            "workers": settings.workers,
            "colour": settings.colour,
            "collision": settings.collision,
            "sources": settings.sources,
        },
    )
    return 0


def cmd_version(args: argparse.Namespace, stdin: TextIO, stdout: TextIO) -> int:
    """Print the version."""
    del stdin
    _emit(stdout, args, {"schema_version": "1.0", "version": get_version()})
    return 0


def _show_plan(args: argparse.Namespace, stdin: TextIO, stdout: TextIO, *, write: bool) -> int:
    plan = _plan(args, stdin)
    fmt = _fmt(args)
    if args.detail == "conflicts" and fmt == "text":
        text = "\n".join(item["message"] for item in plan.conflicts)
        text = f"{text}\n" if text else ""
    elif args.detail == "warnings" and fmt == "text":
        text = "\n".join(plan.warnings)
        text = f"{text}\n" if text else ""
    elif args.detail == "summary" and fmt == "text":
        text = (
            f"Recipe: {plan.recipe_name}\n"
            f"Operations: {plan.estimates.get('operations', 0)}\n"
            f"Conflicts: {len(plan.conflicts)}\n"
        )
    else:
        text = render_plan(plan, fmt, colour=_colour(args))
    if write and args.plan:
        Path(args.plan).write_text(
            json.dumps(plan.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
    if not args.quiet:
        print(text, file=stdout, end="")
    return 3 if plan.conflicts else 0


def _plan(args: argparse.Namespace, stdin: TextIO) -> Plan:
    if args.command == "run" and args.plan and Path(args.plan).is_file():
        return Plan.from_dict(json.loads(Path(args.plan).read_text(encoding="utf-8")))
    return build_plan(_load(args), _request(args, stdin))


def _request(args: argparse.Namespace, stdin: TextIO) -> Request:
    settings = _settings(args)
    roots = None
    stdin_paths = None
    inputs = list(args.input or [])
    if inputs == ["-"]:
        from lupaxa.file_recipe.discover import read_stdin_paths

        stdin_paths = read_stdin_paths(stdin.read(), Path.cwd())
    elif inputs:
        roots = [Path(item) if Path(item).is_absolute() else Path.cwd() / item for item in inputs]
    return Request(
        cwd=Path.cwd(),
        roots=roots,
        output=args.output,
        recursive=args.recursive,
        includes=list(args.include or []),
        excludes=list(args.exclude or []),
        order=args.order,
        variables=_vars(args),
        strict=True if args.strict else None,
        workers=args.workers,
        timeout=args.timeout,
        allow_destructive=bool(args.allow_destructive),
        on_error=args.on_error,
        journal_dir=settings.journal_dir,
        quarantine_dir=settings.quarantine_dir,
        temp_dir=settings.temp_dir,
        stdin_paths=stdin_paths,
        user_recipe_dir=settings.recipe_dir,
    )


def _settings(args: argparse.Namespace) -> Settings:
    cli: dict[str, Any] = {}
    if args.workers is not None:
        cli["workers"] = args.workers
    if args.strict:
        cli["strict"] = True
    if args.no_colour or args.no_color or os.environ.get("NO_COLOR"):
        cli["colour"] = False
    config_file = load_config_file(Path(args.config)) if args.config else {}
    return resolve_settings(cli=cli, config_file=config_file, user=user_config())


def _load(args: argparse.Namespace) -> Recipe:
    if not args.recipe:
        raise RecipeError("pass --recipe")
    return load_named(args.recipe, Path.cwd(), _settings(args).recipe_dir, strict=bool(args.strict))


def _journal(args: argparse.Namespace) -> dict[str, Any]:
    settings = _settings(args)
    target = getattr(args, "target", "last")
    if target in (None, "last"):
        files = list_journals(settings.journal_dir)
        if not files:
            raise RecipeError("no journals")
        return load_journal(files[0])
    path = Path(str(target))
    if path.is_file():
        return load_journal(path)
    for candidate in list_journals(settings.journal_dir):
        if candidate.stem == target:
            return load_journal(candidate)
    raise RecipeError(f"journal not found: {target}")


def _vars(args: argparse.Namespace) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in args.var or []:
        if "=" not in item:
            raise RecipeError(f"invalid --var {item}; use key=value")
        key, value = item.split("=", 1)
        values[key] = value
    return values


def _fmt(args: argparse.Namespace) -> str:
    if args.json:
        return "json"
    return str(args.format)


def _colour(args: argparse.Namespace) -> bool:
    if args.no_colour or args.no_color or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _confirm(stdin: TextIO) -> bool:
    if not stdin.isatty():
        return False
    print("Proceed? [y/N] ", file=sys.stderr, end="")
    return stdin.readline().strip().lower() in {"y", "yes"}


def _emit(stdout: TextIO, args: argparse.Namespace, payload: dict[str, Any]) -> None:
    if args.quiet and _fmt(args) == "text":
        return
    if _fmt(args) != "text":
        print(json.dumps(payload, indent=2, sort_keys=True), file=stdout)
        return
    for key, value in payload.items():
        if key == "schema_version":
            continue
        print(f"{key}: {value}", file=stdout)


def _starter(name: str) -> str:
    text = f"""version = 1

[metadata]
name = "{name}"
description = "Move text files into a folder."
author = "The Lupaxa Project"
tags = ["example"]

[input]
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
destination = "{{output}}/notes/{{filename}}"
"""
    return recipe_from_text(text).source
