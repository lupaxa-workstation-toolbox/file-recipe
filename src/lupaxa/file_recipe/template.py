"""Safe template rendering. No attribute access and no code execution."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from lupaxa.file_recipe.errors import RecipeError
from lupaxa.file_recipe.models import WINDOWS_RESERVED

_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::(0?\d+d|lower|upper|\d+))?\}")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_VALUE = 4096
_MAX_NAME = 64


def validate_variable_name(name: str) -> None:
    """Reject invalid or reserved variable names."""
    if len(name) > _MAX_NAME or not _NAME.match(name):
        raise RecipeError(f"invalid variable name {name!r}")


def validate_variable_value(name: str, value: object) -> None:
    """Reject oversized values and values that carry path escapes."""
    text = value if isinstance(value, str) else str(value)
    if len(text) > _MAX_VALUE:
        raise RecipeError(f"variable {name} exceeds {_MAX_VALUE} characters")
    if isinstance(value, str) and (
        "\x00" in value or ".." in PurePosixPath(value.replace("\\", "/")).parts
    ):
        raise RecipeError(f"variable {name} contains an unsafe path")


def render(pattern: str, values: dict[str, Any], *, allow_absolute: bool) -> str:
    """Render a template and reject traversal, absolutes, and unknown fields."""
    if not isinstance(pattern, str):
        raise RecipeError("template must be a string")
    if "{{" in pattern or "}}" in pattern or pattern.count("{") != pattern.count("}"):
        raise RecipeError(f"unsupported template syntax in {pattern!r}")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        spec = match.group(2)
        if name not in values:
            raise RecipeError(f"unknown variable {{{name}}}")
        return _format(name, values[name], spec)

    rendered = _TOKEN.sub(replace, pattern)
    if "{" in rendered or "}" in rendered:
        raise RecipeError(f"unsupported template syntax in {pattern!r}")
    return check_destination(rendered, allow_absolute=allow_absolute)


def _format(name: str, value: object, spec: str | None) -> str:
    if spec is None:
        return str(value)
    if spec == "lower":
        return str(value).lower()
    if spec == "upper":
        return str(value).upper()
    if spec.endswith("d"):
        try:
            number = int(str(value))
        except (TypeError, ValueError) as exc:
            raise RecipeError(f"variable {name} is not an integer") from exc
        width = int(spec[:-1] or "0")
        return f"{number:0{width}d}"
    width = int(spec)
    return str(value)[:width]


def check_destination(rendered: str, *, allow_absolute: bool) -> str:
    """Reject nulls, ``..``, reserved Windows names, and unwanted absolutes."""
    if "\x00" in rendered or rendered.strip() == "":
        raise RecipeError("empty or unsafe destination")
    posix = rendered.replace("\\", "/")
    parts = PurePosixPath(posix).parts
    if ".." in parts:
        raise RecipeError(f"path traversal in destination {rendered!r}")
    absolute = posix.startswith("/") or PureWindowsPath(rendered).is_absolute()
    if absolute and not allow_absolute:
        raise RecipeError(f"absolute destination is not allowed: {rendered!r}")
    name = parts[-1] if parts else rendered
    stem = name.split(".", 1)[0].upper().rstrip(" .")
    if stem in WINDOWS_RESERVED:
        raise RecipeError(f"reserved destination name {name!r}")
    normalised = unicodedata.normalize("NFC", rendered)
    if len(normalised) > 4096 or any(len(part) > 255 for part in parts):
        raise RecipeError(f"destination is too long: {rendered!r}")
    return normalised


def compound_extension(filename: str) -> str:
    """Return a compound extension without the leading dot.

    ``archive.tar.gz`` yields ``tar.gz``. A single suffix yields that suffix
    without the dot. Extensionless names yield an empty string.
    """
    lowered = filename.lower()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"):
        if lowered.endswith(suffix):
            return suffix[1:]
    dot = filename.rfind(".")
    if dot <= 0:
        return ""
    return filename[dot + 1 :]


def stem_of(filename: str) -> str:
    """Return the file name without its extension."""
    extension = compound_extension(filename)
    if not extension:
        return filename
    return filename[: -(len(extension) + 1)]
