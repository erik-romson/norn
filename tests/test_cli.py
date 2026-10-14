import os
import sys
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
    assert "hello_v1" in captured.out
    assert "vanilla_change_v1" in captured.out
    assert "implement_features_v1" in captured.out


# --- describe subcommand ---


def test_describe_shows_pipeline_details(capsys):
    with patch("sys.argv", ["norn", "describe", "hello_v1"]):
        main()
    captured = capsys.readouterr()
    assert "hello_v1" in captured.out


def test_describe_unknown_pipeline_exits():
    with patch("sys.argv", ["norn", "describe", "nonexistent_pipeline"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


# --- run resolves bundled pipeline names ---


def test_run_resolves_bundled_pipeline_name():
    """Verify that `norn run hello --dry-run` resolves the bundled pipeline."""
    with patch("sys.argv", ["norn", "run", "hello_v1", "--dry-run"]):
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


# ---------------------------------------------------------------------------
# Subtracting norn's own run state from a worktree diff
#
# norn writes `<key>.runs/<n>/events.jsonl`, `<key>.checkpoint` and
# `<key>.history` into the launch directory and keeps writing to them for the
# whole run. A pipeline that computes step ownership by diffing the worktree
# has to subtract them, or it commits its own journal and then the next journal
# write dirties the commit that just went in.
# ---------------------------------------------------------------------------


def test_state_artifact_paths_are_relative_to_the_repo_root():
    from norn.state import state_artifact_paths

    exact, prefixes = state_artifact_paths("/repo/pipe", "/repo")
    assert exact == ["pipe.checkpoint", "pipe.history"]
    assert prefixes == ["pipe.runs"]


def test_state_artifact_paths_drops_state_outside_the_repo():
    """State living elsewhere can never show up in git status — ignoring it is noise."""
    from norn.state import state_artifact_paths

    assert state_artifact_paths("/elsewhere/pipe", "/repo") == ([], [])


def test_state_artifact_paths_without_a_state_key():
    from norn.state import state_artifact_paths

    assert state_artifact_paths(None, "/repo") == ([], [])
    assert state_artifact_paths("", "/repo") == ([], [])


def test_state_artifact_paths_match_the_writers(tmp_path):
    """The three paths must be exactly what checkpoint / history / journal write."""
    from norn.checkpoint import checkpoint_file
    from norn.history import history_file
    from norn.journal import runs_dir
    from norn.state import state_artifact_paths

    key = str(tmp_path / "pipe")
    exact, prefixes = state_artifact_paths(key, str(tmp_path))
    written = {checkpoint_file(key).name, history_file(key).name, runs_dir(key).name}
    assert set(exact) | set(prefixes) == written


def test_state_ignore_flags_render_for_both_snapshot_helpers():
    from norn.state import state_ignore_flags

    flags = state_ignore_flags("/repo/pipe", "/repo")
    assert flags == (
        " --ignore pipe.checkpoint --ignore pipe.history --ignore-prefix pipe.runs"
    )


def test_state_ignore_flags_are_empty_without_a_state_key():
    """Importing a pipeline outside a run must not change the shell it builds."""
    from norn.state import state_ignore_flags

    assert state_ignore_flags(None, "/repo") == ""


def test_run_exports_the_state_key_before_the_pipeline_is_imported(tmp_path, monkeypatch):
    """The pipeline reads NORN_STATE_KEY at import time, so `run` must set it first."""
    seen: dict[str, str | None] = {}

    def fake_load(config_arg):
        seen["at_import"] = os.environ.get("NORN_STATE_KEY")
        raise SystemExit(0)

    (tmp_path / "pipeline.py").write_text("config = None\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NORN_STATE_KEY", raising=False)
    monkeypatch.setattr("norn.cli._load_pipeline", fake_load)
    monkeypatch.setattr(sys, "argv", ["norn", "run", "pipeline.py"])

    with pytest.raises(SystemExit):
        main()

    assert seen["at_import"] == str((tmp_path / "pipeline.py").resolve())


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


def test_history_run_shows_finding(capsys, tmp_path):
    """history --run prints findings when journal metrics exist."""
    import json

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
        ),
    )

    # Create a journal run dir with findings.json. run.json records which
    # history run it belongs to — the two counters are independent.
    runs_path = tmp_path / "pipeline.runs" / "1"
    runs_path.mkdir(parents=True)
    (runs_path / "run.json").write_text(json.dumps({"history_run_id": 1}))
    (runs_path / "findings.json").write_text(json.dumps([{
        "rule": "budget-headroom",
        "severity": "warn",
        "stage": None,
        "evidence": "budget_used_pct=90%",
        "suggestion": "Increase the budget.",
    }]))

    with patch("sys.argv", ["norn", "history", config, "--run", "1"]):
        main()

    captured = capsys.readouterr()
    assert "budget-headroom" in captured.out


