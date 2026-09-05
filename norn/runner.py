from __future__ import annotations

import asyncio
import glob as glob_module
import logging
import os
import pathlib
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from norn.alerts import AlertEvent, AlertManager, AlertMessage
from norn.checkpoint import Checkpoint, save_checkpoint, serialise_output
from norn.dsl import Budget, ClearContext, ContextSpec, Include, Loop, OnFailure, Parallel, Pipeline, PipelineItem, Stage
from norn.events import (
    CallingAgent,
    ClearContextNotice,
    EventKey,
    GotReply,
    IncludeDone,
    IncludeStarted,
    LoopAttempt,
    LoopDraftPR,
    LoopExhausted,
    LoopSuccess,
    ParallelDone,
    ParallelStarted,
    RunCancelled,
    RunError,
    RunFinished,
    RunPaused,
    RunResumed,
    RunStarted,
    StageFinished,
    StageRetrying,
    StageStarted,
    TurnEvent,
    UnitStarted,
    UsageUpdated,
    WaitingInput,
)
from norn.run_control import CancelledError, RunController
from norn.loader import load_pipeline
from norn.models import PipelineContext, StageLogEntry, StageResult, UsageTracker
from norn import ui

if TYPE_CHECKING:
    from norn.stages.base import BaseStage


def resolve_run_path(ctx: PipelineContext, path: str | os.PathLike[str]) -> Path:
    """Resolve *path* relative to the run's working directory.

    Absolute paths are returned unchanged.  Relative paths are resolved under
    ``ctx.working_dir`` when set, falling back to the process cwd otherwise.
    This ensures a ``None`` working dir reproduces today's exact behavior.
    """
    p = Path(path)
    return p if p.is_absolute() else Path(ctx.working_dir or Path.cwd()) / p


def _expand_inline_includes(items: list[PipelineItem], params: dict) -> list[PipelineItem]:
    """Flatten inline (non-isolated) Include items by replacing them with sub-pipeline items.

    Args are merged into *params* in-place so downstream stages see them via ``ctx.params``.
    Recursive: nested inline includes within sub-pipelines are also expanded.
    """
    result: list[PipelineItem] = []
    for item in items:
        if isinstance(item, Include) and not item.isolated:
            params.update(item.args)
            sub = load_pipeline(item.path)
            result.extend(_expand_inline_includes(sub.items, params))
        else:
            result.append(item)
    return result


def _is_skipped(stage: Stage, ctx: PipelineContext) -> bool:
    """Return True if this stage should be skipped based on --skip flags."""
    return stage.name in ctx.params.get("skip", set())


def _is_cached(stage: Stage, ctx: PipelineContext) -> bool:
    """Return True if this stage was pre-loaded from a checkpoint."""
    return stage.name in ctx.checkpoint_stages


log = logging.getLogger(__name__)


async def _cooperative_pause(ctx: PipelineContext) -> None:
    """Check the run controller for pause/cancel between stages.

    Emits ``RunPaused``/``RunResumed`` events when the run transitions.
    No-op when no controller is attached (default CLI path).
    """
    ctrl: RunController | None = ctx.run_controller
    if ctrl is None:
        return
    ctrl.check_cancelled()
    if ctrl.is_paused:
        _ekey = EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id)
        ctx.event_sink.emit(RunPaused(key=_ekey))
        await ctrl.wait_if_paused()
        ctx.event_sink.emit(RunResumed(key=_ekey))


async def _resolve_contexts(
    specs: list[ContextSpec],
    working_dir: str | None = None,
) -> list[tuple[str, str]]:
    """Resolve context specs to ``(label, content)`` pairs.

    When *working_dir* is set, relative file globs are expanded under that
    directory and commands are run with that directory as cwd.

    Raises ``RuntimeError`` if a file glob matches nothing or a command fails.
    """
    resolved: list[tuple[str, str]] = []
    for spec in specs:
        if spec.kind == "file":
            # Resolve relative patterns under working_dir so that file globs
            # stay inside the worktree rather than leaking to process cwd.
            if working_dir is not None and not pathlib.Path(spec.source).is_absolute():
                glob_pattern = str(pathlib.Path(working_dir) / spec.source)
            else:
                glob_pattern = spec.source
            paths = sorted(glob_module.glob(glob_pattern, recursive=True))
            if not paths:
                raise RuntimeError(f"Context spec matched no files: {spec.source!r}")
            content = "\n\n".join(pathlib.Path(p).read_text() for p in paths)
            resolved.append((spec.label, content))
        elif spec.kind == "cmd":
            proc = await asyncio.create_subprocess_shell(
                spec.source,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"Context command failed (exit {proc.returncode}): {spec.source!r}\n"
                    + stderr.decode().strip()
                )
            resolved.append((spec.label, stdout.decode()))
        else:
            raise RuntimeError(f"Unknown context kind: {spec.kind!r}")
    return resolved


class PipelineError(Exception):
    """Raised when a pipeline stage fails with ``on_failure=FAIL``.

    Also raised when a hook fails, a stage times out, or the user aborts
    in stepping mode. Caught by the CLI to print the error and exit.

    Attributes:
        stage_name: Name of the stage (or ``"hook:<event>"`` for hook failures).
        result: The ``StageResult`` that caused the failure.
    """

    def __init__(self, stage_name: str, result: StageResult) -> None:
        self.stage_name = stage_name
        self.result = result
        super().__init__(f"Stage '{stage_name}' failed: {result.error}")


