from __future__ import annotations

import json
from pathlib import Path

import pytest

from norn.models import PipelineContext
from norn.stages.validate import Contains, FileExists, JsonSchema, Validate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(tmp_path=None) -> PipelineContext:
    """Return a PipelineContext with working_dir set to *tmp_path* when given."""
    ctx = PipelineContext()
    if tmp_path is not None:
        ctx.working_dir = str(tmp_path)
    return ctx


# ---------------------------------------------------------------------------
# Validate stage — integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_all_pass(tmp_path):
    f = tmp_path / "hello.py"
    f.write_text("def foo(): pass")
    stage = Validate(checks=[FileExists(str(f)), Contains(str(f), patterns=["def foo"])])
    result = await stage.run(PipelineContext())
    assert result.success
    assert result.output == []


@pytest.mark.asyncio
async def test_validate_collects_all_errors(tmp_path):
    """All checks run — errors from every failing check are collected."""
    f = tmp_path / "hello.py"
    f.write_text("def foo(): pass")
    missing = str(tmp_path / "missing.py")
    stage = Validate(
        checks=[
            FileExists(missing),
            Contains(str(f), patterns=["missing_pattern_1", "missing_pattern_2"]),
        ]
    )
    result = await stage.run(PipelineContext())
    assert not result.success
    assert len(result.output) == 3  # 1 file-not-found + 2 missing patterns
    assert any("missing.py" in e for e in result.output)
    assert any("missing_pattern_1" in e for e in result.output)
    assert any("missing_pattern_2" in e for e in result.output)


@pytest.mark.asyncio
async def test_validate_error_joined_in_stage_error(tmp_path):
    """StageResult.error contains all error strings joined by newline."""
    f = tmp_path / "f.py"
    f.write_text("x")
    stage = Validate(checks=[Contains(str(f), patterns=["a", "b"])])
    result = await stage.run(PipelineContext())
    assert not result.success
    assert "a" in result.error
    assert "b" in result.error


@pytest.mark.asyncio
async def test_validate_resolves_relative_paths_under_working_dir(tmp_path):
    """Relative paths in checks resolve under ctx.working_dir."""
    (tmp_path / "out.txt").write_text("hello world")
    ctx = _ctx(tmp_path)
    stage = Validate(
        checks=[
            FileExists("out.txt"),
            Contains("out.txt", patterns=["hello"]),
        ]
    )
    result = await stage.run(ctx)
    assert result.success


@pytest.mark.asyncio
async def test_validate_relative_missing_file_under_working_dir(tmp_path):
    """A relative path that does not exist under working_dir reports an error."""
    ctx = _ctx(tmp_path)
    stage = Validate(checks=[FileExists("nope.txt")])
    result = await stage.run(ctx)
    assert not result.success
    assert any("nope.txt" in e for e in result.output)


# ---------------------------------------------------------------------------
# FileExists
# ---------------------------------------------------------------------------


