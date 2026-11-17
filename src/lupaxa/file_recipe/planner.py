"""Execution planning. Planning never modifies user files."""

from __future__ import annotations

import hashlib
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from lupaxa.file_recipe.discover import discover
from lupaxa.file_recipe.errors import LimitError, RecipeError
from lupaxa.file_recipe.matching import condition_match, identity_match, rule_shorthand
from lupaxa.file_recipe.models import FileInfo, Operation, Plan
from lupaxa.file_recipe.recipe import Recipe, coerce_parameters, load_named
from lupaxa.file_recipe.template import check_destination, compound_extension, render, stem_of

_WRITERS = {"rename", "move", "copy", "mkdir"}
_DEFAULT_SCAN = 8 * 1024 * 1024


@dataclass
class Request:
    """CLI and config values. ``None`` leaves the choice to the recipe."""

    cwd: Path
    roots: list[Path] | None = None
    output: str | None = None
    recursive: bool | None = None
    includes: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    order: str | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    strict: bool | None = None
    workers: int | None = None
    timeout: float | None = None
    allow_destructive: bool = False
    follow_symlinks: bool | None = None
    on_error: str | None = None
    journal_dir: Path = field(default_factory=Path)
    quarantine_dir: Path = field(default_factory=Path)
    temp_dir: Path = field(default_factory=Path)
    stdin_paths: list[Path] | None = None
    user_recipe_dir: Path = field(default_factory=Path)
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


class Shadow:
    """Planned changes that later recipes in a chain can see."""

    def __init__(self) -> None:
        """Start from the real filesystem."""
        self.gone: set[str] = set()
        self.extra: dict[str, FileInfo] = {}

    def apply(self, op: Operation, lookup: dict[str, FileInfo]) -> None:
        """Record one planned change."""
        if op.skipped:
            return
        source = lookup.get(op.source or "")
        if op.kind in {"move", "rename", "delete", "quarantine"} and op.source:
            self.gone.add(op.source)
            self.extra.pop(op.source, None)
        if op.kind in {"move", "rename", "copy"} and op.destination and source is not None:
            self.extra[op.destination] = replace(source, path=op.destination, origin="planned")
        if op.kind == "mkdir" and op.destination:
            self.extra[op.destination] = _dir_info(op.destination)

    def visible(self, found: list[FileInfo], roots: list[Path]) -> list[FileInfo]:
        """Drop removed paths and add files an earlier recipe planned."""
        rows = [item for item in found if item.path not in self.gone]
        known = {item.path for item in rows}
        for info in self.extra.values():
            if info.is_dir or info.path in known or info.path in self.gone:
                continue
            if any(_inside(root, Path(info.path)) for root in roots):
                rows.append(info)
        return rows

    def state(self, path: Path) -> str:
        """Return missing, file, or dir for a destination."""
        key = str(path.absolute()) if not path.is_absolute() else str(path)
        if key in self.gone and key not in self.extra:
            return "missing"
        if key in self.extra:
            return "dir" if self.extra[key].is_dir else "file"
        if path.is_dir():
            return "dir"
        if path.exists():
            return "file"
        return "missing"


def build_plan(recipe: Recipe, request: Request) -> Plan:
    """Build a plan for a recipe and its chain."""
    planner = _Planner(request)
    planner.plan_recipe(recipe, [], 0)
    planner.order()
    planner.parents()
    planner.order()
    return planner.to_plan(recipe)


