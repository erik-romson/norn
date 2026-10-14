"""Tests for norn.quota — CodexBar quota sampling."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from norn.quota import QuotaProbeError, sample_quota


# ---------------------------------------------------------------------------
# sample_quota unit tests
# ---------------------------------------------------------------------------


def test_sample_quota_unknown_provider():
    """Raises QuotaProbeError for a provider with no codexbar mapping."""
    with pytest.raises(QuotaProbeError, match="No codexbar mapping"):
        sample_quota("unknown-provider")


def test_sample_quota_codexbar_not_found():
    """Raises QuotaProbeError when codexbar binary is not on PATH."""
    with patch("norn.quota.shutil.which", return_value=None):
        with pytest.raises(QuotaProbeError, match="codexbar binary not found"):
            sample_quota("claude-code")


def test_sample_quota_command_failure():
    """Raises QuotaProbeError when codexbar exits non-zero."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "connection refused"

    with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
        with patch("norn.quota.subprocess.run", return_value=mock_result):
            with pytest.raises(QuotaProbeError, match="codexbar exited 1"):
                sample_quota("claude-code")


def test_sample_quota_invalid_json():
    """Raises QuotaProbeError when codexbar returns non-JSON output."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "not json"

    with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
        with patch("norn.quota.subprocess.run", return_value=mock_result):
            with pytest.raises(QuotaProbeError, match="invalid JSON"):
                sample_quota("claude-code")


def test_sample_quota_timeout():
    """Raises QuotaProbeError when codexbar times out."""
    with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
        with patch(
            "norn.quota.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="codexbar", timeout=30),
        ):
            with pytest.raises(QuotaProbeError, match="timed out"):
                sample_quota("claude-code")


def test_sample_quota_strips_identity_fields():
    """Safe fields are returned; identity/account fields are stripped."""
    raw = {
        "provider": "claude",
        "remaining_pct": 72.5,
        "remaining_tokens": 100000,
        "window": "daily",
        "pace": {"will_last_to_reset": True},
        "limit": 200000,
        "used": 100000,
        "reset_at": "2026-09-06T00:00:00Z",
        # identity fields that must be stripped
        "account_id": "acct-secret",
        "email": "user@example.com",
        "api_key": "sk-ant-xxx",
    }
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps(raw)

    with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
        with patch("norn.quota.subprocess.run", return_value=mock_result):
            result = sample_quota("claude-code")

    assert "account_id" not in result
    assert "email" not in result
    assert "api_key" not in result
    assert result["remaining_pct"] == 72.5
    assert result["provider"] == "claude"


def test_sample_quota_opencode_provider():
    """Works for opencode provider (uses its codexbar mapping)."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"remaining_pct": 50.0})

    captured_args: list = []

    def fake_run(args, **kwargs):
        captured_args.extend(args)
        return mock_result

    with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
        with patch("norn.quota.subprocess.run", side_effect=fake_run):
            sample_quota("opencode")

    assert "--provider" in captured_args
    idx = captured_args.index("--provider")
    assert captured_args[idx + 1] == "opencode"


