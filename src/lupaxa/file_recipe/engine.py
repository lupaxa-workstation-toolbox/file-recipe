"""Execute, undo, redo, and recover a plan.

Mutations run in dependency order. Independent hash operations may use the
worker pool. The journal is replaced on disk before each irreversible change.
A source is checked again immediately before it is modified.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from lupaxa.file_recipe.errors import (
    ConflictError,
    FileRecipeError,
    PartialError,
    RecipeError,
    RecoveryError,
    UndoError,
)
from lupaxa.file_recipe.models import JSON_SCHEMA, Operation, Plan
from lupaxa.file_recipe.mutate import (
    apply_row,
    assert_recipe_fresh,
    assert_source_fresh,
    dependency_order,
    digest,
    inspect_row,
    new_journal,
    plan_from_journal,
    rollback,
    save_journal,
    stamp,
    undo_row,
    verify_row,
)


def execute_plan(plan: Plan, *, dry_run: bool = False) -> dict[str, Any]:
    """Run a plan. A dry run validates it and does not create a journal."""
    if plan.conflicts:
        message = "; ".join(item["message"] for item in plan.conflicts)
        raise ConflictError(message)
    assert_recipe_fresh(plan)
    for op in plan.operations:
        if not op.skipped:
            assert_source_fresh(op)
    if dry_run:
        count = len([op for op in plan.operations if not op.skipped])
        return {
            "schema_version": JSON_SCHEMA,
            "state": "dry-run",
            "recipe_name": plan.recipe_name,
            "operations": count,
        }
    if _needs_destructive(plan) and not plan.settings.get("allow_destructive"):
        raise RecipeError("destructive operations require allow_destructive")
    journal = new_journal(plan)
    records = journal["operations"]
    save_journal(journal)
    on_error = str(plan.settings.get("on_error", "stop"))
    failed: FileRecipeError | None = None
    index = 0
    while index < len(records):
        row = records[index]
        if row["state"] == "skipped":
            index += 1
            continue
        if _blocked(row, records):
            row["state"] = "skipped"
            row["skip_reason"] = "a dependency failed"
            index += 1
            save_journal(journal)
            continue
        batched = _hash_batch(records, index, int(plan.settings.get("workers", 1)), journal)
        if batched:
            index = batched
            continue
        row["state"] = "prepared"
        save_journal(journal)
        try:
            assert_source_fresh(Operation.from_dict(row))
            row["state"] = "running"
            save_journal(journal)
            apply_row(row, journal, plan)
            verify_row(row)
            if row["state"] != "skipped":
                row["state"] = "completed"
        except FileRecipeError as exc:
            row["state"] = "failed"
            row["error"] = str(exc)
            failed = exc
            save_journal(journal)
            if on_error == "continue":
                index += 1
                continue
            rollback(records, journal)
            raise
        save_journal(journal)
        index += 1
    journal["finished_at"] = stamp()
    if failed is not None:
        journal["state"] = "partial"
        save_journal(journal)
        raise PartialError(str(failed))
    journal["state"] = "completed"
    save_journal(journal)
    return journal


def undo_journal(journal: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    """Reverse completed operations. Changed files are left in place."""
    rows = [row for row in journal["operations"] if row.get("state") == "completed"]
    done = 0
    refused: list[str] = []
    for row in reversed(dependency_order(rows)):
        if not row.get("reversible", True):
            refused.append(f"{row['id']} is not reversible")
            continue
        if dry_run:
            done += 1
            continue
        try:
            undo_row(row)
            row["state"] = "rolled_back"
            done += 1
        except FileRecipeError as exc:
            row["error"] = str(exc)
            refused.append(str(exc))
    if dry_run:
        return journal
    journal["finished_at"] = stamp()
    if refused and done:
        journal["state"] = "partial"
        save_journal(journal)
        raise PartialError("undo was partial: " + "; ".join(refused))
    if refused and not done:
        journal["state"] = "recovery_required"
        save_journal(journal)
        raise UndoError("undo failed: " + "; ".join(refused))
    journal["state"] = "rolled_back"
    save_journal(journal)
    return journal


def redo_journal(journal: dict[str, Any]) -> dict[str, Any]:
    """Re-run an undone journal. Safety checks run again."""
    if journal.get("state") != "rolled_back":
        raise RecipeError("redo requires an undone execution")
    operations: list[Operation] = []
    for row in journal["operations"]:
        op = Operation.from_dict(row)
        if row.get("state") == "rolled_back":
            op.skipped = False
        operations.append(op)
    fresh = execute_plan(plan_from_journal(journal, operations))
    fresh["redo_of"] = journal["id"]
    save_journal(fresh)
    return fresh


def recover_journal(journal: dict[str, Any], mode: str) -> dict[str, Any]:
    """Compare the journal with the filesystem. Nothing is repaired silently."""
    findings = [inspect_row(row) for row in journal["operations"]]
    journal["findings"] = findings
    if mode == "inspect":
        if any(item["ambiguous"] for item in findings):
            raise RecoveryError("journal needs a decision; it was not repaired")
        return journal
    if mode == "resolved":
        journal["state"] = "externally_resolved"
        save_journal(journal)
        return journal
    if mode == "rollback":
        return undo_journal(journal)
    if mode == "resume":
        return _resume(journal)
    raise RecipeError(f"unsupported recover mode {mode!r}")


def load_journal(path: Path) -> dict[str, Any]:
    """Load a journal. Invalid JSON is reported and left unchanged."""
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(
            f"journal is truncated or invalid and was not repaired: {path}"
        ) from exc
    if not isinstance(data, dict) or data.get("schema_version") != JSON_SCHEMA:
        raise RecoveryError(f"journal schema is unsupported and was not repaired: {path}")
    if "operations" not in data or "id" not in data:
        raise RecoveryError(f"journal is missing required fields and was not repaired: {path}")
    return data


def list_journals(directory: Path) -> list[Path]:
    """Return journal files, newest first."""
    if not directory.is_dir():
        return []
    files = [path for path in directory.glob("*.json") if not path.name.startswith(".")]
    return sorted(files, key=lambda path: path.stat().st_mtime_ns, reverse=True)


def _resume(journal: dict[str, Any]) -> dict[str, Any]:
    pending: list[Operation] = []
    for row in journal["operations"]:
        finding = inspect_row(row)
        if finding["ambiguous"]:
            raise RecoveryError(f"ambiguous operation {row['id']}; not resumed")
        if finding["status"] == "complete" and row["state"] in {"prepared", "running"}:
            row["state"] = "completed"
        elif row["state"] in {"prepared", "running", "pending"} and finding["status"] == "pending":
            row["state"] = "pending"
            pending.append(Operation.from_dict(row))
    if not pending:
        journal["state"] = "completed"
        save_journal(journal)
        return journal
    resumed = execute_plan(plan_from_journal(journal, pending))
    resumed["undo_of"] = journal["id"]
    save_journal(resumed)
    return resumed


def _hash_batch(
    records: list[dict[str, Any]],
    index: int,
    workers: int,
    journal: dict[str, Any],
) -> int:
    if workers < 2 or records[index]["kind"] != "hash":
        return 0
    batch: list[dict[str, Any]] = []
    cursor = index
    while cursor < len(records):
        row = records[cursor]
        if row["kind"] != "hash" or row["state"] != "pending":
            break
        batch.append(row)
        cursor += 1
    if len(batch) < 2:
        return 0
    for row in batch:
        row["state"] = "prepared"
    save_journal(journal)

    def _one(row: dict[str, Any]) -> str:
        algorithm = str(row["payload"].get("algorithm", "sha256"))
        return digest(str(row["source"]), algorithm)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        mapped = list(pool.map(_one, batch))
    for row, hashed in zip(batch, mapped, strict=True):
        row["payload"]["digest"] = hashed
        row["after"] = {"digest": hashed}
        row["state"] = "completed"
    save_journal(journal)
    return cursor


def _blocked(row: dict[str, Any], records: list[dict[str, Any]]) -> bool:
    failed = {item["id"] for item in records if item.get("state") == "failed"}
    return any(dep in failed for dep in row.get("depends_on") or [])


def _needs_destructive(plan: Plan) -> bool:
    for op in plan.operations:
        if op.skipped:
            continue
        if op.kind == "delete" and op.payload.get("permanent"):
            return True
        if op.payload.get("overwrite"):
            return True
    return False
