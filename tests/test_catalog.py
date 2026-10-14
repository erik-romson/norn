"""Tests for norn/catalog.py — AST-based pipeline discovery."""
from __future__ import annotations

import re

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
    info = get_pipeline_info("hello_v1")
    assert info is not None
    assert info.name == "hello_v1"
    assert info.short  # non-empty first line
    assert info.path.name == "hello_v1.py"


def test_get_pipeline_info_nonexistent() -> None:
    assert get_pipeline_info("nonexistent") is None


def test_load_bundled_pipeline_hello() -> None:
    pipeline = load_bundled_pipeline("hello_v1")
    assert isinstance(pipeline, Pipeline)


def test_load_bundled_pipeline_invalid_raises() -> None:
    with pytest.raises(Exception):
        load_bundled_pipeline("__nonexistent_module__")


def test_ast_extraction_reads_metadata() -> None:
    """Metadata dict (env_vars, args) is extracted correctly via AST."""
    info = get_pipeline_info("vanilla_change_v1")
    assert info is not None
    assert "ANTHROPIC_API_KEY" in info.env_vars
    assert "args" in info.args


def test_list_pipelines_excludes_init() -> None:
    names = [info.name for info in list_pipelines()]
    assert "__init__" not in names


def test_list_pipelines_excludes_private_helpers() -> None:
    """Private modules in norn/pipelines/ are helpers, not pipelines.

    ``_snapshot_diff.py`` is a script invoked by ``implement_features_v1``; it has
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


def test_fix_jira_issue_v1_is_listed() -> None:
    """fix_jira_issue_v1 is discoverable via norn list."""
    names = [info.name for info in list_pipelines()]
    assert "fix_jira_issue_v1" in names


def test_bundled_pipeline_names_are_versioned() -> None:
    """Every bundled pipeline name carries a ``_v<N>`` suffix."""
    for info in list_pipelines():
        assert re.match(r"^[a-z0-9_]+_v[0-9]+$", info.name), info.name


def test_every_copy_has_its_original() -> None:
    """Every ``<base>_v<N>`` with N > 1 is listed alongside ``<base>_v<N-1>``."""
    names = {info.name for info in list_pipelines()}
    copies = 0
    for name in names:
        m = re.match(r"^(?P<base>[a-z0-9_]+)_v(?P<n>[0-9]+)$", name)
        assert m, name
        n = int(m.group("n"))
        if n > 1:
            copies += 1
            predecessor = f"{m.group('base')}_v{n - 1}"
            assert predecessor in names, f"{name} has no predecessor {predecessor}"
    assert copies > 0
