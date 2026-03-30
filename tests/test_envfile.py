"""Tests for norn/envfile.py — env file parsing and loading."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from norn.envfile import _parse_env_file, apply_env_files


class TestParseEnvFile:
    """Tests for _parse_env_file."""

    def test_key_value(self, tmp_path: Path) -> None:
        f = tmp_path / "env"
        f.write_text("FOO=bar\nBAZ=qux\n")
        assert _parse_env_file(f) == {"FOO": "bar", "BAZ": "qux"}

    def test_comments_and_blank_lines(self, tmp_path: Path) -> None:
        f = tmp_path / "env"
        f.write_text("# comment\n\nKEY=val\n  # indented comment\n")
        assert _parse_env_file(f) == {"KEY": "val"}

    def test_double_quoted_value(self, tmp_path: Path) -> None:
        f = tmp_path / "env"
        f.write_text('KEY="hello world"\n')
        assert _parse_env_file(f) == {"KEY": "hello world"}

    def test_single_quoted_value(self, tmp_path: Path) -> None:
        f = tmp_path / "env"
        f.write_text("KEY='hello world'\n")
        assert _parse_env_file(f) == {"KEY": "hello world"}

    def test_value_with_equals(self, tmp_path: Path) -> None:
        f = tmp_path / "env"
        f.write_text("KEY=a=b=c\n")
        assert _parse_env_file(f) == {"KEY": "a=b=c"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _parse_env_file(tmp_path / "nonexistent") == {}

    def test_line_without_equals_skipped(self, tmp_path: Path) -> None:
        f = tmp_path / "env"
        f.write_text("NOPE\nKEY=val\n")
        assert _parse_env_file(f) == {"KEY": "val"}

    def test_empty_value(self, tmp_path: Path) -> None:
        f = tmp_path / "env"
        f.write_text("KEY=\n")
        assert _parse_env_file(f) == {"KEY": ""}


class TestApplyEnvFiles:
    """Tests for apply_env_files."""

    def test_project_overrides_global(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        global_env = tmp_path / "global" / "env"
        global_env.parent.mkdir()
        global_env.write_text("COLOR=red\nSIZE=large\n")

        project_env = tmp_path / ".norn.env"
        project_env.write_text("COLOR=blue\n")

        # Remove the keys so setdefault can apply
        monkeypatch.delenv("COLOR", raising=False)
        monkeypatch.delenv("SIZE", raising=False)

        with patch("norn.envfile.GLOBAL_ENV_FILE", global_env), \
             patch("norn.envfile.PROJECT_ENV_FILE", project_env):
            apply_env_files()

        assert os.environ["COLOR"] == "blue"
        assert os.environ["SIZE"] == "large"

        # Cleanup
        monkeypatch.delenv("COLOR", raising=False)
        monkeypatch.delenv("SIZE", raising=False)

    def test_explicit_env_not_overwritten(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / "env"
        env_file.write_text("MY_VAR=from_file\n")

        monkeypatch.setenv("MY_VAR", "explicit")

        with patch("norn.envfile.GLOBAL_ENV_FILE", env_file), \
             patch("norn.envfile.PROJECT_ENV_FILE", tmp_path / "nonexistent"):
            apply_env_files()

        assert os.environ["MY_VAR"] == "explicit"

    def test_missing_files_silently_skipped(self, tmp_path: Path) -> None:
        with patch("norn.envfile.GLOBAL_ENV_FILE", tmp_path / "nope1"), \
             patch("norn.envfile.PROJECT_ENV_FILE", tmp_path / "nope2"):
            apply_env_files()  # should not raise
