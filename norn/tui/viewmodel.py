"""Textual-free RunViewModel projected from run-events.

Projects the run-event stream into render-ready state.  This module is
**pure stdlib + norn core** — no ``import textual`` anywhere here.  The
widget layer holds a reference to a ``RunViewModel`` and reads its
attributes; all mutation goes through :meth:`RunViewModel.apply`.

Usage::

    vm = RunViewModel()
    for event in event_stream:
        vm.apply(event)
    print(vm.header.pipeline_name, vm.header.total_input_tokens)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Per-stage detail record
# ---------------------------------------------------------------------------


@dataclass
class StageDetailRecord:
    """Detail snapshot for a completed stage, populated from ``StageFinished``.

    Attributes:
        name:                 Human-readable stage name.
        status:               Terminal status string (passed/failed/skipped/cached).
        duration_ms:          Wall-clock duration reported by the runner.
        artifacts:            Artifact paths produced by the stage.
        error:                Error text if the stage failed, else ``None``.
        usage_input_tokens:   Input tokens consumed by this stage.
        usage_output_tokens:  Output tokens produced by this stage.
        usage_cost_usd:       Cost in USD for this stage.
        attempts:             Number of attempts (loop retries + 1).
    """

    name: str
    status: str = "pending"
    duration_ms: int = 0
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    usage_cost_usd: float = 0.0
    attempts: int = 1


# ---------------------------------------------------------------------------
# Render-ready header summary
# ---------------------------------------------------------------------------


@dataclass
class HeaderSummary:
    """Render-ready summary of the current run for display in the TUI header.

    Attributes:
        pipeline_name: Name of the executing pipeline.
        run_id:        Unique run identifier from the first ``RunStarted`` event.
        provider:      Agent provider name (e.g. ``"claude-code"``).
        elapsed_s:     Seconds since run start (updated on ``RunFinished``).
        total_input_tokens: Authoritative run-wide cumulative input tokens,
            sourced from the most recent ``UsageUpdated`` event.
        total_output_tokens: Cumulative output tokens (same source).
        total_cost_usd: Cumulative cost in USD (same source).
        stages_done:    Number of ``StageFinished`` events received (includes
            cached and skipped stages that emit no ``StageStarted``).
        stages_started: Number of stages for which a ``StageStarted`` OR a
            bare ``StageFinished`` (cached/skipped) was received — always
            >= ``stages_done``; the two are equal when the full pipeline
            completes so the header reads ``N/N``.
        status:         Overall run status — ``"pending"`` → ``"running"`` →
                        ``"passed"`` or ``"failed"``.
    """

    pipeline_name: str = ""
    run_id: str = ""
    provider: str = ""
    elapsed_s: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    stages_done: int = 0
    stages_started: int = 0
    status: str = "pending"


# ---------------------------------------------------------------------------
# RunViewModel
# ---------------------------------------------------------------------------


class RunViewModel:
    """Projects a run-event stream into render-ready state.

    Feed every event from the ``EventSink`` to :meth:`apply`.  The ViewModel
    then exposes:

    * :attr:`header` — a :class:`HeaderSummary` with pipeline-level metadata.
    * :attr:`node_status` — ``{node_id: status}`` for every node seen so far
      (``"pending"`` for unseen nodes, per :class:`~norn.graph.PipelineNode`
      semantics).
    * :attr:`transcript` — ``{stage_id: [AgentMessageBlock, …]}`` — ordered
      transcript blocks per stage, already redacted by the sink.
    * :attr:`total_input_tokens`, :attr:`total_output_tokens`,
      :attr:`total_cost_usd` — cumulative usage totals.
    * :attr:`waiting_input` — the most recent :class:`~norn.events.WaitingInput`
      event (or ``None`` if not waiting).
    * :attr:`last_error` — the most recent :class:`~norn.events.RunError`
      event (or ``None``).
    """

    def __init__(self) -> None:
        self.header: HeaderSummary = HeaderSummary()
        # node_id -> status string: pending|running|passed|failed|skipped|cached|retrying
        self.node_status: dict[str, str] = {}
        # stage_id -> ordered list of AgentMessageBlock objects
        self.transcript: dict[str, list[Any]] = {}
        # stage_id -> StageDetailRecord (populated on StageFinished)
        self.stage_details: dict[str, StageDetailRecord] = {}
        # Authoritative run-wide cumulative totals — set exclusively from
        # UsageUpdated (which carries tracker totals: all stages so far).
        # StageFinished.usage_* is per-stage only and goes into StageDetailRecord.
        self._run_input_tokens: int = 0
        self._run_output_tokens: int = 0
        self._run_cost_usd: float = 0.0
        # Current active stage_id (for clearing on StageFinished)
        self._active_stage_id: str | None = None
        # Stage ids seen in StageStarted — used to detect cached/skipped stages
        # that emit StageFinished with no preceding StageStarted (runner.py
        # emits bare StageFinished for cached, skipped, and condition-skipped
        # stages) so stages_started stays >= stages_done at all times.
        self._seen_stage_ids: set[str] = set()
        # Track attempts per stage_id (set from StageStarted.attempt)
        self._stage_attempts: dict[str, int] = {}
        # Blocking-input and error state
        self.waiting_input: Any | None = None
        self.last_error: Any | None = None
        self._start_time: float | None = None
        # Tree root node id (pipeline:<name>), set on RunStarted.
        self._root_node_id: str | None = None

    # ------------------------------------------------------------------
    # Public totals (authoritative run-wide from UsageUpdated)
    # ------------------------------------------------------------------

    @property
    def total_input_tokens(self) -> int:
        """Total input tokens: authoritative run-wide cumulative from UsageUpdated."""
        return self._run_input_tokens

    @property
    def total_output_tokens(self) -> int:
        """Total output tokens: authoritative run-wide cumulative from UsageUpdated."""
        return self._run_output_tokens

    @property
    def total_cost_usd(self) -> float:
        """Total cost: authoritative run-wide cumulative from UsageUpdated."""
        return self._run_cost_usd

    @property
    def elapsed_s(self) -> float:
        """Seconds since the run started (0.0 if not yet started)."""
        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    def apply(self, event: Any) -> None:
        """Consume *event* and update ViewModel state.

        Unknown event types are silently ignored so that adding new event
        types to :mod:`norn.events` does not break existing ViewModels.
        """
        handler = _HANDLERS.get(type(event).__name__)
        if handler is not None:
            handler(self, event)

    # ------------------------------------------------------------------
    # Private handlers (called from module-level dispatch table)
    # ------------------------------------------------------------------

    def _on_run_started(self, event: Any) -> None:
        self._start_time = time.monotonic()
        self.header.pipeline_name = event.pipeline_name
        self.header.run_id = event.key.run_id
        self.header.provider = event.provider
        self.header.status = "running"
        # The Tree root node id is pipeline:<name>; reflect run status on it.
        self._root_node_id = f"pipeline:{event.pipeline_name}"
        self.node_status[self._root_node_id] = "running"

    def _on_unit_started(self, event: Any) -> None:
        # Nothing to project for read-only single-unit step
        pass

    def _on_stage_started(self, event: Any) -> None:
        stage_id = event.key.stage_id
        if stage_id:
            self.node_status[stage_id] = "running"
            self._active_stage_id = stage_id
            self._stage_attempts[stage_id] = event.attempt
            # Record that this stage went through StageStarted so that
            # _on_stage_finished can distinguish normal stages from cached/skipped
            # ones (which emit StageFinished with no preceding StageStarted).
            self._seen_stage_ids.add(stage_id)
        self.header.stages_started += 1
        self._update_header_usage()

    def _on_turn_event(self, event: Any) -> None:
        stage_id = event.key.stage_id
        if not stage_id:
            return
        if stage_id not in self.transcript:
            self.transcript[stage_id] = []
        agent_evt = event.event
        if agent_evt.block is not None:
            self.transcript[stage_id].append(agent_evt.block)
        elif agent_evt.text is not None:
            from norn.agents.base import TextBlock  # noqa: PLC0415

            self.transcript[stage_id].append(TextBlock(text=agent_evt.text))

    def _on_command_output(self, event: Any) -> None:
        """Append streamed command output to the stage's transcript.

        Stored as a ``TextBlock`` so the transcript renders shell output and
        agent prose through the same path.  The text is already redacted by
        the sink.
        """
        stage_id = event.key.stage_id
        if not stage_id:
            return
        from norn.agents.base import TextBlock  # noqa: PLC0415

        self.transcript.setdefault(stage_id, []).append(TextBlock(text=event.text))

    def _on_usage_updated(self, event: Any) -> None:
        # COALESCIBLE — latest-wins.  UsageUpdated carries the tracker's
        # run-wide CUMULATIVE totals (all stages completed so far, including
        # the current one); store them directly as the authoritative run totals.
        # Do NOT add them on top of anything.  StageFinished.usage_* is the
        # per-stage slice and lives only in StageDetailRecord.
        self._run_input_tokens = event.input_tokens
        self._run_output_tokens = event.output_tokens
        self._run_cost_usd = event.total_cost_usd
        self._update_header_usage()

    def _on_stage_finished(self, event: Any) -> None:
        stage_id = event.key.stage_id
        if stage_id:
            self.node_status[stage_id] = event.status
            if stage_id == self._active_stage_id:
                self._active_stage_id = None
            # event.usage_* is the per-stage figure — goes into the detail
            # record only.  Run-wide totals come from UsageUpdated exclusively.
            self.stage_details[stage_id] = StageDetailRecord(
                name=event.name,
                status=event.status,
                duration_ms=event.duration_ms,
                artifacts=list(event.artifacts),
                error=event.error,
                usage_input_tokens=event.usage_input_tokens,
                usage_output_tokens=event.usage_output_tokens,
                usage_cost_usd=event.usage_cost_usd,
                attempts=self._stage_attempts.get(stage_id, 1),
            )
        # Cached, skipped, and condition-skipped stages emit StageFinished with
        # no preceding StageStarted (runner.py emits bare StageFinished for those
        # paths).  Keep the pair consistent so stages_done never exceeds
        # stages_started and a fully-cached resume reads N/N.
        if not stage_id or stage_id not in self._seen_stage_ids:
            self.header.stages_started += 1
        self.header.stages_done += 1
        self._update_header_usage()

    def _on_stage_retrying(self, event: Any) -> None:
        stage_id = event.key.stage_id
        if stage_id:
            self.node_status[stage_id] = "retrying"

    def _on_loop_attempt(self, event: Any) -> None:
        # Mark the loop container node running while the body executes.
        # Skip if it is already retrying so the retry glyph survives the attempt.
        loop_id = event.key.stage_id
        if loop_id and self.node_status.get(loop_id) != "retrying":
            self.node_status[loop_id] = "running"

    def _on_loop_success(self, event: Any) -> None:
        loop_id = event.key.stage_id
        if loop_id:
            self.node_status[loop_id] = "passed"

    def _on_parallel_started(self, event: Any) -> None:
        par_id = event.key.stage_id
        if par_id:
            self.node_status[par_id] = "running"

    def _on_parallel_done(self, event: Any) -> None:
        par_id = event.key.stage_id
        if par_id:
            self.node_status[par_id] = "passed"

    def _on_clear_context(self, event: Any) -> None:
        clear_id = event.key.stage_id
        if clear_id:
            self.node_status[clear_id] = "passed"

    def _on_waiting_input(self, event: Any) -> None:
        self.waiting_input = event

    def _on_loop_exhausted(self, event: Any) -> None:
        # Mark the loop node as failed when retries are exhausted
        loop_id = event.loop_id
        if loop_id and loop_id in self.node_status:
            self.node_status[loop_id] = "failed"

    def _on_run_error(self, event: Any) -> None:
        self.last_error = event

    def _on_run_paused(self, event: Any) -> None:
        self.header.status = "paused"

    def _on_run_resumed(self, event: Any) -> None:
        self.header.status = "running"

    def _on_run_cancelled(self, event: Any) -> None:
        self.header.status = "cancelled"
        self.header.elapsed_s = self.elapsed_s
        if self._root_node_id:
            self.node_status[self._root_node_id] = "cancelled"
        self._update_header_usage()

    def _on_run_finished(self, event: Any) -> None:
        self.header.status = "passed" if event.success else "failed"
        self.header.elapsed_s = self.elapsed_s
        if self._root_node_id:
            self.node_status[self._root_node_id] = "passed" if event.success else "failed"
        self._update_header_usage()

    def _update_header_usage(self) -> None:
        """Sync header usage fields from the computed properties."""
        self.header.total_input_tokens = self.total_input_tokens
        self.header.total_output_tokens = self.total_output_tokens
        self.header.total_cost_usd = self.total_cost_usd


# ---------------------------------------------------------------------------
# Module-level dispatch table (avoids per-call attribute lookups on class)
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, Any] = {
    "RunStarted": RunViewModel._on_run_started,
    "UnitStarted": RunViewModel._on_unit_started,
    "StageStarted": RunViewModel._on_stage_started,
    "TurnEvent": RunViewModel._on_turn_event,
    "CommandOutput": RunViewModel._on_command_output,
    "UsageUpdated": RunViewModel._on_usage_updated,
    "StageFinished": RunViewModel._on_stage_finished,
    "StageRetrying": RunViewModel._on_stage_retrying,
    "WaitingInput": RunViewModel._on_waiting_input,
    "LoopAttempt": RunViewModel._on_loop_attempt,
    "LoopSuccess": RunViewModel._on_loop_success,
    "LoopExhausted": RunViewModel._on_loop_exhausted,
    "ParallelStarted": RunViewModel._on_parallel_started,
    "ParallelDone": RunViewModel._on_parallel_done,
    "ClearContextNotice": RunViewModel._on_clear_context,
    "RunError": RunViewModel._on_run_error,
    "RunPaused": RunViewModel._on_run_paused,
    "RunResumed": RunViewModel._on_run_resumed,
    "RunCancelled": RunViewModel._on_run_cancelled,
    "RunFinished": RunViewModel._on_run_finished,
    # No-op events (lifecycle notes — nothing to project)
    "LoopDraftPR": None,
    "IncludeStarted": None,
    "IncludeDone": None,
    "CallingAgent": None,
    "GotReply": None,
    "UnitMerged": None,
}
