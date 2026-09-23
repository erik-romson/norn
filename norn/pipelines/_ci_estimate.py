"""Private helper for ``merge_branch``: per-workflow median CI durations and the poll plan.

Not a pipeline module — ``norn/catalog.py`` skips anything under
``norn/pipelines/`` whose name starts with ``_``.

This is a deliberate gh-CLI rewrite of the idea in
``norn.stages.check_ci._estimate_typical_duration``
(``norn/stages/check_ci.py:684-751``), not a shared extraction. The two
differ on purpose:

* the core estimator is githubkit-based and single-workflow; this one calls
  ``gh`` (through ``_merge_gh.gh``, the pipeline's one home for every gh
  invocation) and estimates *every* workflow the PR runs, then takes the max
  — the workflows run concurrently, so the slowest one is the wait.
* the core estimator falls back to *all branches* when the branch itself has
  no successful run. This one falls back to the *base* branch specifically
  (locked decision 7): "how long does this repo's CI usually take on the
  branch we're about to merge into" is a closer proxy for a fresh feature
  branch than a global average across every branch in the repo.
* the core estimator swallows every error (network, gh, parsing) to
  ``None`` so a flaky estimate never blocks the CI wait it feeds. This one
  lets a ``GhError`` propagate — per AGENTS.md's no-fallbacks rule, a gh
  failure here is a real problem (``HaltOnFailure`` in the caller turns it
  into ``unexpected_failure``), not something to silently degrade past.

Sharing the core function would make ``norn/`` (the core estimator lives
under ``norn/stages/``) depend on the pipeline layer's gh shape, which is
backwards — hence the duplication. ``check_ci.py`` is untouched by this
module.
"""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from norn.pipelines._merge_gh import gh

# (repo, workflow, branch) -> (cached_at, median_seconds). Only a *hit* (a
# non-None median) is cached — a miss (no successful run yet) is worth
# re-checking on the very next call, since the run that would answer it may
# land any moment.
_DURATION_CACHE: dict[tuple[str, str, str], tuple[float, float]] = {}
_CACHE_TTL_SECONDS = 3600.0

_POLL_INTERVAL = 30.0
_TIMEOUT_FLOOR = 600.0
_TIMEOUT_CEILING = 3600.0
_NO_ESTIMATE_TIMEOUT = 1800.0


def _parse_iso(timestamp: str) -> datetime:
    """Parse a gh-CLI timestamp (ISO-8601 with a trailing ``Z``)."""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


async def median_duration(
    repo: str,
    workflow: str,
    branch: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> float | None:
    """Median wall-clock duration (seconds) of *branch*'s 5 latest successful runs.

    ``None`` when *branch* has no successful run for *workflow*. Cached per
    ``(repo, workflow, branch)`` for ``_CACHE_TTL_SECONDS``, using the
    injected *clock* so tests can control expiry without sleeping. A ``gh``
    failure propagates as ``GhError`` — never swallowed to ``None``.
    """
    key = (repo, workflow, branch)
    cached = _DURATION_CACHE.get(key)
    if cached is not None:
        cached_at, value = cached
        if clock() - cached_at <= _CACHE_TTL_SECONDS:
            return value
        del _DURATION_CACHE[key]

    _, out, _ = await gh(
        [
            "run", "list", "-R", repo, "-w", workflow, "-b", branch,
            "-s", "success", "-L", "5", "--json", "startedAt,updatedAt",
        ],
        cwd=cwd,
        env=env,
    )
    runs = json.loads(out)
    durations = [
        (_parse_iso(run["updatedAt"]) - _parse_iso(run["startedAt"])).total_seconds()
        for run in runs
    ]
    if not durations:
        return None

    value = statistics.median(durations)
    _DURATION_CACHE[key] = (clock(), value)
    return value


async def workflow_estimate(
    repo: str,
    workflow: str,
    branch: str,
    base: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> float | None:
    """*branch*'s own median for *workflow*, else *base*'s (locked decision 7)."""
    value = await median_duration(repo, workflow, branch, clock=clock, cwd=cwd, env=env)
    if value is not None:
        return value
    return await median_duration(repo, workflow, base, clock=clock, cwd=cwd, env=env)


async def estimate_seconds(
    repo: str,
    workflows: list[str],
    branch: str,
    base: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[float | None, dict[str, float]]:
    """Per-workflow estimates and their max — the workflows run concurrently.

    ``(None, {})`` when not one of *workflows* has history on either *branch*
    or *base*.
    """
    per_workflow: dict[str, float] = {}
    for workflow in workflows:
        value = await workflow_estimate(repo, workflow, branch, base, clock=clock, cwd=cwd, env=env)
        if value is not None:
            per_workflow[workflow] = value

    if not per_workflow:
        return None, {}
    return max(per_workflow.values()), per_workflow


@dataclass(frozen=True)
class PollPlan:
    """How long to wait before the first check, how often, and when to give up."""

    pre_sleep: float
    interval: float
    timeout: float


def poll_plan(estimate: float | None, elapsed: float = 0.0) -> PollPlan:
    """Return the :class:`PollPlan` for a check wait, given an optional *estimate*.

    ``interval`` is always 30s. ``timeout`` is ``clamp(2 * estimate, 600,
    3600)``, or 1800 with no estimate. ``pre_sleep`` — how long to sleep
    before the first poll, since polling before the run is even likely to be
    done just burns API calls — targets 80% of the estimate, reduced by
    *elapsed* (time already spent, e.g. by an earlier estimate call), and is
    capped so it can never push past two poll intervals before the timeout;
    it is 0 with no estimate.
    """
    if estimate is None:
        return PollPlan(pre_sleep=0.0, interval=_POLL_INTERVAL, timeout=_NO_ESTIMATE_TIMEOUT)

    timeout = min(max(2.0 * estimate, _TIMEOUT_FLOOR), _TIMEOUT_CEILING)
    pre_sleep = max(0.0, min(0.8 * estimate - elapsed, timeout - elapsed - 2 * _POLL_INTERVAL))
    return PollPlan(pre_sleep=pre_sleep, interval=_POLL_INTERVAL, timeout=timeout)
