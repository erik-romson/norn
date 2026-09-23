"""Tests for norn/pipelines/_ci_estimate.py.

All offline: a fake ``gh`` on PATH (tests.merge_fakes.install_fake_gh) answers
``gh run list`` with canned ``startedAt``/``updatedAt`` pairs, and an injected
``clock`` callable drives the duration cache's TTL without sleeping.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from norn.pipelines import _ci_estimate
from norn.pipelines._merge_gh import GhError
from tests.merge_fakes import install_fake_gh


@pytest.fixture(autouse=True)
def _clear_cache():
    _ci_estimate._DURATION_CACHE.clear()
    yield
    _ci_estimate._DURATION_CACHE.clear()


_START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _runs(durations_minutes: list[int]) -> str:
    """Return a ``gh run list --json startedAt,updatedAt`` payload.

    Each entry in *durations_minutes* becomes one run whose ``updatedAt`` is
    that many minutes after a fixed ``startedAt`` — using real ``timedelta``
    arithmetic (rather than string-formatting a minute field) so a duration
    over 59 minutes still produces a valid timestamp.
    """
    return json.dumps(
        [
            {
                "startedAt": _START.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "updatedAt": (_START + timedelta(minutes=d)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            for d in durations_minutes
        ]
    )


def _gh_env(tmp_path: Path, responses: list[tuple[int, str, str]]) -> dict[str, str]:
    bin_dir, _ = install_fake_gh(tmp_path, {("run", "list"): responses})
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


class _Clock:
    """A controllable ``clock`` callable — advances only when told to."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# median_duration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_median_duration_odd_count(tmp_path):
    env = _gh_env(tmp_path, [(0, _runs([10, 20, 30, 40, 50]), "")])
    value = await _ci_estimate.median_duration(
        "acme/widgets", "ci.yml", "feature", cwd=str(tmp_path), env=env
    )
    assert value == 30 * 60


@pytest.mark.asyncio
async def test_median_duration_even_count(tmp_path):
    env = _gh_env(tmp_path, [(0, _runs([10, 20, 30, 40]), "")])
    value = await _ci_estimate.median_duration(
        "acme/widgets", "ci.yml", "feature", cwd=str(tmp_path), env=env
    )
    assert value == (20 * 60 + 30 * 60) / 2


@pytest.mark.asyncio
async def test_median_duration_none_when_no_runs(tmp_path):
    env = _gh_env(tmp_path, [(0, _runs([]), "")])
    value = await _ci_estimate.median_duration(
        "acme/widgets", "ci.yml", "feature", cwd=str(tmp_path), env=env
    )
    assert value is None


@pytest.mark.asyncio
async def test_median_duration_gh_error_propagates(tmp_path):
    env = _gh_env(tmp_path, [(1, "", "boom")])
    with pytest.raises(GhError):
        await _ci_estimate.median_duration(
            "acme/widgets", "ci.yml", "feature", cwd=str(tmp_path), env=env
        )


@pytest.mark.asyncio
async def test_median_duration_cache_hit_within_ttl(tmp_path):
    bin_dir, calls_log = install_fake_gh(tmp_path, {("run", "list"): [(0, _runs([10, 20, 30]), "")]})
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    clock = _Clock(start=0.0)

    first = await _ci_estimate.median_duration(
        "acme/widgets", "ci.yml", "feature", clock=clock, cwd=str(tmp_path), env=env
    )
    clock.now = 1800.0  # inside the 3600s TTL
    second = await _ci_estimate.median_duration(
        "acme/widgets", "ci.yml", "feature", clock=clock, cwd=str(tmp_path), env=env
    )

    assert first == second == 20 * 60
    calls = Path(calls_log).read_text().splitlines()
    assert len(calls) == 1  # the second call was served from cache


@pytest.mark.asyncio
async def test_median_duration_cache_miss_after_ttl(tmp_path):
    bin_dir, calls_log = install_fake_gh(
        tmp_path,
        {("run", "list"): [(0, _runs([10, 20, 30]), ""), (0, _runs([100, 200, 300]), "")]},
    )
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    clock = _Clock(start=0.0)

    first = await _ci_estimate.median_duration(
        "acme/widgets", "ci.yml", "feature", clock=clock, cwd=str(tmp_path), env=env
    )
    clock.now = 3600.1  # just past the TTL
    second = await _ci_estimate.median_duration(
        "acme/widgets", "ci.yml", "feature", clock=clock, cwd=str(tmp_path), env=env
    )

    assert first == 20 * 60
    assert second == 200 * 60
    calls = Path(calls_log).read_text().splitlines()
    assert len(calls) == 2  # the expired entry forced a second gh call


