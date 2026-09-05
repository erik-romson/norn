"""Tests for norn/pipelines/_jira_fetch.sh.

All tests use a curl shim (tests/fixtures/fix_jira_issue/bin/curl) so no
network calls are made.  The shim maps fixture URLs to local JSON files and
returns dummy bytes for attachment downloads; it exits 22 for unknown URLs
(the same exit code curl --fail uses for HTTP 4xx/5xx errors).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "norn" / "pipelines" / "_jira_fetch.sh"
FIXTURES = Path(__file__).parent / "fixtures" / "fix_jira_issue"
BIN_DIR = FIXTURES / "bin"

JIRA_BASE = "https://example.invalid/rest/api/3/"
JIRA_AUTH = "test@example.com:fake-api-token"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _run(
    key: str,
    out_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run _jira_fetch.sh with the curl shim on PATH."""
    env = {
        **os.environ,
        "PATH": str(BIN_DIR) + ":" + os.environ.get("PATH", ""),
        "JIRA_BASE": JIRA_BASE,
        "JIRA_AUTH": JIRA_AUTH,
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), key, str(out_dir)],
        capture_output=True,
        text=True,
        env=env,
    )


# ---------------------------------------------------------------------------
# Shared fixture: run CBS-2249 once and reuse the result across ADF tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cbs2249_md(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Return the rendered issue.md text for CBS-2249."""
    out = tmp_path_factory.mktemp("cbs2249")
    result = _run("CBS-2249", out)
    assert result.returncode == 0, (
        f"_jira_fetch.sh exited {result.returncode}\n"
        f"stderr: {result.stderr}"
    )
    return (out / "issue.md").read_text()


@pytest.fixture(scope="module")
def cbs2249_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return the output directory for CBS-2249 (for attachment checks)."""
    out = tmp_path_factory.mktemp("cbs2249_dir")
    result = _run("CBS-2249", out)
    assert result.returncode == 0, (
        f"_jira_fetch.sh exited {result.returncode}\n"
        f"stderr: {result.stderr}"
    )
    return out


# ---------------------------------------------------------------------------
# ADF node rendering
# ---------------------------------------------------------------------------


def test_paragraph_renders(cbs2249_md: str) -> None:
    assert "A paragraph.\n" in cbs2249_md


def test_heading_renders(cbs2249_md: str) -> None:
    assert "A Heading\n" in cbs2249_md


def test_bullet_list_items_render(cbs2249_md: str) -> None:
    assert "Item 1\n" in cbs2249_md
    assert "Item 2\n" in cbs2249_md


def test_table_cells_tab_separated(cbs2249_md: str) -> None:
    # Header row and data row must be tab-separated, each ending with \n
    assert "Col1\tCol2\n" in cbs2249_md
    assert "A\tB\n" in cbs2249_md


def test_code_block_renders(cbs2249_md: str) -> None:
    assert "print('hello')\n" in cbs2249_md


def test_mention_renders_at_display_name(cbs2249_md: str) -> None:
    assert "@Alice" in cbs2249_md


def test_media_renders_image_tag(cbs2249_md: str) -> None:
    assert "[image: screenshot]" in cbs2249_md


def test_hard_break_renders_as_newline(cbs2249_md: str) -> None:
    # paragraph: text("Before") hardBreak text("After") -> "Before\nAfter\n"
    assert "Before\nAfter\n" in cbs2249_md


def test_inline_card_renders_url(cbs2249_md: str) -> None:
    assert "https://example.com/issue" in cbs2249_md


# ---------------------------------------------------------------------------
# Attachment handling
# ---------------------------------------------------------------------------


def test_attachments_saved_with_id_prefix(cbs2249_dir: Path) -> None:
    assert (cbs2249_dir / "attachments" / "att001_screenshot.png").exists()
    assert (cbs2249_dir / "attachments" / "att002_debug.log").exists()


# ---------------------------------------------------------------------------
# Zero-attachment case
# ---------------------------------------------------------------------------


def test_zero_attachments_still_writes_issue_md(tmp_path: Path) -> None:
    result = _run("CBS-2250", tmp_path)
    assert result.returncode == 0, (
        f"_jira_fetch.sh exited {result.returncode}\n"
        f"stderr: {result.stderr}"
    )
    assert (tmp_path / "issue.md").exists()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unset_jira_auth_exits_nonzero(tmp_path: Path) -> None:
    """Script must fail fast without touching the network when JIRA_AUTH is absent."""
    env = {
        **os.environ,
        "PATH": str(BIN_DIR) + ":" + os.environ.get("PATH", ""),
        "JIRA_BASE": JIRA_BASE,
    }
    env.pop("JIRA_AUTH", None)  # ensure completely absent
    result = subprocess.run(
        ["bash", str(SCRIPT), "CBS-2249", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0
    # No network activity => no issue.json downloaded, no issue.md written
    assert not (tmp_path / "issue.md").exists()
    assert not (tmp_path / "issue.json").exists()


def test_404_exits_nonzero_and_leaves_no_issue_md(tmp_path: Path) -> None:
    """Curl failure (shim exits 22) must propagate; issue.md must not be created."""
    # CBS-9999 has no fixture file, so the shim exits 22
    result = _run("CBS-9999", tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "issue.md").exists()