class RetriesExhaustedError(PipelineError):
    """Raised when a loop exhausts all retry attempts with ``on_exhaust=FAIL``.

    The ``result`` attribute contains the last failed ``StageResult`` from
    the final attempt.
    """

    def __init__(self, loop_name: str, result: StageResult) -> None:
        super().__init__(loop_name, result)


class BudgetExceededError(Exception):
    """Raised when a pipeline budget limit is crossed.

    Triggered after a stage completes and the cumulative cost or token
    count exceeds the configured ``Budget``. When ``on_exceed=ASK_USER``,
    this is only raised if the user chooses to abort.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)


async def _fire_hooks(
    event: str,
    hooks: dict[str, list[BaseStage]],
    ctx: PipelineContext,
) -> None:
    """Run all registered pipeline-level hooks for the given lifecycle event."""
    for impl in hooks.get(event, []):
        result = await impl.run(ctx)
        if not result.success:
            raise PipelineError(f"hook:{event}", result)


async def _check_budget(
    budgets: list[Budget],
    tracker: UsageTracker,
    ctx: PipelineContext,
    ekey: EventKey,
) -> None:
    """Check all budgets against current cumulative usage. Raises or prompts on excess."""
    for b in budgets:
        exceeded = False
        detail = ""
        if b.max_cost_usd is not None and tracker.total_cost_usd > b.max_cost_usd:
            exceeded = True
            detail = f"${tracker.total_cost_usd:.4f} > ${b.max_cost_usd:.2f}"
        elif b.max_tokens is not None and tracker.total_tokens > b.max_tokens:
            exceeded = True
            detail = f"{tracker.total_tokens:,} tokens > {b.max_tokens:,} limit"

        if not exceeded:
            continue

        if b.on_exceed == OnFailure.FAIL:
            raise BudgetExceededError(f"Budget exceeded: {detail}")
        if b.on_exceed == OnFailure.ASK_USER:
            ctx.event_sink.emit(WaitingInput(key=ekey, kind="budget"))
            choice = await ctx.input_responder.ask_budget(tracker, b)
            if choice == "a":
                raise BudgetExceededError(f"Budget exceeded: {detail}")


def _append_stage_log(
    ctx: PipelineContext,
    *,
    stage_name: str,
    status: str,
    success: bool,
    attempt: int = 1,
    duration_ms: int = 0,
    error: str | None = None,
) -> None:
    """Record a detailed stage event for later inspection in run history."""
    usage = ctx.results.get(stage_name).usage if stage_name in ctx.results else None
    ctx.stage_log.append(
        StageLogEntry(
            name=stage_name,
            status=status,
            success=success,
            attempt=attempt,
            duration_ms=duration_ms,
            cost_usd=usage.total_cost_usd if usage else 0.0,
            running_total_cost_usd=ctx.usage_tracker.total_cost_usd,
            running_total_tokens=ctx.usage_tracker.total_tokens,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cache_read_input_tokens=usage.cache_read_input_tokens if usage else 0,
            cache_creation_input_tokens=usage.cache_creation_input_tokens if usage else 0,
            duration_api_ms=usage.duration_api_ms if usage else 0,
            num_turns=usage.num_turns if usage else 0,
            model=usage.model if usage else None,
            session_id=usage.session_id if usage else None,
            error=error,
            provider=usage.provider if usage else None,
        )
    )
    _persist_history_snapshot(ctx)


async def _run_stage(
    stage: Stage,
    ctx: PipelineContext,
    *,
    session_id: str | None = None,
    attempt: int = 1,
    budgets: list[Budget] | None = None,
    fork_session: bool = False,
    node_id: str | None = None,
) -> StageResult:
    """Run a single stage and store the result in context.

    *node_id* is the fully-qualified graph node id used as the event
    ``stage_id`` so the TUI graph can attribute events to the right node.
    Top-level stages default to ``stage:<name>``; stages inside a loop or
    parallel pass the nested form (``loop:<L>/stage:<name>`` etc.) so the
    id matches :func:`norn.graph.build_graph`.
    """
    start = time.monotonic()
    stage_id = node_id or f"stage:{stage.name}"
    _ekey = EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id, stage_id=stage_id, attempt=attempt)
    ctx.event_sink.emit(StageStarted(key=_ekey, name=stage.name, attempt=attempt))

    if stage.impl.needs_agent:
        log.debug(
            "[%s] Agent stage — session=%s attempt=%d fork=%s",
            stage.name, session_id, attempt, fork_session,
        )
        agent_kwargs: dict = {
            "session_id": session_id,
            "attempt": attempt,
            "fork_session": fork_session,
            "stage_name": stage.name,
            # Pass the fully-qualified graph node id so agent-backed stages
            # key their CallingAgent/TurnEvent/GotReply events consistently
            # with the StageStarted/StageFinished events emitted here.
            "node_id": stage_id,
        }
        mcp_tools = getattr(stage.impl, "mcp_tools", None)
        if isinstance(mcp_tools, list) and mcp_tools:
            if ctx.agent_provider != "claude-code":
                error = (
                    f"Stage '{stage.name}' declares mcp_tools but provider "
                    f"'{ctx.agent_provider}' does not support MCP tools. "
                    "MCP tools are only available with the 'claude-code' provider."
                )
                result = StageResult(name=stage.name, success=False, error=error)
                elapsed = time.monotonic() - start
                ctx.results[stage.name] = result
                _append_stage_log(
                    ctx,
                    stage_name=stage.name,
                    status="failed",
                    success=False,
                    attempt=attempt,
                    duration_ms=int(elapsed * 1000),
                    error=error,
                )
                ctx.event_sink.emit(StageFinished(
                    key=_ekey,
                    name=stage.name,
                    status="failed",
                    success=False,
                    duration_ms=int(elapsed * 1000),
                    error=error,
                ))
                return result
            agent_kwargs["mcp_tools"] = mcp_tools
            log.debug("[%s] Attached MCP tools (%d tool(s))", stage.name, len(mcp_tools))
        coro = stage.impl.run(ctx, **agent_kwargs)
    elif stage.impl.emits_events:
        # Non-agent stage that emits its own run-events (RunCommand streaming
        # its output).  It needs the same fully-qualified node id this function
        # uses for StageStarted/StageFinished, or its events would be filed
        # under a different graph node and never reach the transcript pane.
        coro = stage.impl.run(ctx, node_id=stage_id, attempt=attempt)
    else:
        coro = stage.impl.run(ctx)

    # Wrap in a task so RunController.cancel() can cancel the active stage
    # immediately, rather than waiting for the next between-stage cooperative
    # check.  Always deregister in `finally` so a later cancel() cannot touch
    # a finished task.
    task = asyncio.create_task(coro)
    ctrl: RunController | None = ctx.run_controller
    if ctrl is not None:
        ctrl.set_active_task(task, stage.name)
    try:
        if stage.timeout is not None:
            result = await asyncio.wait_for(task, timeout=stage.timeout)
        else:
            result = await task
    except asyncio.CancelledError:
        # Distinguish user-initiated cancel from outer task cancellation.
        if ctrl is not None and ctrl.is_cancelled:
            elapsed = time.monotonic() - start
            error = f"Cancelled during {stage.name!r}"
            result = StageResult(name=stage.name, success=False, error=error)
            ctx.results[stage.name] = result
            _append_stage_log(
                ctx,
                stage_name=stage.name,
                status="cancelled",
                success=False,
                attempt=attempt,
                duration_ms=int(elapsed * 1000),
                error=error,
            )
            ctx.event_sink.emit(StageFinished(
                key=_ekey,
                name=stage.name,
                status="cancelled",
                success=False,
                duration_ms=int(elapsed * 1000),
                error=error,
            ))
            raise CancelledError(stage.name)
        # Outer cancellation (e.g. the run task itself was cancelled) — never
        # swallow, let it propagate.
        raise
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        result = StageResult(name=stage.name, success=False, error=f"Timed out after {stage.timeout}s")
        ctx.results[stage.name] = result
        _append_stage_log(
            ctx,
            stage_name=stage.name,
            status="failed",
            success=False,
            attempt=attempt,
            duration_ms=int(elapsed * 1000),
            error=result.error,
        )
        ctx.event_sink.emit(StageFinished(
            key=_ekey,
            name=stage.name,
            status="failed",
            success=False,
            duration_ms=int(elapsed * 1000),
            error=result.error,
        ))
        return result
    finally:
        if ctrl is not None:
            ctrl.set_active_task(None, None)

    elapsed = time.monotonic() - start
    result.name = stage.name
    if result.usage:
        result.usage.stage_name = stage.name
        ctx.usage_tracker.add(result.usage)
        ctx.event_sink.emit(UsageUpdated(
            key=_ekey,
            input_tokens=ctx.usage_tracker.total_input_tokens,
            output_tokens=ctx.usage_tracker.total_output_tokens,
            total_cost_usd=ctx.usage_tracker.total_cost_usd,
        ))
    ctx.results[stage.name] = result
    _append_stage_log(
        ctx,
        stage_name=stage.name,
        status="passed" if result.success else "failed",
        success=result.success,
        attempt=attempt,
        duration_ms=int(elapsed * 1000),
        error=result.error,
    )

    ctx.event_sink.emit(StageFinished(
        key=_ekey,
        name=stage.name,
        status="passed" if result.success else "failed",
        success=result.success,
        duration_ms=int(elapsed * 1000),
        artifacts=list(result.artifacts),
        error=result.error,
        usage_input_tokens=result.usage.input_tokens if result.usage else 0,
        usage_output_tokens=result.usage.output_tokens if result.usage else 0,
        usage_cost_usd=result.usage.total_cost_usd if result.usage else 0.0,
    ))

    if budgets and result.usage:
        await _check_budget(budgets, ctx.usage_tracker, ctx, _ekey)

    return result


async def _handle_failure(
    on_failure: OnFailure,
    name: str,
    result: StageResult,
    *,
    ctx: PipelineContext,
    pipeline_name: str = "",
    alert_manager: AlertManager | None = None,
) -> str:
    """Handle a stage failure according to the configured policy.

    Returns ``"continue"`` to proceed past the failure (treat it as
    non-fatal) or ``"retry"`` to re-run the failed stage.  Raises
    :class:`PipelineError` on ``FAIL`` policy or when the user aborts.
    """
    if on_failure == OnFailure.FAIL:
        raise PipelineError(name, result)
    if on_failure == OnFailure.ASK_USER:
        if alert_manager:
            await alert_manager.fire(
                AlertMessage(
                    event=AlertEvent.ASK_USER,
                    pipeline_name=pipeline_name,
                    stage_name=name,
                    detail=result.error or "",
                )
            )
        # Top-level path. `_handle_failure` is reached only from `run_pipeline`'s
        # own item loop, so `stage:<name>` *is* the fully-qualified id here.
        # Nested stages carry a parent prefix and must take their id from the
        # `node_id` the runner already computed — never rebuild one there.
        _failure_ekey = EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id, stage_id=f"stage:{name}")
        ctx.event_sink.emit(WaitingInput(
            key=_failure_ekey,
            kind="failure_recovery",
            prompt_excerpt=result.error or "",
        ))
        choice = await ctx.input_responder.ask_failure(name, result.error)
        if choice == "a":
            raise PipelineError(name, result)
        if choice == "r":
            return "retry"
    return "continue"


def _save_checkpoint_state(
    config_path: str,
    pipeline_name: str,
    session_id: str | None,
    completed_stages: list[str],
    ctx: PipelineContext,
) -> None:
    """Persist current pipeline progress to a checkpoint file."""
    stage_outputs = {
        name: serialise_output(ctx.results[name].output)
        for name in completed_stages
        if name in ctx.results
    }
    save_checkpoint(config_path, pipeline_name, session_id, completed_stages, stage_outputs, ctx.agent_provider)


async def _run_loop(
    loop: Loop,
    ctx: PipelineContext,
    *,
    initial_session_id: str | None = None,
    pipeline_name: str = "",
    alert_manager: AlertManager | None = None,
    pipeline_hooks: dict[str, list[BaseStage]] | None = None,
    budgets: list[Budget] | None = None,
    checkpoint_path: str | None = None,
    completed_stages: list[str] | None = None,
    fork_session: bool = False,
    step_mode: bool = False,
) -> str | None:
    """Run a do-while loop: execute all stages, retry from top on failure.

    Agent-backed stages (Generate) within the same loop share a session so
    the agent remembers prior errors when retrying.

    Returns the final session_id used (if any), so the caller can persist it.
    """
    last_result: StageResult | None = None
    session_id: str | None = initial_session_id
    _fork_pending = fork_session

    _loop_ekey = EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id, stage_id=f"loop:{loop.name}")

    for attempt in range(1, loop.max_retries + 1):
        if attempt > 1:
            ctx.retries += 1
            if pipeline_hooks:
                await _fire_hooks("on_retry", pipeline_hooks, ctx)
            ctx.event_sink.emit(StageRetrying(
                key=_loop_ekey,
                next_attempt=attempt,
                reason=last_result.error if last_result else "",
            ))
        ctx.event_sink.emit(LoopAttempt(
            key=_loop_ekey,
            name=loop.name,
            attempt=attempt,
            max_retries=loop.max_retries,
        ))
        all_passed = True

        for stage in loop.stages:
            # Cooperative pause/cancel check between loop body stages
            await _cooperative_pause(ctx)

            # Fully-qualified graph node id for this loop-body stage so the
            # TUI attributes events to the nested node, not a flat one.
            stage_node_id = f"loop:{loop.name}/stage:{stage.name}"

            if _is_cached(stage, ctx):
                _append_stage_log(
                    ctx,
                    stage_name=stage.name,
                    status="cached",
                    success=True,
                    attempt=attempt,
                )
                ctx.event_sink.emit(StageFinished(
                    key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id, stage_id=stage_node_id),
                    name=stage.name, status="cached", success=True,
                ))
                continue

            if _is_skipped(stage, ctx):
                ctx.results[stage.name] = StageResult(name=stage.name, success=True)
                _append_stage_log(
                    ctx,
                    stage_name=stage.name,
                    status="skipped",
                    success=True,
                    attempt=attempt,
                )
                ctx.event_sink.emit(StageFinished(
                    key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id, stage_id=stage_node_id),
                    name=stage.name, status="skipped", success=True,
                ))
                continue

            if stage.when is not None and not stage.when(ctx):
                ctx.results[stage.name] = StageResult(name=stage.name, success=True)
                _append_stage_log(
                    ctx,
                    stage_name=stage.name,
                    status="skipped_condition",
                    success=True,
                    attempt=attempt,
                )
                ctx.event_sink.emit(StageFinished(
                    key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id, stage_id=stage_node_id),
                    name=stage.name, status="skipped_condition", success=True,
                ))
                continue

            if step_mode:
                _step_ekey = EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id, stage_id=stage_node_id)
                ctx.event_sink.emit(WaitingInput(key=_step_ekey, kind="step"))
                action = await ctx.input_responder.ask_step(stage, ctx, session_id=session_id)
                if action == "s":
                    ctx.results[stage.name] = StageResult(name=stage.name, success=True)
                    _append_stage_log(
                        ctx,
                        stage_name=stage.name,
                        status="skipped",
                        success=True,
                        attempt=attempt,
                    )
                    ctx.event_sink.emit(StageFinished(
                        key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id, stage_id=stage_node_id),
                        name=stage.name, status="skipped", success=True,
                    ))
                    continue
                if action == "a":
                    raise PipelineError(
                        stage.name,
                        StageResult(name=stage.name, success=False, error="Aborted by user"),
                    )

            if pipeline_hooks:
                await _fire_hooks("pre_stage", pipeline_hooks, ctx)
            result = await _run_stage(
                stage, ctx, session_id=session_id, attempt=attempt, budgets=budgets,
                fork_session=_fork_pending, node_id=stage_node_id,
            )
            last_result = result

            # Capture session_id from first Generate result for reuse; update on fork
            if result.usage and result.usage.session_id:
                if session_id is None:
                    session_id = result.usage.session_id
                    log.debug("[%s] Captured session %s for reuse", loop.name, session_id)
                elif _fork_pending and result.usage.session_id != session_id:
                    session_id = result.usage.session_id
                    log.debug("[%s] Forked to session %s", loop.name, session_id)
                if _fork_pending:
                    _fork_pending = False

            if not result.success:
                if pipeline_hooks:
                    await _fire_hooks("on_failure", pipeline_hooks, ctx)
                all_passed = False
                break

            # Save checkpoint after each successful stage
            if checkpoint_path and completed_stages is not None:
                if stage.name not in completed_stages:
                    completed_stages.append(stage.name)
                _save_checkpoint_state(
                    checkpoint_path,
                    pipeline_name,
                    ctx.usage_tracker.last_session_id or session_id,
                    completed_stages,
                    ctx,
                )

            if pipeline_hooks:
                await _fire_hooks("post_stage", pipeline_hooks, ctx)

        if all_passed:
            ctx.event_sink.emit(LoopSuccess(
                key=_loop_ekey,
                name=loop.name,
            ))
            return session_id

    # Retries exhausted
    assert last_result is not None
    ctx.event_sink.emit(LoopExhausted(
        key=_loop_ekey,
        loop_id=f"loop:{loop.name}",
    ))
    if alert_manager:
        await alert_manager.fire(
            AlertMessage(
                event=AlertEvent.RETRIES_EXHAUSTED,
                pipeline_name=pipeline_name,
                stage_name=loop.name,
                detail=last_result.error or "",
            )
        )
    if loop.on_exhaust == OnFailure.FAIL:
        raise RetriesExhaustedError(loop.name, last_result)
    if loop.on_exhaust == OnFailure.ASK_USER:
        if alert_manager:
            await alert_manager.fire(
                AlertMessage(
                    event=AlertEvent.ASK_USER,
                    pipeline_name=pipeline_name,
                    stage_name=loop.name,
                    detail=last_result.error or "",
                )
            )
        ctx.event_sink.emit(WaitingInput(
            key=_loop_ekey,
            kind="failure_recovery",
            prompt_excerpt=last_result.error or "",
        ))
        choice = await ctx.input_responder.ask_failure(loop.name, last_result.error)
        if choice == "a":
            raise RetriesExhaustedError(loop.name, last_result)
        if choice == "r":
            # Re-run the whole loop (a fresh round of attempts), keeping the
            # current agent session so the body still remembers prior context.
            return await _run_loop(
                loop,
                ctx,
                initial_session_id=session_id,
                pipeline_name=pipeline_name,
                alert_manager=alert_manager,
                pipeline_hooks=pipeline_hooks,
                budgets=budgets,
                checkpoint_path=checkpoint_path,
                completed_stages=completed_stages,
                fork_session=False,
                step_mode=step_mode,
            )
    if loop.on_exhaust == OnFailure.DRAFT_PR:
        ctx.event_sink.emit(LoopDraftPR(
            key=_loop_ekey,
            name=loop.name,
        ))
    return session_id


async def _run_parallel(
    parallel: Parallel,
    ctx: PipelineContext,
    *,
    budgets: list[Budget] | None = None,
) -> None:
    """Run all stages in the Parallel block concurrently via asyncio.gather().

    Each stage gets a fresh agent session (session_id=None). All results are
    stored in context. Raises PipelineError on the first failure encountered.
    """
    _par_ekey = EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id, stage_id=f"parallel:{parallel.name}")
    ctx.event_sink.emit(ParallelStarted(
        key=_par_ekey,
        name=parallel.name,
        stage_count=len(parallel.stages),
    ))

    tasks = [
        _run_stage(
            stage, ctx, budgets=budgets,
            node_id=f"parallel:{parallel.name}/stage:{stage.name}",
        )
        for stage in parallel.stages
    ]
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    first_failure: tuple[str, StageResult] | None = None
    for stage, outcome in zip(parallel.stages, outcomes):
        if isinstance(outcome, BaseException):
            # _run_stage shouldn't raise, but guard against it
            err_result = StageResult(name=stage.name, success=False, error=str(outcome))
            ctx.results[stage.name] = err_result
            if first_failure is None:
                first_failure = (stage.name, err_result)
        elif not outcome.success and first_failure is None:
            first_failure = (stage.name, outcome)

    if first_failure:
        name, result = first_failure
        raise PipelineError(name, result)

    ctx.event_sink.emit(ParallelDone(
        key=_par_ekey,
        name=parallel.name,
    ))


def _append_history_snapshot(
    config_path: str,
    ctx: PipelineContext,
    start_time: float,
    failed_stage: str | None,
    *,
    run_id: int,
    in_progress: bool,
) -> None:
    """Append a run snapshot to the JSONL history file."""
    from norn.history import RunRecord, StageHistoryEntry, append_run

    stage_costs: dict[str, float] = {}
    for rec in ctx.usage_tracker.records:
        stage_costs[rec.stage_name] = stage_costs.get(rec.stage_name, 0.0) + rec.total_cost_usd

    stages = [
        StageHistoryEntry(name=name, success=result.success, cost_usd=stage_costs.get(name, 0.0))
        for name, result in ctx.results.items()
    ]

    duration_ms = int((time.monotonic() - start_time) * 1000)

    record = RunRecord(
        run_id=run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        success=failed_stage is None and not in_progress,
        total_cost_usd=ctx.usage_tracker.total_cost_usd,
        total_tokens=ctx.usage_tracker.total_tokens,
        duration_ms=duration_ms,
        stages=stages,
        retries=ctx.retries,
        session_id=ctx.usage_tracker.last_session_id,
        failed_stage=failed_stage,
        in_progress=in_progress,
        stage_log=list(ctx.stage_log),
        agent_provider=ctx.agent_provider,
    )
    try:
        append_run(config_path, record)
    except Exception:
        log.warning("Failed to write run history for %s", config_path)


def _persist_history_snapshot(
    ctx: PipelineContext,
    *,
    failed_stage: str | None = None,
    in_progress: bool = True,
) -> None:
    """Persist the current run snapshot when history tracking is active."""
    config_path = getattr(ctx, "_history_config_path", None)
    run_id = getattr(ctx, "_history_run_id", None)
    start_time = getattr(ctx, "_history_start_time", None)
    if not config_path or run_id is None or start_time is None:
        return
    _append_history_snapshot(
        config_path,
        ctx,
        start_time,
        failed_stage,
        run_id=run_id,
        in_progress=in_progress,
    )


async def run_pipeline(
    pipeline: Pipeline,
    params: dict[str, str] | None = None,
    *,
    resume_session: str | None = None,
    resume_checkpoint: Checkpoint | None = None,
    config_path: str | None = None,
    alert_manager: AlertManager | None = None,
    fork_session: bool = False,
    step_mode: bool = False,
    agent_provider: str = "claude-code",
    event_sink: object | None = None,
    input_responder: object | None = None,
    run_controller: RunController | None = None,
    working_dir: str | None = None,
    run_id: str | None = None,
) -> PipelineContext:
    """Execute a pipeline definition, returning the final context.

    Pass ``resume_session`` to seed the first agent stage with a prior
    session ID, so Claude can continue from where it left off.

    Pass ``resume_checkpoint`` to restore completed stage results and skip
    those stages on this run (showing them as cached in the output).

    Pass ``alert_manager`` to receive notifications on completion, failure,
    retries-exhausted, and ask-user events.

    Pass ``fork_session=True`` to branch the first agent stage off an existing
    session (requires ``resume_session`` to be set). Used internally when
    running isolated sub-pipelines via ``.include(..., isolated=True)``.

    Pass ``working_dir`` to run the pipeline in an isolated directory (e.g. a
    git worktree). Relative stage paths resolve under it; absolute paths are
    unchanged. When ``None``, behavior is identical to today's.

    Pass ``run_id`` to seed ``ctx.run_id`` with a caller-supplied identifier
    (e.g. a worktree branch name correlator). When omitted a UUID is generated
    as before.
    """
    ctx = PipelineContext(params=dict(params or {}))
    ctx.agent_provider = agent_provider
    ctx.working_dir = working_dir
    if event_sink is not None:
        ctx.event_sink = event_sink
    else:
        # Wire the default CLI renderer as event subscriber so console
        # output flows through the event seam instead of inline prints.
        from norn.cli_render import CLIRenderer
        from norn.event_sink import EventSink

        _renderer = CLIRenderer(budgets=pipeline.budgets or None)
        ctx.event_sink = EventSink(on_event=_renderer)
    if input_responder is not None:
        ctx.input_responder = input_responder
    if run_controller is not None:
        ctx.run_controller = run_controller
    ctx.run_id = run_id if run_id is not None else str(uuid.uuid4())
    ctx.unit_id = "unit-0"
    ctx.event_sink.emit(RunStarted(
        key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id),
        pipeline_name=pipeline.name,
        provider=agent_provider,
        # Passed when the user runs with --continue; the CLI renderer prints
        # "Resuming session <id>" so the user sees which session is continuing.
        resume_session=resume_session,
    ))
    ctx.event_sink.emit(UnitStarted(
        key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id),
        model=ctx.params.get("default_model") or pipeline.default_model,
    ))
    if pipeline.default_model:
        ctx.params.setdefault("default_model", pipeline.default_model)
    start_time = time.monotonic()
    if config_path:
        from norn.history import next_run_id

        ctx._history_config_path = config_path
        ctx._history_run_id = next_run_id(config_path)
        ctx._history_start_time = start_time
    if ctx.params:
        log.debug("Pipeline params: %s", ctx.params)

    # Resolve pipeline-level env vars and secrets before running any stages
    if pipeline.env_vars or pipeline.secret_specs:
        from norn.secrets import resolve_secret

        ctx.env = dict(pipeline.env_vars)
        for spec in pipeline.secret_specs:
            ctx.secrets[spec.name] = resolve_secret(spec.name, spec.source)
        if ctx.secrets:
            ui.register_secrets(ctx.secrets.values())
            log.debug("[secrets] Resolved %d secret(s)", len(ctx.secrets))

    if pipeline.contexts:
        ctx.injected_context = await _resolve_contexts(pipeline.contexts, working_dir=ctx.working_dir)
        total_chars = sum(len(content) for _, content in ctx.injected_context)
        log.info(
            "[context] Loaded %d context(s), ~%d chars (~%d tokens)",
            len(ctx.injected_context),
            total_chars,
            total_chars // 4,
        )

    if pipeline.pipeline_skills:
        ctx.pipeline_skills = list(pipeline.pipeline_skills)
        log.debug("[skills] Pipeline-level skills: %s", [s if isinstance(s, str) else s.name for s in ctx.pipeline_skills])

    if pipeline._session_profile:
        ctx.pipeline_profile = pipeline._session_profile
        log.debug("[profile] Pipeline-level session profile: %s", pipeline._session_profile.name)

    current_session: str | None = resume_session
    budgets = pipeline.budgets or None
    completed_stages: list[str] = []
    _fork_pending = fork_session

    # Expand inline includes before running — they share context and session
    items = _expand_inline_includes(pipeline.items, ctx.params)

    # Restore state from a prior checkpoint
    if resume_checkpoint:
        # A loop is atomic: its body stages are only valid as cache when ALL
        # of them passed in the same iteration. If only a subset is present,
        # the loop crashed mid-attempt — drop those partial entries so the
        # loop replays cleanly with fresh outputs (otherwise downstream
        # stages like ``fix`` would replay against the *original* failure
        # rather than the current one).
        restored = list(resume_checkpoint.completed_stages)
        restored_set = set(restored)
        dropped: set[str] = set()
        for item in items:
            if isinstance(item, Loop):
                body_names = {s.name for s in item.stages}
                in_cache = body_names & restored_set
                if in_cache and not body_names.issubset(restored_set):
                    dropped.update(in_cache)
        if dropped:
            log.info(
                "[resume] dropping %d partial-loop cache entries: %s",
                len(dropped),
                ", ".join(sorted(dropped)),
            )
            restored = [n for n in restored if n not in dropped]
            restored_set -= dropped

        for name in restored:
            ctx.results[name] = StageResult(
                name=name,
                success=True,
                output=resume_checkpoint.results.get(name),
            )
        ctx.checkpoint_stages = restored_set
        completed_stages = list(restored)
        if current_session is None:
            current_session = resume_checkpoint.session_id

    _persist_history_snapshot(ctx)

    _failed_stage: str | None = None
    _cancelled = False
    _clear_count = 0  # position counter for top-level clear-context markers
    try:
        for item in items:
            # Cooperative pause/cancel check between items
            await _cooperative_pause(ctx)

            if isinstance(item, ClearContext):
                # Carry the graph node id (clear:<N>, matching build_graph) so
                # the TUI can mark the clear-context node as done.
                ctx.event_sink.emit(ClearContextNotice(
                    key=EventKey(
                        run_id=ctx.run_id, unit_id=ctx.unit_id,
                        stage_id=f"clear:{_clear_count}",
                    ),
                ))
                _clear_count += 1
                if current_session:
                    log.debug("[clear_context] Session %s discarded", current_session)
                current_session = None
                continue

            # Top-level path. Every `stage:<name>` id built in this branch is
            # correct: `run_pipeline` iterates the pipeline's own items, so there
            # is no parent prefix to qualify them with. Nested stages are a
            # different story — Loop/Parallel/Include bodies must use the
            # pre-computed `stage_node_id` handed down by their runner (see
            # `_run_loop`) rather than rebuilding the string, which is the bug
            # step 4 fixed. Do not copy this pattern into a nested path.
            if isinstance(item, Stage):
                if _is_cached(item, ctx):
                    _append_stage_log(ctx, stage_name=item.name, status="cached", success=True)
                    ctx.event_sink.emit(StageFinished(
                        key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id, stage_id=f"stage:{item.name}"),
                        name=item.name, status="cached", success=True,
                    ))
                    continue

                if _is_skipped(item, ctx):
                    ctx.results[item.name] = StageResult(name=item.name, success=True)
                    _append_stage_log(ctx, stage_name=item.name, status="skipped", success=True)
                    ctx.event_sink.emit(StageFinished(
                        key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id, stage_id=f"stage:{item.name}"),
                        name=item.name, status="skipped", success=True,
                    ))
                    continue

                if item.when is not None and not item.when(ctx):
                    ctx.results[item.name] = StageResult(name=item.name, success=True)
                    _append_stage_log(
                        ctx,
                        stage_name=item.name,
                        status="skipped_condition",
                        success=True,
                    )
                    ctx.event_sink.emit(StageFinished(
                        key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id, stage_id=f"stage:{item.name}"),
                        name=item.name, status="skipped_condition", success=True,
                    ))
                    continue

                if step_mode:
                    _step_ekey = EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id, stage_id=f"stage:{item.name}")
                    ctx.event_sink.emit(WaitingInput(key=_step_ekey, kind="step"))
                    action = await ctx.input_responder.ask_step(item, ctx, session_id=current_session)
                    if action == "s":
                        ctx.results[item.name] = StageResult(name=item.name, success=True)
                        _append_stage_log(ctx, stage_name=item.name, status="skipped", success=True)
                        ctx.event_sink.emit(StageFinished(
                            key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id, stage_id=f"stage:{item.name}"),
                            name=item.name, status="skipped", success=True,
                        ))
                        continue
                    if action == "a":
                        raise PipelineError(
                            item.name,
                            StageResult(name=item.name, success=False, error="Aborted by user"),
                        )

                if pipeline.hooks:
                    await _fire_hooks("pre_stage", pipeline.hooks, ctx)
                # attempt tracks how many times this stage has been tried so
                # that each run gets a distinct EventKey — matching how _run_loop
                # passes attempt= so retry keys never collide with first-run keys.
                attempt = 1
                result = await _run_stage(
                    item, ctx, session_id=current_session, budgets=budgets,
                    fork_session=_fork_pending, attempt=attempt,
                )
                if result.usage and result.usage.session_id:
                    if _fork_pending:
                        _fork_pending = False
                    current_session = result.usage.session_id
                # Failure handling with optional user-driven retry: on a
                # "retry" decision re-run the stage and re-evaluate; on
                # "continue" proceed past it; abort/FAIL raise inside
                # _handle_failure.
                while not result.success:
                    if pipeline.hooks:
                        await _fire_hooks("on_failure", pipeline.hooks, ctx)
                    decision = await _handle_failure(
                        item.on_failure,
                        item.name,
                        result,
                        ctx=ctx,
                        pipeline_name=pipeline.name,
                        alert_manager=alert_manager,
                    )
                    if decision != "retry":
                        break
                    attempt += 1
                    if pipeline.hooks:
                        await _fire_hooks("pre_stage", pipeline.hooks, ctx)
                    result = await _run_stage(
                        item, ctx, session_id=current_session, budgets=budgets,
                        fork_session=_fork_pending, attempt=attempt,
                    )
                    if result.usage and result.usage.session_id:
                        if _fork_pending:
                            _fork_pending = False
                        current_session = result.usage.session_id

                if result.success:
                    # Save checkpoint after each successful stage
                    if config_path:
                        if item.name not in completed_stages:
                            completed_stages.append(item.name)
                        _save_checkpoint_state(
                            config_path,
                            pipeline.name,
                            current_session,
                            completed_stages,
                            ctx,
                        )
                    if pipeline.hooks:
                        await _fire_hooks("post_stage", pipeline.hooks, ctx)

            elif isinstance(item, Loop):
                loop_initial_session = None if item.new_session else current_session
                coro = _run_loop(
                    item,
                    ctx,
                    initial_session_id=loop_initial_session,
                    pipeline_name=pipeline.name,
                    alert_manager=alert_manager,
                    pipeline_hooks=pipeline.hooks or None,
                    budgets=budgets,
                    checkpoint_path=config_path,
                    completed_stages=completed_stages,
                    fork_session=_fork_pending,
                    step_mode=step_mode,
                )
                try:
                    if item.timeout is not None:
                        loop_session = await asyncio.wait_for(coro, timeout=item.timeout)
                    else:
                        loop_session = await coro
                except asyncio.TimeoutError:
                    raise PipelineError(
                        item.name,
                        StageResult(name=item.name, success=False, error=f"Timed out after {item.timeout}s"),
                    )
                if _fork_pending:
                    _fork_pending = False
                if loop_session:
                    current_session = loop_session

            elif isinstance(item, Parallel):
                await _run_parallel(item, ctx, budgets=budgets)

            elif isinstance(item, Include):
                # isolated=True: run in a fresh context with a forked agent session
                sub = load_pipeline(item.path)
                sub_params = {**ctx.params, **item.args}
                cost_offset = ctx.usage_tracker.total_cost_usd
                token_offset = ctx.usage_tracker.total_tokens
                ctx.event_sink.emit(IncludeStarted(
                    key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id),
                    path=item.path,
                    isolated=True,
                ))
                sub_ctx = await run_pipeline(
                    sub,
                    params=sub_params,
                    resume_session=current_session,
                    fork_session=True,
                    working_dir=ctx.working_dir,
                    run_id=ctx.run_id,
                    # Frontend seams are owned by the caller and shared with
                    # the sub-pipeline: events flow into the same sink so the
                    # TUI sees them, pause/cancel propagates through the same
                    # controller, and ask_user prompts reach the correct
                    # responder.  Results, session, and usage tracking stay
                    # isolated in the fresh sub-context by design.
                    # NOTE: the sub-run emits its own RunStarted/RunFinished
                    # pair into the shared sink (same run_id, unit_id="unit-0").
                    # Making isolated includes omit those envelope events is a
                    # separate change.
                    event_sink=ctx.event_sink,
                    input_responder=ctx.input_responder,
                    run_controller=ctx.run_controller,
                )
                ctx.event_sink.emit(IncludeDone(
                    key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id),
                    path=item.path,
                ))
                # Merge usage records into parent tracker
                for record in sub_ctx.usage_tracker.records:
                    ctx.usage_tracker.add(record)
                for entry in sub_ctx.stage_log:
                    ctx.stage_log.append(
                        replace(
                            entry,
                            running_total_cost_usd=entry.running_total_cost_usd + cost_offset,
                            running_total_tokens=entry.running_total_tokens + token_offset,
                        )
                    )
                _persist_history_snapshot(ctx)
                # Copy requested stage outputs to parent context
                for name in item.outputs:
                    if name in sub_ctx.results:
                        ctx.results[name] = sub_ctx.results[name]

    except CancelledError as exc:
        _failed_stage = "cancelled"
        _cancelled = True
        ctx.event_sink.emit(RunCancelled(
            key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id),
        ))
        raise
    except Exception as exc:
        _failed_stage = getattr(exc, "stage_name", None) or type(exc).__name__
        ctx.event_sink.emit(RunError(
            key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id),
            error_kind=type(exc).__name__,
            detail=str(exc),
        ))
        if alert_manager:
            await alert_manager.fire(
                AlertMessage(
                    event=AlertEvent.FAILED,
                    pipeline_name=pipeline.name,
                    stage_name=getattr(exc, "stage_name", None),
                    detail=str(exc),
                )
            )
        raise
    finally:
        _persist_history_snapshot(ctx, failed_stage=_failed_stage, in_progress=False)
        ctx.event_sink.emit(RunFinished(
            key=EventKey(run_id=ctx.run_id, unit_id=ctx.unit_id),
            success=_failed_stage is None,
        ))

    ui.print_usage_report(pipeline.name, ctx.usage_tracker, config_path)
    if alert_manager:
        await alert_manager.fire(
            AlertMessage(event=AlertEvent.COMPLETE, pipeline_name=pipeline.name)
        )
    return ctx