# ---------------------------------------------------------------------------
# workflow_estimate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_estimate_falls_back_to_base(tmp_path):
    env = _gh_env(tmp_path, [(0, _runs([]), ""), (0, _runs([10, 20, 30]), "")])
    value = await _ci_estimate.workflow_estimate(
        "acme/widgets", "ci.yml", "feature", "main", cwd=str(tmp_path), env=env
    )
    assert value == 20 * 60


@pytest.mark.asyncio
async def test_workflow_estimate_none_when_neither_branch_has_history(tmp_path):
    env = _gh_env(tmp_path, [(0, _runs([]), "")])
    value = await _ci_estimate.workflow_estimate(
        "acme/widgets", "ci.yml", "feature", "main", cwd=str(tmp_path), env=env
    )
    assert value is None


# ---------------------------------------------------------------------------
# estimate_seconds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_estimate_seconds_takes_max_not_sum(tmp_path):
    env = _gh_env(tmp_path, [(0, _runs([10, 10, 10]), ""), (0, _runs([50, 50, 50]), "")])
    estimate, per_workflow = await _ci_estimate.estimate_seconds(
        "acme/widgets", ["fast.yml", "slow.yml"], "feature", "main", cwd=str(tmp_path), env=env
    )
    assert estimate == 50 * 60
    assert per_workflow == {"fast.yml": 10 * 60, "slow.yml": 50 * 60}


@pytest.mark.asyncio
async def test_estimate_seconds_none_when_no_workflow_has_history(tmp_path):
    env = _gh_env(tmp_path, [(0, _runs([]), "")])
    estimate, per_workflow = await _ci_estimate.estimate_seconds(
        "acme/widgets", ["fast.yml", "slow.yml"], "feature", "main", cwd=str(tmp_path), env=env
    )
    assert estimate is None
    assert per_workflow == {}


# ---------------------------------------------------------------------------
# poll_plan
# ---------------------------------------------------------------------------


def test_poll_plan_no_estimate():
    plan = _ci_estimate.poll_plan(None)
    assert plan == _ci_estimate.PollPlan(pre_sleep=0.0, interval=30.0, timeout=1800.0)


def test_poll_plan_timeout_floor():
    # 2 * 60s = 120s would be below the 600s floor.
    plan = _ci_estimate.poll_plan(60.0)
    assert plan.timeout == 600.0


def test_poll_plan_timeout_ceiling():
    # 2 * 4000s = 8000s would be above the 3600s ceiling.
    plan = _ci_estimate.poll_plan(4000.0)
    assert plan.timeout == 3600.0


def test_poll_plan_pre_sleep_targets_80_percent():
    # estimate=1000 -> timeout = clamp(2000, 600, 3600) = 2000
    # pre_sleep = min(0.8*1000 - 0, 2000 - 0 - 60) = min(800, 1940) = 800
    plan = _ci_estimate.poll_plan(1000.0)
    assert plan.timeout == 2000.0
    assert plan.pre_sleep == 800.0


def test_poll_plan_pre_sleep_reduced_by_elapsed():
    plan = _ci_estimate.poll_plan(1000.0, elapsed=300.0)
    assert plan.pre_sleep == 500.0  # 800 - 300


def test_poll_plan_pre_sleep_never_negative():
    plan = _ci_estimate.poll_plan(1000.0, elapsed=10_000.0)
    assert plan.pre_sleep == 0.0


def test_poll_plan_pre_sleep_never_beyond_timeout():
    # A large estimate against the clamped ceiling: 0.8*estimate would blow
    # past timeout - 2*interval, so pre_sleep must be capped there instead.
    plan = _ci_estimate.poll_plan(10_000.0)
    assert plan.timeout == 3600.0
    assert plan.pre_sleep == plan.timeout - 2 * plan.interval
    assert plan.pre_sleep + 2 * plan.interval <= plan.timeout
