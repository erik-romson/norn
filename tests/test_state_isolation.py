"""norn's own run state must never reach the target repository's git surface.

The failure this guards against: a run launched from inside the target repo
writes `<key>.runs/<n>/events.jsonl` for the whole run.  When a snapshot-diff
pipeline does not subtract it, step 01 commits the journal, the next journal
write dirties it again, and the `owned paths are clean` postcondition of the
step that just committed it fails — killing a multi-dollar run at stage 14.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from norn.state import (
    StateTrackedError,
    prepare_state_isolation,
    publish_state_key,
    register_git_exclude,
    state_exclude_lines,
    tracked_state_paths,
)


def _init_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "kept.txt").write_text("hi\n")
    subprocess.run(["git", "-C", str(path), "add", "kept.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    r = _init_repo(tmp_path / "repo")
    monkeypatch.chdir(r)
    monkeypatch.delenv("NORN_STATE_KEY", raising=False)
    return r


# --- publishing the state key ------------------------------------------------


def test_publish_state_key_exports_the_resolved_key(repo, monkeypatch):
    key = publish_state_key("implement_features_v2")

    assert key == "implement_features_v2"
    assert os.environ["NORN_STATE_KEY"] == key


def test_tui_publishes_the_state_key_before_importing_the_pipeline(tmp_path, monkeypatch):
    """The regression: `norn ui` imported the pipeline without publishing the key.

    Pipeline modules read NORN_STATE_KEY at import time, so the TUI's single
    import site must publish it first — exactly as `norn run` does.
    """
    from norn.tui import launch

    seen: dict[str, str | None] = {}

    def fake_load(ref):
        seen["at_import"] = os.environ.get("NORN_STATE_KEY")
        return object()

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NORN_STATE_KEY", raising=False)
    monkeypatch.setattr(launch, "load_bundled_pipeline", fake_load)
    # The loader reloads an already-imported bundled module; drop it so the
    # faked loader is the only thing that runs.
    monkeypatch.delitem(sys.modules, "norn.pipelines.implement_features_v2", raising=False)

    launch.load_pipeline_with_args("implement_features_v2", is_bundled=True, params={})

    assert seen["at_import"] == "implement_features_v2"


# --- git exclude lines -------------------------------------------------------


def test_exclude_lines_are_anchored_and_mark_the_runs_directory():
    lines = state_exclude_lines("/repo/pipe", "/repo")

    assert lines == ["/pipe.checkpoint", "/pipe.history", "/pipe.runs/"]


def test_exclude_lines_escape_gitignore_pattern_characters():
    lines = state_exclude_lines("/repo/pi*pe", "/repo")

    assert lines[0] == "/pi\\*pe.checkpoint"


def test_exclude_lines_are_empty_for_state_outside_the_repo():
    assert state_exclude_lines("/elsewhere/pipe", "/repo") == []


def test_register_git_exclude_makes_git_blind_to_the_run_state(repo):
    added = register_git_exclude("implement_features_v2", str(repo))

    assert added == [
        "/implement_features_v2.checkpoint",
        "/implement_features_v2.history",
        "/implement_features_v2.runs/",
    ]

    (repo / "implement_features_v2.runs" / "1").mkdir(parents=True)
    (repo / "implement_features_v2.runs" / "1" / "events.jsonl").write_text("{}\n")
    (repo / "implement_features_v2.checkpoint").write_text("{}\n")

    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "-uall"],
        capture_output=True,
        text=True,
    ).stdout
    assert status == ""


def test_register_git_exclude_is_idempotent(repo):
    first = register_git_exclude("implement_features_v2", str(repo))
    body = (repo / ".git" / "info" / "exclude").read_text()

    assert register_git_exclude("implement_features_v2", str(repo)) == []
    assert (repo / ".git" / "info" / "exclude").read_text() == body
    assert body.count("/implement_features_v2.runs/") == 1
    assert first


def test_register_git_exclude_keeps_existing_content(repo):
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("*.tmp")  # no trailing newline on purpose

    register_git_exclude("implement_features_v2", str(repo))

    lines = exclude.read_text().splitlines()
    assert lines[0] == "*.tmp"
    assert "/implement_features_v2.runs/" in lines


# --- already-tracked state ---------------------------------------------------


def test_tracked_state_paths_finds_a_committed_journal(repo):
    journal = repo / "implement_features_v2.runs" / "1"
    journal.mkdir(parents=True)
    (journal / "events.jsonl").write_text("{}\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "oops"], check=True)

    assert tracked_state_paths("implement_features_v2", str(repo)) == [
        "implement_features_v2.runs/1/events.jsonl"
    ]


def test_tracked_state_paths_is_empty_on_a_clean_repo(repo):
    assert tracked_state_paths("implement_features_v2", str(repo)) == []


def test_prepare_state_isolation_refuses_to_start_on_tracked_state(repo):
    journal = repo / "implement_features_v2.runs" / "1"
    journal.mkdir(parents=True)
    (journal / "events.jsonl").write_text("{}\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "oops"], check=True)

    with pytest.raises(StateTrackedError) as excinfo:
        prepare_state_isolation("implement_features_v2")

    message = str(excinfo.value)
    assert "implement_features_v2.runs/1/events.jsonl" in message
    assert "git rm -r --cached implement_features_v2.runs" in message


def test_prepare_state_isolation_registers_the_exclude_and_repairs_the_key(repo):
    prepare_state_isolation("implement_features_v2")

    assert os.environ["NORN_STATE_KEY"] == "implement_features_v2"
    body = (repo / ".git" / "info" / "exclude").read_text()
    assert "/implement_features_v2.runs/" in body


def test_prepare_state_isolation_outside_a_git_repo_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NORN_STATE_KEY", raising=False)

    prepare_state_isolation("pipe")

    assert os.environ["NORN_STATE_KEY"] == "pipe"


# --- the runner applies it for every entry point -----------------------------


def test_run_pipeline_prepares_state_isolation(repo, monkeypatch):
    """Any entry point that reaches the runner with a state key gets the guard."""
    import asyncio

    from norn.dsl import Pipeline
    from norn.runner import run_pipeline

    pipeline = Pipeline(name="p")
    monkeypatch.setattr(sys, "argv", ["norn"])
    asyncio.run(run_pipeline(pipeline, config_path="implement_features_v2"))

    body = (repo / ".git" / "info" / "exclude").read_text()
    assert "/implement_features_v2.runs/" in body