class _Planner:
    """Accumulate operations for one top-level plan."""

    def __init__(self, request: Request) -> None:
        self.request = request
        self.shadow = Shadow()
        self.ops: list[Operation] = []
        self.conflicts: list[dict[str, str]] = []
        self.warnings: list[str] = []
        self.loaded: dict[str, Recipe] = {}
        self.lookup: dict[str, FileInfo] = {}
        self.roots: list[str] = []
        self._serial = 0
        self._resolved: set[str] = set()
        timeout = request.timeout
        self._deadline = time.monotonic() + timeout if timeout else None

    def plan_recipe(self, recipe: Recipe, stack: list[str], depth: int) -> None:
        """Plan one recipe, then explicit successors."""
        self._tick()
        if recipe.name in stack:
            raise RecipeError(f"recipe chain cycle at {recipe.name}")
        limit = int((recipe.body.get("settings") or {}).get("max_chain_depth", 5))
        if depth >= limit:
            raise LimitError(f"recipe chain exceeds max depth {limit}")
        self.loaded[recipe.name] = recipe
        self.warnings.extend(recipe.warnings)
        if self.strict(recipe) and recipe.warnings:
            raise RecipeError(recipe.warnings[0])
        start = len(self.ops)
        if recipe.body.get("rules"):
            self._rules(recipe)
            if bool((recipe.body.get("settings") or {}).get("snapshot_hash")):
                self._add_content_hashes(start)
        self._collisions(start)
        for op in self.ops[start:]:
            if not op.skipped:
                self.shadow.apply(op, self.lookup)
        saved = dict(self.request.variables)
        for item in recipe.body.get("recipes") or []:
            child = load_named(
                str(item["name"]),
                self.request.cwd,
                self.request.user_recipe_dir,
                strict=self.strict(recipe),
            )
            overrides = dict(item.get("variables") or {})
            if item.get("inherit_variables"):
                declared = set(child.body.get("variables") or {})
                declared.update(str(param["name"]) for param in child.body.get("parameters") or [])
                shared = {
                    key: value
                    for key, value in coerce_parameters(recipe, saved).items()
                    if key in declared
                }
                overrides = {**shared, **overrides}
            self.request.variables = {**saved, **overrides}
            self.plan_recipe(child, [*stack, recipe.name], depth + 1)
        self.request.variables = saved

    def to_plan(self, recipe: Recipe) -> Plan:
        """Serialise the accumulated plan."""
        active = [op for op in self.ops if not op.skipped]
        return Plan(
            recipe_name=recipe.name,
            recipe_digest=recipe.digest,
            recipe_path=str(recipe.path) if recipe.path else None,
            created_at=self.request.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            variables=coerce_parameters(recipe, self.request.variables),
            roots=self.roots,
            operations=self.ops,
            conflicts=self.conflicts,
            warnings=self.warnings,
            estimates={
                "files": len({op.source for op in active if op.source}),
                "operations": len(active),
                "bytes": sum(int((op.snapshot or {}).get("size", 0) or 0) for op in active),
                "conflicts": len(self.conflicts),
            },
            settings=self._settings(recipe),
        )

    def _add_content_hashes(self, start: int) -> None:
        """Record SHA-256 for filesystem sources when the recipe asks for it."""
        for op in self.ops[start:]:
            if op.skipped or not op.source or op.source_origin != "filesystem":
                continue
            path = Path(op.source)
            if path.is_file() and not path.is_symlink():
                op.snapshot["sha256"] = self.hash_file(op.source, "sha256")

    def _rules(self, recipe: Recipe) -> None:
        rules = recipe.body["rules"]
        mode = str(rules.get("mode", "first"))
        items = [item for item in rules.get("items") or [] if isinstance(item, dict)]
        found, notes = self._discover(recipe)
        self.warnings.extend(notes)
        found = self.shadow.visible(found, self._roots(recipe))
        for info in found:
            self.lookup[info.path] = info
        for item in items:
            if not any(item.get(key) for key in ("glob", "globs", "regex", "exact")):
                self.warnings.append(f"rule {item['name']} matches every file")
        counters = {str(item["name"]): 0 for item in items}
        dedupe_rules = []
        for info in found:
            self._tick()
            hits = [item for item in items if self._match(info, item, recipe)]
            if mode == "exclusive" and len(hits) > 1:
                names = ", ".join(str(item["name"]) for item in hits)
                self._conflict("exclusive-rule", f"{info.relative} matches {names}")
                continue
            chosen = hits[:1] if mode == "first" else hits
            for item in chosen:
                if _is_dedupe(item):
                    dedupe_rules.append(item)
                    continue
                counters[str(item["name"])] += 1
                self._steps(recipe, info, item, counters[str(item["name"])])
        seen_dedupe: set[str] = set()
        for item in dedupe_rules:
            if str(item["name"]) in seen_dedupe:
                continue
            seen_dedupe.add(str(item["name"]))
            group = [info for info in found if self._match(info, item, recipe)]
            self._dedupe(recipe, item, group)

    def _steps(self, recipe: Recipe, info: FileInfo, rule: dict[str, Any], counter: int) -> None:
        steps = list(rule.get("steps") or [])
        if not steps and "action" in rule:
            steps = [rule["action"]]
        current = info
        previous: str | None = None
        for step in steps:
            self._tick()
            if not self._when(current, step.get("when") or {}, recipe):
                continue
            op = self._operation(recipe, current, rule, step, counter)
            if previous:
                op.depends_on.append(previous)
            self.ops.append(op)
            previous = op.id
            if op.destination and op.kind in {"move", "rename", "copy"} and not op.skipped:
                current = replace(current, path=op.destination, origin="planned")
                self.lookup[current.path] = current

    def _operation(
        self,
        recipe: Recipe,
        info: FileInfo,
        rule: dict[str, Any],
        action: dict[str, Any],
        counter: int,
    ) -> Operation:
        kind = str(action["type"])
        ctx = self._context(recipe, info, str(rule["name"]), counter, action)
        allow_absolute = bool((recipe.body.get("settings") or {}).get("allow_absolute", False))
        source: str | None = info.path
        destination: str | None = None
        payload: dict[str, Any] = {}
        if kind == "rename":
            destination = _rename_destination(info, action, ctx, allow_absolute)
        elif kind in {"move", "copy"}:
            if "destination" not in action:
                raise RecipeError(f"rule {rule['name']} {kind} needs a destination")
            destination = self._abs(
                render(str(action["destination"]), ctx, allow_absolute=allow_absolute)
            )
        elif kind == "mkdir":
            source = None
            destination = self._abs(
                render(str(action.get("path", "")), ctx, allow_absolute=allow_absolute)
            )
        elif kind in {"delete", "quarantine"}:
            permanent = bool(action.get("permanent", False))
            if permanent and not self.destructive(recipe):
                raise RecipeError(
                    f"rule {rule['name']} permanent delete requires settings.allow_destructive"
                )
            payload = {"permanent": permanent, "quarantine_relative": _quarantine_relative(info)}
        elif kind == "text":
            self._guard(info, recipe)
            if "encoding" not in action:
                raise RecipeError(f"rule {rule['name']} text action needs an explicit encoding")
            payload = {
                "encoding": action["encoding"],
                "literal": list(action.get("literal") or []),
                "regex": list(action.get("regex") or []),
                "line_endings": action.get("line_endings"),
                "trim_trailing": bool(action.get("trim_trailing", False)),
                "final_newline": bool(action.get("final_newline", False)),
                "case": action.get("case"),
                "binary": bool(action.get("binary", False)),
            }
            destination = info.path
        elif kind == "metadata":
            payload = {
                "mtime": action.get("mtime"),
                "atime": action.get("atime"),
                "mode": action.get("mode"),
                "readonly": action.get("readonly"),
            }
            destination = info.path
        elif kind == "hash":
            self._guard(info, recipe)
            payload = {"algorithm": str(action.get("algorithm") or self._hash_name(recipe))}
        else:
            raise RecipeError(f"unsupported action {kind}")
        self._path_limit(recipe, destination)
        skipped = False
        reason = ""
        if (
            source
            and destination
            and _same(source, destination)
            and kind in {"move", "rename", "copy"}
        ):
            skipped = True
            reason = "source and destination are the same"
        undo = bool((recipe.body.get("settings") or {}).get("undo", True))
        return Operation(
            id=self._next(),
            kind=kind,
            rule=str(rule["name"]),
            recipe=recipe.name,
            source=source,
            destination=destination,
            payload=payload,
            source_origin=info.origin,
            snapshot=info.snapshot() if source else {},
            expected={"size": info.size} if kind in {"move", "rename", "copy"} else {},
            reversible=not (kind == "delete" and payload.get("permanent") and not undo),
            skipped=skipped,
            skip_reason=reason,
        )

    def _dedupe(self, recipe: Recipe, rule: dict[str, Any], group: list[FileInfo]) -> None:
        action = rule.get("action") or next(
            step for step in rule.get("steps") or [] if step.get("type") == "dedupe"
        )
        algorithm = str(action.get("hash") or self._hash_name(recipe))
        keep = str(action.get("keep", "first"))
        then = str(action.get("then", "report"))
        if then == "delete" and not self.destructive(recipe):
            raise RecipeError(
                f"rule {rule['name']} permanent delete requires settings.allow_destructive"
            )
        buckets: dict[str, list[FileInfo]] = {}
        for info in group:
            self._guard(info, recipe)
            buckets.setdefault(self.hash_file(info.path, algorithm), []).append(info)
        for digest, infos in buckets.items():
            if len(infos) < 2:
                continue
            keeper = _keeper(infos, keep)
            for info in infos:
                kept = info.path == keeper.path
                if then == "report" or kept:
                    self.ops.append(self._report(recipe, rule, info, digest, algorithm, kept))
                    continue
                if then == "move":
                    ctx = self._context(recipe, info, str(rule["name"]), 0, action)
                    ctx["hash"] = digest
                    destination = self._abs(
                        render(
                            str(action.get("destination", "{output}/duplicates/{filename}")),
                            ctx,
                            allow_absolute=False,
                        )
                    )
                    self.ops.append(
                        self._simple(recipe, rule, info, "move", destination, {"hash": digest})
                    )
                    continue
                permanent = then == "delete"
                self.ops.append(
                    self._simple(
                        recipe,
                        rule,
                        info,
                        "delete" if permanent else "quarantine",
                        None,
                        {
                            "hash": digest,
                            "permanent": permanent,
                            "quarantine_relative": _quarantine_relative(info),
                        },
                    )
                )

    def _report(
        self,
        recipe: Recipe,
        rule: dict[str, Any],
        info: FileInfo,
        digest: str,
        algorithm: str,
        kept: bool,
    ) -> Operation:
        return Operation(
            id=self._next(),
            kind="report",
            rule=str(rule["name"]),
            recipe=recipe.name,
            source=info.path,
            destination=None,
            payload={"hash": digest, "kept": kept, "algorithm": algorithm},
            source_origin=info.origin,
            snapshot=info.snapshot(),
        )

    def _simple(
        self,
        recipe: Recipe,
        rule: dict[str, Any],
        info: FileInfo,
        kind: str,
        destination: str | None,
        payload: dict[str, Any],
    ) -> Operation:
        undo = bool((recipe.body.get("settings") or {}).get("undo", True))
        return Operation(
            id=self._next(),
            kind=kind,
            rule=str(rule["name"]),
            recipe=recipe.name,
            source=info.path,
            destination=destination,
            payload=payload,
            source_origin=info.origin,
            snapshot=info.snapshot(),
            expected={"size": info.size} if destination else {},
            reversible=not (kind == "delete" and payload.get("permanent") and not undo),
        )

    def _collisions(self, start: int) -> None:
        occupied: dict[str, str] = {}
        claimed: set[str] = set()
        extras: list[Operation] = []
        for op in self.ops:
            if op.skipped or op.id in self._resolved:
                if op.destination and not op.skipped:
                    occupied[_norm(op.destination)] = op.destination
                continue
            self._resolved.add(op.id)
            if op.source and op.kind in {
                "move",
                "rename",
                "delete",
                "quarantine",
                "text",
                "metadata",
            }:
                if op.source in claimed:
                    self._conflict(
                        "removed-earlier", f"{op.source} is already changed by another operation"
                    )
                    op.skipped = True
                    op.skip_reason = "source was already claimed"
                    continue
                claimed.add(op.source)
            if op.kind not in _WRITERS or not op.destination:
                continue
            if op.source and _overlap(op.source, op.destination):
                self._conflict("overlap", f"{op.source} overlaps {op.destination}")
                continue
            key = _norm(op.destination)
            existing = self.shadow.state(Path(op.destination))
            if key in occupied and occupied[key] != op.destination:
                self._conflict("normalisation", f"{op.destination} collides with {occupied[key]}")
                continue
            if key in occupied or existing == "file":
                self._policy(op, occupied, existing, extras)
                continue
            if existing == "dir" and op.kind != "mkdir":
                self._conflict("file-dir", f"{op.destination} is a directory")
                continue
            sibling = _case_sibling(Path(op.destination))
            if sibling:
                self._conflict("case", f"{op.destination} collides with existing {sibling}")
                continue
            occupied[key] = op.destination
        if start:
            pass
        self.ops.extend(extras)

    def _policy(
        self,
        op: Operation,
        occupied: dict[str, str],
        existing: str,
        extras: list[Operation],
    ) -> None:
        policy = self.collision(op.recipe)
        dest = Path(op.destination or "")
        key = _norm(str(dest))
        if policy == "skip":
            op.skipped = True
            op.skip_reason = "destination collision"
            return
        if policy == "rename":
            renamed = _indexed(dest, occupied)
            op.destination = str(renamed)
            occupied[_norm(op.destination)] = op.destination
            return
        if policy == "overwrite" and existing == "file" and key not in occupied:
            if not self.destructive(self.loaded[op.recipe]):
                self._conflict(
                    "overwrite", f"overwrite of {op.destination} requires allow_destructive"
                )
                return
            op.payload["overwrite"] = True
            occupied[key] = op.destination or ""
            return
        if (
            policy == "quarantine-existing"
            and existing == "file"
            and key not in occupied
            and dest.is_file()
        ):
            stat = dest.stat()
            prep = Operation(
                id=self._next(),
                kind="quarantine",
                rule=op.rule,
                recipe=op.recipe,
                source=str(dest),
                destination=None,
                payload={
                    "permanent": False,
                    "quarantine_relative": _quarantine_relative(
                        FileInfo(
                            path=str(dest),
                            relative=dest.name,
                            root=str(dest.parent),
                            size=stat.st_size,
                            mtime_ns=stat.st_mtime_ns,
                            ctime_ns=stat.st_ctime_ns,
                            atime_ns=stat.st_atime_ns,
                            mode=stat.st_mode,
                            uid=getattr(stat, "st_uid", 0),
                            nlink=stat.st_nlink,
                            is_dir=False,
                            is_file=True,
                            is_symlink=False,
                        )
                    ),
                },
                snapshot={
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "is_file": True,
                    "is_dir": False,
                    "is_symlink": False,
                },
            )
            self._resolved.add(prep.id)
            op.depends_on.append(prep.id)
            extras.append(prep)
            occupied[key] = op.destination or ""
            return
        self._conflict("duplicate-destination", f"multiple operations write {op.destination}")

    def parents(self) -> None:
        """Plan directories that destinations need. Nothing is created here."""
        extras: list[Operation] = []
        known = {
            op.destination: op.id
            for op in self.ops
            if op.kind == "mkdir" and op.destination and not op.skipped
        }
        for op in list(self.ops):
            if op.skipped or not op.destination or op.kind not in {"move", "copy", "rename"}:
                continue
            parent = Path(op.destination).parent
            missing: list[Path] = []
            cursor = parent
            anchor = known.get(str(cursor))
            while not cursor.exists() and str(cursor) not in known and cursor != cursor.parent:
                if self.shadow.state(cursor) == "dir":
                    break
                missing.append(cursor)
                cursor = cursor.parent
            previous = None
            for directory in reversed(missing):
                made = Operation(
                    id=self._next(),
                    kind="mkdir",
                    rule=op.rule,
                    recipe=op.recipe,
                    source=None,
                    destination=str(directory),
                    payload={"implicit": True},
                    source_origin="planned",
                    depends_on=[previous] if previous else [],
                )
                known[str(directory)] = made.id
                previous = made.id
                extras.append(made)
            if previous and previous not in op.depends_on:
                op.depends_on.append(previous)
            elif anchor and anchor not in op.depends_on:
                op.depends_on.append(anchor)
        self.ops.extend(extras)

    def order(self) -> None:
        """Run operations that vacate a path before another write uses it."""
        by_source = {
            op.source: op.id
            for op in self.ops
            if op.source and op.kind in {"move", "rename"} and not op.skipped
        }
        by_dest = {
            op.destination: op.id
            for op in self.ops
            if op.destination and op.kind in {"move", "rename", "copy", "mkdir"} and not op.skipped
        }
        graph: dict[str, set[str]] = {}
        for op in self.ops:
            if (
                op.skipped
                or op.kind not in {"move", "rename"}
                or not op.source
                or not op.destination
            ):
                continue
            graph.setdefault(op.source, set()).add(op.destination)
        if _has_cycle(graph):
            self._conflict("rename-cycle", "rename or move operations form a cycle")
        for op in self.ops:
            if op.destination and op.kind in {"move", "rename", "copy"}:
                vacater = by_source.get(op.destination)
                if vacater and vacater != op.id and vacater not in op.depends_on:
                    op.depends_on.append(vacater)
            producer = by_dest.get(op.source or "")
            if producer and producer != op.id and producer not in op.depends_on:
                op.depends_on.append(producer)
        pending = {op.id: op for op in self.ops}
        ordered: list[Operation] = []
        seen: set[str] = set()

        def visit(op: Operation, stack: set[str]) -> None:
            if op.id in seen or op.id in stack:
                return
            stack.add(op.id)
            for dep in op.depends_on:
                if dep in pending:
                    visit(pending[dep], stack)
            stack.remove(op.id)
            seen.add(op.id)
            ordered.append(op)

        for op in self.ops:
            visit(op, set())
        self.ops = ordered

    def _discover(self, recipe: Recipe) -> tuple[list[FileInfo], list[str]]:
        spec = recipe.body.get("input") or {}
        limits = recipe.body.get("limits") or {}
        roots = self._roots(recipe)
        for root in roots:
            absolute = str(root.absolute())
            if absolute not in self.roots:
                self.roots.append(absolute)
        recursive = self.request.recursive
        if recursive is None:
            recursive = bool(spec.get("recursive", True))
        follow = self.request.follow_symlinks
        if follow is None:
            follow = bool(spec.get("follow_symlinks", False))
        return discover(
            roots,
            recursive=recursive,
            include_hidden=bool(spec.get("include_hidden", False)),
            follow_symlinks=follow,
            max_depth=int(spec.get("max_depth", 0) or 0),
            same_filesystem=bool(spec.get("same_filesystem", False)),
            include_directories=bool(spec.get("include_directories", False)),
            includes=[*list(spec.get("include") or []), *self.request.includes],
            excludes=[*list(spec.get("exclude") or []), *self.request.excludes],
            exclude_dirs=[
                self._output(recipe),
                self.request.journal_dir,
                self.request.quarantine_dir,
                self.request.temp_dir,
                self.request.cwd / ".file-recipe",
            ],
            max_files=int(limits.get("max_files", spec.get("max_files", 100_000)) or 0),
            max_total_bytes=int(limits.get("max_total_bytes", spec.get("max_total_bytes", 0)) or 0),
            order=self.request.order or str(spec.get("order", "path")),
            stdin_paths=self.request.stdin_paths,
        )

    def _match(self, info: FileInfo, rule: dict[str, Any], recipe: Recipe) -> bool:
        if not identity_match(info, rule):
            return False
        return self._when(info, rule.get("when") or {}, recipe) and self._when(
            info, rule_shorthand(rule), recipe
        )

    def _when(self, info: FileInfo, node: dict[str, Any], recipe: Recipe) -> bool:
        limits = recipe.body.get("limits") or {}
        return condition_match(
            info,
            node,
            scan_limit=int(limits.get("max_scan_bytes", _DEFAULT_SCAN) or _DEFAULT_SCAN),
            hash_algorithm=self._hash_name(recipe),
            hash_file=self.hash_file,
        )

    def _context(
        self,
        recipe: Recipe,
        info: FileInfo,
        rule: str,
        counter: int,
        action: dict[str, Any],
    ) -> dict[str, Any]:
        name = Path(info.path).name
        moment = _moment(info, recipe, self.request.now)
        values: dict[str, Any] = {
            "filename": name,
            "stem": stem_of(name),
            "extension": compound_extension(name),
            "parent": Path(info.path).parent.name,
            "relative_path": info.relative,
            "year": moment.year,
            "month": moment.month,
            "day": moment.day,
            "hour": moment.hour,
            "minute": moment.minute,
            "size": info.size,
            "counter": counter,
            "hash": "",
            "recipe": recipe.name,
            "rule": rule,
            "output": self._output_token(recipe),
            **coerce_parameters(recipe, self.request.variables),
        }
        if "{hash" in str(action) and info.is_file and info.origin != "planned":
            values["hash"] = self.hash_file(info.path, self._hash_name(recipe))
        return values

    def hash_file(self, path: str, algorithm: str) -> str:
        """Stream a file hash. The file is not modified."""
        self._tick()
        digest = hashlib.new(algorithm)
        with Path(path).open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                self._tick()
        return digest.hexdigest()

    def _roots(self, recipe: Recipe) -> list[Path]:
        if self.request.stdin_paths is not None:
            return [self.request.cwd]
        if self.request.roots:
            return self.request.roots
        configured = (recipe.body.get("input") or {}).get("paths") or ["."]
        roots: list[Path] = []
        for item in configured:
            path = Path(str(item))
            roots.append(path if path.is_absolute() else self.request.cwd / path)
        return roots

    def _output(self, recipe: Recipe) -> Path:
        path = Path(self._output_token(recipe))
        return path if path.is_absolute() else (self.request.cwd / path).absolute()

    def _output_token(self, recipe: Recipe) -> str:
        if self.request.output:
            return str(self.request.output)
        return str((recipe.body.get("output") or {}).get("directory", "output"))

    def _abs(self, rendered: str) -> str:
        path = Path(rendered)
        if not path.is_absolute():
            path = self.request.cwd / path
        return str(path.absolute())

    def _settings(self, recipe: Recipe) -> dict[str, Any]:
        settings = recipe.body.get("settings") or {}
        return {
            "on_error": self.request.on_error or settings.get("on_error", "stop"),
            "undo": bool(settings.get("undo", True)),
            "allow_destructive": self.destructive(recipe),
            "allow_absolute": bool(settings.get("allow_absolute", False)),
            "timezone": str(settings.get("timezone", "UTC")),
            "date_source": str(settings.get("date_source", "mtime")),
            "hash": self._hash_name(recipe),
            "workers": self.workers(recipe),
            "strict": self.strict(recipe),
            "collision": self.collision(recipe.name),
            "journal_dir": str(self.request.journal_dir),
            "quarantine_dir": str(self.request.quarantine_dir),
            "temp_dir": str(self.request.temp_dir),
            "quarantine_days": int(settings.get("quarantine_days", 0) or 0),
            "snapshot_hash": bool(settings.get("snapshot_hash", False)),
        }

    def workers(self, recipe: Recipe) -> int:
        """Return the worker cap. Values above the recipe limit are refused."""
        cap = int((recipe.body.get("limits") or {}).get("max_workers", 8) or 8)
        chosen = self.request.workers or int(
            (recipe.body.get("settings") or {}).get("workers", 1) or 1
        )
        if chosen < 1:
            raise RecipeError("workers must be at least 1")
        if chosen > cap:
            raise LimitError(f"workers {chosen} exceeds max_workers {cap}")
        return chosen

    def strict(self, recipe: Recipe) -> bool:
        """Return whether unknown settings are errors."""
        if self.request.strict is not None:
            return self.request.strict
        return bool((recipe.body.get("settings") or {}).get("strict", False))

    def collision(self, recipe_name: str) -> str:
        """Return the collision policy for a recipe in this plan."""
        recipe = self.loaded[recipe_name]
        return str((recipe.body.get("output") or {}).get("collision", "error"))

    def destructive(self, recipe: Recipe) -> bool:
        """Return whether permanent delete and overwrite were authorised."""
        flag = bool((recipe.body.get("settings") or {}).get("allow_destructive", False))
        return flag or self.request.allow_destructive

    def _hash_name(self, recipe: Recipe) -> str:
        return str((recipe.body.get("settings") or {}).get("hash", "sha256"))

    def _guard(self, info: FileInfo, recipe: Recipe) -> None:
        limit = int((recipe.body.get("limits") or {}).get("max_file_bytes", 0) or 0)
        if limit and info.size > limit:
            raise LimitError(f"file exceeds max_file_bytes ({limit} bytes): {info.relative}")

    def _path_limit(self, recipe: Recipe, destination: str | None) -> None:
        limit = int((recipe.body.get("limits") or {}).get("max_path_length", 0) or 0)
        if destination and limit and len(destination) > limit:
            raise LimitError(f"path length exceeds {limit}: {destination}")

    def _conflict(self, code: str, message: str) -> None:
        self.conflicts.append({"code": code, "message": message})

    def _next(self) -> str:
        self._serial += 1
        return f"op-{self._serial:04d}"

    def _tick(self) -> None:
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise LimitError("duration limit exceeded")


