"""Tests for norn/catalog.py — AST-based pipeline discovery."""
from __future__ import annotations

import pytest

from norn.catalog import PipelineInfo, get_pipeline_info, list_pipelines, load_bundled_pipeline
from norn.dsl import Pipeline


def test_list_pipelines_returns_non_empty() -> None:
    infos = list_pipelines()
    assert len(infos) > 0


def test_each_entry_has_name_short_and_path() -> None:
    for info in list_pipelines():
        assert info.name, f"missing name: {info}"
        assert info.short, f"missing short description: {info}"
        assert info.path.exists(), f"path does not exist: {info.path}"


def test_get_pipeline_info_hello() -> None:
    info = get_pipeline_info("hello")
    assert info is not None
    assert info.name == "hello"
    assert info.short  # non-empty first line
    assert info.path.name == "hello.py"


def test_get_pipeline_info_nonexistent() -> None:
    assert get_pipeline_info("nonexistent") is None


def test_load_bundled_pipeline_hello() -> None:
    pipeline = load_bundled_pipeline("hello")
    assert isinstance(pipeline, Pipeline)


def test_load_bundled_pipeline_invalid_raises() -> None:
    with pytest.raises(Exception):
        load_bundled_pipeline("__nonexistent_module__")


def test_ast_extraction_reads_metadata() -> None:
    """Metadata dict (env_vars, args) is extracted correctly via AST."""
    info = get_pipeline_info("vanilla_change")
    assert info is not None
    assert "ANTHROPIC_API_KEY" in info.env_vars
    assert "args" in info.args


def test_list_pipelines_excludes_init() -> None:
    names = [info.name for info in list_pipelines()]
    assert "__init__" not in names


def test_list_pipelines_sorted() -> None:
    infos = list_pipelines()
    names = [info.name for info in infos]
    assert names == sorted(names)
