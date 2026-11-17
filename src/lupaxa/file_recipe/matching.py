"""Declarative file matching. Conditions are data, not expressions."""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lupaxa.file_recipe.errors import LimitError, RecipeError
from lupaxa.file_recipe.models import FileInfo

PREDICATE_KEYS = frozenset(
    {
        "glob",
        "regex",
        "exact",
        "type",
        "size_min",
        "size_max",
        "mtime_after",
        "mtime_before",
        "ctime_after",
        "ctime_before",
        "atime_after",
        "atime_before",
        "permissions",
        "read_only",
        "empty",
        "symlink",
        "owner",
        "contains",
        "not_contains",
        "content_regex",
        "hash",
        "mime",
        "binary",
        "full_path",
        "case_sensitive",
    }
)
_DEFAULT_SCAN = 8 * 1024 * 1024


def identity_match(info: FileInfo, rule: dict[str, Any]) -> bool:
    """Return whether the name, glob, or expression selects this path."""
    globs = [str(item) for item in rule.get("globs") or []]
    if rule.get("glob"):
        globs.append(str(rule["glob"]))
    regex = rule.get("regex")
    exact = rule.get("exact")
    if not globs and regex is None and exact is None:
        return True
    target = info.relative if rule.get("full_path") else Path(info.path).name
    sensitive = bool(rule.get("case_sensitive", True))
    if any(glob_match(target, pattern, sensitive) for pattern in globs):
        return True
    if regex is not None and regex_match(target, str(regex), sensitive):
        return True
    if exact is not None:
        expected = str(exact)
        if sensitive:
            return target == expected
        return target.casefold() == expected.casefold()
    return False


def glob_match(text: str, pattern: str, sensitive: bool) -> bool:
    """Match a glob. Case folding is explicit because platforms disagree."""
    if not sensitive:
        return fnmatch.fnmatch(text.casefold(), pattern.casefold())
    return re.fullmatch(fnmatch.translate(pattern), text) is not None


def regex_match(text: str, pattern: str, sensitive: bool) -> bool:
    """Search with a regular expression and no code execution."""
    if len(pattern) > 1000:
        raise RecipeError("regular expression is too long")
    flags = 0 if sensitive else re.IGNORECASE
    return re.search(pattern, text, flags) is not None


def condition_match(
    info: FileInfo,
    node: dict[str, Any] | None,
    *,
    scan_limit: int,
    hash_algorithm: str,
    hash_file: Callable[[str, str], str],
) -> bool:
    """Evaluate an all/any/not group or a single predicate table."""
    if not node:
        return True
    if "all" in node:
        return all(
            condition_match(
                info,
                child,
                scan_limit=scan_limit,
                hash_algorithm=hash_algorithm,
                hash_file=hash_file,
            )
            for child in node["all"]
        )
    if "any" in node:
        return any(
            condition_match(
                info,
                child,
                scan_limit=scan_limit,
                hash_algorithm=hash_algorithm,
                hash_file=hash_file,
            )
            for child in node["any"]
        )
    if "not" in node:
        return not condition_match(
            info,
            node["not"],
            scan_limit=scan_limit,
            hash_algorithm=hash_algorithm,
            hash_file=hash_file,
        )
    return _predicates(
        info,
        node,
        scan_limit=scan_limit,
        hash_algorithm=hash_algorithm,
        hash_file=hash_file,
    )


def rule_shorthand(rule: dict[str, Any]) -> dict[str, Any]:
    """Return predicate keys set directly on a rule, aside from its identity."""
    skipped = {"glob", "regex", "exact", "globs"}
    return {key: value for key, value in rule.items() if key in PREDICATE_KEYS - skipped}