def _is_dedupe(rule: dict[str, Any]) -> bool:
    if (rule.get("action") or {}).get("type") == "dedupe":
        return True
    return any(step.get("type") == "dedupe" for step in rule.get("steps") or [])


def _rename_destination(
    info: FileInfo,
    action: dict[str, Any],
    ctx: dict[str, Any],
    allow_absolute: bool,
) -> str:
    name = Path(info.path).name
    if action.get("regex"):
        pattern = str(action["regex"])
        if len(pattern) > 1000:
            raise RecipeError("rename pattern is too long")
        name = re.sub(pattern, str(action.get("replacement", "")), name)
    if action.get("case") == "lower":
        name = name.lower()
    elif action.get("case") == "upper":
        name = name.upper()
    if action.get("prefix"):
        name = render(str(action["prefix"]), ctx, allow_absolute=False) + name
    if action.get("suffix"):
        extension = compound_extension(name)
        stem = stem_of(name)
        suffix = render(str(action["suffix"]), ctx, allow_absolute=False)
        name = f"{stem}{suffix}.{extension}" if extension else f"{stem}{suffix}"
    if action.get("template"):
        name = render(str(action["template"]), ctx, allow_absolute=False)
        if "/" in name or "\\" in name:
            raise RecipeError("rename template must be a single file name")
    if "new_extension" in action:
        extension = str(action["new_extension"])
        stem = stem_of(name)
        name = f"{stem}.{extension}" if extension else stem
    return check_destination(str(Path(info.path).parent / name), allow_absolute=allow_absolute)


