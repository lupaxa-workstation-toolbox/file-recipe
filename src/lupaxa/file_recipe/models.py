"""Shared data records for plans and journals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

JSON_SCHEMA = "1.0"
RECIPE_VERSION = 1

RESERVED_TEMPLATE_NAMES = frozenset(
    {
        "filename",
        "stem",
        "extension",
        "parent",
        "relative_path",
        "year",
        "month",
        "day",
        "hour",
        "minute",
        "size",
        "counter",
        "hash",
        "recipe",
        "rule",
        "output",
    }
)

WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

OPERATION_CATALOGUE: tuple[tuple[str, str], ...] = (
    ("rename", "Change the file name in its current directory."),
    ("move", "Move a file, copying across devices and deleting the source after verification."),
    ("copy", "Copy a file and verify the destination size."),
    ("delete", "Quarantine a file, or delete it permanently when that is authorised."),
    ("quarantine", "Move a file aside while keeping its original path for restore."),
    ("mkdir", "Create a directory from a template."),
    ("metadata", "Set mtime, atime, mode, or the read-only bit where the platform allows it."),
    ("hash", "Stream SHA-256, SHA-512, or BLAKE2b and record the digest."),
    ("dedupe", "Group identical files and report, quarantine, or move the extras."),
    ("text", "Rewrite text through a temporary file, with a backup when undo is enabled."),
    ("report", "Record a finding without changing the file."),
)


@dataclass
class FileInfo:
    """One discovered path and the metadata a plan is allowed to trust."""

    path: str
    relative: str
    root: str
    size: int
    mtime_ns: int
    ctime_ns: int
    atime_ns: int
    mode: int
    uid: int
    nlink: int
    is_dir: bool
    is_file: bool
    is_symlink: bool
    origin: str = "filesystem"

    def snapshot(self) -> dict[str, Any]:
        """Return the identity checked again before a file is modified."""
        return {
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "is_dir": self.is_dir,
            "is_file": self.is_file,
            "is_symlink": self.is_symlink,
        }


@dataclass
class Operation:
    """One planned filesystem change."""

    id: str
    kind: str
    rule: str
    recipe: str
    source: str | None
    destination: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    source_origin: str = "filesystem"
    snapshot: dict[str, Any] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)
    reversible: bool = True
    skipped: bool = False
    skip_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise the operation for a plan or journal."""
        return {
            "id": self.id,
            "kind": self.kind,
            "rule": self.rule,
            "recipe": self.recipe,
            "source": self.source,
            "destination": self.destination,
            "payload": self.payload,
            "depends_on": list(self.depends_on),
            "source_origin": self.source_origin,
            "snapshot": self.snapshot,
            "expected": self.expected,
            "reversible": self.reversible,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Operation:
        """Build an operation from plan JSON."""
        return cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            rule=str(data.get("rule", "")),
            recipe=str(data.get("recipe", "")),
            source=data.get("source"),
            destination=data.get("destination"),
            payload=dict(data.get("payload") or {}),
            depends_on=[str(item) for item in data.get("depends_on") or []],
            source_origin=str(data.get("source_origin", "filesystem")),
            snapshot=dict(data.get("snapshot") or {}),
            expected=dict(data.get("expected") or {}),
            reversible=bool(data.get("reversible", True)),
            skipped=bool(data.get("skipped", False)),
            skip_reason=str(data.get("skip_reason", "")),
        )


@dataclass
class Plan:
    """A complete execution plan. Building one does not modify user files."""

    recipe_name: str
    recipe_digest: str
    recipe_path: str | None
    created_at: str
    variables: dict[str, Any]
    roots: list[str]
    operations: list[Operation]
    conflicts: list[dict[str, str]]
    warnings: list[str]
    estimates: dict[str, int]
    settings: dict[str, Any]
    schema_version: str = JSON_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        """Serialise the plan."""
        return {
            "schema_version": self.schema_version,
            "recipe_name": self.recipe_name,
            "recipe_digest": self.recipe_digest,
            "recipe_path": self.recipe_path,
            "created_at": self.created_at,
            "variables": self.variables,
            "roots": self.roots,
            "operations": [op.to_dict() for op in self.operations],
            "conflicts": self.conflicts,
            "warnings": self.warnings,
            "estimates": self.estimates,
            "settings": self.settings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        """Load a plan. The caller revalidates it before execution."""
        if str(data.get("schema_version")) != JSON_SCHEMA:
            message = f"unsupported plan schema {data.get('schema_version')!r}"
            raise ValueError(message)
        operations = [Operation.from_dict(item) for item in data.get("operations") or []]
        return cls(
            recipe_name=str(data.get("recipe_name", "")),
            recipe_digest=str(data.get("recipe_digest", "")),
            recipe_path=data.get("recipe_path"),
            created_at=str(data.get("created_at", "")),
            variables=dict(data.get("variables") or {}),
            roots=[str(item) for item in data.get("roots") or []],
            operations=operations,
            conflicts=[dict(item) for item in data.get("conflicts") or []],
            warnings=[str(item) for item in data.get("warnings") or []],
            estimates={
                str(key): int(value) for key, value in dict(data.get("estimates") or {}).items()
            },
            settings=dict(data.get("settings") or {}),
        )