# ---------------------------------------------------------------------------
# Integration: quota_probe kwarg in run_pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_pipeline_quota_before_written_to_journal(tmp_path):
    """quota_probe causes quota_before to be written in run.json."""
    from norn.dsl import Pipeline
    from norn.models import PipelineContext, StageResult
    from norn.runner import run_pipeline
    from norn.stages.base import BaseStage

    class _OkStage(BaseStage):
        async def run(self, ctx: PipelineContext) -> StageResult:
            return StageResult(name="", success=True, output="ok")

    pipeline = Pipeline("quota-test").stage("ok", _OkStage())
    config_path = str(tmp_path / "quota-test")

    quota_data = {"remaining_pct": 80.0, "provider": "claude"}

    with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
        with patch("norn.quota.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(quota_data))
            await run_pipeline(
                pipeline,
                config_path=config_path,
                quota_probe="codexbar",
            )

    from norn.journal import runs_dir
    rd = runs_dir(config_path) / "1"
    run_json = json.loads((rd / "run.json").read_text())
    assert "quota_before" in run_json
    assert run_json["quota_before"]["remaining_pct"] == 80.0


@pytest.mark.asyncio
async def test_run_pipeline_quota_after_written_to_journal(tmp_path):
    """quota_probe causes quota_after to be written in run.json after the run."""
    from norn.dsl import Pipeline
    from norn.models import PipelineContext, StageResult
    from norn.runner import run_pipeline
    from norn.stages.base import BaseStage

    class _OkStage(BaseStage):
        async def run(self, ctx: PipelineContext) -> StageResult:
            return StageResult(name="", success=True, output="ok")

    pipeline = Pipeline("quota-after-test").stage("ok", _OkStage())
    config_path = str(tmp_path / "quota-after-test")

    quota_data = {"remaining_pct": 75.0, "provider": "claude"}

    with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
        with patch("norn.quota.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(quota_data))
            await run_pipeline(
                pipeline,
                config_path=config_path,
                quota_probe="codexbar",
            )

    from norn.journal import runs_dir
    rd = runs_dir(config_path) / "1"
    run_json = json.loads((rd / "run.json").read_text())
    assert "quota_after" in run_json
    assert run_json["quota_after"]["remaining_pct"] == 75.0


@pytest.mark.asyncio
async def test_run_pipeline_quota_probe_failure_warns_by_default(tmp_path, caplog):
    """When quota probe fails, a warning is logged and the run continues."""
    import logging

    from norn.dsl import Pipeline
    from norn.models import PipelineContext, StageResult
    from norn.runner import run_pipeline
    from norn.stages.base import BaseStage

    class _OkStage(BaseStage):
        async def run(self, ctx: PipelineContext) -> StageResult:
            return StageResult(name="", success=True, output="ok")

    pipeline = Pipeline("quota-warn-test").stage("ok", _OkStage())
    config_path = str(tmp_path / "quota-warn-test")

    with patch("norn.quota.shutil.which", return_value=None):
        with caplog.at_level(logging.WARNING):
            await run_pipeline(
                pipeline,
                config_path=config_path,
                quota_probe="codexbar",
            )

    assert any("quota" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_run_pipeline_require_quota_raises_on_failure(tmp_path):
    """When require_quota=True and probe fails, run_pipeline raises."""
    from norn.dsl import Pipeline
    from norn.models import PipelineContext, StageResult
    from norn.quota import QuotaProbeError
    from norn.runner import run_pipeline
    from norn.stages.base import BaseStage

    class _OkStage(BaseStage):
        async def run(self, ctx: PipelineContext) -> StageResult:
            return StageResult(name="", success=True, output="ok")

    pipeline = Pipeline("quota-require-test").stage("ok", _OkStage())
    config_path = str(tmp_path / "quota-require-test")

    with patch("norn.quota.shutil.which", return_value=None):
        with pytest.raises(QuotaProbeError):
            await run_pipeline(
                pipeline,
                config_path=config_path,
                quota_probe="codexbar",
                require_quota=True,
            )


# ---------------------------------------------------------------------------
# parse_quota_threshold
# ---------------------------------------------------------------------------


def test_parse_threshold_defaults_to_session_window():
    """A bare percentage means the session window."""
    from norn.quota import parse_quota_threshold

    assert parse_quota_threshold("20") == (20.0, "session")


def test_parse_threshold_accepts_weekly_window():
    """``<pct>:weekly`` selects the secondary window."""
    from norn.quota import parse_quota_threshold

    assert parse_quota_threshold("12.5:weekly") == (12.5, "weekly")


def test_parse_threshold_rejects_unknown_window():
    """An unsupported window is a caller mistake, not a quota state."""
    from norn.quota import parse_quota_threshold

    with pytest.raises(QuotaProbeError, match="Invalid quota window"):
        parse_quota_threshold("20:monthly")


def test_parse_threshold_rejects_non_numeric_percentage():
    from norn.quota import parse_quota_threshold

    with pytest.raises(QuotaProbeError, match="Invalid quota threshold"):
        parse_quota_threshold("plenty")


def test_parse_threshold_rejects_out_of_range_percentage():
    from norn.quota import parse_quota_threshold

    with pytest.raises(QuotaProbeError, match="out of range"):
        parse_quota_threshold("120")


# ---------------------------------------------------------------------------
# check_quota_guard
# ---------------------------------------------------------------------------


def _guard_result(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def test_guard_allows_when_codexbar_exits_zero():
    """Exit 0 means enough headroom; the run may start."""
    from norn.quota import check_quota_guard

    with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
        with patch(
            "norn.quota.subprocess.run",
            return_value=_guard_result(0, "claude session: 73% remaining — OK"),
        ):
            decision = check_quota_guard("claude-code", "20")

    assert decision.allowed is True
    assert decision.exit_code == 0
    assert decision.threshold_pct == 20.0
    assert decision.window == "session"
    assert "73%" in decision.message


def test_guard_passes_min_remaining_and_window_to_codexbar():
    """The threshold spec becomes codexbar's --min-remaining / --window flags."""
    from norn.quota import check_quota_guard

    captured: list = []

    def fake_run(args, **kwargs):
        captured.extend(args)
        return _guard_result(0)

    with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
        with patch("norn.quota.subprocess.run", side_effect=fake_run):
            check_quota_guard("claude-code", "20:weekly")

    assert captured[1] == "guard"
    assert captured[captured.index("--provider") + 1] == "claude"
    assert captured[captured.index("--min-remaining") + 1] == "20"
    assert captured[captured.index("--window") + 1] == "weekly"


def test_guard_blocks_on_insufficient_quota_even_with_fail_open():
    """Exit 1 is a real quota answer — --quota-fail-open must not override it."""
    from norn.quota import check_quota_guard

    with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
        with patch(
            "norn.quota.subprocess.run",
            return_value=_guard_result(1, "claude session: 4% remaining — LOW"),
        ):
            decision = check_quota_guard("claude-code", "20", fail_open=True)

    assert decision.allowed is False
    assert decision.exit_code == 1


def test_guard_blocks_when_quota_unavailable():
    """Exit 69 (unavailable) blocks by default."""
    from norn.quota import check_quota_guard

    with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
        with patch(
            "norn.quota.subprocess.run",
            return_value=_guard_result(69, "claude session: unknown — UNKNOWN"),
        ):
            decision = check_quota_guard("claude-code", "20")

    assert decision.allowed is False
    assert decision.exit_code == 69


def test_guard_fail_open_allows_when_quota_unavailable():
    """--quota-fail-open turns exit 69 into a started run."""
    from norn.quota import check_quota_guard

    with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
        with patch("norn.quota.subprocess.run", return_value=_guard_result(69)):
            decision = check_quota_guard("claude-code", "20", fail_open=True)

    assert decision.allowed is True
    assert decision.exit_code == 69


def test_guard_missing_binary_is_undetermined():
    """A missing codexbar is treated exactly like exit 69."""
    from norn.quota import check_quota_guard

    with patch("norn.quota.shutil.which", return_value=None):
        blocked = check_quota_guard("claude-code", "20")
        allowed = check_quota_guard("claude-code", "20", fail_open=True)

    assert blocked.allowed is False
    assert blocked.exit_code == 69
    assert "not found" in blocked.message
    assert allowed.allowed is True


def test_guard_timeout_is_undetermined():
    """A guard that times out is undetermined, not a quota answer."""
    from norn.quota import check_quota_guard

    with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
        with patch(
            "norn.quota.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="codexbar", timeout=30),
        ):
            decision = check_quota_guard("claude-code", "20", fail_open=True)

    assert decision.allowed is True
    assert decision.exit_code == 69
    assert "timed out" in decision.message


def test_guard_invalid_arguments_raises():
    """Exit 64 means norn built a bad command line; that is an error, not a block."""
    from norn.quota import check_quota_guard

    with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
        with patch(
            "norn.quota.subprocess.run",
            return_value=_guard_result(64, "", "Unknown option --nope"),
        ):
            with pytest.raises(QuotaProbeError, match="rejected its arguments"):
                check_quota_guard("claude-code", "20")


def test_guard_unknown_provider_raises():
    from norn.quota import check_quota_guard

    with pytest.raises(QuotaProbeError, match="No codexbar mapping"):
        check_quota_guard("unknown-provider", "20")


# ---------------------------------------------------------------------------
# CLI wiring: --require-quota / --quota-fail-open
# ---------------------------------------------------------------------------


def test_cli_require_quota_threshold_blocks_run(capsys):
    """`--require-quota 20:weekly` with guard exit 1 exits 1 before the run."""
    from unittest.mock import AsyncMock

    from norn.cli import main

    mock_run = AsyncMock(return_value=None)
    with patch("norn.cli.run_pipeline", mock_run):
        with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
            with patch(
                "norn.quota.subprocess.run",
                return_value=_guard_result(1, "claude weekly: 4% remaining — LOW"),
            ):
                with patch("sys.argv", ["norn", "run", "hello_v1", "--require-quota", "20:weekly"]):
                    with pytest.raises(SystemExit) as exc_info:
                        main()

    assert exc_info.value.code == 1
    assert not mock_run.called
    assert "Quota guard blocked" in capsys.readouterr().out


def test_cli_quota_fail_open_starts_run_on_unavailable_quota():
    """`--quota-fail-open` turns guard exit 69 into a started run."""
    from unittest.mock import AsyncMock

    from norn.cli import main

    mock_run = AsyncMock(return_value=None)
    with patch("norn.cli.run_pipeline", mock_run):
        with patch("norn.quota.shutil.which", return_value="/usr/bin/codexbar"):
            with patch("norn.quota.subprocess.run", return_value=_guard_result(69)):
                with patch(
                    "sys.argv",
                    ["norn", "run", "hello_v1", "--require-quota", "20", "--quota-fail-open"],
                ):
                    main()

    assert mock_run.called


def test_cli_quota_fail_open_overrides_require_quota_for_probe_failures():
    """--quota-fail-open also stops --require-quota aborting on probe failure."""
    from unittest.mock import AsyncMock

    from norn.cli import main

    mock_run = AsyncMock(return_value=None)
    with patch("norn.cli.run_pipeline", mock_run):
        with patch("sys.argv", ["norn", "run", "hello_v1", "--require-quota", "--quota-fail-open"]):
            main()

    assert mock_run.call_args.kwargs["require_quota"] is False


def test_cli_bare_require_quota_still_aborts_on_probe_failure():
    """A bare --require-quota keeps the fail-fast probe behaviour."""
    from unittest.mock import AsyncMock

    from norn.cli import main

    mock_run = AsyncMock(return_value=None)
    with patch("norn.cli.run_pipeline", mock_run):
        with patch("sys.argv", ["norn", "run", "hello_v1", "--require-quota"]):
            main()

    assert mock_run.call_args.kwargs["require_quota"] is True


def test_cli_no_quota_flags_leaves_require_quota_false():
    from unittest.mock import AsyncMock

    from norn.cli import main

    mock_run = AsyncMock(return_value=None)
    with patch("norn.cli.run_pipeline", mock_run):
        with patch("sys.argv", ["norn", "run", "hello_v1"]):
            main()

    assert mock_run.call_args.kwargs["require_quota"] is False


@pytest.mark.asyncio
async def test_post_run_probe_failure_never_aborts_the_run(tmp_path):
    """require_quota is a pre-run gate: a failing post-run probe must not raise.

    Raising there happened inside `finally`, which skipped `journal.finish`
    (leaving run.json with only its header) and replaced the pipeline's own
    result with a QuotaProbeError.
    """
    from norn.dsl import Pipeline
    from norn.journal import runs_dir
    from norn.models import PipelineContext, StageResult
    from norn.quota import QuotaProbeError
    from norn.runner import run_pipeline
    from norn.stages.base import BaseStage

    class _OkStage(BaseStage):
        async def run(self, ctx: PipelineContext) -> StageResult:
            return StageResult(name="", success=True, output="ok")

    pipeline = Pipeline("quota-post-fail").stage("ok", _OkStage())
    config_path = str(tmp_path / "quota-post-fail")

    calls: list[int] = []

    def _probe(_provider: str) -> dict:
        calls.append(1)
        if len(calls) == 1:
            return {"remaining_pct": 80.0}
        raise QuotaProbeError("codexbar vanished mid-run")

    with patch("norn.quota.sample_quota", side_effect=_probe):
        ctx = await run_pipeline(
            pipeline,
            config_path=config_path,
            quota_probe="codexbar",
            require_quota=True,
        )

    assert ctx is not None

    run_json = json.loads((runs_dir(config_path) / "1" / "run.json").read_text())
    assert run_json["quota_after"]["error"] == "codexbar vanished mid-run"
    # The journal was still finished — the run's own result survived the probe.
    assert run_json["success"] is True
    assert "finished_at" in run_json
    assert "totals" in run_json
