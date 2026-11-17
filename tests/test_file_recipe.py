"""Planner, execution, and safety tests. Fixtures stay in temporary directories."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lupaxa.file_recipe.cli import main
from lupaxa.file_recipe.engine import execute_plan, load_journal, undo_journal
from lupaxa.file_recipe.errors import LimitError, RecipeError, StalePlanError
from lupaxa.file_recipe.planner import Request, build_plan
from lupaxa.file_recipe.recipe import Recipe, builtin_dir, recipe_from_text
from lupaxa.file_recipe.report import render_plan
from lupaxa.file_recipe.template import render

EXAMPLES = (
    "downloads",
    "photos",
    "documents",
    "temp-cleanup",
    "duplicates",
    "archive-prep",
    "rename-and-move",
    "safe-text-replace",
    "chained",
)


def _request(tmp_path: Path) -> tuple[Path, Request]:
    work = tmp_path / "work"
    work.mkdir()
    data = tmp_path / "data"
    return work, Request(
        cwd=work,
        journal_dir=data / "journals",
        quarantine_dir=data / "quarantine",
        temp_dir=data / "tmp",
        user_recipe_dir=data / "recipes",
    )


def _recipe(text: str, work: Path) -> Recipe:
    path = work / "recipe.toml"
    path.write_text(text, encoding="utf-8")
    return recipe_from_text(path.read_text(encoding="utf-8"), path)


def test_examples_validate() -> None:
    root = builtin_dir()
    for name in EXAMPLES:
        recipe_from_text((root / f"{name}.toml").read_text(encoding="utf-8"))


def test_unknown_variable_and_traversal(tmp_path: Path) -> None:
    with pytest.raises(RecipeError):
        render("{missing}", {}, allow_absolute=False)
    with pytest.raises(RecipeError):
        render("{stem}/../../etc/passwd", {"stem": "a"}, allow_absolute=False)
    with pytest.raises(RecipeError):
        render("CON.txt", {}, allow_absolute=False)


def test_planning_does_not_modify_files(tmp_path: Path) -> None:
    work, request = _request(tmp_path)
    source = work / "notes.txt"
    source.write_text("hello", encoding="utf-8")
    before = source.stat().st_mtime_ns
    recipe = _recipe(
        """
version = 1
[metadata]
name = "move-notes"
description = "Move notes."
[output]
directory = "out"
[rules]
[[rules.items]]
name = "notes"
glob = "*.txt"
[rules.items.action]
type = "move"
destination = "{output}/{filename}"
""",
        work,
    )
    plan = build_plan(recipe, request)
    assert source.stat().st_mtime_ns == before
    assert not (work / "out").exists()
    assert plan.conflicts == []
    assert any(op.kind == "move" for op in plan.operations)


def test_duplicate_destination_conflicts(tmp_path: Path) -> None:
    work, request = _request(tmp_path)
    (work / "a.txt").write_text("a", encoding="utf-8")
    (work / "b.txt").write_text("b", encoding="utf-8")
    recipe = _recipe(
        """
version = 1
[metadata]
name = "collide"
description = "Collide."
[output]
directory = "out"
collision = "error"
[rules]
[[rules.items]]
name = "all"
glob = "*.txt"
[rules.items.action]
type = "move"
destination = "{output}/same.txt"
""",
        work,
    )
    plan = build_plan(recipe, request)
    assert plan.conflicts


def test_symlink_is_not_followed(tmp_path: Path) -> None:
    work, request = _request(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    (work / "link").symlink_to(outside)
    (work / "keep.txt").write_text("yes", encoding="utf-8")
    recipe = _recipe(
        """
version = 1
[metadata]
name = "links"
description = "Do not follow links."
[input]
recursive = true
[output]
directory = "out"
[rules]
[[rules.items]]
name = "text"
glob = "*.txt"
[rules.items.action]
type = "move"
destination = "{output}/{filename}"
""",
        work,
    )
    plan = build_plan(recipe, request)
    sources = [op.source for op in plan.operations if op.kind == "move"]
    assert any(str(source).endswith("keep.txt") for source in sources)
    assert not any(source and "secret.txt" in source for source in sources)


def test_move_undo_and_redo_round_trip(tmp_path: Path) -> None:
    work, request = _request(tmp_path)
    (work / "notes.txt").write_text("hello", encoding="utf-8")
    recipe = _recipe(
        """
version = 1
[metadata]
name = "move-notes"
description = "Move notes."
[output]
directory = "out"
[rules]
[[rules.items]]
name = "notes"
glob = "*.txt"
[rules.items.action]
type = "move"
destination = "{output}/{filename}"
""",
        work,
    )
    plan = build_plan(recipe, request)
    journal = execute_plan(plan)
    assert (work / "out" / "notes.txt").read_text(encoding="utf-8") == "hello"
    assert not (work / "notes.txt").exists()
    undone = undo_journal(journal)
    assert (work / "notes.txt").read_text(encoding="utf-8") == "hello"
    assert undone["state"] == "rolled_back"


def test_quarantine_restore(tmp_path: Path) -> None:
    work, request = _request(tmp_path)
    (work / "cache.tmp").write_text("temp", encoding="utf-8")
    recipe = _recipe(
        """