def test_history_compare_shows_per_stage_table(capsys, tmp_path):
    """history --compare prints a per-stage table when both runs have metrics."""
    import json
    from dataclasses import asdict
    from norn.metrics import RunMetrics, StageMetrics

    config = str(tmp_path / "pipeline.py")
    for rid in (1, 2):
        append_run(
            config,
            RunRecord(
                run_id=rid,
                timestamp="2026-03-25T09:00:00+00:00",
                success=True,
                total_cost_usd=0.10 * rid,
                total_tokens=10000,
                duration_ms=5000,
                stages=[StageHistoryEntry(name="gen", success=True, cost_usd=0.05 * rid)],
                retries=0,
            ),
        )
        # Create metrics.json in the journal run dir
        runs_path = tmp_path / "pipeline.runs" / str(rid)
        runs_path.mkdir(parents=True)
        (runs_path / "run.json").write_text(json.dumps({"history_run_id": rid}))
        sm = StageMetrics(
            name="gen", node_id="gen", attempt=1, kind="agent", status="passed",
            success=True, cost_usd=0.05 * rid, cost_share=100.0,
            billed_input_tokens=1000, output_tokens=500, cache_hit_ratio=0.0,
            turns=3, tokens_per_turn=500.0, tool_uses=2, tool_errors=0,
            prompt_chars=100, system_prompt_chars=0, unresolved_placeholders=[],
            duration_ms=2000, model="sonnet", model_id="claude-sonnet-4-6",
            result_subtype="success", stop_reason=None, nonfatal_error=False,
        )
        metrics = RunMetrics(
            run_id=str(rid), pipeline="test", provider="claude-code",
            cost_mode="tracked", success=True, failed_stage=None,
            cost_usd=0.10 * rid, billed_input_tokens=2000, output_tokens=500,
            cache_read_tokens=0, cache_hit_ratio=0.0, turns=3,
            wall_ms=5000, api_ms=3000, tool_time_ms=2000,
            agent_stage_count=1, command_stage_count=0,
            failed_attempts=0, failed_attempt_cost_usd=0.0, timed_out_attempts=0, retries=0,
            budget_max_cost_usd=None, budget_used_pct=None,
            waiting_input={}, top_cost_stages=["gen"],
            quota=None, codex_cost_usd_estimate=None, stages=[sm],
        )
        (runs_path / "metrics.json").write_text(
            json.dumps(asdict(metrics), indent=2)
        )

    with patch("sys.argv", ["norn", "history", config, "--compare", "1", "2"]):
        main()

    captured = capsys.readouterr()
    assert "Per-stage" in captured.out
    assert "gen" in captured.out


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
        with patch("sys.argv", ["norn", "run", "hello_v1", "--non-interactive"]):
            main()

    assert mock_run.called
    call_kwargs = mock_run.call_args.kwargs
    assert "input_responder" in call_kwargs
    assert isinstance(call_kwargs["input_responder"], NonInteractiveResponder)


# --- --budget / --token-budget flags ---


def test_budget_flags_override_the_pipeline_limit():
    """--budget/--token-budget raise the loaded pipeline's limits before the run."""
    mock_run = AsyncMock(return_value=None)
    with patch("norn.cli.run_pipeline", mock_run):
        with patch(
            "sys.argv",
            ["norn", "run", "hello_v1", "--budget", "250", "--token-budget", "4000000"],
        ):
            main()

    pipeline = mock_run.call_args.args[0]
    assert pipeline.budgets[0].max_cost_usd == 250.0
    assert pipeline.budgets[0].max_tokens == 4_000_000


def test_without_budget_flags_the_pipeline_keeps_its_own_limits():
    """No flags means no budget is invented for a pipeline that declares none."""
    import importlib

    import norn.pipelines.hello_v1

    # A bundled pipeline object is module-level and lives as long as the
    # process; reload it so an earlier test's override cannot leak in here.
    importlib.reload(norn.pipelines.hello_v1)
    mock_run = AsyncMock(return_value=None)
    with patch("norn.cli.run_pipeline", mock_run):
        with patch("sys.argv", ["norn", "run", "hello_v1"]):
            main()

    assert mock_run.call_args.args[0].budgets == []


def test_step_and_non_interactive_are_mutually_exclusive():
    """--step --non-interactive must exit non-zero."""
    with patch("sys.argv", ["norn", "run", "hello_v1", "--step", "--non-interactive"]):
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


# ---------------------------------------------------------------------------
# norn models subcommand
# ---------------------------------------------------------------------------


