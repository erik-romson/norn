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