def _moment(info: FileInfo, recipe: Recipe, now: datetime) -> datetime:
    settings = recipe.body.get("settings") or {}
    source = str(settings.get("date_source", "mtime"))
    if source == "now":
        stamp = now if now.tzinfo else now.replace(tzinfo=UTC)
    else:
        nanos = info.ctime_ns if source == "ctime" else info.mtime_ns
        stamp = datetime.fromtimestamp(nanos / 1_000_000_000, UTC)
    zone = str(settings.get("timezone", "UTC"))
    if zone == "UTC":
        return stamp.astimezone(UTC)
    if zone == "local":
        return stamp.astimezone()
    sign = 1 if zone[0] == "+" else -1
    offset = timezone(timedelta(hours=sign * int(zone[1:3]), minutes=sign * int(zone[4:6])))
    return stamp.astimezone(offset)


def _quarantine_relative(info: FileInfo) -> str:
    token = hashlib.sha256(info.root.encode()).hexdigest()[:8]
    return f"{token}/{info.relative}"


def _keeper(infos: list[FileInfo], keep: str) -> FileInfo:
    if keep == "newest":
        return max(infos, key=lambda item: (item.mtime_ns, item.relative))
    if keep == "oldest":
        return min(infos, key=lambda item: (item.mtime_ns, item.relative))
    if keep != "first":
        raise RecipeError(f"unsupported dedupe keep {keep!r}")
    return infos[0]


