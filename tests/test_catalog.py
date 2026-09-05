"""Tests for norn/catalog.py — AST-based pipeline discovery."""
from __future__ import annotations

import pytest

from norn.catalog import (
    PipelineInfo,
    get_pipeline_info,
    list_discovered_pipelines,
    list_pipelines,
    load_bundled_pipeline,
)
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


def test_list_pipelines_excludes_private_helpers() -> None:
    """Private modules in norn/pipelines/ are helpers, not pipelines.

    ``_snapshot_diff.py`` is a script invoked by ``implement_features``; it has
    no ``config`` so listing it hands the user a name that cannot be run.
    """
    names = [info.name for info in list_pipelines()]
    assert "_snapshot_diff" not in names
    assert not [n for n in names if n.startswith("_")]


def test_list_discovered_pipelines_excludes_private_helpers(tmp_path) -> None:
    (tmp_path / "_helper.py").write_text('"""Helper, not a pipeline."""\n')
    (tmp_path / "real.py").write_text('"""A real pipeline."""\n')

    names = [info.name for info in list_discovered_pipelines(extra_dirs=[tmp_path])]
    assert "real" in names
    assert "_helper" not in names


def test_list_pipelines_sorted() -> None:
    infos = list_pipelines()
    names = [info.name for info in infos]
    assert names == sorted(names)


def test_fix_jira_issue_is_listed() -> None:
    """fix_jira_issue is discoverable via norn list."""
    names = [info.name for info in list_pipelines()]
    assert "fix_jira_issue" in names
