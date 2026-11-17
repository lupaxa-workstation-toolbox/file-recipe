"""Human and machine renderings of plans and journals."""

from __future__ import annotations

import csv
import html
import io
import json
from typing import Any

from lupaxa.file_recipe.models import JSON_SCHEMA, Plan


def render_plan(plan: Plan, fmt: str, *, colour: bool = False) -> str:
    """Render a plan in the requested format."""
    if fmt == "json":
        return json.dumps(plan.to_dict(), indent=2, sort_keys=True)
    if fmt == "jsonl":
        summary = {
            "schema_version": JSON_SCHEMA,
            "type": "summary",
            "recipe_name": plan.recipe_name,
            "operations": plan.estimates.get("operations", 0),
            "conflicts": len(plan.conflicts),
        }
        lines = [json.dumps(summary, sort_keys=True)]
        for op in plan.operations:
            lines.append(
                json.dumps(
                    {"schema_version": JSON_SCHEMA, "type": "operation", **op.to_dict()},
                    sort_keys=True,
                )
            )
        return "\n".join(lines) + "\n"
    if fmt == "csv":
        return _csv(plan.operations)
    if fmt == "markdown":
        return _markdown(plan.recipe_name, plan.operations, plan.conflicts, plan.warnings)
    if fmt == "html":
        return _html(plan.recipe_name, plan.operations, plan.conflicts, plan.warnings)
    return _text(plan, colour=colour)


def render_journal(journal: dict[str, Any], fmt: str) -> str:
    """Render a journal."""
    payload = {"schema_version": JSON_SCHEMA, **journal}
    if fmt == "json":
        return json.dumps(payload, indent=2, sort_keys=True)
    if fmt == "jsonl":
        lines = [
            json.dumps(
                {
                    "schema_version": JSON_SCHEMA,
                    "type": "journal",
                    "id": journal.get("id"),
                    "state": journal.get("state"),
                },
                sort_keys=True,
            )
        ]
        for row in journal.get("operations") or []:
            lines.append(
                json.dumps(
                    {"schema_version": JSON_SCHEMA, "type": "operation", **row}, sort_keys=True
                )
            )
        return "\n".join(lines) + "\n"
    if fmt == "csv":
        return _csv(journal.get("operations") or [])
    if fmt == "markdown":
        return _markdown(
            str(journal.get("recipe_name", "")), journal.get("operations") or [], [], []
        )
    if fmt == "html":
        return _html(str(journal.get("recipe_name", "")), journal.get("operations") or [], [], [])
    lines = [
        f"Journal: {journal.get('id', '')}",
        f"State: {journal.get('state', '')}",
        f"Recipe: {journal.get('recipe_name', '')}",
    ]
    for row in journal.get("operations") or []:
        lines.append(
            f"{str(row.get('kind', '')).upper():10} {row.get('state', ''):12} "
            f"{row.get('source') or '-'} -> {row.get('destination') or '-'}"
        )
    return "\n".join(lines) + "\n"


def _text(plan: Plan, *, colour: bool) -> str:
    lines = [
        f"Recipe: {plan.recipe_name}",
        f"Files: {plan.estimates.get('files', 0)}",
        f"Operations: {plan.estimates.get('operations', 0)}",
        f"Conflicts: {len(plan.conflicts)}",
        "",
    ]
    for warning in plan.warnings:
        lines.append(f"Warning: {warning}")
    for conflict in plan.conflicts:
        lines.append(f"Conflict: {conflict['message']}")
    if plan.warnings or plan.conflicts:
        lines.append("")
    for op in plan.operations:
        if op.skipped:
            verb = "SKIP"
            detail = op.skip_reason
        else:
            verb = op.kind.upper()
            detail = ""
        if colour and not op.skipped:
            verb = f"\033[36m{verb}\033[0m"
        source = op.source or "-"
        dest = op.destination or op.payload.get("quarantine_relative") or "-"
        lines.append(f"{verb:10} {source} -> {dest} {detail}".rstrip())
    return "\n".join(lines) + "\n"


def _csv(rows: list[Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "kind", "source", "destination", "rule", "recipe", "state"])
    for row in rows:
        data = row.to_dict() if hasattr(row, "to_dict") else row
        writer.writerow(
            [
                data.get("id", ""),
                data.get("kind", ""),
                data.get("source") or "",
                data.get("destination") or "",
                data.get("rule", ""),
                data.get("recipe", ""),
                data.get("state", ""),
            ]
        )
    return buffer.getvalue()


def _markdown(
    title: str, rows: list[Any], conflicts: list[dict[str, str]], warnings: list[str]
) -> str:
    lines = [f"# {title}", "", "| Id | Kind | Source | Destination |", "| --- | --- | --- | --- |"]
    for row in rows:
        data = row.to_dict() if hasattr(row, "to_dict") else row
        lines.append(
            "| {id} | {kind} | {source} | {dest} |".format(
                id=_cell(data.get("id")),
                kind=_cell(data.get("kind")),
                source=_cell(data.get("source")),
                dest=_cell(data.get("destination")),
            )
        )
    if conflicts:
        lines.extend(["", "## Conflicts", ""])
        lines.extend(f"- {item['message']}" for item in conflicts)
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {item}" for item in warnings)
    lines.append("")
    return "\n".join(lines)


def _html(title: str, rows: list[Any], conflicts: list[dict[str, str]], warnings: list[str]) -> str:
    body = [
        "<!DOCTYPE html>",
        '<html><head><meta charset="utf-8">',
        f"<title>{html.escape(title)}</title>",
        "<style>",
        "body{font-family:sans-serif}",
        "table{border-collapse:collapse}",
        "td,th{border:1px solid #ccc;padding:4px}",
        "</style>",
        "</head><body>",
        f"<h1>{html.escape(title)}</h1>",
        "<table><thead><tr><th>Id</th><th>Kind</th><th>Source</th><th>Destination</th></tr></thead><tbody>",
    ]
    for row in rows:
        data = row.to_dict() if hasattr(row, "to_dict") else row
        body.append(
            "<tr><td>{id}</td><td>{kind}</td><td>{source}</td><td>{dest}</td></tr>".format(
                id=html.escape(str(data.get("id", ""))),
                kind=html.escape(str(data.get("kind", ""))),
                source=html.escape(str(data.get("source") or "")),
                dest=html.escape(str(data.get("destination") or "")),
            )
        )
    body.append("</tbody></table>")
    if conflicts:
        body.append("<h2>Conflicts</h2><ul>")
        body.extend(f"<li>{html.escape(item['message'])}</li>" for item in conflicts)
        body.append("</ul>")
    if warnings:
        body.append("<h2>Warnings</h2><ul>")
        body.extend(f"<li>{html.escape(item)}</li>" for item in warnings)
        body.append("</ul>")
    body.append("</body></html>")
    return "\n".join(body) + "\n"


def _cell(value: object) -> str:
    return str(value or "").replace("|", "\\|")
