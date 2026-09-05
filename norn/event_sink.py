"""EventSink — classifies, redacts, and fans out run-events.

The sink is the single boundary between the runner (producer) and all
consumers (TUI, CLI subscriber, journal).  It **never blocks the producer**:

* **Lossless** events are appended to an unbounded list (low volume).
* **Coalescible** events overwrite a latest-wins slot keyed by
  ``(stage_id, attempt)``.
* **Pageable** events (transcript ``TurnEvent``s) are appended to a per-stage
  spool keyed by ``(stage_id, attempt)``; consumers page through by ``seq``.

**Redaction** is applied once at the seam via :func:`norn.ui.mask` before any
event reaches a consumer, so every downstream renderer is safe.

No Textual imports.  No SDK imports.
"""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from typing import Any

from norn.events import (
    CommandOutput,
    Delivery,
    RunEvent,
    TurnEvent,
    UsageUpdated,
)
from norn.ui import mask


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------


def _redact_event(event: RunEvent) -> RunEvent:
    """Return a copy of *event* with displayable text fields redacted.

    Only event types that carry user-visible text need treatment.
    Frozen dataclasses are cheap to reconstruct since they are small.
    """
    if isinstance(event, TurnEvent):
        agent_event = event.event
        # Redact text on the wrapped AgentEvent.
        redacted_text = mask(agent_event.text) if agent_event.text else agent_event.text

        # Redact block summaries.
        # INVARIANT: every block class in norn/agents/base.py that carries a
        # displayable string must have a branch here — this is the single
        # point where secrets are scrubbed before any consumer sees the block.
        from norn.agents.base import ThinkingBlock, ToolResultBlock, ToolUseBlock

        redacted_block = agent_event.block
        if isinstance(agent_event.block, ToolUseBlock):
            redacted_block = ToolUseBlock(
                name=agent_event.block.name,
                input_summary=mask(agent_event.block.input_summary),
            )
        elif isinstance(agent_event.block, ToolResultBlock):
            redacted_block = ToolResultBlock(
                ok=agent_event.block.ok,
                summary=mask(agent_event.block.summary),
            )
        elif isinstance(agent_event.block, ThinkingBlock):
            # Extended thinking can echo tokens (e.g. when the agent reasons
            # about an auth failure).  Mask like any other displayable text.
            redacted_block = ThinkingBlock(text=mask(agent_event.block.text))

        if redacted_text is not agent_event.text or redacted_block is not agent_event.block:
            from norn.agents.base import AgentEvent as AE

            new_agent = AE(
                text=redacted_text,
                session_id=agent_event.session_id,
                usage=agent_event.usage,
                structured_output=agent_event.structured_output,
                artifact=agent_event.artifact,
                block=redacted_block,
            )
            return TurnEvent(key=event.key, event=new_agent)
        return event

    if isinstance(event, CommandOutput):
        # Command output is raw process stdout/stderr — the most likely place
        # for a token to appear verbatim (a curl -H header, an env echo, a
        # failing auth log).  Mask it here like any other displayable text.
        masked_text = mask(event.text)
        if masked_text is not event.text:
            return CommandOutput(key=event.key, text=masked_text, stream=event.stream)
        return event

    # Redact displayable string fields on other event types.
    from norn.events import (
        LoopExhausted,
        RunError,
        RunFinished,
        StageFinished,
        StageRetrying,
        WaitingInput,
    )

    if isinstance(event, StageFinished) and event.error:
        masked_error = mask(event.error)
        if masked_error is not event.error:
            return StageFinished(
                key=event.key,
                name=event.name,
                status=event.status,
                success=event.success,
                duration_ms=event.duration_ms,
                artifacts=event.artifacts,
                error=masked_error,
                usage_input_tokens=event.usage_input_tokens,
                usage_output_tokens=event.usage_output_tokens,
                usage_cost_usd=event.usage_cost_usd,
            )
    elif isinstance(event, RunError) and event.detail:
        masked = mask(event.detail)
        if masked is not event.detail:
            return RunError(key=event.key, error_kind=event.error_kind, detail=masked)
    elif isinstance(event, WaitingInput) and event.prompt_excerpt:
        masked = mask(event.prompt_excerpt)
        if masked is not event.prompt_excerpt:
            return WaitingInput(key=event.key, kind=event.kind, prompt_excerpt=masked)
    elif isinstance(event, StageRetrying) and event.reason:
        masked = mask(event.reason)
        if masked is not event.reason:
            return StageRetrying(key=event.key, next_attempt=event.next_attempt, reason=masked)
    elif isinstance(event, RunFinished) and event.summary:
        masked = mask(event.summary)
        if masked is not event.summary:
            return RunFinished(key=event.key, success=event.success, summary=masked)

    return event