def _norm(path: str) -> str:
    return unicodedata.normalize("NFC", os.path.normcase(os.path.abspath(path)))


def _same(left: str, right: str) -> bool:
    return Path(left).absolute() == Path(right).absolute()


def _overlap(source: str, destination: str) -> bool:
    src = Path(source).absolute()
    dst = Path(destination).absolute()
    if src == dst:
        return False
    try:
        dst.relative_to(src)
        return True
    except ValueError:
        pass
    try:
        src.relative_to(dst)
    except ValueError:
        return False
    return dst.exists() and dst.is_dir()


def _case_sibling(dest: Path) -> str | None:
    parent = dest.parent
    if not parent.is_dir():
        return None
    wanted = unicodedata.normalize("NFC", dest.name.casefold())
    for entry in parent.iterdir():
        if entry.name == dest.name:
            continue
        if unicodedata.normalize("NFC", entry.name.casefold()) == wanted:
            return str(entry)
    return None


def _indexed(dest: Path, occupied: dict[str, str]) -> Path:
    number = 1
    while number <= 10000:
        extension = compound_extension(dest.name)
        if extension:
            candidate = dest.with_name(f"{stem_of(dest.name)}_{number:03d}.{extension}")
        elif dest.suffix:
            candidate = dest.with_name(f"{dest.stem}_{number:03d}{dest.suffix}")
        else:
            candidate = dest.with_name(f"{dest.name}_{number:03d}")
        key = _norm(str(candidate))
        if key not in occupied and not candidate.exists() and _case_sibling(candidate) is None:
            return candidate
        number += 1
    raise LimitError("collision rename counter exceeded 10000")


def _has_cycle(graph: dict[str, set[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for nxt in graph.get(node, ()):
            if walk(nxt):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(walk(node) for node in list(graph))


def _inside(root: Path, path: Path) -> bool:
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError:
        return False
    return True


def _dir_info(path: str) -> FileInfo:
    return FileInfo(
        path=path,
        relative=Path(path).name,
        root="",
        size=0,
        mtime_ns=0,
        ctime_ns=0,
        atime_ns=0,
        mode=0o40755,
        uid=0,
        nlink=1,
        is_dir=True,
        is_file=False,
        is_symlink=False,
        origin="planned",
    )