def test_models_subcommand_shows_all_intent_labels(capsys):
    """norn models prints every intent label (light, standard, deep, review)."""
    with patch("sys.argv", ["norn", "models"]):
        main()
    captured = capsys.readouterr()
    for label in ("light", "standard", "deep"):
        assert label in captured.out, f"Expected label {label!r} in norn models output"
    assert "review" in captured.out, "Expected 'review' (codex) label in norn models output"


def test_models_subcommand_shows_providers(capsys):
    """norn models table includes all three provider names."""
    with patch("sys.argv", ["norn", "models"]):
        main()
    captured = capsys.readouterr()
    for provider in ("claude-code", "codex", "opencode"):
        assert provider in captured.out, f"Expected provider {provider!r} in norn models output"


def test_models_subcommand_shows_file_path(capsys):
    """norn models prints the path to model_labels.toml."""
    with patch("sys.argv", ["norn", "models"]):
        main()
    captured = capsys.readouterr()
    assert "model_labels.toml" in captured.out


def test_models_subcommand_shows_effort_values(capsys):
    """norn models shows the effort each claude-code label resolves to."""
    from norn.model_labels import labels_for, resolve_label

    with patch("sys.argv", ["norn", "models"]):
        main()
    captured = capsys.readouterr()
    # Read the expected efforts from the TOML rather than pinning literals here,
    # so adopting a new model stays a one-file change.
    for label in labels_for("claude-code"):
        effort = resolve_label("claude-code", label).effort
        assert effort is not None, f"claude-code label {label!r} has no effort"
        assert effort in captured.out, f"Expected effort {effort!r} in norn models output"


# --- compare subcommand ---


def _compare_runs(root, key, costs):
    import json
    from dataclasses import asdict
    from norn.metrics import RunMetrics

    for i, c in enumerate(costs, start=1):
        d = root / f"{key}.runs" / str(i)
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({"history_run_id": i}))
        m = RunMetrics(
            run_id=str(i), pipeline=key, provider="claude-code", cost_mode="tracked",
            success=True, failed_stage=None, cost_usd=c, billed_input_tokens=1,
            output_tokens=1, cache_read_tokens=0, cache_hit_ratio=0.0, turns=1,
            wall_ms=1, api_ms=1, tool_time_ms=1, agent_stage_count=1,
            command_stage_count=0, failed_attempts=0, failed_attempt_cost_usd=0.0,
            timed_out_attempts=0, retries=0, budget_max_cost_usd=None,
            budget_used_pct=None, waiting_input={}, top_cost_stages=[], quota=None,
            codex_cost_usd_estimate=None, stages=[],
        )
        (d / "metrics.json").write_text(json.dumps(asdict(m)))


def test_compare_prints_both_sides(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _compare_runs(tmp_path, "pa", [10, 11, 12])
    _compare_runs(tmp_path, "pb", [1, 2, 3])
    with patch("sys.argv", ["norn", "compare", "pa", "pb", "--group", "g=^x"]):
        main()
    out = capsys.readouterr().out
    assert "A pa: 3 runs" in out and "B pb: 3 runs" in out
    assert "B lower" in out
    assert "group_share:g" in out


def test_compare_unknown_pipeline_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _compare_runs(tmp_path, "pa", [1, 2, 3])
    with patch("sys.argv", ["norn", "compare", "pa", "missing"]):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1
    assert "missing" in capsys.readouterr().err


def test_compare_no_runs_with_metrics_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _compare_runs(tmp_path, "pa", [1, 2, 3])
    (tmp_path / "pb.runs" / "1").mkdir(parents=True)
    with patch("sys.argv", ["norn", "compare", "pa", "pb"]):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["compare", "only-one"],
        ["compare", "a", "b", "c"],
        ["compare", "a", "b", "--group", "noequals"],
        ["compare", "a", "b", "--group", "g=("],
        ["compare", "a", "b", "--bar", "digest.md"],
        ["compare", "a", "--bar", "missing-digest.md"],
        ["compare", "a", "b", "--last", "0"],
        ["compare", "a", "b", "--bogus"],
    ],
)
def test_compare_argument_errors_are_usage_errors(argv, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "digest.md").write_text("## Pass bar\n")
    with patch("sys.argv", ["norn", *argv]):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 2


def test_compare_bar_reads_digest(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _compare_runs(tmp_path, "pa", [10, 11, 12, 1, 1, 1])
    digest = tmp_path / "digest.md"
    digest.write_text(
        "## Pass bar\nmetric=cost_usd direction=lower threshold=50% baseline=1,2,3\n"
    )
    with patch("sys.argv", ["norn", "compare", "pa", "--bar", str(digest)]):
        main()
    assert "met" in capsys.readouterr().out
