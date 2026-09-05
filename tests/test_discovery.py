"""Tests for configured-directory pipeline discovery in norn/catalog.py.

Verifies that:
- Pipelines placed in a configured temp directory are discovered with the
  correct metadata (name, short description, path).
- Bundled discovery (list_pipelines) is unaffected by the new code.
- Files that fail to parse are silently skipped.
- Duplicate paths across directories are deduplicated.
- Files named __init__.py are excluded.

All tests are offline (no imports of discovered pipelines, no SDK calls).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from norn.catalog import (
    PipelineInfo,
    _extract_metadata,
    list_discovered_pipelines,
    list_pipelines,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SIMPLE_PIPELINE = '''\
"""A simple test pipeline.

Used only for discovery tests.
"""

from norn.dsl import Pipeline, Stage
from norn.stages.base import BaseStage

class _NoOp(BaseStage):
    async def run(self, ctx, **kwargs):
        from norn.models import StageResult
        return StageResult(name="", success=True)

config = Pipeline("test-pipe", items=[Stage("noop", _NoOp())])
'''

_METADATA_PIPELINE = '''\
"""Pipeline with metadata.

Longer description here.
"""

metadata = {
    "env_vars": ["MY_API_KEY"],
    "args": {"input": "The input file"},
}

from norn.dsl import Pipeline
config = Pipeline("meta-pipe", items=[])
'''

_BAD_PIPELINE = '''\
# This file has a syntax error
def broken(:
    pass
'''

_NO_CONFIG_PIPELINE = '''\
"""A file that doesn't define config."""
x = 42
'''


# ---------------------------------------------------------------------------
# Basic discovery
# ---------------------------------------------------------------------------


def test_discovered_pipeline_found(tmp_path: Path) -> None:
    """A .py file in extra_dirs is returned by list_discovered_pipelines."""
    (tmp_path / "mypipe.py").write_text(_SIMPLE_PIPELINE)
    results = list_discovered_pipelines(extra_dirs=[tmp_path])
    names = [r.name for r in results]
    assert "mypipe" in names


def test_discovered_pipeline_short_description(tmp_path: Path) -> None:
    """The first docstring line becomes the short description."""
    (tmp_path / "mypipe.py").write_text(_SIMPLE_PIPELINE)
    results = list_discovered_pipelines(extra_dirs=[tmp_path])
    info = next(r for r in results if r.name == "mypipe")
    assert info.short == "A simple test pipeline."


def test_discovered_pipeline_long_description(tmp_path: Path) -> None:
    """The full module docstring becomes the long description."""
    (tmp_path / "mypipe.py").write_text(_SIMPLE_PIPELINE)
    results = list_discovered_pipelines(extra_dirs=[tmp_path])
    info = next(r for r in results if r.name == "mypipe")
    assert "Used only for discovery tests." in info.long


def test_discovered_pipeline_path(tmp_path: Path) -> None:
    """The path field points to the actual file."""
    pipe_file = tmp_path / "mypipe.py"
    pipe_file.write_text(_SIMPLE_PIPELINE)
    results = list_discovered_pipelines(extra_dirs=[tmp_path])
    info = next(r for r in results if r.name == "mypipe")
    assert info.path == pipe_file or info.path.resolve() == pipe_file.resolve()


def test_discovered_pipeline_is_pipeline_info(tmp_path: Path) -> None:
    """Results are PipelineInfo instances."""
    (tmp_path / "mypipe.py").write_text(_SIMPLE_PIPELINE)
    results = list_discovered_pipelines(extra_dirs=[tmp_path])
    for r in results:
        assert isinstance(r, PipelineInfo)


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------


def test_discovered_pipeline_metadata_env_vars(tmp_path: Path) -> None:
    """env_vars from metadata dict are parsed correctly."""
    (tmp_path / "meta.py").write_text(_METADATA_PIPELINE)
    results = list_discovered_pipelines(extra_dirs=[tmp_path])
    info = next(r for r in results if r.name == "meta")
    assert "MY_API_KEY" in info.env_vars


def test_discovered_pipeline_metadata_args(tmp_path: Path) -> None:
    """args from metadata dict are parsed correctly."""
    (tmp_path / "meta.py").write_text(_METADATA_PIPELINE)
    results = list_discovered_pipelines(extra_dirs=[tmp_path])
    info = next(r for r in results if r.name == "meta")
    assert "input" in info.args


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_bad_file_is_skipped(tmp_path: Path) -> None:
    """Files with parse errors are silently skipped (no exception raised)."""
    (tmp_path / "broken.py").write_text(_BAD_PIPELINE)
    # Should not raise; broken file is just excluded
    results = list_discovered_pipelines(extra_dirs=[tmp_path])
    names = [r.name for r in results]
    assert "broken" not in names


def test_no_config_file_is_included(tmp_path: Path) -> None:
    """Files without a Pipeline config are still listed (AST scan only)."""
    # list_discovered_pipelines uses AST-only scan like list_pipelines —
    # it does not import the file to verify config, so it includes any .py
    # with a parseable module.
    (tmp_path / "noconfig.py").write_text(_NO_CONFIG_PIPELINE)
    results = list_discovered_pipelines(extra_dirs=[tmp_path])
    names = [r.name for r in results]
    assert "noconfig" in names


def test_init_py_excluded(tmp_path: Path) -> None:
    """__init__.py files are excluded from discovery."""
    (tmp_path / "__init__.py").write_text('"""Package init."""\n')
    results = list_discovered_pipelines(extra_dirs=[tmp_path])
    names = [r.name for r in results]
    assert "__init__" not in names


def test_duplicate_path_deduplicated(tmp_path: Path) -> None:
    """The same directory passed twice yields no duplicates."""
    (tmp_path / "mypipe.py").write_text(_SIMPLE_PIPELINE)
    results = list_discovered_pipelines(extra_dirs=[tmp_path, tmp_path])
    names = [r.name for r in results]
    assert names.count("mypipe") == 1


def test_multiple_pipelines_in_dir(tmp_path: Path) -> None:
    """Multiple .py files in the same directory are all discovered."""
    (tmp_path / "alpha.py").write_text('"""Alpha pipeline."""\n')
    (tmp_path / "beta.py").write_text('"""Beta pipeline."""\n')
    results = list_discovered_pipelines(extra_dirs=[tmp_path])
    names = [r.name for r in results]
    assert "alpha" in names
    assert "beta" in names


def test_multiple_dirs_merged(tmp_path: Path) -> None:
    """Pipelines from multiple directories are merged into one list."""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "pipe_a.py").write_text('"""Pipe A."""\n')
    (dir_b / "pipe_b.py").write_text('"""Pipe B."""\n')
    results = list_discovered_pipelines(extra_dirs=[dir_a, dir_b])
    names = [r.name for r in results]
    assert "pipe_a" in names
    assert "pipe_b" in names


def test_empty_dir_returns_empty(tmp_path: Path) -> None:
    """An empty directory yields an empty list."""
    results = list_discovered_pipelines(extra_dirs=[tmp_path])
    assert results == []


def test_nonexistent_extra_dir_returns_empty(tmp_path: Path) -> None:
    """A nonexistent extra_dir is silently ignored (Path.glob returns empty)."""
    missing = tmp_path / "does_not_exist"
    # Should not raise; glob on a non-existent path just yields nothing
    results = list_discovered_pipelines(extra_dirs=[missing])
    assert results == []


# ---------------------------------------------------------------------------
# Bundled discovery regression
# ---------------------------------------------------------------------------


def test_bundled_discovery_still_works() -> None:
    """list_pipelines() still discovers bundled pipelines after the extension."""
    infos = list_pipelines()
    assert len(infos) > 0
    for info in infos:
        assert info.name
        assert info.path.exists()


def test_bundled_discovery_not_contaminated_by_extra_dirs(tmp_path: Path) -> None:
    """list_pipelines() is unaffected by extra_dirs (it doesn't use them)."""
    (tmp_path / "intruder.py").write_text('"""Intruder."""\n')
    bundled = list_pipelines()
    names = [i.name for i in bundled]
    assert "intruder" not in names


def test_discovered_pipelines_not_include_bundled_by_default() -> None:
    """list_discovered_pipelines() with no args doesn't include bundled pipelines
    (unless the CWD happens to have a pipelines/ subdir — controlled environment
    so that's not expected in CI)."""
    # We can only assert the function returns a list without error.
    results = list_discovered_pipelines()
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# _extract_metadata (unit test, re-used by both paths)
# ---------------------------------------------------------------------------


def test_extract_metadata_no_docstring() -> None:
    source = "x = 1\n"
    short, long, env_vars, args = _extract_metadata(source)
    assert short == ""
    assert long == ""
    assert env_vars == []
    assert args == {}


def test_extract_metadata_with_docstring() -> None:
    source = '"""Short desc.\n\nLong desc."""\n'
    short, long, _, _ = _extract_metadata(source)
    assert short == "Short desc."
    assert "Long desc." in long
