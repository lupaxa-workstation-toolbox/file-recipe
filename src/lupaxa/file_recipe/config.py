"""Configuration loading and precedence."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lupaxa.file_recipe.errors import RecipeError
from lupaxa.file_recipe.paths import (
    config_path,
    default_journal_dir,
    default_quarantine_dir,
    default_temp_dir,
    default_user_recipe_dir,
)

CONFIG_KEYS = frozenset(
    {
        "recipe_dir",
        "journal_dir",
        "quarantine_dir",
        "temp_dir",
        "strict",
        "workers",
        "colour",
        "collision",
        "follow_symlinks",
    }
)


@dataclass
class Settings:
    """Effective settings after precedence is applied."""

    strict: bool = False
    workers: int = 1
    colour: bool = True
    collision: str = "error"
    follow_symlinks: bool = False
    journal_dir: Path = field(default_factory=default_journal_dir)
    quarantine_dir: Path = field(default_factory=default_quarantine_dir)
    temp_dir: Path = field(default_factory=default_temp_dir)
    recipe_dir: Path = field(default_factory=default_user_recipe_dir)
    sources: dict[str, str] = field(default_factory=dict)


def load_config_file(path: Path) -> dict[str, Any]:
    """Load a TOML config file. A missing file is an empty config."""
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise RecipeError(f"invalid config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RecipeError(f"invalid config {path}: expected a table")
    unknown = sorted(set(data) - CONFIG_KEYS)
    if unknown:
        raise RecipeError(f"invalid config {path}: unknown keys {', '.join(unknown)}")
    return data


def _pick(
    name: str,
    layers: list[tuple[str, dict[str, Any]]],
    default: object,
) -> tuple[object, str]:
    for source, data in layers:
        if name in data and data[name] is not None:
            return data[name], source
    return default, "default"


def resolve_settings(
    *,
    cli: dict[str, Any] | None = None,
    config_file: dict[str, Any] | None = None,
    recipe: dict[str, Any] | None = None,
    user: dict[str, Any] | None = None,
) -> Settings:
    """Apply CLI, ``--config``, recipe, user config, then built-in defaults.

    ``allow_destructive`` is not a config key. Only the recipe or the CLI can
    authorise destructive operations. A user config file cannot turn that on.
    """
    layers = [
        ("cli", cli or {}),
        ("config", config_file or {}),
        ("recipe", recipe or {}),
        ("user", user or {}),
    ]
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    defaults: dict[str, Any] = {
        "strict": False,
        "workers": 1,
        "colour": True,
        "collision": "error",
        "follow_symlinks": False,
        "journal_dir": str(default_journal_dir()),
        "quarantine_dir": str(default_quarantine_dir()),
        "temp_dir": str(default_temp_dir()),
        "recipe_dir": str(default_user_recipe_dir()),
    }
    for key, default in defaults.items():
        value, source = _pick(key, layers, default)
        values[key] = value
        sources[key] = source
    try:
        workers = int(values["workers"])
    except (TypeError, ValueError) as exc:
        raise RecipeError(f"invalid workers value {values['workers']!r}") from exc
    if workers < 1:
        raise RecipeError("workers must be at least 1")
    collision = str(values["collision"])
    if collision not in {"error", "skip", "rename", "overwrite", "quarantine-existing"}:
        raise RecipeError(f"invalid collision policy {collision!r}")
    return Settings(
        strict=_as_bool(values["strict"], "strict"),
        workers=workers,
        colour=_as_bool(values["colour"], "colour"),
        collision=collision,
        follow_symlinks=_as_bool(values["follow_symlinks"], "follow_symlinks"),
        journal_dir=Path(str(values["journal_dir"])).expanduser(),
        quarantine_dir=Path(str(values["quarantine_dir"])).expanduser(),
        temp_dir=Path(str(values["temp_dir"])).expanduser(),
        recipe_dir=Path(str(values["recipe_dir"])).expanduser(),
        sources=sources,
    )


def _as_bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise RecipeError(f"invalid boolean {name}={value!r}")


def user_config() -> dict[str, Any]:
    """Load the user config if it exists."""
    return load_config_file(config_path())
