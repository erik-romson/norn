"""Tests for norn.pipelines._test_cmd.detect_test_cmd."""
from __future__ import annotations

from norn.pipelines._test_cmd import detect_test_cmd


def test_pom_xml(tmp_path) -> None:
    (tmp_path / "pom.xml").write_text("<project/>")
    assert detect_test_cmd(str(tmp_path)) == "mvn -q verify"


def test_gradlew(tmp_path) -> None:
    (tmp_path / "gradlew").write_text("#!/bin/sh")
    assert detect_test_cmd(str(tmp_path)) == "./gradlew test"


def test_package_json(tmp_path) -> None:
    (tmp_path / "package.json").write_text("{}")
    assert detect_test_cmd(str(tmp_path)) == "npm test"


def test_cargo_toml(tmp_path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]")
    assert detect_test_cmd(str(tmp_path)) == "cargo test"


def test_go_mod(tmp_path) -> None:
    (tmp_path / "go.mod").write_text("module x")
    assert detect_test_cmd(str(tmp_path)) == "go test ./..."


def test_tests_dir_wins_over_pyproject(tmp_path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]")
    assert detect_test_cmd(str(tmp_path)) == "uv run python -m pytest tests/ -v"


def test_pyproject_toml_only(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]")
    assert detect_test_cmd(str(tmp_path)) == "uv run python -m pytest -v"


def test_first_match_wins_pom_beats_package_json(tmp_path) -> None:
    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "package.json").write_text("{}")
    assert detect_test_cmd(str(tmp_path)) == "mvn -q verify"


def test_empty_directory_returns_true(tmp_path) -> None:
    assert detect_test_cmd(str(tmp_path)) == "true"
