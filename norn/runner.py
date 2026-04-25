from __future__ import annotations

import asyncio
import glob as glob_module
import logging
import pathlib
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from norn.alerts import AlertEvent, AlertManager, AlertMessage
from norn.checkpoint import Checkpoint, save_checkpoint, serialise_output
from norn.dsl import Budget, ClearContext, ContextSpec, Include, Loop, OnFailure, Parallel, Pipeline, PipelineItem, Stage
from norn.loader import load_pipeline
from norn.models import PipelineContext, StageLogEntry, StageResult, UsageTracker
from norn import ui

if TYPE_CHECKING:
    from norn.stages.base import BaseStage


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


async def _resolve_contexts(specs: list[ContextSpec]) -> list[tuple[str, str]]:
    """Resolve context specs to ``(label, content)`` pairs.

    Raises ``RuntimeError`` if a file glob matches nothing or a command fails.
    """
    resolved: list[tuple[str, str]] = []
    for spec in specs:
        if spec.kind == "file":
            paths = sorted(glob_module.glob(spec.source, recursive=True))
            if not paths:
                raise RuntimeError(f"Context spec matched no files: {spec.source!r}")
            content = "\n\n".join(pathlib.Path(p).read_text() for p in paths)
            resolved.append((spec.label, content))
        elif spec.kind == "cmd":
            proc = await asyncio.create_subprocess_shell(
                spec.source,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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


async def _check_budget(budgets: list[Budget], tracker: UsageTracker) -> None:
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
            choice = ui.ask_budget_exceeded(tracker, b)
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
) -> StageResult:
    """Run a single stage and store the result in context."""
    start = time.monotonic()

    if stage.impl.needs_agent:
        log.debug(
            "[%s] Agent stage — session=%s attempt=%d fork=%s",
            stage.name, session_id, attempt, fork_session,
        )
        agent_kwargs: dict = {"session_id": session_id, "attempt": attempt, "fork_session": fork_session}
        mcp_tools = getattr(stage.impl, "mcp_tools", None)
        if isinstance(mcp_tools, list) and mcp_tools:
            from claude_agent_sdk import create_sdk_mcp_server
            mcp_server = create_sdk_mcp_server(stage.name, tools=mcp_tools)
            agent_kwargs["mcp_servers"] = {stage.name: mcp_server}
            log.debug("[%s] Attached MCP server with %d tool(s)", stage.name, len(mcp_tools))
        coro = stage.impl.run(ctx, **agent_kwargs)
    else:
        coro = stage.impl.run(ctx)

    try:
        if stage.timeout is not None:
            result = await asyncio.wait_for(coro, timeout=stage.timeout)
        else:
            result = await coro
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
        ui.print_stage_failure(stage.name, elapsed, result)
        ui.print_running_total(ctx.usage_tracker, budgets)
        return result

    elapsed = time.monotonic() - start
    result.name = stage.name
    if result.usage:
        result.usage.stage_name = stage.name
        ctx.usage_tracker.add(result.usage)
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

    if result.success:
        ui.print_stage_success(stage.name, elapsed, result)
    else:
        ui.print_stage_failure(stage.name, elapsed, result)

    ui.print_running_total(ctx.usage_tracker, budgets)

    if budgets and result.usage:
        await _check_budget(budgets, ctx.usage_tracker)

    return result


