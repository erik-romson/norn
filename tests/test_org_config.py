from __future__ import annotations

import os
import textwrap

import pytest

from norn.dsl import Pipeline
from norn.loader import _get_config_dir, find_org_for_project, list_orgs, load_org_config


@pytest.fixture
def org_config_dir(tmp_path, monkeypatch):
    """Set up a temporary config dir with an orgs/ subdirectory."""
    config_dir = tmp_path / "issueprocessing"
    orgs_dir = config_dir / "orgs"
    orgs_dir.mkdir(parents=True)
    monkeypatch.setenv("ISSUEPROC_CONFIG_DIR", str(config_dir))
    return orgs_dir


def _write_org(orgs_dir, name: str, project_keys: list[str] | None = None) -> None:
    keys_line = ""
    if project_keys:
        keys_repr = ", ".join(f'"{k}"' for k in project_keys)
        keys_line = f".projects({keys_repr})"
    content = textwrap.dedent(f"""\
        from norn.dsl import Pipeline
        from norn.stages.run_command import RunCommand
        from norn.dsl import Stage

        config = Pipeline("{name}"){keys_line}
    """)
    (orgs_dir / f"{name}.py").write_text(content)


# ---------------------------------------------------------------------------
# list_orgs
# ---------------------------------------------------------------------------


def test_list_orgs_empty(org_config_dir):
    assert list_orgs() == []


def test_list_orgs_returns_names(org_config_dir):
    _write_org(org_config_dir, "acme")
    _write_org(org_config_dir, "beta")
    assert list_orgs() == ["acme", "beta"]


def test_list_orgs_sorted(org_config_dir):
    _write_org(org_config_dir, "zeta")
    _write_org(org_config_dir, "alpha")
    result = list_orgs()
    assert result == sorted(result)


def test_list_orgs_no_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ISSUEPROC_CONFIG_DIR", str(tmp_path / "nonexistent"))
    assert list_orgs() == []


# ---------------------------------------------------------------------------
# load_org_config
# ---------------------------------------------------------------------------


def test_load_org_config_returns_pipeline(org_config_dir):
    _write_org(org_config_dir, "myorg")
    pipeline = load_org_config("myorg")
    assert isinstance(pipeline, Pipeline)
    assert pipeline.name == "myorg"


def test_load_org_config_not_found_raises(org_config_dir):
    with pytest.raises(FileNotFoundError, match="missing"):
        load_org_config("missing")


# ---------------------------------------------------------------------------
# find_org_for_project
# ---------------------------------------------------------------------------


def test_find_org_for_project_returns_match(org_config_dir):
    _write_org(org_config_dir, "acme", project_keys=["ACME", "PROJ"])
    org_name, pipeline = find_org_for_project("ACME")
    assert org_name == "acme"
    assert isinstance(pipeline, Pipeline)


def test_find_org_for_project_not_found_raises(org_config_dir):
    _write_org(org_config_dir, "acme", project_keys=["ACME"])
    with pytest.raises(ValueError, match="UNKNOWN"):
        find_org_for_project("UNKNOWN")


def test_find_org_for_project_no_orgs_dir_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ISSUEPROC_CONFIG_DIR", str(tmp_path / "nonexistent"))
    with pytest.raises(FileNotFoundError, match="No orgs directory"):
        find_org_for_project("PROJ")


def test_find_org_for_project_multiple_keys(org_config_dir):
    _write_org(org_config_dir, "acme", project_keys=["ACME", "ALPHA", "BETA"])
    org_name, pipeline = find_org_for_project("BETA")
    assert org_name == "acme"


# ---------------------------------------------------------------------------
# _get_config_dir — NORN_CONFIG_DIR / legacy / fallback
# ---------------------------------------------------------------------------


def test_norn_config_dir_env_takes_priority(tmp_path, monkeypatch):
    """NORN_CONFIG_DIR takes priority over ISSUEPROC_CONFIG_DIR."""
    norn_dir = tmp_path / "norn"
    legacy_dir = tmp_path / "legacy"
    norn_dir.mkdir()
    legacy_dir.mkdir()
    monkeypatch.setenv("NORN_CONFIG_DIR", str(norn_dir))
    monkeypatch.setenv("ISSUEPROC_CONFIG_DIR", str(legacy_dir))
    assert _get_config_dir() == norn_dir


def test_issueproc_config_dir_still_works(tmp_path, monkeypatch):
    """Legacy ISSUEPROC_CONFIG_DIR is honoured when NORN_CONFIG_DIR is unset."""
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    monkeypatch.delenv("NORN_CONFIG_DIR", raising=False)
    monkeypatch.setenv("ISSUEPROC_CONFIG_DIR", str(legacy_dir))
    assert _get_config_dir() == legacy_dir


def test_old_dir_used_when_new_dir_absent(tmp_path, monkeypatch):
    """~/.issueprocessing is used if ~/.norn does not exist."""
    monkeypatch.delenv("NORN_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ISSUEPROC_CONFIG_DIR", raising=False)
    old_dir = tmp_path / ".issueprocessing"
    old_dir.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert _get_config_dir() == old_dir


def test_new_dir_preferred_when_both_exist(tmp_path, monkeypatch):
    """~/.norn wins when both ~/.norn and ~/.issueprocessing exist."""
    monkeypatch.delenv("NORN_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ISSUEPROC_CONFIG_DIR", raising=False)
    new_dir = tmp_path / ".norn"
    old_dir = tmp_path / ".issueprocessing"
    new_dir.mkdir()
    old_dir.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert _get_config_dir() == new_dir


def test_default_is_norn_when_neither_exists(tmp_path, monkeypatch):
    """Defaults to ~/.norn when no env vars and no dirs exist."""
    monkeypatch.delenv("NORN_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ISSUEPROC_CONFIG_DIR", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert _get_config_dir() == tmp_path / ".norn"
