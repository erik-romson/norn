from __future__ import annotations

import json

import pytest

from norn.models import PipelineContext
from norn.stages.validate import Contains, FileExists, JsonSchema, Validate


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


# ---------------------------------------------------------------------------
# FileExists
# ---------------------------------------------------------------------------


def test_file_exists_pass(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("content")
    assert FileExists(str(f)).run() == []


def test_file_exists_fail(tmp_path):
    errors = FileExists(str(tmp_path / "no.txt")).run()
    assert len(errors) == 1
    assert "no.txt" in errors[0]


# ---------------------------------------------------------------------------
# Contains
# ---------------------------------------------------------------------------


def test_contains_all_present(tmp_path):
    f = tmp_path / "src.py"
    f.write_text("@app.route\ndef handler(): pass")
    assert Contains(str(f), patterns=["@app.route", "def "]).run() == []


def test_contains_missing_pattern(tmp_path):
    f = tmp_path / "src.py"
    f.write_text("def handler(): pass")
    errors = Contains(str(f), patterns=["@app.route", "def "]).run()
    assert len(errors) == 1
    assert "@app.route" in errors[0]


def test_contains_file_not_found(tmp_path):
    errors = Contains(str(tmp_path / "no.py"), patterns=["x"]).run()
    assert len(errors) == 1
    assert "Cannot read" in errors[0]


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
    assert JsonSchema(str(f), schema=schema).run() == []


def test_json_schema_missing_required(tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"name": "alice"}))
    schema = {"type": "object", "required": ["name", "active"]}
    errors = JsonSchema(str(f), schema=schema).run()
    assert len(errors) == 1
    assert "active" in errors[0]


def test_json_schema_wrong_type(tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"name": 42}))
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    errors = JsonSchema(str(f), schema=schema).run()
    assert len(errors) == 1
    assert "string" in errors[0]


def test_json_schema_invalid_json(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("not json {")
    errors = JsonSchema(str(f), schema={"type": "object"}).run()
    assert len(errors) == 1
    assert "invalid JSON" in errors[0]


def test_json_schema_file_not_found(tmp_path):
    errors = JsonSchema(str(tmp_path / "no.json"), schema={"type": "object"}).run()
    assert len(errors) == 1
    assert "Cannot read" in errors[0]


def test_json_schema_array_pass(tmp_path):
    f = tmp_path / "list.json"
    f.write_text(json.dumps(["a", "b", "c"]))
    assert JsonSchema(str(f), schema={"type": "array", "items": {"type": "string"}}).run() == []


def test_json_schema_array_wrong_item_type(tmp_path):
    f = tmp_path / "list.json"
    f.write_text(json.dumps(["a", 1, "c"]))
    errors = JsonSchema(str(f), schema={"type": "array", "items": {"type": "string"}}).run()
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
    errors = JsonSchema(str(f), schema=schema).run()
    assert len(errors) == 1
    assert "email" in errors[0]
