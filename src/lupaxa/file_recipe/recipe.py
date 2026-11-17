"""Recipe loading, schema checks, and search paths."""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

from lupaxa.file_recipe.errors import RecipeError
from lupaxa.file_recipe.models import RECIPE_VERSION, RESERVED_TEMPLATE_NAMES
from lupaxa.file_recipe.template import validate_variable_name, validate_variable_value

TOP_KEYS = frozenset(
    {
        "version",
        "metadata",
        "input",
        "output",
        "rules",
        "variables",
        "parameters",
        "settings",
        "recipes",
        "limits",
    }
)
METADATA_KEYS = frozenset({"name", "description", "author", "tags", "docs", "id"})
INPUT_KEYS = frozenset(
    {
        "paths",
        "recursive",
        "include_hidden",
        "follow_symlinks",
        "max_depth",
        "same_filesystem",
        "max_files",
        "max_total_bytes",
        "include",
        "exclude",
        "include_directories",
        "order",
    }
)
OUTPUT_KEYS = frozenset({"directory", "collision"})
SETTINGS_KEYS = frozenset(
    {
        "on_error",
        "undo",
        "allow_destructive",
        "allow_absolute",
        "timezone",
        "date_source",
        "hash",
        "case_insensitive",
        "workers",
        "strict",
        "snapshot_hash",
        "quarantine_days",
        "max_chain_depth",
        "inherit_variables",
    }
)
LIMIT_KEYS = frozenset(
    {
        "max_files",
        "max_total_bytes",
        "max_file_bytes",
        "max_scan_bytes",
        "max_seconds",
        "max_workers",
        "max_path_length",
    }
)
ACTION_TYPES = frozenset(
    {
        "rename",
        "move",
        "copy",
        "delete",
        "quarantine",
        "mkdir",
        "metadata",
        "hash",
        "dedupe",
        "text",
    }
)
PARAMETER_TYPES = frozenset({"string", "int", "bool", "date", "path", "list"})


@dataclass
class Recipe:
    """A parsed recipe. Metadata is stored and does not select files."""

    version: int
    metadata: dict[str, Any]
    body: dict[str, Any]
    path: Path | None
    digest: str
    source: str
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Return the recipe name."""
        return str(self.metadata.get("name") or (self.path.stem if self.path else "recipe"))


def recipe_from_text(text: str, path: Path | None = None, *, strict: bool = False) -> Recipe:
    """Parse recipe TOML from text."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RecipeError(f"invalid recipe TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise RecipeError("recipe must be a TOML table")
    warnings: list[str] = []
    _check_table(data, TOP_KEYS, "recipe", warnings, strict)
    version = data.get("version")
    if version != RECIPE_VERSION:
        raise RecipeError(f"unsupported recipe version {version!r}; expected {RECIPE_VERSION}")
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise RecipeError("metadata must be a table")
    _check_table(metadata, METADATA_KEYS, "metadata", warnings, strict)
    if "name" not in metadata or not str(metadata["name"]).strip():
        raise RecipeError("metadata.name is required")
    for section, allowed in (
        ("input", INPUT_KEYS),
        ("output", OUTPUT_KEYS),
        ("settings", SETTINGS_KEYS),
        ("limits", LIMIT_KEYS),
    ):
        table = data.get(section) or {}
        if not isinstance(table, dict):
            raise RecipeError(f"{section} must be a table")
        _check_table(table, allowed, section, warnings, strict)
    _validate_variables(data.get("variables") or {}, warnings, strict)
    _validate_parameters(data.get("parameters") or [])
    _validate_rules(data.get("rules"))
    _validate_chain(data.get("recipes"))
    _validate_choices(data)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Recipe(
        version=RECIPE_VERSION,
        metadata=dict(metadata),
        body=data,
        path=path,
        digest=digest,
        source=text,
        warnings=warnings,
    )


def _check_table(
    data: dict[str, Any],
    allowed: frozenset[str],
    where: str,
    warnings: list[str],
    strict: bool,
) -> None:
    for key in sorted(set(data) - allowed):
        message = f"unknown setting {where}.{key}"
        if strict:
            raise RecipeError(message)
        warnings.append(message)


