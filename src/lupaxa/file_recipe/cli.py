"""Command-line interface for file-recipe."""

from __future__ import annotations

import argparse
import sys
from typing import TextIO

from lupaxa.file_recipe.commands import COMMANDS, dispatch
from lupaxa.file_recipe.errors import FileRecipeError

_FORMATS = ("text", "json", "jsonl", "csv", "markdown", "html")


def main(
    argv: list[str] | None = None, stdin: TextIO | None = None, stdout: TextIO | None = None
) -> int:
    """Run one file-recipe command and return an exit code."""
    out = stdout or sys.stdout
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 0
    if not getattr(args, "command", None):
        parser.print_help(out)
        return 2
    try:
        return int(dispatch(args, stdin or sys.stdin, out))
    except FileRecipeError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser. Every command accepts ``--help``."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--recipe")
    common.add_argument("--input", action="append", default=[])
    common.add_argument("--output")
    common.add_argument("--recursive", action=argparse.BooleanOptionalAction)
    common.add_argument("--include", action="append", default=[])
    common.add_argument("--exclude", action="append", default=[])
    common.add_argument("--var", action="append", default=[])
    common.add_argument("--config")
    common.add_argument("--format", choices=_FORMATS, default="text")
    common.add_argument("--json", action="store_true")
    common.add_argument("--quiet", action="store_true")
    common.add_argument("--verbose", action="store_true")
    common.add_argument("--no-colour", action="store_true")
    common.add_argument("--no-color", action="store_true")
    common.add_argument("--yes", action="store_true")
    common.add_argument("--force", action="store_true")
    common.add_argument("--dry-run", action="store_true")
    common.add_argument("--strict", action="store_true")
    common.add_argument("--workers", type=int)
    common.add_argument("--timeout", type=float)
    common.add_argument(
        "--order", choices=("path", "filename", "relative", "size", "mtime", "ctime")
    )
    common.add_argument(
        "--detail", choices=("summary", "detailed", "conflicts", "warnings"), default="detailed"
    )
    common.add_argument("--plan")
    common.add_argument("--allow-destructive", action="store_true")
    common.add_argument("--on-error", choices=("stop", "continue", "rollback"))
    parser = argparse.ArgumentParser(
        prog="file-recipe", description="Plan and run filesystem recipes."
    )
    sub = parser.add_subparsers(dest="command")
    for name in COMMANDS:
        command = sub.add_parser(name, parents=[common], help=f"{name} files from a recipe")
        if name in {"undo", "redo", "status", "report", "recover"}:
            command.add_argument("target", nargs="?", default="last")
        if name == "recover":
            command.add_argument("--resume", action="store_true")
            command.add_argument("--rollback", action="store_true")
            command.add_argument("--resolved", action="store_true")
        if name == "create":
            command.add_argument("name")
    return parser
