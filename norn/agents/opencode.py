"""OpenCode agent provider for Norn pipelines.

Implements the ``AgentProvider`` protocol by driving the ``opencode run``
CLI in JSON-event mode.  Sessions are first-class: a new session is created
when ``request.session_id`` is absent, and an existing session is continued
when it is present.

All tests are offline — the subprocess call is the only integration
surface and is always mocked in unit tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from typing import Any, AsyncIterator

from norn.agents.base import AgentError, AgentEvent, AgentRequest, AgentUsage
from norn.agents.capabilities import AgentCapabilities, CostMode
from norn.agents.models import resolve_model

log = logging.getLogger(__name__)


class OpenCodeError(AgentError):
    """Wraps exceptions raised during an OpenCode CLI invocation."""


class OpenCodeProvider:
    """``AgentProvider`` backed by the ``opencode run --format json`` CLI.

    The provider launches ``opencode run`` as a subprocess, streams
    JSON-line events, and maps them to provider-neutral ``AgentEvent``
    objects.  Session continuation, model selection, working directory,
    and permission mapping are all handled through CLI flags.

    Unsupported features (structured output, ``fork_session``,
    ``setting_sources``, ``hooks``, ``mcp_servers``, ``mcp_tools``)
    are rejected before the subprocess is started.
    """

    name: str = "opencode"

    capabilities: AgentCapabilities = AgentCapabilities(
        block_kinds=frozenset({"text"}),
        cost_mode=CostMode.ZERO_UNKNOWN,
        supports_structured_output=False,
        supports_fork=False,
        supports_hooks=False,
        supports_mcp=False,
        supports_thinking=False,
        file_edit_without_terminal=False,
        session_resumable=True,
        session_forkable=False,
        session_attachable=False,
        live_model_switch=False,
        model_alias_table="opencode",
        can_list_models=False,
    )

    # ------------------------------------------------------------------ #
    # Feature-gate helpers
    # ------------------------------------------------------------------ #

    def _reject_unsupported(self, request: AgentRequest) -> None:
        """Fail fast for features not declared in this provider's capabilities."""
        from norn.agents.capabilities import validate_capabilities

        try:
            validate_capabilities(request, self.capabilities, self.name)
        except ValueError as exc:
            raise OpenCodeError(exc) from exc

    # ------------------------------------------------------------------ #
    # Permission mapping
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_permission_flags(request: AgentRequest) -> bool:
        """Determine whether ``--dangerously-skip-permissions`` is needed.

        Returns ``True`` when the flag should be added.  Raises when the
        requested permission cannot be represented safely.

        OpenCode's only runtime permission toggle is the blanket
        ``--dangerously-skip-permissions``.  We map from Norn's normalised
        permissions:

        - ``plan_only`` → no flag (OpenCode defaults to asking).
        - ``file_edit`` without ``terminal`` → cannot be represented
          safely (the skip flag would also grant terminal), so we fail.
        - ``file_edit`` **and** ``terminal`` → use the skip flag.
        - No permissions requested → no flag.
        """
        perms = request.permissions
        if perms is None:
            return False

        if perms.plan_only:
            return False

        if perms.file_edit and not perms.terminal:
            raise OpenCodeError(
                ValueError(
                    "The opencode provider cannot grant file-edit permissions without also "
                    "granting terminal permissions.  Add 'Bash' to allowed_tools or use "
                    "permission_mode='bypassPermissions' to enable both, or switch to a "
                    "provider that supports fine-grained permissions."
                ),
            )

        if perms.file_edit and perms.terminal:
            return True

        if perms.terminal and not perms.file_edit:
            # Terminal-only also requires the blanket skip flag.
            return True

        return False

    # ------------------------------------------------------------------ #
    # CLI command builder
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_command(request: AgentRequest, skip_permissions: bool) -> list[str]:
        """Build the ``opencode run`` command-line arguments."""
        cmd = ["opencode", "run", "--format", "json"]

        resolved_model = resolve_model("opencode", request.model)
        if resolved_model:
            cmd.extend(["-m", resolved_model])

        if request.session_id:
            cmd.extend(["--session", request.session_id])

        if request.cwd:
            cmd.extend(["--dir", request.cwd])

        if skip_permissions:
            cmd.append("--dangerously-skip-permissions")

        # System prompt is prepended to the user prompt since OpenCode
        # does not have a separate system prompt CLI flag.
        prompt = request.prompt
        if request.system_prompt:
            prompt = f"{request.system_prompt}\n\n---\n\n{prompt}"

        cmd.append(prompt)
        return cmd

    # ------------------------------------------------------------------ #
    # Event parsing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_text_event(data: dict) -> AgentEvent | None:
        """Extract text from a ``text`` event."""
        part = data.get("part", {})
        text = part.get("text")
        if text:
            return AgentEvent(text=text)
        return None

    @staticmethod
    def _parse_tool_event(data: dict) -> list[str]:
        """Extract artifact file paths from a ``tool_use`` event."""
        artifacts: list[str] = []
        part = data.get("part", {})
        state = part.get("state", {})
        metadata = state.get("metadata", {})
        files = metadata.get("files", [])
        for f in files:
            file_path = f.get("filePath")
            if file_path:
                artifacts.append(file_path)
        return artifacts

    @staticmethod
    def _parse_step_finish(data: dict) -> tuple[dict[str, int], float]:
        """Extract tokens and cost from a ``step_finish`` event.

        Returns ``(tokens_dict, cost)``.
        """
        part = data.get("part", {})
        tokens = part.get("tokens", {})
        cost = part.get("cost", 0.0)
        return tokens, cost

    # ------------------------------------------------------------------ #
    # Main run loop
    # ------------------------------------------------------------------ #

    async def run(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        """Execute an OpenCode CLI query, yielding provider-neutral events."""
        self._reject_unsupported(request)
        skip_permissions = self._resolve_permission_flags(request)

        if shutil.which("opencode") is None:
            raise OpenCodeError(
                FileNotFoundError(
                    "opencode is not installed or not in PATH. "
                    "Install with: npm install -g opencode"
                ),
            )

        cmd = self._build_command(request, skip_permissions)
        log.debug("[opencode] Running: %s", " ".join(cmd[:4]) + " ...")

        # Build subprocess env: inherit current env, overlay request.env
        import os

        proc_env: dict[str, str] | None = None
        if request.env:
            proc_env = dict(os.environ)
            proc_env.update(request.env)

        session_id: str | None = request.session_id
        artifacts: list[str] = []
        total_tokens: dict[str, int] = {}
        total_cost: float = 0.0
        step_count: int = 0
        start_time = time.monotonic()
        is_error: bool = False
        error_messages: list[str] = []

        # Watchdog: kill the subprocess if no stdout line arrives for this many
        # seconds. opencode has no request timeout of its own; a wedged Copilot
        # API call would otherwise sit forever in `S+` consuming no CPU. Set
        # NORN_OPENCODE_IDLE_TIMEOUT=0 to disable.
        try:
            idle_timeout = float(os.environ.get("NORN_OPENCODE_IDLE_TIMEOUT", "300"))
        except ValueError:
            idle_timeout = 300.0
        if idle_timeout <= 0:
            idle_timeout = 0.0

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
                cwd=request.cwd,
            )

            assert proc.stdout is not None
            assert proc.stderr is not None

            while True:
                if idle_timeout > 0:
                    try:
                        raw_line = await asyncio.wait_for(
                            proc.stdout.readline(), timeout=idle_timeout
                        )
                    except asyncio.TimeoutError as exc:
                        # opencode has been silent past the watchdog window.
                        # Tear the subprocess down so it can't sit forever and
                        # raise so the stage fails loudly instead of hanging.
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        await proc.wait()
                        raise OpenCodeError(
                            TimeoutError(
                                f"opencode produced no output for {idle_timeout:.0f}s "
                                f"(likely a stalled upstream API call). "
                                f"Set NORN_OPENCODE_IDLE_TIMEOUT to adjust or 0 to disable."
                            ),
                        ) from exc
                else:
                    raw_line = await proc.stdout.readline()

                if not raw_line:
                    break

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                # OpenCode may emit non-JSON lines (e.g. permission messages)
                if not line.startswith("{"):
                    log.debug("[opencode] Non-JSON output: %s", line)
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("[opencode] Skipping malformed JSON: %s", line[:200])
                    continue

                event_type = data.get("type")
                event_session_id = data.get("sessionID")

                # Capture session ID from the first event
                if event_session_id and not session_id:
                    session_id = event_session_id
                    yield AgentEvent(session_id=session_id)

                if event_type == "text":
                    text_event = self._parse_text_event(data)
                    if text_event:
                        yield text_event

                elif event_type == "tool_use":
                    tool_artifacts = self._parse_tool_event(data)
                    for path in tool_artifacts:
                        if path not in artifacts:
                            artifacts.append(path)

                elif event_type == "error":
                    # OpenCode reports model lookup failures, authentication
                    # problems, etc. as `error` events and then exits with
                    # status 0 — silently dropping them would let stages that
                    # never invoked a model report success. Collect the
                    # message so we can raise once the process finishes.
                    is_error = True
                    err = data.get("error", {})
                    err_data = err.get("data", {}) if isinstance(err, dict) else {}
                    message = err_data.get("message") if isinstance(err_data, dict) else None
                    if not message:
                        message = err.get("name") if isinstance(err, dict) else None
                    error_messages.append(str(message) if message else "opencode reported an error")

                elif event_type == "step_finish":
                    step_count += 1
                    tokens, cost = self._parse_step_finish(data)
                    # Accumulate tokens across steps
                    for key in ("total", "input", "output", "reasoning"):
                        total_tokens[key] = total_tokens.get(key, 0) + tokens.get(key, 0)
                    cache = tokens.get("cache", {})
                    total_tokens["cache_read"] = total_tokens.get("cache_read", 0) + cache.get("read", 0)
                    total_tokens["cache_write"] = total_tokens.get("cache_write", 0) + cache.get("write", 0)
                    total_cost += cost

                    # Check for error finish reason
                    reason = data.get("part", {}).get("reason", "")
                    if reason == "error":
                        is_error = True

            # Wait for process to complete
            await proc.wait()
            duration_ms = int((time.monotonic() - start_time) * 1000)

            # Capture stderr
            stderr_data = await proc.stderr.read()
            stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
            stderr_lines = [line for line in stderr_text.splitlines() if line.strip()] if stderr_text else []

            if proc.returncode and proc.returncode != 0:
                is_error = True
                error_msg = stderr_text or f"opencode exited with code {proc.returncode}"
                raise OpenCodeError(
                    RuntimeError(error_msg),
                    stderr_lines=stderr_lines,
                )

            if error_messages:
                # opencode emits `error` events even when it exits 0 (e.g.
                # "Model not found: ..."). Surface them so a misconfigured
                # provider fails the stage instead of silently no-op'ing.
                joined = "; ".join(error_messages)
                raise OpenCodeError(
                    RuntimeError(f"opencode reported error: {joined}"),
                    stderr_lines=stderr_lines,
                )

            # Emit final session ID (may have been updated during the run)
            if session_id:
                yield AgentEvent(session_id=session_id)

            # Emit usage
            usage = AgentUsage(
                provider="opencode",
                model=request.model,
                session_id=session_id,
                input_tokens=total_tokens.get("input", 0),
                output_tokens=total_tokens.get("output", 0),
                cache_read_input_tokens=total_tokens.get("cache_read", 0),
                cache_creation_input_tokens=total_tokens.get("cache_write", 0),
                total_cost_usd=total_cost,
                duration_ms=duration_ms,
                duration_api_ms=0,
                num_turns=step_count,
                is_error=is_error,
            )
            yield AgentEvent(usage=usage)

            # Emit artifacts
            for artifact_path in artifacts:
                yield AgentEvent(artifact=artifact_path)

        except OpenCodeError:
            raise
        except Exception as exc:
            raise OpenCodeError(exc) from exc
