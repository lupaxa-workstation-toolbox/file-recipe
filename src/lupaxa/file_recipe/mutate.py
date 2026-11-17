"""Filesystem changes used by the journal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lupaxa.file_recipe.errors import (
    FileRecipeError,
    FilesystemError,
    RecipeError,
    RecoveryError,
    StalePlanError,
    UndoError,
    VerificationError,
)
from lupaxa.file_recipe.models import JSON_SCHEMA, Operation, Plan


def new_journal(plan: Plan) -> dict[str, Any]:
    """Create an in-memory journal for a plan."""
    rows = []
    for op in plan.operations:
        row = op.to_dict()
        row["state"] = "skipped" if op.skipped else "pending"
        row["error"] = op.skip_reason if op.skipped else ""
        row["backup"] = ""
        row["before"] = {}
        row["after"] = {}
        rows.append(row)
    return {
        "schema_version": JSON_SCHEMA,
        "id": __import__("uuid").uuid4().hex,
        "recipe_name": plan.recipe_name,
        "recipe_digest": plan.recipe_digest,
        "recipe_path": plan.recipe_path,
        "started_at": stamp(),
        "finished_at": "",
        "state": "running",
        "input_roots": plan.roots,
        "variables": plan.variables,
        "settings": plan.settings,
        "operations": rows,
        "undo_of": None,
        "redo_of": None,
    }


def save_journal(journal: dict[str, Any]) -> None:
    """Atomically replace the journal file."""
    directory = Path(str(journal["settings"]["journal_dir"]))
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{journal['id']}.json"
    temporary = directory / f".{journal['id']}.json.tmp"
    temporary.write_text(json.dumps(journal, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)


def apply_row(row: dict[str, Any], journal: dict[str, Any], plan: Plan) -> None:
    """Perform one operation. The caller has already written a prepared record."""
    kind = row["kind"]
    if kind == "mkdir":
        _mkdir(row)
    elif kind in {"move", "rename"}:
        _transfer(row, journal, plan, delete_source=True)
    elif kind == "copy":
        _transfer(row, journal, plan, delete_source=False)
    elif kind in {"delete", "quarantine"}:
        _take_aside(row, journal, plan)
    elif kind == "text":
        _text(row, journal, plan)
    elif kind == "metadata":
        _metadata(row)
    elif kind == "hash":
        algorithm = str(row["payload"].get("algorithm", "sha256"))
        row["payload"]["digest"] = digest(str(row["source"]), algorithm)
        row["after"] = {"digest": row["payload"]["digest"]}
    elif kind == "report":
        row["after"] = {"reported": True}
    else:
        raise RecipeError(f"unsupported operation {kind}")


def verify_row(row: dict[str, Any]) -> None:
    """Confirm the filesystem matches the operation that just ran."""
    if row["state"] == "skipped":
        return
    kind = row["kind"]
    if kind in {"hash", "report"}:
        return
    if kind in {"delete", "quarantine"}:
        if Path(str(row["source"])).exists():
            raise VerificationError(f"source still exists after {kind}: {row['source']}")
        return
    dest = row.get("destination") or row.get("source")
    if dest and not Path(str(dest)).exists():
        raise VerificationError(f"missing result for {row['id']}")


def undo_row(row: dict[str, Any]) -> None:
    """Reverse one completed operation or refuse with a clear error."""
    kind = row["kind"]
    if kind in {"hash", "report"}:
        return
    if kind == "mkdir":
        path = Path(str(row["destination"]))
        if row["payload"].get("existed_before"):
            return
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
            return
        raise UndoError(f"directory is not empty: {path}")
    if kind == "copy":
        dest = Path(str(row["destination"]))
        if not matches_after(dest, row.get("after") or {}):
            raise UndoError(f"refusing to remove changed file {dest}")
        dest.unlink()
        return
    if kind in {"move", "rename"}:
        dest = Path(str(row["destination"]))
        source = Path(str(row["source"]))
        if source.exists():
            raise UndoError(f"refusing to overwrite {source}")
        if not matches_after(dest, row.get("after") or {}):
            raise UndoError(f"refusing to move changed file {dest}")
        source.parent.mkdir(parents=True, exist_ok=True)
        os.replace(dest, source)
        restore_mtime(source, row.get("snapshot") or {})
        return
    if kind in {"delete", "quarantine", "text"}:
        restore_backup(row)
        return
    if kind == "metadata":
        path = Path(str(row["source"]))
        before = row.get("before") or {}
        if "mode" in before:
            os.chmod(path, int(before["mode"]))
        if "mtime_ns" in before and "atime_ns" in before:
            os.utime(path, ns=(int(before["atime_ns"]), int(before["mtime_ns"])))
        return
    raise UndoError(f"cannot undo {kind}")


def rollback(records: list[dict[str, Any]], journal: dict[str, Any]) -> None:
    """Undo completed reversible operations after a failed run."""
    completed = [
        row for row in records if row.get("state") == "completed" and row.get("reversible", True)
    ]
    for row in reversed(completed):
        try:
            undo_row(row)
            row["state"] = "rolled_back"
        except FileRecipeError as exc:
            row["state"] = "recovery_required"
            row["error"] = str(exc)
            journal["state"] = "recovery_required"
            save_journal(journal)
            raise RecoveryError(str(exc)) from exc
    journal["state"] = "rolled_back"
    save_journal(journal)


def assert_recipe_fresh(plan: Plan) -> None:
    """Refuse a plan whose recipe file no longer matches the recorded digest."""
    if not plan.recipe_path:
        return
    path = Path(plan.recipe_path)
    if not path.is_file():
        return
    current = hashlib.sha256(path.read_bytes()).hexdigest()
    if current != plan.recipe_digest:
        raise StalePlanError(f"recipe changed after the plan was written: {path}")


def assert_source_fresh(op: Operation) -> None:
    """Refuse a filesystem source whose size or mtime changed."""
    if op.skipped or not op.source or op.kind == "mkdir":
        return
    path = Path(op.source)
    try:
        stat = path.lstat()
    except OSError as exc:
        raise StalePlanError(f"source is missing: {op.source}") from exc
    if op.source_origin == "planned":
        return
    snapshot = op.snapshot or {}
    if (
        "size" in snapshot
        and not snapshot.get("is_symlink")
        and int(stat.st_size) != int(snapshot["size"])
    ):
        raise StalePlanError(f"source changed size: {op.source}")
    if "mtime_ns" in snapshot and int(stat.st_mtime_ns) != int(snapshot["mtime_ns"]):
        raise StalePlanError(f"source changed mtime: {op.source}")
    if (
        snapshot.get("sha256")
        and not snapshot.get("is_symlink")
        and digest(path, "sha256") != snapshot["sha256"]
    ):
        raise StalePlanError(f"source content changed: {op.source}")


def inspect_row(row: dict[str, Any]) -> dict[str, Any]:
    """Describe how a journal row compares with the filesystem."""
    kind = row["kind"]
    source = Path(str(row["source"])) if row.get("source") else None
    dest = Path(str(row["destination"])) if row.get("destination") else None
    ambiguous = False
    status = "pending"
    if kind in {"hash", "report"} or row.get("state") == "skipped":
        status = "complete"
    elif kind in {"move", "rename"} and source and dest:
        if source.exists() and dest.exists():
            ambiguous = True
            status = "ambiguous"
        elif dest.exists() and not source.exists():
            status = "complete"
        else:
            status = "pending"
    elif kind == "copy" and dest:
        status = "complete" if dest.exists() else "pending"
    elif kind in {"delete", "quarantine"} and source:
        status = "pending" if source.exists() else "complete"
    elif kind == "mkdir" and dest:
        status = "complete" if dest.is_dir() else "pending"
    elif kind == "text" and source:
        after = row.get("after") or {}
        if after.get("sha256") and source.exists() and digest(source, "sha256") == after["sha256"]:
            status = "complete"
        elif source.exists():
            status = "pending"
        else:
            ambiguous = True
            status = "ambiguous"
    return {
        "id": row["id"],
        "kind": kind,
        "state": row.get("state"),
        "status": status,
        "ambiguous": ambiguous,
    }


def digest(path: str | Path, algorithm: str) -> str:
    """Stream a hash of a file."""
    hashed = hashlib.new(algorithm)
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hashed.update(chunk)
    return hashed.hexdigest()


def stamp() -> str:
    """Return a UTC timestamp."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def plan_from_journal(journal: dict[str, Any], operations: list[Operation]) -> Plan:
    """Build a plan object from a journal so it can be executed again."""
    return Plan(
        recipe_name=str(journal.get("recipe_name", "")),
        recipe_digest=str(journal.get("recipe_digest", "")),
        recipe_path=journal.get("recipe_path"),
        created_at=str(journal.get("started_at", "")),
        variables=dict(journal.get("variables") or {}),
        roots=list(journal.get("input_roots") or []),
        operations=operations,
        conflicts=[],
        warnings=[],
        estimates={},
        settings=dict(journal.get("settings") or {}),
    )