async def _handle_failure(
    on_failure: OnFailure,
    name: str,
    result: StageResult,
    *,
    pipeline_name: str = "",
    alert_manager: AlertManager | None = None,
) -> None:
    """Handle a stage failure according to the configured policy."""
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
        choice = ui.ask_user_continue(name, result.error)
        if choice == "a":
            raise PipelineError(name, result)


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
    save_checkpoint(config_path, pipeline_name, session_id, completed_stages, stage_outputs)


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
    Claude remembers prior errors when retrying.

    Returns the final session_id used (if any), so the caller can persist it.
    """
    last_result: StageResult | None = None
    session_id: str | None = initial_session_id
    _fork_pending = fork_session

    for attempt in range(1, loop.max_retries + 1):
        if attempt > 1:
            ctx.retries += 1
            if pipeline_hooks:
                await _fire_hooks("on_retry", pipeline_hooks, ctx)
        ui.print_loop_attempt(loop.name, attempt, loop.max_retries)
        all_passed = True

        for stage in loop.stages:
            if _is_cached(stage, ctx):
                ui.print_stage_cached(stage.name)
                _append_stage_log(
                    ctx,
                    stage_name=stage.name,
                    status="cached",
                    success=True,
                    attempt=attempt,
                )
                continue

            if _is_skipped(stage, ctx):
                ui.print_stage_skipped(stage.name)
                ctx.results[stage.name] = StageResult(name=stage.name, success=True)
                _append_stage_log(
                    ctx,
                    stage_name=stage.name,
                    status="skipped",
                    success=True,
                    attempt=attempt,
                )
                continue

            if stage.when is not None and not stage.when(ctx):
                ui.print_stage_skipped_condition(stage.name)
                ctx.results[stage.name] = StageResult(name=stage.name, success=True)
                _append_stage_log(
                    ctx,
                    stage_name=stage.name,
                    status="skipped_condition",
                    success=True,
                    attempt=attempt,
                )
                continue

            if step_mode:
                action = ui.step_prompt(stage, ctx, session_id=session_id)
                if action == "s":
                    ui.print_stage_skipped(stage.name)
                    ctx.results[stage.name] = StageResult(name=stage.name, success=True)
                    _append_stage_log(
                        ctx,
                        stage_name=stage.name,
                        status="skipped",
                        success=True,
                        attempt=attempt,
                    )
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
                fork_session=_fork_pending,
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
            ui.print_loop_success(loop.name)
            return session_id

    # Retries exhausted
    assert last_result is not None
    ui.print_loop_exhausted(loop.name, loop.max_retries)
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
        choice = ui.ask_user_continue(loop.name, last_result.error)
        if choice == "a":
            raise RetriesExhaustedError(loop.name, last_result)
    if loop.on_exhaust == OnFailure.DRAFT_PR:
        ui.print_loop_draft_pr(loop.name)
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
    ui.print_parallel_start(parallel.name, len(parallel.stages))

    tasks = [_run_stage(stage, ctx, budgets=budgets) for stage in parallel.stages]
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

    ui.print_parallel_done(parallel.name)


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
    """
    ui.print_pipeline_start(pipeline.name, resume_session)
    ctx = PipelineContext(params=dict(params or {}))
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
        ctx.injected_context = await _resolve_contexts(pipeline.contexts)
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
        for name in resume_checkpoint.completed_stages:
            ctx.results[name] = StageResult(
                name=name,
                success=True,
                output=resume_checkpoint.results.get(name),
            )
        ctx.checkpoint_stages = set(resume_checkpoint.completed_stages)
        completed_stages = list(resume_checkpoint.completed_stages)
        if current_session is None:
            current_session = resume_checkpoint.session_id

    _persist_history_snapshot(ctx)

    _failed_stage: str | None = None
    try:
        for item in items:
            if isinstance(item, ClearContext):
                ui.print_clear_context()
                if current_session:
                    log.debug("[clear_context] Session %s discarded", current_session)
                current_session = None
                continue

            if isinstance(item, Stage):
                if _is_cached(item, ctx):
                    ui.print_stage_cached(item.name)
                    _append_stage_log(ctx, stage_name=item.name, status="cached", success=True)
                    continue

                if _is_skipped(item, ctx):
                    ui.print_stage_skipped(item.name)
                    ctx.results[item.name] = StageResult(name=item.name, success=True)
                    _append_stage_log(ctx, stage_name=item.name, status="skipped", success=True)
                    continue

                if item.when is not None and not item.when(ctx):
                    ui.print_stage_skipped_condition(item.name)
                    ctx.results[item.name] = StageResult(name=item.name, success=True)
                    _append_stage_log(
                        ctx,
                        stage_name=item.name,
                        status="skipped_condition",
                        success=True,
                    )
                    continue

                if step_mode:
                    action = ui.step_prompt(item, ctx, session_id=current_session)
                    if action == "s":
                        ui.print_stage_skipped(item.name)
                        ctx.results[item.name] = StageResult(name=item.name, success=True)
                        _append_stage_log(ctx, stage_name=item.name, status="skipped", success=True)
                        continue
                    if action == "a":
                        raise PipelineError(
                            item.name,
                            StageResult(name=item.name, success=False, error="Aborted by user"),
                        )

                if pipeline.hooks:
                    await _fire_hooks("pre_stage", pipeline.hooks, ctx)
                result = await _run_stage(
                    item, ctx, session_id=current_session, budgets=budgets, fork_session=_fork_pending
                )
                if result.usage and result.usage.session_id:
                    if _fork_pending:
                        _fork_pending = False
                    current_session = result.usage.session_id
                if not result.success:
                    if pipeline.hooks:
                        await _fire_hooks("on_failure", pipeline.hooks, ctx)
                    await _handle_failure(
                        item.on_failure,
                        item.name,
                        result,
                        pipeline_name=pipeline.name,
                        alert_manager=alert_manager,
                    )
                else:
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
                ui.print_include_start(item.path, isolated=True)
                sub_ctx = await run_pipeline(
                    sub,
                    params=sub_params,
                    resume_session=current_session,
                    fork_session=True,
                )
                ui.print_include_done(item.path)
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

    except Exception as exc:
        _failed_stage = getattr(exc, "stage_name", None) or type(exc).__name__
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

    ui.print_usage_report(pipeline.name, ctx.usage_tracker, config_path)
    if alert_manager:
        await alert_manager.fire(
            AlertMessage(event=AlertEvent.COMPLETE, pipeline_name=pipeline.name)
        )
    return ctx
