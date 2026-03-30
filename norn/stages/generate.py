from __future__ import annotations

import logging
import pathlib
import re
from typing import TYPE_CHECKING, Any

from norn import ui
from norn.models import PipelineContext, StageResult, UsageRecord
from norn.secrets import resolve_env
from norn.stages.base import BaseStage
from norn.templates import PromptTemplate, load_template

if TYPE_CHECKING:
    from norn.profiles import SessionProfile

log = logging.getLogger(__name__)

MODEL_MAP: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


class Generate(BaseStage):
    """Send a prompt to Claude via the claude-agent-sdk.

    This is the primary AI-powered stage. It calls the Claude Agent SDK's
    ``query()`` function, streams the response, tracks token usage and cost,
    and captures file artifacts written by the agent.

    Prompts can be provided directly or via a named template. Placeholders
    like ``{stage_name.output}`` and ``{param.key}`` are resolved before
    sending.

    When ``permission_mode`` or ``allowed_tools`` are set, the agent has
    access to tools (Read, Write, Edit, Bash, etc.) and writes files
    directly — the ``output_file`` parameter is ignored in that case.

    Args:
        prompt: The prompt string (supports placeholders). Required unless
            ``template`` is provided.
        template: Name of a ``PromptTemplate`` to load from ``templates/``.
        input: Input value for template placeholders (``{input}``).
        output_file: Write extracted code to this path (only when the agent
            has no file-writing tools).
        model: Model shorthand (``"opus"``, ``"sonnet"``, ``"haiku"``) or
            full model ID. Falls back to ``Pipeline.default_model``.
        thinking: Thinking budget config, e.g.
            ``{"type": "enabled", "budget_tokens": 10000}``.
        permission_mode: Agent permission level — ``"default"``,
            ``"acceptEdits"``, ``"plan"``, ``"bypassPermissions"``.
        allowed_tools: List of pre-approved tool names
            (e.g. ``["Read", "Edit", "Bash"]``).
        max_turns: Maximum conversation turns before the agent stops.
        cwd: Working directory for the agent process.
        setting_sources: Config sources to load (e.g. ``["project"]`` loads
            CLAUDE.md).
        add_dirs: Additional directories the agent may access.
        hooks: SDK-level hooks dict (``PreToolUse``, ``PostToolUse``).
            A ``PostToolUse`` hook for artifact tracking is always added.
        env: Environment variables for the agent. Supports
            ``{secret.NAME}`` placeholders.
        skills: List of skill names or ``Skill`` objects to inject into
            the agent's system prompt.

    Output:
        ``StageResult.output`` is the raw assistant text, extracted code,
        or structured JSON (when ``output_format`` is set via template).
        ``StageResult.artifacts`` lists file paths written by the agent.

    Example::

        Stage("implement", Generate(
            prompt="Implement this feature: {param.args}",
            model="sonnet",
            permission_mode="acceptEdits",
            allowed_tools=["Read", "Write", "Edit", "Bash"],
            setting_sources=["project"],
        ))
    """

    needs_agent = True

    def __init__(
        self,
        *,
        prompt: str | None = None,
        template: str | None = None,
        input: str | None = None,
        output_file: str | None = None,
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        max_turns: int | None = None,
        cwd: str | None = None,
        setting_sources: list[str] | None = None,
        add_dirs: list[str] | None = None,
        hooks: dict | None = None,
        env: dict[str, str] | None = None,
        model: str | None = None,
        thinking: dict | None = None,
        skills: list[str | Any] | None = None,
        profile: SessionProfile | None = None,
    ) -> None:
        if prompt is None and template is None:
            raise ValueError("Generate requires either 'prompt' or 'template'")
        if profile:
            if allowed_tools is None:
                allowed_tools = profile.allowed_tools
            if permission_mode is None:
                permission_mode = profile.permission_mode
            if max_turns is None:
                max_turns = profile.max_turns
            if hooks is None and profile.blocked_patterns:
                from norn.profiles import build_block_hooks
                hooks = build_block_hooks(profile.blocked_patterns)
            if env is None and profile.env:
                env = profile.env
        self.prompt = prompt
        self.template = template
        self.input = input
        self.output_file = output_file
        self.allowed_tools = allowed_tools
        self.permission_mode = permission_mode
        self.max_turns = max_turns
        self.cwd = cwd
        self.setting_sources = setting_sources
        self.add_dirs = add_dirs
        self.hooks = hooks
        self.env = env
        self.model = model
        self.thinking = thinking
        self.skills = skills

    def _resolve(self, text: str, ctx: PipelineContext) -> str:
        """Replace ``{stage_name.output}`` and ``{param.name}`` placeholders."""

        def replacer(match: re.Match[str]) -> str:
            prefix = match.group(1)
            suffix = match.group(2)
            if prefix == "param":
                return ctx.params.get(suffix, match.group(0))
            if prefix in ctx.results:
                return str(ctx.get(prefix))
            return match.group(0)  # leave unresolved placeholders as-is

        return re.sub(r"\{([^{}]+)\.(\w+)\}", replacer, text)

    def _resolve_prompt(self, ctx: PipelineContext) -> str:
        """Resolve placeholders in the prompt string."""
        assert self.prompt is not None
        return self._resolve(self.prompt, ctx)

    @staticmethod
    def _extract_code(text: str) -> str:
        """Extract content from a fenced code block if present, otherwise return as-is."""
        match = re.search(r"```(?:\w*)\n(.*?)```", text, re.DOTALL)
        return match.group(1).strip() if match else text.strip()

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        session_id: str | None = kwargs.get("session_id")
        attempt: int = kwargs.get("attempt", 1)
        fork_session: bool = kwargs.get("fork_session", False)
        mcp_servers: dict | None = kwargs.get("mcp_servers")

        try:
            from claude_agent_sdk import query
        except ImportError:
            return StageResult(
                name="",
                success=False,
                error="claude-agent-sdk is not installed. Install with: uv add claude-agent-sdk",
            )

        # Resolve prompt: either from a named template or directly from self.prompt
        tmpl: PromptTemplate | None = None
        if self.template:
            tmpl = load_template(self.template)
            resolved_input = self._resolve(self.input, ctx) if self.input else ""
            resolved_prompt = tmpl.template.format(input=resolved_input)
        else:
            resolved_prompt = self._resolve_prompt(ctx)
        log.debug("[generate] Resolved prompt (%d chars)", len(resolved_prompt))
        if session_id:
            log.debug("[generate] Reusing session %s", session_id)

        context_text: str | None = None
        if ctx.injected_context:
            context_text = "\n\n".join(
                f"## {label}\n{content}" for label, content in ctx.injected_context
            )

        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ResultMessage,
            )

            chunks: list[str] = []
            artifacts: list[str] = []
            structured_output: Any = None
            resolved_model = self.model or ctx.params.get("default_model")
            usage_record = UsageRecord(stage_name="", attempt=attempt, model=resolved_model)

            # Resolve effective settings: stage-level overrides pipeline-level profile
            _pp = ctx.pipeline_profile
            effective_allowed_tools = self.allowed_tools
            effective_permission_mode = self.permission_mode
            effective_max_turns = self.max_turns
            effective_env = self.env
            effective_hooks = self.hooks
            if _pp:
                if effective_allowed_tools is None:
                    effective_allowed_tools = _pp.allowed_tools
                if effective_permission_mode is None:
                    effective_permission_mode = _pp.permission_mode
                if effective_max_turns is None:
                    effective_max_turns = _pp.max_turns
                if effective_env is None and _pp.env:
                    effective_env = _pp.env
                if effective_hooks is None and _pp.blocked_patterns:
                    from norn.profiles import build_block_hooks
                    effective_hooks = build_block_hooks(_pp.blocked_patterns)

            async def _track_artifacts(input_data: dict, tool_use_id: str, context: Any) -> dict:
                if input_data.get("tool_name") in ("Write", "Edit", "NotebookEdit"):
                    file_path = input_data.get("tool_input", {}).get("file_path")
                    if file_path and file_path not in artifacts:
                        artifacts.append(file_path)
                return {}

            opt_kwargs: dict[str, Any] = {}
            if session_id:
                opt_kwargs["resume"] = session_id
            if fork_session and session_id:
                opt_kwargs["fork_session"] = True
            if effective_allowed_tools:
                opt_kwargs["allowed_tools"] = effective_allowed_tools
            if effective_permission_mode:
                opt_kwargs["permission_mode"] = effective_permission_mode
            if effective_max_turns is not None:
                opt_kwargs["max_turns"] = effective_max_turns
            if self.cwd:
                opt_kwargs["cwd"] = self.cwd
            if self.setting_sources:
                opt_kwargs["setting_sources"] = self.setting_sources
            if self.add_dirs:
                opt_kwargs["add_dirs"] = self.add_dirs
            if resolved_model:
                opt_kwargs["model"] = MODEL_MAP.get(resolved_model, resolved_model)
            if self.thinking:
                opt_kwargs["thinking"] = self.thinking

            # Build system_prompt from skills, template, and context injection
            system_prompt_parts: list[str] = []

            # Merge pipeline-level and stage-level skills; prepend to system prompt
            all_skills: list[Any] = [*(ctx.pipeline_skills or []), *(self.skills or [])]
            if all_skills:
                from norn.skills import resolve_skill_content
                skill_texts = [resolve_skill_content(s) for s in all_skills]
                system_prompt_parts.append("\n\n".join(skill_texts))
                log.debug("[generate] Injected %d skill(s) into system_prompt", len(all_skills))

            if tmpl and tmpl.system_prompt:
                system_prompt_parts.append(tmpl.system_prompt)
            if context_text:
                agent_has_tools = bool(effective_allowed_tools or effective_permission_mode or (tmpl and tmpl.system_prompt))
                if agent_has_tools:
                    system_prompt_parts.append(context_text)
                else:
                    resolved_prompt = f"{context_text}\n\n{resolved_prompt}"
            if system_prompt_parts:
                opt_kwargs["system_prompt"] = "\n\n".join(system_prompt_parts)

            if tmpl and tmpl.output_format:
                opt_kwargs["output_format"] = tmpl.output_format

            if mcp_servers:
                opt_kwargs["mcp_servers"] = mcp_servers

            # Build env: merge pipeline-level env and stage-level env (with secret resolution)
            merged_env: dict[str, str] = {}
            if ctx.env:
                merged_env.update(ctx.env)
            if effective_env:
                merged_env.update(resolve_env(effective_env, ctx))
            if merged_env:
                opt_kwargs["env"] = merged_env

            artifact_hook = {"hooks": [_track_artifacts]}
            if effective_hooks:
                merged = dict(effective_hooks)
                existing = merged.get("PostToolUse", [])
                merged["PostToolUse"] = [*existing, artifact_hook]
                opt_kwargs["hooks"] = merged
            else:
                opt_kwargs["hooks"] = {"PostToolUse": [artifact_hook]}

            options = ClaudeAgentOptions(**opt_kwargs)

            async for message in query(prompt=resolved_prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if hasattr(block, "text"):
                            chunks.append(block.text)
                            print(ui.mask(block.text), end="", flush=True)

                elif isinstance(message, ResultMessage):
                    usage_record.session_id = message.session_id
                    usage_record.total_cost_usd = message.total_cost_usd or 0.0
                    usage_record.duration_ms = message.duration_ms
                    usage_record.duration_api_ms = message.duration_api_ms
                    usage_record.num_turns = message.num_turns
                    usage_record.is_error = message.is_error
                    if hasattr(message, "structured_output") and message.structured_output is not None:
                        structured_output = message.structured_output
                    if message.usage:
                        usage_record.input_tokens = message.usage.get("input_tokens", 0)
                        usage_record.output_tokens = message.usage.get("output_tokens", 0)
                        usage_record.cache_read_input_tokens = message.usage.get(
                            "cache_read_input_tokens", 0
                        )
                        usage_record.cache_creation_input_tokens = message.usage.get(
                            "cache_creation_input_tokens", 0
                        )
                    log.debug(
                        "[generate] ResultMessage: tokens=%d cost=$%.4f duration=%dms turns=%d session=%s",
                        usage_record.total_tokens,
                        usage_record.total_cost_usd,
                        usage_record.duration_ms,
                        usage_record.num_turns,
                        usage_record.session_id,
                    )

                else:
                    # Capture session_id from init/system messages
                    if hasattr(message, "session_id") and message.session_id:
                        if not usage_record.session_id:
                            usage_record.session_id = message.session_id
                            log.debug("[generate] Captured session_id from %s: %s",
                                      type(message).__name__, message.session_id)

                    if hasattr(message, "content"):
                        for block in message.content:
                            if hasattr(block, "text"):
                                chunks.append(block.text)
                    elif isinstance(message, str):
                        chunks.append(message)

            if chunks:
                print()  # end the streamed output line

            raw_output = "".join(chunks)

            # When output_format was declared (via template), prefer structured_output
            if structured_output is not None:
                return StageResult(name="", success=True, output=structured_output, usage=usage_record, artifacts=artifacts)

            # Only write output_file when set AND the agent had no file-writing
            # tools — otherwise Claude already wrote files via Edit/Write tools
            # and the raw assistant text is prose, not code.
            agent_has_file_tools = bool(effective_allowed_tools or effective_permission_mode)
            if self.output_file and not agent_has_file_tools:
                code = self._extract_code(raw_output)
                out_path = pathlib.Path(self.output_file)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(code)
                return StageResult(name="", success=True, output=code, usage=usage_record, artifacts=artifacts)

            return StageResult(name="", success=True, output=raw_output, usage=usage_record, artifacts=artifacts)
        except Exception as e:
            log.debug("[generate] Exception: %s", e)
            return StageResult(name="", success=False, error=str(e))
