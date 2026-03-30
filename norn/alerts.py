from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import urllib.request
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

log = logging.getLogger(__name__)


class AlertEvent(Enum):
    """Events that trigger alert notifications.

    Used by alert channels to filter which events they subscribe to.
    Set ``events={AlertEvent.FAILED}`` on a channel to receive only
    failure notifications, or ``events=None`` (default) to receive all.
    """

    COMPLETE = auto()           # Pipeline finished successfully
    FAILED = auto()             # Pipeline terminated with an error
    ASK_USER = auto()           # Pipeline paused — waiting for user input
    RETRIES_EXHAUSTED = auto()  # A loop ran out of retry attempts


@dataclass
class AlertMessage:
    """Payload passed to every alert channel.

    Created by the runner at key lifecycle points (completion, failure,
    retries exhausted, ask-user) and dispatched via ``AlertManager.fire()``.

    Attributes:
        event: The event type that triggered this alert.
        pipeline_name: Name of the pipeline that fired the alert.
        stage_name: Name of the stage that caused the event (if applicable).
        detail: Additional context (e.g. error message), truncated to 200 chars
            in the ``body`` property.
    """

    event: AlertEvent
    pipeline_name: str
    stage_name: str | None = None
    detail: str = ""

    @property
    def title(self) -> str:
        labels = {
            AlertEvent.COMPLETE: "✅ Complete",
            AlertEvent.FAILED: "❌ Failed",
            AlertEvent.ASK_USER: "⏸ Needs attention",
            AlertEvent.RETRIES_EXHAUSTED: "🔁 Retries exhausted",
        }
        return labels.get(self.event, self.event.name)

    @property
    def body(self) -> str:
        parts = [self.pipeline_name]
        if self.stage_name:
            parts.append(f"stage: {self.stage_name}")
        if self.detail:
            parts.append(self.detail[:200])
        return " — ".join(parts)


# ---------------------------------------------------------------------------
# Built-in channels
# ---------------------------------------------------------------------------


@dataclass
class SlackChannel:
    """Send alerts to a Slack incoming webhook URL.

    Get a webhook URL from https://api.slack.com/messaging/webhooks.

    Example::

        SlackChannel(webhook_url="https://hooks.slack.com/services/XXX/YYY/ZZZ")
        SlackChannel(webhook_url="...", events={AlertEvent.FAILED, AlertEvent.ASK_USER})
    """

    webhook_url: str
    events: set[AlertEvent] | None = None  # None = subscribe to all events

    async def send(self, msg: AlertMessage) -> None:
        text = f"*{msg.title}*\n{msg.body}"
        payload = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10))
        log.debug("Slack alert sent: %s", msg.event.name)


@dataclass
class MacOSChannel:
    """Send macOS system notifications via `osascript`.

    Requires macOS. Works in terminal, does not require any extra packages.

    Example::

        MacOSChannel()
        MacOSChannel(app_name="My Pipeline", events={AlertEvent.COMPLETE, AlertEvent.FAILED})
    """

    app_name: str = "norn"
    events: set[AlertEvent] | None = None  # None = subscribe to all events

    async def send(self, msg: AlertMessage) -> None:
        # Escape double-quotes for AppleScript string literals
        title = msg.title.replace('"', '\\"')
        subtitle = msg.pipeline_name.replace('"', '\\"')
        body = msg.body.replace('"', '\\"')

        script = (
            f'display notification "{body}" '
            f'with title "{self.app_name}" '
            f'subtitle "{subtitle}: {title}" '
            f'sound name "Glass"'
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                check=False,
            ),
        )
        log.debug("macOS alert sent: %s", msg.event.name)


@dataclass
class FileChannel:
    """Append alert messages as JSON lines to a file.

    Useful for testing and CI environments where macOS notifications or Slack
    are not available.  Each alert is written as a single JSON object on its
    own line (JSONL format).

    Example::

        FileChannel(path="tmp/alerts.jsonl")
        FileChannel(path="alerts.jsonl", events={AlertEvent.COMPLETE, AlertEvent.FAILED})
    """

    path: str
    events: set[AlertEvent] | None = None  # None = subscribe to all events

    async def send(self, msg: AlertMessage) -> None:
        entry = {"event": msg.event.name, "title": msg.title, "body": msg.body}
        line = json.dumps(entry)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._append, line)
        log.debug("File alert written: %s → %s", msg.event.name, self.path)

    def _append(self, line: str) -> None:
        import os

        dir_name = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(dir_name, exist_ok=True)
        with open(self.path, "a") as fh:
            fh.write(line + "\n")


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


@dataclass
class AlertManager:
    """Dispatches AlertMessages to one or more channels.

    Channels can filter by event type via their ``events`` attribute.
    If a channel has ``events=None`` it receives every event.
    """

    channels: list[Any] = field(default_factory=list)

    async def fire(self, msg: AlertMessage) -> None:
        """Send *msg* to all channels that subscribe to its event."""
        for channel in self.channels:
            channel_events: set[AlertEvent] | None = getattr(channel, "events", None)
            if channel_events is not None and msg.event not in channel_events:
                continue
            try:
                await channel.send(msg)
            except Exception as exc:
                log.warning("Alert channel %s failed for %s: %s", type(channel).__name__, msg.event.name, exc)
