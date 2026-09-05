from unittest.mock import AsyncMock, patch

import pytest

from norn.cli import _assert_provider_compatible, _expand_file_refs, _primary_state_key, _state_key_candidates, main
from norn.responder import NonInteractiveResponder
from norn.checkpoint import Checkpoint, save_checkpoint
from norn.history import RunRecord, StageHistoryEntry, append_run
from norn.models import StageLogEntry


def test_expand_single_file_ref(tmp_path):
    f = tmp_path / "spec.txt"
    f.write_text("hello world")
    result = _expand_file_refs(f"Based on @{f}")
    assert result == "Based on hello world"


def test_expand_multiple_file_refs(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("AAA")
    b.write_text("BBB")
    result = _expand_file_refs(f"@{a} and @{b}")
    assert result == "AAA and BBB"


def test_no_file_refs_unchanged():
    result = _expand_file_refs("just a normal string with an email@address")
    assert result == "just a normal string with an email@address"


def test_expand_preserves_surrounding_text(tmp_path):
    f = tmp_path / "data.md"
    f.write_text("contents")
    result = _expand_file_refs(f"before @{f} after")
    assert result == "before contents after"


def test_expand_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        _expand_file_refs("@nonexistent/file.txt")


def test_expand_relative_path():
    result = _expand_file_refs("See @examples/spec.txt for details")
    assert "Greeter" in result
    assert "@examples/spec.txt" not in result


# --- list subcommand ---


def test_continue_help_says_agent_not_claude(capsys):
    """--continue help text should not say 'Claude remembers'; it should be provider-neutral."""
    import argparse
    # Trigger argparse to print help text and capture it
    with pytest.raises(SystemExit):
        with patch("sys.argv", ["norn", "run", "--help"]):
            main()
    captured = capsys.readouterr()
    assert "Claude remembers" not in captured.out
    assert "agent remembers" in captured.out


def test_list_shows_bundled_pipelines(capsys):
    with patch("sys.argv", ["norn", "list"]):
        main()
    captured = capsys.readouterr()
    assert "hello" in captured.out
    assert "vanilla_change" in captured.out
    assert "implement_features" in captured.out


# --- describe subcommand ---


def test_describe_shows_pipeline_details(capsys):
    with patch("sys.argv", ["norn", "describe", "hello"]):
        main()
    captured = capsys.readouterr()
    assert "hello" in captured.out


def test_describe_unknown_pipeline_exits():
    with patch("sys.argv", ["norn", "describe", "nonexistent_pipeline"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


# --- run resolves bundled pipeline names ---


def test_run_resolves_bundled_pipeline_name():
    """Verify that `norn run hello --dry-run` resolves the bundled pipeline."""
    with patch("sys.argv", ["norn", "run", "hello", "--dry-run"]):
        # dry-run prints structure and returns without executing
        main()


def test_run_unknown_file_exits():
    with patch("sys.argv", ["norn", "run", "no_such_file.py"]):
        with pytest.raises(SystemExit):
            main()


def test_external_file_state_key_uses_cwd(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    shared = tmp_path / "shared"
    workspace.mkdir()
    shared.mkdir()
    external = shared / "pipeline.py"
    external.write_text("config = None\n")

    monkeypatch.chdir(workspace)

    assert _state_key_candidates(str(external)) == [
        str((workspace / "pipeline.py").resolve()),
        str(external.resolve()),
    ]
    assert _primary_state_key(str(external)) == str((workspace / "pipeline.py").resolve())


def test_internal_file_state_key_stays_beside_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    internal = workspace / "pipeline.py"
    internal.write_text("config = None\n")

    monkeypatch.chdir(workspace)

    assert _state_key_candidates(str(internal)) == [str(internal.resolve())]
    assert _primary_state_key(str(internal)) == str(internal.resolve())


def test_history_run_shows_detailed_step_log(capsys, tmp_path):
    config = str(tmp_path / "pipeline.py")
    append_run(
        config,
        RunRecord(
            run_id=1,
            timestamp="2026-03-25T09:00:00+00:00",
            success=True,
            total_cost_usd=0.19,
            total_tokens=15100,
            duration_ms=10500,
            stages=[StageHistoryEntry(name="gen", success=True, cost_usd=0.12)],
            retries=0,
            session_id="abc123",
            stage_log=[
                StageLogEntry(
                    name="gen",
                    status="passed",
                    success=True,
                    attempt=1,
                    duration_ms=4200,
                    cost_usd=0.12,
                    running_total_cost_usd=0.12,
                    running_total_tokens=15100,
                    input_tokens=10000,
                    output_tokens=5100,
                    duration_api_ms=3900,
                    num_turns=3,
                    model="sonnet",
                    session_id="abc123",
                )
            ],
        ),
    )

    with patch("sys.argv", ["norn", "history", config, "--run", "1"]):
        main()

    captured = capsys.readouterr()
    assert "Step Log" in captured.out
    assert "gen" in captured.out
    assert "$0.1200" in captured.out
    assert "sonnet" in captured.out


def test_history_for_external_config_prefers_cwd_state(capsys, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    shared = tmp_path / "shared"
    workspace.mkdir()
    shared.mkdir()
    external = shared / "pipeline.py"
    external.write_text("config = None\n")

    monkeypatch.chdir(workspace)

    append_run(
        str(workspace / "pipeline.py"),
        RunRecord(
            run_id=1,
            timestamp="2026-03-25T09:00:00+00:00",
            success=True,
            total_cost_usd=0.19,
            total_tokens=15100,
            duration_ms=10500,
            stages=[StageHistoryEntry(name="gen", success=True, cost_usd=0.12)],
            retries=0,
        ),
    )

    with patch("sys.argv", ["norn", "history", str(external)]):
        main()

    captured = capsys.readouterr()
    assert "#1" in captured.out


# --- provider compatibility checks ---


def test_assert_provider_compatible_raises_on_mismatch():
    """_assert_provider_compatible exits when providers differ."""
    cp = Checkpoint(
        pipeline="test",
        timestamp="2026-01-01T00:00:00Z",
        session_id=None,
        completed_stages=[],
        results={},
        usage=[],
        agent_provider="opencode",
    )
    with pytest.raises(SystemExit) as exc_info:
        _assert_provider_compatible("claude-code", cp, "resume")
    assert exc_info.value.code == 1


def test_assert_provider_compatible_passes_on_match():
    """_assert_provider_compatible does not exit when providers match."""
    cp = Checkpoint(
        pipeline="test",
        timestamp="2026-01-01T00:00:00Z",
        session_id=None,
        completed_stages=[],
        results={},
        usage=[],
        agent_provider="claude-code",
    )
    # Must not raise
    _assert_provider_compatible("claude-code", cp, "resume")


# --- --non-interactive flag ---


def test_non_interactive_passes_responder_to_run_pipeline():
    """--non-interactive wires a NonInteractiveResponder into run_pipeline."""
    mock_run = AsyncMock(return_value=None)
    with patch("norn.cli.run_pipeline", mock_run):
        with patch("sys.argv", ["norn", "run", "hello", "--non-interactive"]):
            main()

    assert mock_run.called
    call_kwargs = mock_run.call_args.kwargs
    assert "input_responder" in call_kwargs
    assert isinstance(call_kwargs["input_responder"], NonInteractiveResponder)


def test_step_and_non_interactive_are_mutually_exclusive():
    """--step --non-interactive must exit non-zero."""
    with patch("sys.argv", ["norn", "run", "hello", "--step", "--non-interactive"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
    assert exc_info.value.code != 0


def test_resume_fails_with_mismatched_provider(tmp_path):
    """--resume exits with error when the selected provider differs from checkpoint."""
    config = tmp_path / "pipeline.py"
    config.write_text(
        "from norn.dsl import Pipeline\nconfig = Pipeline('test')\n"
    )
    save_checkpoint(str(config), "test", None, [], {}, agent_provider="opencode")

    with patch("sys.argv", ["norn", "run", str(config), "--resume", "--agent-provider", "claude-code"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


def test_continue_fails_with_mismatched_provider(tmp_path):
    """--continue exits with error when the selected provider differs from checkpoint."""
    config = tmp_path / "pipeline.py"
    config.write_text(
        "from norn.dsl import Pipeline\nconfig = Pipeline('test')\n"
    )
    save_checkpoint(str(config), "test", "sess-abc", [], {}, agent_provider="opencode")

    with patch("sys.argv", ["norn", "run", str(config), "--continue", "--agent-provider", "claude-code"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


def test_resume_allows_matching_provider(tmp_path):
    """--resume proceeds normally when the selected provider matches the checkpoint."""
    config = tmp_path / "pipeline.py"
    config.write_text(
        "from norn.dsl import Pipeline\nconfig = Pipeline('test')\n"
    )
    save_checkpoint(str(config), "test", None, [], {}, agent_provider="claude-code")

    # No stages to run; should complete without raising
    with patch("sys.argv", ["norn", "run", str(config), "--resume"]):
        main()


def test_continue_allows_matching_provider(tmp_path):
    """--continue proceeds normally when the selected provider matches the checkpoint."""
    config = tmp_path / "pipeline.py"
    config.write_text(
        "from norn.dsl import Pipeline\nconfig = Pipeline('test')\n"
    )
    save_checkpoint(str(config), "test", None, [], {}, agent_provider="claude-code")

    # No session_id in checkpoint -> "No saved session found" path, but no error
    with patch("sys.argv", ["norn", "run", str(config), "--continue"]):
        main()


def test_run_external_config_writes_state_in_cwd(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    shared = tmp_path / "shared"
    workspace.mkdir()
    shared.mkdir()
    external = shared / "pipeline.py"
    external.write_text(
        "from norn.dsl import Pipeline\n"
        "from norn.stages.run_command import RunCommand\n"
        "config = Pipeline('test').stage('step1', RunCommand(cmd='echo hello'))\n"
    )

    monkeypatch.chdir(workspace)

    with patch("sys.argv", ["norn", "run", str(external)]):
        main()

    assert (workspace / "pipeline.history").exists()
    assert (workspace / "pipeline.checkpoint").exists()
    assert not (shared / "pipeline.history").exists()