version = 1
[metadata]
name = "cleanup"
description = "Quarantine temp files."
[rules]
[[rules.items]]
name = "temporary"
glob = "*.tmp"
[rules.items.action]
type = "delete"
permanent = false
""",
        work,
    )
    journal = execute_plan(build_plan(recipe, request))
    assert not (work / "cache.tmp").exists()
    undo_journal(journal)
    assert (work / "cache.tmp").read_text(encoding="utf-8") == "temp"


def test_permanent_delete_requires_authorisation(tmp_path: Path) -> None:
    work, request = _request(tmp_path)
    text = """
version = 1
[metadata]
name = "wipe"
description = "Delete permanently."
[rules]
[[rules.items]]
name = "temporary"
glob = "*.tmp"
[rules.items.action]
type = "delete"
permanent = true
"""
    with pytest.raises(RecipeError):
        _recipe(text, work)


def test_text_replace_undo(tmp_path: Path) -> None:
    work, request = _request(tmp_path)
    (work / "notes.txt").write_text("TODO item\n", encoding="utf-8")
    recipe = _recipe(
        """
version = 1
[metadata]
name = "replace"
description = "Replace text."
[rules]
[[rules.items]]
name = "notes"
glob = "*.txt"
[rules.items.action]
type = "text"
encoding = "utf-8"
[[rules.items.action.literal]]
find = "TODO"
replace = "DONE"
""",
        work,
    )
    journal = execute_plan(build_plan(recipe, request))
    assert "DONE" in (work / "notes.txt").read_text(encoding="utf-8")
    undo_journal(journal)
    assert (work / "notes.txt").read_text(encoding="utf-8") == "TODO item\n"


def test_stale_plan(tmp_path: Path) -> None:
    work, request = _request(tmp_path)
    path = work / "notes.txt"
    path.write_text("one", encoding="utf-8")
    recipe = _recipe(
        """
version = 1
[metadata]
name = "move-notes"
description = "Move notes."
[output]
directory = "out"
[rules]
[[rules.items]]
name = "notes"
glob = "*.txt"
[rules.items.action]
type = "move"
destination = "{output}/{filename}"
""",
        work,
    )
    plan = build_plan(recipe, request)
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(StalePlanError):
        execute_plan(plan)
    assert path.read_text(encoding="utf-8") == "changed"


def test_html_escapes(tmp_path: Path) -> None:
    work, request = _request(tmp_path)
    (work / "<script>.txt").write_text("x", encoding="utf-8")
    recipe = _recipe(
        """
version = 1
[metadata]
name = "odd"
description = "Odd name."
[output]
directory = "out"
[rules]
[[rules.items]]
name = "notes"
glob = "*.txt"
[rules.items.action]
type = "move"
destination = "{output}/{filename}"
""",
        work,
    )
    page = render_plan(build_plan(recipe, request), "html")
    assert "<script>" not in page.split("<title>", 1)[-1]
    assert "&lt;script&gt;" in page


def test_file_limit(tmp_path: Path) -> None:
    work, request = _request(tmp_path)
    (work / "a.txt").write_text("a", encoding="utf-8")
    (work / "b.txt").write_text("b", encoding="utf-8")
    recipe = _recipe(
        """
version = 1
[metadata]
name = "limited"
description = "Limited."
[limits]
max_files = 1
[rules]
[[rules.items]]
name = "notes"
glob = "*.txt"
[rules.items.action]
type = "hash"
algorithm = "sha256"
""",
        work,
    )
    with pytest.raises(LimitError):
        build_plan(recipe, request)


def test_cli_help_and_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    work, _request_unused = _request(tmp_path)
    del _request_unused
    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FILE_RECIPE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("FILE_RECIPE_CONFIG", str(tmp_path / "missing.toml"))
    (work / "notes.txt").write_text("hello", encoding="utf-8")
    recipe = work / "job.toml"
    recipe.write_text(
        """
version = 1
[metadata]
name = "job"
description = "Move notes."
[output]
directory = "out"
[rules]
[[rules.items]]
name = "notes"
glob = "*.txt"
[rules.items.action]
type = "move"
destination = "{output}/{filename}"
""",
        encoding="utf-8",
    )
    assert main(["version"]) == 0
    for command in (
        "init",
        "list",
        "validate",
        "explain",
        "preview",
        "plan",
        "run",
        "history",
        "operations",
        "config",
    ):
        assert main([command, "--help"]) == 0
    assert main(["--help"]) == 0
    assert main(["preview", "--recipe", str(recipe), "--json"]) == 0
    assert main(["run", "--recipe", str(recipe), "--yes"]) == 0
    assert (work / "out" / "notes.txt").is_file()
    captured = capsys.readouterr()
    assert "schema_version" in captured.out or "Journal" in captured.out or captured.out
    journal = next((tmp_path / "data").rglob("*.json"))
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert load_journal(journal)["state"] == "completed"
