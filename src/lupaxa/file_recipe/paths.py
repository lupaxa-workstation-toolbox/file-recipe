"""Platform paths for config, journals, and quarantine."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def home() -> Path:
    """Return the home directory, honouring HOME in tests."""
    return Path(os.environ.get("HOME") or Path.home())


def config_path() -> Path:
    """Return the user config file path.

    ``FILE_RECIPE_CONFIG`` wins. Otherwise the file is
    ``~/.config/file-recipe/config.toml`` on POSIX and
    ``%APPDATA%/file-recipe/config.toml`` on Windows.
    """
    override = os.environ.get("FILE_RECIPE_CONFIG")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or home())
        return base / "file-recipe" / "config.toml"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg else home() / ".config"
    return root / "file-recipe" / "config.toml"


def data_root() -> Path:
    """Return the platform data directory for journals and quarantine."""
    override = os.environ.get("FILE_RECIPE_DATA")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return home() / "Library" / "Application Support" / "file-recipe"
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or home())
        return base / "file-recipe"
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else home() / ".local" / "share"
    return root / "file-recipe"


def default_journal_dir() -> Path:
    """Return the default journal directory."""
    return data_root() / "journals"


def default_quarantine_dir() -> Path:
    """Return the default quarantine directory."""
    return data_root() / "quarantine"


def default_temp_dir() -> Path:
    """Return the default temporary directory."""
    return data_root() / "tmp"


def default_user_recipe_dir() -> Path:
    """Return the default user recipe directory."""
    return config_path().parent / "recipes"