def _validate_variables(variables: object, warnings: list[str], strict: bool) -> None:
    if not isinstance(variables, dict):
        raise RecipeError("variables must be a table")
    for name, value in variables.items():
        if name in RESERVED_TEMPLATE_NAMES:
            raise RecipeError(f"variable name {name!r} is reserved")
        validate_variable_name(str(name))
        if isinstance(value, (dict, list)):
            raise RecipeError(f"variable {name} must be a scalar")
        validate_variable_value(str(name), value)
        if value is None:
            message = f"variable {name} is null"
            if strict:
                raise RecipeError(message)
            warnings.append(message)


def _validate_parameters(parameters: object) -> None:
    if parameters in ({}, None):
        return
    if not isinstance(parameters, list):
        raise RecipeError("parameters must be an array of tables")
    seen: set[str] = set()
    for item in parameters:
        if not isinstance(item, dict) or "name" not in item:
            raise RecipeError("each parameter needs a name")
        name = str(item["name"])
        if name in RESERVED_TEMPLATE_NAMES:
            raise RecipeError(f"parameter name {name!r} is reserved")
        validate_variable_name(name)
        if name in seen:
            raise RecipeError(f"duplicate parameter {name}")
        seen.add(name)
        kind = str(item.get("type", "string"))
        if kind not in PARAMETER_TYPES:
            raise RecipeError(f"parameter {name} has unsupported type {kind!r}")
        if "default" in item:
            _coerce_parameter(item, item["default"])


def _validate_rules(rules: object) -> None:
    if rules is None:
        return
    if not isinstance(rules, dict):
        raise RecipeError("rules must be a table")
    mode = str(rules.get("mode", "first"))
    if mode not in {"first", "all", "exclusive"}:
        raise RecipeError(f"unsupported rule mode {mode!r}")
    items = rules.get("items") or []
    if not isinstance(items, list):
        raise RecipeError("rules.items must be an array")
    for item in items:
        if not isinstance(item, dict):
            raise RecipeError("each rule must be a table")
        if "name" not in item:
            raise RecipeError("each rule needs a name")
        if "action" in item and "steps" in item:
            raise RecipeError(f"rule {item['name']} cannot set both action and steps")
        if "action" not in item and "steps" not in item:
            raise RecipeError(f"rule {item['name']} needs an action or steps")
        if "action" in item:
            _validate_action(item["action"], str(item["name"]))
        for step in item.get("steps") or []:
            _validate_action(step, str(item["name"]))


def _validate_action(action: object, rule: str) -> None:
    if not isinstance(action, dict):
        raise RecipeError(f"rule {rule} action must be a table")
    kind = str(action.get("type", ""))
    if kind not in ACTION_TYPES:
        raise RecipeError(f"rule {rule} has unsupported action {kind!r}")
    if "xattr" in action or "owner" in action and kind == "metadata":
        raise RecipeError("ownership and extended attributes are not supported")
    if kind == "text" and "encoding" not in action:
        raise RecipeError(f"rule {rule} text action needs an explicit encoding")
    if (
        kind == "delete"
        and action.get("permanent")
        and not isinstance(action.get("permanent"), bool)
    ):
        raise RecipeError(f"rule {rule} permanent must be a boolean")


def _validate_chain(recipes: object) -> None:
    if recipes is None:
        return
    if not isinstance(recipes, list):
        raise RecipeError("recipes must be an array of tables")
    for item in recipes:
        if not isinstance(item, dict) or not item.get("name"):
            raise RecipeError("each chained recipe needs a name")