def _predicates(
    info: FileInfo,
    node: dict[str, Any],
    *,
    scan_limit: int,
    hash_algorithm: str,
    hash_file: Callable[[str, str], str],
) -> bool:
    checks: list[bool] = []
    target = info.relative if node.get("full_path") else Path(info.path).name
    sensitive = bool(node.get("case_sensitive", True))
    if "glob" in node:
        checks.append(glob_match(target, str(node["glob"]), sensitive))
    if "regex" in node:
        checks.append(regex_match(target, str(node["regex"]), sensitive))
    if "exact" in node:
        expected = str(node["exact"])
        if sensitive:
            checks.append(target == expected)
        else:
            checks.append(target.casefold() == expected.casefold())
    if "type" in node:
        kind = str(node["type"])
        checks.append(
            (kind == "file" and info.is_file)
            or (kind == "dir" and info.is_dir)
            or (kind == "symlink" and info.is_symlink)
        )
    if "size_min" in node:
        checks.append(info.size >= int(node["size_min"]))
    if "size_max" in node:
        checks.append(info.size <= int(node["size_max"]))
    for attr, name in (("mtime_ns", "mtime"), ("ctime_ns", "ctime"), ("atime_ns", "atime")):
        after = node.get(f"{name}_after")
        before = node.get(f"{name}_before")
        current = int(getattr(info, attr))
        if after is not None:
            checks.append(current >= parse_time(after))
        if before is not None:
            checks.append(current <= parse_time(before))
    if "permissions" in node:
        checks.append((info.mode & 0o777) == int(str(node["permissions"]), 8))
    if "read_only" in node:
        checks.append((not os.access(info.path, os.W_OK)) is bool(node["read_only"]))
    if "empty" in node:
        checks.append((info.size == 0) is bool(node["empty"]))
    if "symlink" in node:
        checks.append(info.is_symlink is bool(node["symlink"]))
    if "owner" in node:
        if os.name == "nt":
            raise RecipeError("ownership filters are not supported on this platform")
        checks.append(info.uid == int(node["owner"]))
    content_keys = {"contains", "not_contains", "content_regex", "hash", "mime", "binary"}
    if content_keys & set(node):
        checks.append(
            _content(
                info,
                node,
                scan_limit=scan_limit,
                hash_algorithm=hash_algorithm,
                hash_file=hash_file,
            )
        )
    if not checks and not (PREDICATE_KEYS & set(node)):
        return True
    return all(checks)


def _content(
    info: FileInfo,
    node: dict[str, Any],
    *,
    scan_limit: int,
    hash_algorithm: str,
    hash_file: Callable[[str, str], str],
) -> bool:
    if not info.is_file or info.origin == "planned":
        return False
    needs_text = any(key in node for key in ("contains", "not_contains", "content_regex", "binary"))
    limit = scan_limit or _DEFAULT_SCAN
    if needs_text and info.size > limit:
        raise LimitError(f"content scan exceeds max_scan_bytes ({limit} bytes) for {info.relative}")
    data = b""
    if needs_text or "mime" in node:
        with Path(info.path).open("rb") as handle:
            data = handle.read(limit if needs_text else 8192)
    binary = b"\0" in data[:8192]
    if "binary" in node and binary is not bool(node["binary"]):
        return False
    if (
        binary
        and not node.get("binary")
        and any(key in node for key in ("contains", "not_contains", "content_regex"))
    ):
        return False
    text = data.decode("utf-8", errors="replace")
    if "contains" in node and str(node["contains"]) not in text:
        return False
    if "not_contains" in node and str(node["not_contains"]) in text:
        return False
    if "content_regex" in node and re.search(str(node["content_regex"]), text) is None:
        return False
    if "hash" in node and hash_file(info.path, hash_algorithm) != str(node["hash"]).lower():
        return False
    if "mime" in node:
        import mimetypes

        guessed, _encoding = mimetypes.guess_type(info.path)
        if guessed != str(node["mime"]):
            return False
    return True


def parse_time(value: object) -> int:
    """Parse an ISO timestamp or a unix second count into nanoseconds."""
    text = str(value)
    if text.isdigit():
        return int(text) * 1_000_000_000
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RecipeError(f"invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000_000)
