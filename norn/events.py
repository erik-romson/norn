"""Typed run-event model for the runner → UI event seam.

Every event carries an :class:`EventKey` for ordering and a :class:`Delivery`
classification that tells the :class:`~norn.event_sink.EventSink` how to
handle backpressure.  Events wrap **norn's own types** (``AgentEvent``,
``UsageRecord``, etc.) — never raw SDK types.

The module is pure data — no Textual, no SDK imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from norn.agents.base import AgentEvent
    from norn.agents.capabilities import AgentCapabilities


# ---------------------------------------------------------------------------
# Ordering key
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventKey:
    """Total-order key for a single run-event.

    Every event carries one.  ``stage_id`` is ``None`` for run-level events
    (``RunStarted``, ``RunFinished``).  ``seq`` is a monotonically increasing
    counter **per stage per attempt**, giving each ``TurnEvent`` a unique
    position in the transcript spool.
    """

    run_id: str
    unit_id: str
    stage_id: str | None = None
    attempt: int = 0
    seq: int = 0


# ---------------------------------------------------------------------------
# Delivery classification
# ---------------------------------------------------------------------------


class Delivery(Enum):
    """How the :class:`~norn.event_sink.EventSink` handles an event.

    * ``LOSSLESS`` — lifecycle events that must never be dropped.
    * ``COALESCIBLE`` — counters keyed latest-wins (e.g. usage updates).
    * ``PAGEABLE`` — high-volume transcript appended to a per-stage spool.
    """

    LOSSLESS = auto()
    COALESCIBLE = auto()
    PAGEABLE = auto()


# ---------------------------------------------------------------------------
# Run-event dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunStarted:
    """Emitted once at the start of a pipeline run."""

    key: EventKey
    pipeline_name: str
    provider: str
    budget: Any | None = None
    capabilities: AgentCapabilities | None = None
    # Set when the run continues an existing agent session (``norn run --continue``).
    # The CLI renderer prints "Resuming session <id>" when this is non-None.
    resume_session: str | None = None
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class UnitStarted:
    """Emitted when a unit begins (one implicit ``unit-0`` pre-Ratatosk)."""

    key: EventKey
    model: str | None = None
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class StageStarted:
    """Emitted when a stage begins executing."""

    key: EventKey
    name: str
    attempt: int = 1
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class TurnEvent:
    """Wraps a single :class:`~norn.agents.base.AgentEvent` from the provider.

    High-volume — classified as ``PAGEABLE`` so it never blocks the producer.
    """

    key: EventKey
    event: AgentEvent
    delivery: Delivery = field(default=Delivery.PAGEABLE, repr=False)


@dataclass(frozen=True)
class CommandOutput:
    """A chunk of live stdout/stderr from a shell-command stage.

    Emitted by :class:`~norn.stages.run_command.RunCommand` while the command
    is still running, so the transcript shows build/test output as it happens
    instead of staying blank until the stage ends.

    High-volume — classified as ``PAGEABLE`` so it never blocks the producer.
    The producer batches lines before emitting (see ``RunCommand``), so one
    event usually carries several lines of ``stream`` output.
    """

    key: EventKey
    text: str
    stream: str = "stdout"
    delivery: Delivery = field(default=Delivery.PAGEABLE, repr=False)


@dataclass(frozen=True)
class UsageUpdated:
    """Coalescible usage counter update during a stage."""

    key: EventKey
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0
    delivery: Delivery = field(default=Delivery.COALESCIBLE, repr=False)


@dataclass(frozen=True)
class StageFinished:
    """Emitted when a stage completes (pass or fail)."""

    key: EventKey
    name: str
    status: str  # passed | failed | skipped | cached
    success: bool
    duration_ms: int = 0
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    usage_cost_usd: float = 0.0
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class StageRetrying:
    """Emitted when a loop re-enters a stage."""

    key: EventKey
    next_attempt: int
    reason: str = ""
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class WaitingInput:
    """Emitted when the runner blocks waiting for user input.

    ``kind`` distinguishes agent questions, budget-exceeded, step gates, and
    failure-recovery prompts.
    """

    key: EventKey
    kind: str  # agent | budget | step | failure_recovery
    prompt_excerpt: str = ""
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class LoopExhausted:
    """Emitted when a loop has used all its retries."""

    key: EventKey
    loop_id: str = ""
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class RunError:
    """Emitted on an error that may or may not be terminal."""

    key: EventKey
    error_kind: str  # StageFailed | ProviderError | BudgetExceeded | Timeout | RunFailed | RunCrashed
    detail: str = ""
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class LoopAttempt:
    """Emitted at the start of each loop attempt (including the first)."""

    key: EventKey
    name: str
    attempt: int
    max_retries: int
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class LoopSuccess:
    """Emitted when a loop's body passes on the current attempt."""

    key: EventKey
    name: str
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class LoopDraftPR:
    """Emitted when a loop exhausts retries and continues as draft PR."""

    key: EventKey
    name: str
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class ParallelStarted:
    """Emitted when a Parallel block begins execution."""

    key: EventKey
    name: str
    stage_count: int = 0
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class ParallelDone:
    """Emitted when a Parallel block finishes successfully."""

    key: EventKey
    name: str
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class IncludeStarted:
    """Emitted when an Include block begins."""

    key: EventKey
    path: str
    isolated: bool = True
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class IncludeDone:
    """Emitted when an Include block finishes."""

    key: EventKey
    path: str
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class ClearContextNotice:
    """Emitted when a clear_context item is encountered."""

    key: EventKey
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class CallingAgent:
    """Emitted just before a Generate stage invokes the agent provider."""

    key: EventKey
    stage_name: str
    provider: str
    model: str | None = None
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class GotReply:
    """Emitted when the agent query finishes, before the runner's result line."""

    key: EventKey
    stage_name: str
    elapsed_s: float = 0.0
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class UnitMerged:
    """Emitted when a unit's merge completes (Ratatosk era)."""

    key: EventKey
    merge_status: str = ""
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class RunPaused:
    """Emitted when the run is paused between stages."""

    key: EventKey
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class RunResumed:
    """Emitted when a paused run is resumed."""

    key: EventKey
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class RunCancelled:
    """Emitted when the run is cancelled by the user."""

    key: EventKey
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


@dataclass(frozen=True)
class RunFinished:
    """Emitted once at the end of a pipeline run."""

    key: EventKey
    success: bool = True
    summary: str = ""
    delivery: Delivery = field(default=Delivery.LOSSLESS, repr=False)


# Union of all run-event types for type annotations.
RunEvent = (
    RunStarted
    | UnitStarted
    | StageStarted
    | TurnEvent
    | CommandOutput
    | UsageUpdated
    | StageFinished
    | StageRetrying
    | WaitingInput
    | LoopExhausted
    | LoopAttempt
    | LoopSuccess
    | LoopDraftPR
    | ParallelStarted
    | ParallelDone
    | IncludeStarted
    | IncludeDone
    | ClearContextNotice
    | CallingAgent
    | GotReply
    | RunError
    | UnitMerged
    | RunPaused
    | RunResumed
    | RunCancelled
    | RunFinished
)