def test_file_exists_pass(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("content")
    assert FileExists(str(f)).run(_ctx()) == []


def test_file_exists_fail(tmp_path):
    errors = FileExists(str(tmp_path / "no.txt")).run(_ctx())
    assert len(errors) == 1
    assert "no.txt" in errors[0]


def test_file_exists_relative_pass(tmp_path):
    (tmp_path / "rel.txt").write_text("x")
    assert FileExists("rel.txt").run(_ctx(tmp_path)) == []


def test_file_exists_relative_fail(tmp_path):
    errors = FileExists("missing.txt").run(_ctx(tmp_path))
    assert len(errors) == 1
    assert "missing.txt" in errors[0]


def test_file_exists_absolute_ignores_working_dir(tmp_path):
    """An absolute path is not joined with working_dir."""
    other = tmp_path / "sub"
    other.mkdir()
    f = tmp_path / "abs.txt"
    f.write_text("y")
    # absolute path points to tmp_path/abs.txt, working_dir is sub/ — should still find it
    assert FileExists(str(f)).run(_ctx(other)) == []


def test_file_exists_no_working_dir_uses_cwd(tmp_path):
    """When working_dir is unset, relative paths resolve against process cwd."""
    # Write a file in process cwd so the check can find it via a relative path.
    target = Path.cwd() / "__norn_test_validate_fe_cwd__.txt"
    target.write_text("x")
    try:
        assert FileExists("__norn_test_validate_fe_cwd__.txt").run(_ctx()) == []
    finally:
        target.unlink()


# ---------------------------------------------------------------------------
# Contains
# ---------------------------------------------------------------------------


def test_contains_all_present(tmp_path):
    f = tmp_path / "src.py"
    f.write_text("@app.route\ndef handler(): pass")
    assert Contains(str(f), patterns=["@app.route", "def "]).run(_ctx()) == []


def test_contains_missing_pattern(tmp_path):
    f = tmp_path / "src.py"
    f.write_text("def handler(): pass")
    errors = Contains(str(f), patterns=["@app.route", "def "]).run(_ctx())
    assert len(errors) == 1
    assert "@app.route" in errors[0]


def test_contains_file_not_found(tmp_path):
    errors = Contains(str(tmp_path / "no.py"), patterns=["x"]).run(_ctx())
    assert len(errors) == 1
    assert "Cannot read" in errors[0]


def test_contains_relative_path(tmp_path):
    (tmp_path / "rel.py").write_text("import os")
    assert Contains("rel.py", patterns=["import os"]).run(_ctx(tmp_path)) == []


def test_contains_relative_missing(tmp_path):
    errors = Contains("ghost.py", patterns=["x"]).run(_ctx(tmp_path))
    assert len(errors) == 1
    assert "Cannot read" in errors[0]


def test_contains_absolute_ignores_working_dir(tmp_path):
    """An absolute path is read regardless of working_dir."""
    other = tmp_path / "sub"
    other.mkdir()
    f = tmp_path / "abs.txt"
    f.write_text("target content")
    assert Contains(str(f), patterns=["target"]).run(_ctx(other)) == []


def test_contains_no_working_dir_uses_cwd(tmp_path):
    """When working_dir is unset, relative paths resolve against process cwd."""
    target = Path.cwd() / "__norn_test_validate_contains_cwd__.txt"
    target.write_text("sentinel text")
    try:
        assert Contains("__norn_test_validate_contains_cwd__.txt", patterns=["sentinel"]).run(_ctx()) == []
    finally:
        target.unlink()


# ---------------------------------------------------------------------------
# JsonSchema
# ---------------------------------------------------------------------------


def test_json_schema_pass(tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"name": "alice", "active": True}))
    schema = {
        "type": "object",
        "required": ["name", "active"],
        "properties": {
            "name": {"type": "string"},
            "active": {"type": "boolean"},
        },
    }
    assert JsonSchema(str(f), schema=schema).run(_ctx()) == []


def test_json_schema_missing_required(tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"name": "alice"}))
    schema = {"type": "object", "required": ["name", "active"]}
    errors = JsonSchema(str(f), schema=schema).run(_ctx())
    assert len(errors) == 1
    assert "active" in errors[0]


def test_json_schema_wrong_type(tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"name": 42}))
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    errors = JsonSchema(str(f), schema=schema).run(_ctx())
    assert len(errors) == 1
    assert "string" in errors[0]


def test_json_schema_invalid_json(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("not json {")
    errors = JsonSchema(str(f), schema={"type": "object"}).run(_ctx())
    assert len(errors) == 1
    assert "invalid JSON" in errors[0]


def test_json_schema_file_not_found(tmp_path):
    errors = JsonSchema(str(tmp_path / "no.json"), schema={"type": "object"}).run(_ctx())
    assert len(errors) == 1
    assert "Cannot read" in errors[0]


def test_json_schema_array_pass(tmp_path):
    f = tmp_path / "list.json"
    f.write_text(json.dumps(["a", "b", "c"]))
    assert JsonSchema(str(f), schema={"type": "array", "items": {"type": "string"}}).run(_ctx()) == []


def test_json_schema_array_wrong_item_type(tmp_path):
    f = tmp_path / "list.json"
    f.write_text(json.dumps(["a", 1, "c"]))
    errors = JsonSchema(str(f), schema={"type": "array", "items": {"type": "string"}}).run(_ctx())
    assert len(errors) == 1
    assert "string" in errors[0]


def test_json_schema_nested_object(tmp_path):
    f = tmp_path / "nested.json"
    f.write_text(json.dumps({"user": {"id": "u1"}}))
    schema = {
        "type": "object",
        "properties": {
            "user": {
                "type": "object",
                "required": ["id", "email"],
                "properties": {"id": {"type": "string"}},
            }
        },
    }
    errors = JsonSchema(str(f), schema=schema).run(_ctx())
    assert len(errors) == 1
    assert "email" in errors[0]


def test_json_schema_relative_path(tmp_path):
    (tmp_path / "data.json").write_text(json.dumps({"x": 1}))
    schema = {"type": "object", "required": ["x"]}
    assert JsonSchema("data.json", schema=schema).run(_ctx(tmp_path)) == []


def test_json_schema_absolute_ignores_working_dir(tmp_path):
    """An absolute path is read regardless of working_dir."""
    other = tmp_path / "sub"
    other.mkdir()
    f = tmp_path / "abs.json"
    f.write_text(json.dumps({"k": "v"}))
    schema = {"type": "object", "required": ["k"]}
    assert JsonSchema(str(f), schema=schema).run(_ctx(other)) == []