# ---------------------------------------------------------------------------
# Coalesce key helper
# ---------------------------------------------------------------------------


def _coalesce_key(event: RunEvent) -> tuple[str | None, int]:
    """Return the ``(stage_id, attempt)`` pair used for coalescible slots."""
    return (event.key.stage_id, event.key.attempt)


def _spool_key(event: RunEvent) -> tuple[str | None, int]:
    """Return the ``(stage_id, attempt)`` pair used for transcript spools."""
    return (event.key.stage_id, event.key.attempt)


# ---------------------------------------------------------------------------
# EventSink
# ---------------------------------------------------------------------------


class EventSink:
    """Non-blocking event sink with delivery-class aware storage.

    Parameters
    ----------
    on_event:
        Optional synchronous callback invoked with every redacted event.
        Useful for lightweight consumers that don't need the async drain API.
    """

    def __init__(self, *, on_event: Any | None = None) -> None:
        self._lossless: list[RunEvent] = []
        self._coalescible: dict[tuple[str | None, int], RunEvent] = {}
        self._spools: dict[tuple[str | None, int], list[RunEvent]] = defaultdict(list)

        # Async notification for drain consumers.
        self._notify: asyncio.Event | None = None
        self._on_event = on_event
        self._lock = threading.Lock()

    # -- producer API (called from the runner, possibly from a thread) ------

    def emit(self, event: RunEvent) -> None:
        """Accept, redact, classify, and store *event*.

        **Never blocks the producer.** Safe to call from sync or async context.
        """
        event = _redact_event(event)

        with self._lock:
            delivery = event.delivery

            if delivery is Delivery.LOSSLESS:
                self._lossless.append(event)
            elif delivery is Delivery.COALESCIBLE:
                self._coalescible[_coalesce_key(event)] = event
            elif delivery is Delivery.PAGEABLE:
                self._spools[_spool_key(event)].append(event)

        # Fire optional synchronous callback.
        if self._on_event is not None:
            self._on_event(event)

        # Wake any async waiters.
        if self._notify is not None:
            self._notify.set()

    # -- consumer API -------------------------------------------------------

    @property
    def lossless_events(self) -> list[RunEvent]:
        """Return a snapshot of all lossless events received so far."""
        with self._lock:
            return list(self._lossless)

    def latest_coalescible(self, stage_id: str | None = None, attempt: int = 0) -> RunEvent | None:
        """Return the latest coalescible event for *(stage_id, attempt)*, or ``None``."""
        with self._lock:
            return self._coalescible.get((stage_id, attempt))

    def all_coalescible(self) -> dict[tuple[str | None, int], RunEvent]:
        """Return a snapshot of all coalescible slots."""
        with self._lock:
            return dict(self._coalescible)

    def transcript(
        self,
        stage_id: str | None = None,
        attempt: int = 0,
        *,
        after_seq: int = -1,
    ) -> list[RunEvent]:
        """Return transcript events for *(stage_id, attempt)*.

        If *after_seq* is given, only events with ``key.seq > after_seq`` are
        returned — this is the paging primitive.
        """
        with self._lock:
            spool = self._spools.get((stage_id, attempt), [])
            if after_seq < 0:
                return list(spool)
            return [e for e in spool if e.key.seq > after_seq]

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Bind an asyncio event loop for ``drain()`` notifications.

        Must be called from the consumer's event loop before using ``drain``.
        """
        self._notify = asyncio.Event()

    async def drain(self) -> None:
        """Await until at least one new event has been emitted.

        Requires a prior :meth:`bind_loop` call.  Resets the notification flag
        so the next ``drain`` blocks again.
        """
        if self._notify is None:
            raise RuntimeError("bind_loop() must be called before drain()")
        await self._notify.wait()
        self._notify.clear()


# ---------------------------------------------------------------------------
# NullSink — no-op for contexts that don't need events
# ---------------------------------------------------------------------------


class NullSink:
    """Drop-in replacement for :class:`EventSink` that silently discards events."""

    def emit(self, event: RunEvent) -> None:  # noqa: ARG002
        pass

    @property
    def lossless_events(self) -> list[RunEvent]:
        return []

    def latest_coalescible(self, stage_id: str | None = None, attempt: int = 0) -> RunEvent | None:
        return None

    def all_coalescible(self) -> dict[tuple[str | None, int], RunEvent]:
        return {}

    def transcript(
        self,
        stage_id: str | None = None,
        attempt: int = 0,
        *,
        after_seq: int = -1,
    ) -> list[RunEvent]:
        return []

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        pass

    async def drain(self) -> None:
        # NullSink never wakes; await forever (consumer should not call this).
        await asyncio.sleep(3600)