def dependency_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order rows so dependencies come first."""
    pending = {row["id"]: row for row in rows}
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(row: dict[str, Any], stack: set[str]) -> None:
        if row["id"] in seen or row["id"] in stack:
            return
        stack.add(row["id"])
        for dep in row.get("depends_on") or []:
            if dep in pending:
                visit(pending[dep], stack)
        stack.remove(row["id"])
        seen.add(row["id"])
        ordered.append(row)

    for row in rows:
        visit(row, set())
    return ordered


def _mkdir(row: dict[str, Any]) -> None:
    path = Path(str(row["destination"]))
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    row["payload"]["existed_before"] = existed
    row["after"] = {"is_dir": True}


def _transfer(
    row: dict[str, Any],
    journal: dict[str, Any],
    plan: Plan,
    *,
    delete_source: bool,
) -> None:
    source = Path(str(row["source"]))
    dest = Path(str(row["destination"]))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not row["payload"].get("overwrite"):
        raise FilesystemError(f"destination exists: {dest}")
    if dest.exists() and row["payload"].get("overwrite"):
        row["backup"] = str(_backup(dest, journal, plan))
    if delete_source and _same_device(source, dest):
        if dest.exists():
            os.replace(source, dest)
        else:
            os.rename(source, dest)
    elif delete_source:
        _copy_verified(source, dest)
        if digest(source, "sha256") != digest(dest, "sha256"):
            dest.unlink()
            raise VerificationError(f"cross-device copy did not match {source}")
        source.unlink()
    else:
        if dest.exists():
            dest.unlink()
        shutil.copy2(source, dest)
    stat = dest.stat()
    expected = int(row.get("expected", {}).get("size", stat.st_size))
    if expected != stat.st_size:
        raise VerificationError(f"size mismatch after writing {dest}")
    row["after"] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _take_aside(row: dict[str, Any], journal: dict[str, Any], plan: Plan) -> None:
    source = Path(str(row["source"]))
    permanent = bool(row["payload"].get("permanent"))
    undo = bool(plan.settings.get("undo", True))
    if permanent and not plan.settings.get("allow_destructive"):
        raise RecipeError("permanent delete requires allow_destructive")
    if permanent and not undo:
        source.unlink()
        row["reversible"] = False
        row["after"] = {"deleted": True}
        return
    dest = _quarantine_path(row, journal, plan)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _copy_verified(source, dest)
    meta = {
        "original": str(source),
        "size": source.stat().st_size,
        "mtime_ns": source.stat().st_mtime_ns,
        "mode": source.stat().st_mode & 0o777,
    }
    sidecar = dest.parent / f"{dest.name}.file-recipe.json"
    sidecar.write_text(json.dumps(meta), encoding="utf-8")
    source.unlink()
    row["destination"] = str(dest)
    row["backup"] = str(dest)
    row["after"] = {"size": dest.stat().st_size, "mtime_ns": dest.stat().st_mtime_ns}


def _text(row: dict[str, Any], journal: dict[str, Any], plan: Plan) -> None:
    path = Path(str(row["source"]))
    payload = row["payload"]
    original = path.read_bytes()
    if b"\0" in original[:8192] and not payload.get("binary"):
        row["state"] = "skipped"
        row["skip_reason"] = "binary file skipped"
        row["after"] = {"skipped": True}
        return
    encoding = str(payload["encoding"])
    try:
        text = original.decode(encoding)
    except UnicodeError as exc:
        raise FilesystemError(f"cannot decode {path} as {encoding}") from exc
    for item in payload.get("literal") or []:
        text = text.replace(str(item.get("find", "")), str(item.get("replace", "")))
    for item in payload.get("regex") or []:
        pattern = str(item.get("pattern", ""))
        if len(pattern) > 1000:
            raise RecipeError("text pattern is too long")
        text = re.sub(pattern, str(item.get("replace", "")), text)
    if payload.get("line_endings"):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if payload["line_endings"] == "crlf":
            text = text.replace("\n", "\r\n")
    if payload.get("trim_trailing"):
        text = "\n".join(part.rstrip(" \t") for part in text.split("\n"))
        if payload.get("line_endings") == "crlf":
            text = text.replace("\n", "\r\n")
    if payload.get("final_newline") and text and not text.endswith("\n"):
        text += "\r\n" if payload.get("line_endings") == "crlf" else "\n"
    if payload.get("case") == "lower":
        text = text.lower()
    elif payload.get("case") == "upper":
        text = text.upper()
    data = text.encode(encoding)
    if plan.settings.get("undo", True):
        row["backup"] = str(_backup(path, journal, plan))
    temporary = path.parent / f".{path.name}.{journal['id']}.tmp"
    temporary.write_bytes(data)
    os.replace(temporary, path)
    if path.read_bytes() != data:
        raise VerificationError(f"text rewrite did not match {path}")
    row["after"] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def _metadata(row: dict[str, Any]) -> None:
    path = Path(str(row["source"]))
    stat = path.stat()
    row["before"] = {
        "mode": stat.st_mode & 0o777,
        "mtime_ns": stat.st_mtime_ns,
        "atime_ns": stat.st_atime_ns,
    }
    payload = row["payload"]
    mode = stat.st_mode
    if payload.get("mode") is not None:
        mode = (mode & ~0o777) | int(str(payload["mode"]), 8)
    if payload.get("readonly") is True:
        mode = mode & ~0o222
    elif payload.get("readonly") is False:
        mode = mode | 0o200
    os.chmod(path, mode & 0o777)
    atime = stat.st_atime_ns
    mtime = stat.st_mtime_ns
    if payload.get("atime"):
        atime = _parse_ns(payload["atime"])
    if payload.get("mtime"):
        mtime = _parse_ns(payload["mtime"])
    os.utime(path, ns=(atime, mtime))
    fresh = path.stat()
    row["after"] = {"mode": fresh.st_mode & 0o777, "mtime_ns": fresh.st_mtime_ns}


def restore_backup(row: dict[str, Any]) -> None:
    """Put a verified backup back at the original path."""
    backup = Path(str(row.get("backup") or ""))
    source = Path(str(row["source"]))
    if not backup.is_file():
        raise UndoError(f"backup is missing for {source}")
    if source.exists() and row["kind"] != "text":
        raise UndoError(f"refusing to overwrite {source}")
    if row["kind"] == "text":
        after = row.get("after") or {}
        if after.get("sha256") and digest(source, "sha256") != after["sha256"]:
            raise UndoError(f"refusing to restore over changed file {source}")
    shutil.copy2(backup, source)
    if source.stat().st_size != backup.stat().st_size:
        raise VerificationError(f"restored file does not match backup {backup}")


def matches_after(path: Path, after: dict[str, Any]) -> bool:
    """Return whether a file still matches the recorded result."""
    if not path.exists():
        return False
    stat = path.stat()
    if "size" in after and stat.st_size != int(after["size"]):
        return False
    if "mtime_ns" in after and stat.st_mtime_ns != int(after["mtime_ns"]):
        return False
    return "sha256" not in after or digest(path, "sha256") == after["sha256"]


def restore_mtime(path: Path, snapshot: dict[str, Any]) -> None:
    """Put back the planned mtime so a later redo can recognise the file."""
    if "mtime_ns" not in snapshot:
        return
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, int(snapshot["mtime_ns"])))


def _backup(path: Path, journal: dict[str, Any], plan: Plan) -> Path:
    dest = Path(str(plan.settings["quarantine_dir"])) / str(journal["id"]) / "backups" / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    if dest.stat().st_size != path.stat().st_size:
        raise VerificationError(f"backup does not match {path}")
    return dest


def _quarantine_path(row: dict[str, Any], journal: dict[str, Any], plan: Plan) -> Path:
    relative = str(row["payload"]["quarantine_relative"])
    return Path(str(plan.settings["quarantine_dir"])) / str(journal["id"]) / relative


def _copy_verified(source: Path, dest: Path) -> None:
    shutil.copy2(source, dest)
    if dest.stat().st_size != source.stat().st_size:
        dest.unlink(missing_ok=True)
        raise VerificationError(f"copy did not match {source}")


def _same_device(source: Path, dest: Path) -> bool:
    target = dest if dest.exists() else dest.parent
    return source.stat().st_dev == target.stat().st_dev


def _parse_ns(value: object) -> int:
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000_000)