def _validate_choices(data: dict[str, Any]) -> None:
    settings = data.get("settings") or {}
    output = data.get("output") or {}
    order = str((data.get("input") or {}).get("order", "path"))
    if order not in {"path", "filename", "relative", "size", "mtime", "ctime"}:
        raise RecipeError(f"unsupported order {order!r}")
    collision = str(output.get("collision", "error"))
    if collision not in {"error", "skip", "rename", "overwrite", "quarantine-existing"}:
        raise RecipeError(f"unsupported collision policy {collision!r}")
    on_error = str(settings.get("on_error", "stop"))
    if on_error not in {"stop", "continue", "rollback"}:
        raise RecipeError(f"unsupported on_error {on_error!r}")
    timezone = str(settings.get("timezone", "UTC"))
    if timezone not in {"UTC", "local"} and not _offset(timezone):
        raise RecipeError(f"unsupported timezone {timezone!r}")
    date_source = str(settings.get("date_source", "mtime"))
    if date_source not in {"mtime", "ctime", "now"}:
        raise RecipeError(f"unsupported date_source {date_source!r}")
    algorithm = str(settings.get("hash", "sha256"))
    if algorithm not in {"sha256", "sha512", "blake2b"}:
        raise RecipeError(f"unsupported hash {algorithm!r}")
    if not data.get("rules") and not data.get("recipes"):
        raise RecipeError("recipe needs rules or a recipe chain")
    if output.get("collision") == "overwrite" and not settings.get("allow_destructive"):
        raise RecipeError("overwrite requires settings.allow_destructive")
    _reject_unauthorised_destructive(
        data.get("rules") or {}, bool(settings.get("allow_destructive"))
    )


def _reject_unauthorised_destructive(rules: dict[str, Any], allowed: bool) -> None:
    for item in rules.get("items") or []:
        actions = []
        if "action" in item:
            actions.append(item["action"])
        actions.extend(item.get("steps") or [])
        for action in actions:
            permanent = action.get("type") == "delete" and action.get("permanent") is True
            dedupe_delete = action.get("type") == "dedupe" and action.get("then") == "delete"
            if (permanent or dedupe_delete) and not allowed:
                raise RecipeError(
                    f"rule {item.get('name')} permanent delete requires settings.allow_destructive"
                )


def _offset(value: str) -> bool:
    if len(value) != 6 or value[0] not in "+-" or value[3] != ":":
        return False
    return value[1:3].isdigit() and value[4:6].isdigit()


def coerce_parameters(recipe: Recipe, overrides: dict[str, Any]) -> dict[str, Any]:
    """Resolve parameters and variables. Unknown names are rejected."""
    declared = dict(recipe.body.get("variables") or {})
    parameters = recipe.body.get("parameters") or []
    by_name = {str(item["name"]): item for item in parameters}
    resolved: dict[str, Any] = {}
    for item in parameters:
        name = str(item["name"])
        if name in overrides:
            resolved[name] = _coerce_parameter(item, overrides[name])
        elif "default" in item:
            resolved[name] = _coerce_parameter(item, item["default"])
        elif item.get("required", False):
            raise RecipeError(f"missing required parameter {name}")
    for name, value in declared.items():
        if name not in resolved:
            resolved[name] = value
    for name, value in overrides.items():
        if name in RESERVED_TEMPLATE_NAMES:
            raise RecipeError(f"variable name {name!r} is reserved")
        if name in by_name:
            continue
        if name not in declared:
            raise RecipeError(f"unknown variable {name}")
        if name in by_name:
            resolved[name] = _coerce_parameter(by_name[name], value)
        else:
            validate_variable_value(name, value)
            resolved[name] = _coerce_loose(value)
    for name, item in by_name.items():
        if name not in resolved:
            continue
        _check_constraints(item, resolved[name])
    return resolved


def _coerce_loose(value: object) -> object:
    if isinstance(value, str):
        validate_variable_value("value", value)
    return value


def _coerce_parameter(spec: dict[str, Any], raw: object) -> object:
    kind = str(spec.get("type", "string"))
    name = str(spec.get("name", "parameter"))
    if kind == "string":
        value: Any = str(raw)
    elif kind == "int":
        try:
            value = int(str(raw))
        except (TypeError, ValueError) as exc:
            raise RecipeError(f"parameter {name} must be an integer") from exc
    elif kind == "bool":
        if isinstance(raw, bool):
            value = raw
        elif str(raw).strip().lower() in {"1", "true", "yes", "on"}:
            value = True
        elif str(raw).strip().lower() in {"0", "false", "no", "off"}:
            value = False
        else:
            raise RecipeError(f"parameter {name} must be a boolean")
    elif kind == "date":
        text = str(raw)
        if len(text) < 10 or text[4] != "-" or text[7] != "-":
            raise RecipeError(f"parameter {name} must be a date")
        value = text
    elif kind == "path":
        value = str(raw)
        validate_variable_value(name, value)
    elif kind == "list":
        if isinstance(raw, list):
            value = [str(item) for item in raw]
        else:
            value = [part.strip() for part in str(raw).split(",") if part.strip()]
    else:
        raise RecipeError(f"parameter {name} has unsupported type {kind!r}")
    return value


def _check_constraints(spec: dict[str, Any], value: object) -> None:
    name = str(spec["name"])
    allowed = spec.get("allowed")
    if (
        allowed is not None
        and value not in allowed
        and str(value) not in [str(item) for item in allowed]
    ):
        raise RecipeError(f"parameter {name} must be one of {allowed}")
    if "min" in spec and isinstance(value, int) and value < int(spec["min"]):
        raise RecipeError(f"parameter {name} is below {spec['min']}")
    if "max" in spec and isinstance(value, int) and value > int(spec["max"]):
        raise RecipeError(f"parameter {name} is above {spec['max']}")
    pattern = spec.get("pattern")
    if pattern:
        import re

        if not isinstance(value, str) or re.fullmatch(str(pattern), value) is None:
            raise RecipeError(f"parameter {name} does not match {pattern}")


def builtin_dir() -> Path:
    """Return the example recipe directory.

    A checkout keeps the recipes in ``examples/`` at the repository root.
    An installed copy reads those same files from the package.
    """
    here = Path(__file__).resolve().parent
    if here.parent.parent.name == "src":
        checkout = here.parent.parent.parent / "examples"
        if checkout.is_dir():
            return checkout
    return Path(str(files("lupaxa.file_recipe").joinpath("examples")))


def recipe_search_dirs(cwd: Path, user_recipe_dir: Path) -> list[Path]:
    """Return recipe directories from most specific to built-ins."""
    return [
        cwd / ".file-recipe" / "recipes",
        cwd / "recipes",
        user_recipe_dir,
        builtin_dir(),
    ]


def load_named(name: str, cwd: Path, user_recipe_dir: Path, *, strict: bool = False) -> Recipe:
    """Load a recipe by path or by an unambiguous name."""
    candidate = Path(name)
    if candidate.suffix == ".toml" or "/" in name or name.startswith("."):
        path = candidate if candidate.is_absolute() else cwd / candidate
        if not path.is_file():
            raise RecipeError(f"recipe not found: {name}")
        return recipe_from_text(path.read_text(encoding="utf-8"), path, strict=strict)
    project_hits: list[Path] = []
    for directory in recipe_search_dirs(cwd, user_recipe_dir)[:2]:
        project_hits.extend(_named_in(directory, name))
    if len(project_hits) > 1:
        joined = ", ".join(str(path) for path in project_hits)
        raise RecipeError(f"ambiguous recipe {name}: {joined}")
    if project_hits:
        path = project_hits[0]
        return recipe_from_text(path.read_text(encoding="utf-8"), path, strict=strict)
    user_hits = _named_in(user_recipe_dir, name)
    if len(user_hits) > 1:
        raise RecipeError(f"ambiguous recipe {name}")
    if user_hits:
        path = user_hits[0]
        return recipe_from_text(path.read_text(encoding="utf-8"), path, strict=strict)
    builtin_hits = _named_in(builtin_dir(), name)
    if len(builtin_hits) == 1:
        path = builtin_hits[0]
        return recipe_from_text(path.read_text(encoding="utf-8"), path, strict=strict)
    raise RecipeError(f"recipe not found: {name}")


def _named_in(directory: Path, name: str) -> list[Path]:
    if not directory.is_dir():
        return []
    direct = directory / f"{name}.toml"
    hits = [direct] if direct.is_file() else []
    return hits


def list_recipes(cwd: Path, user_recipe_dir: Path) -> list[tuple[str, Path, str]]:
    """List recipe names, paths, and which tier they came from."""
    labels = ("project", "project", "user", "builtin")
    rows: list[tuple[str, Path, str]] = []
    seen: set[str] = set()
    for directory, label in zip(recipe_search_dirs(cwd, user_recipe_dir), labels, strict=False):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.toml")):
            if path.stem in seen and label != "builtin":
                rows.append((path.stem, path, label))
                continue
            if path.stem in seen:
                continue
            seen.add(path.stem)
            rows.append((path.stem, path, label))
    return rows
