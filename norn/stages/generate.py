from __future__ import annotations

import logging
import pathlib
import re
import time
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

_STDERR_PLACEHOLDER = "Check stderr output for details"


def _normalize_error_text(text: str) -> str:
    """Trim duplicate SDK boilerplate from CLI failure messages."""
    cleaned = text.strip()
    cleaned = re.sub(
        r"Command failed with exit code (\d+) \(exit code: \1\)",
        r"Command failed with exit code \1",
        cleaned,
    )
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _render_cli_exception(
    exc: Exception,
    stderr_lines: list[str],
    assistant_chunks: list[str] | None = None,
) -> str:
    """Merge the provider exception with captured stderr, if available.

    When the exception's stderr is the bare "Check stderr output for
    details" placeholder AND no stderr lines were captured (e.g. the failure
    happened in the assistant message stream — usage-limit messages,
    invalid-API-key messages, etc.), fall back to the tail of the
    assistant output. That's where the agent prints "You've hit your
    limit", so without this fallback the user gets a useless error.
    """
    message = ui.mask(str(exc)).strip() or exc.__class__.__name__
    captured_stderr = "\n".join(ui.mask(line).strip() for line in stderr_lines if line.strip()).strip()
    if captured_stderr:
        if _STDERR_PLACEHOLDER in message:
            message = message.replace(_STDERR_PLACEHOLDER, captured_stderr)
        elif captured_stderr not in message:
            message = f"{message}\n{captured_stderr}"
    else:
        exc_stderr = getattr(exc, "stderr", None)
        if isinstance(exc_stderr, str):
            exc_stderr = ui.mask(exc_stderr).strip()
            if exc_stderr and exc_stderr != _STDERR_PLACEHOLDER and exc_stderr not in message:
                message = f"{message}\n{exc_stderr}"

    if _STDERR_PLACEHOLDER in message and assistant_chunks:
        tail = ui.mask("".join(assistant_chunks)).strip()
        if tail:
            tail_excerpt = tail[-1000:].strip()
            message = message.replace(_STDERR_PLACEHOLDER, tail_excerpt)
    return _normalize_error_text(message)


class Generate(BaseStage):
    """Send a prompt to an agent provider.

    This is the primary AI-powered stage. It delegates to whichever
    ``AgentProvider`` is selected for the pipeline run (via
    ``ctx.agent_provider``), streams the response, tracks token usage and
    cost, and captures file artifacts written by the agent.

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
        hooks: Provider-level hooks dict (``PreToolUse``, ``PostToolUse``).
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
        mcp_tools: list | None = kwargs.get("mcp_tools")
        stage_name: str = kwargs.get("stage_name", "claude")

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

        resolved_model = self.model or ctx.params.get("default_model")

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

        # Build system_prompt from skills, template, context injection, and project guidance
        system_prompt_parts: list[str] = []

        # Resolve portable project guidance when setting_sources includes "project"
        resolved_setting_sources = list(self.setting_sources) if self.setting_sources else None
        if resolved_setting_sources and "project" in resolved_setting_sources:
            from norn.agents.guidance import resolve_project_guidance

            guidance_text = resolve_project_guidance(cwd=self.cwd)
            if guidance_text:
                system_prompt_parts.append(guidance_text)
                log.debug("[generate] Injected portable project guidance into system_prompt")
            # Remove "project" so providers don't double-inject
            resolved_setting_sources.remove("project")
            if not resolved_setting_sources:
                resolved_setting_sources = None

        # Fail fast: non-portable features are not allowed for non-claude-code providers.
        # SDK hooks (including those compiled from blocked_patterns via profile) are
        # Claude Code-only.  MCP tools and non-project setting_sources are also
        # provider-specific.  Return a clear StageResult so callers don't receive a
        # raw exception and the error message includes the provider, feature, stage
        # name, and a hint.
        if ctx.agent_provider != "claude-code":
            if effective_hooks:
                return StageResult(
                    name="",
                    success=False,
                    error=(
                        f"Stage '{stage_name}' uses hooks, which are not supported by provider "
                        f"'{ctx.agent_provider}'. SDK hooks are a Claude Code feature. "
                        f"Use provider 'claude-code' for hook-based features, or remove hooks "
                        f"from this stage."
                    ),
                )
            if mcp_tools:
                return StageResult(
                    name="",
                    success=False,
                    error=(
                        f"Stage '{stage_name}' declares mcp_tools, which are not supported by "
                        f"provider '{ctx.agent_provider}'. MCP tools are a Claude Code SDK "
                        f"feature. Use provider 'claude-code' for MCP tool features."
                    ),
                )
            if resolved_setting_sources:
                unsupported = ", ".join(repr(s) for s in resolved_setting_sources)
                return StageResult(
                    name="",
                    success=False,
                    error=(
                        f"Stage '{stage_name}' uses setting_sources {unsupported}, which are "
                        f"not supported by provider '{ctx.agent_provider}'. Only 'project' is "
                        f"portable across providers. Use provider 'claude-code' for "
                        f"provider-specific setting sources, or remove them."
                    ),
                )

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

        system_prompt: str | None = "\n\n".join(system_prompt_parts) if system_prompt_parts else None

        output_format: dict | None = tmpl.output_format if (tmpl and tmpl.output_format) else None

        # Build env: merge pipeline-level env and stage-level env (with secret resolution)
        merged_env: dict[str, str] = {}
        if ctx.env:
            merged_env.update(ctx.env)
        if effective_env:
            merged_env.update(resolve_env(effective_env, ctx))

        # Build the provider-neutral request
        from norn.agents.base import AgentRequest
        from norn.agents.permissions import normalize_permissions

        permissions = normalize_permissions(effective_allowed_tools, effective_permission_mode)

        request = AgentRequest(
            prompt=resolved_prompt,
            stage_name=stage_name,
            provider=ctx.agent_provider,
            model=resolved_model,
            session_id=session_id,
            fork_session=fork_session,
            allowed_tools=effective_allowed_tools,
            permission_mode=effective_permission_mode,
            max_turns=effective_max_turns,
            cwd=self.cwd,
            env=merged_env,
            system_prompt=system_prompt,
            output_format=output_format,
            thinking=self.thinking,
            attempt=attempt,
            add_dirs=self.add_dirs,
            setting_sources=resolved_setting_sources,
            hooks=effective_hooks,
            mcp_servers=mcp_servers,
            mcp_tools=mcp_tools,
            permissions=permissions,
        )

        # Run via the registered provider for this pipeline run
        from norn.agents.registry import get_provider

        provider = get_provider(ctx.agent_provider)

        chunks: list[str] = []
        artifacts: list[str] = []
        structured_output: Any = None
        usage_record = UsageRecord(stage_name="", attempt=attempt, model=resolved_model)

        try:
            ui.print_calling_agent(stage_name, ctx.agent_provider, resolved_model)
            _query_start = time.monotonic()

            async for event in provider.run(request):
                if event.text is not None:
                    chunks.append(event.text)
                    print(ui.mask(event.text), end="", flush=True)

                if event.session_id is not None:
                    if not usage_record.session_id:
                        usage_record.session_id = event.session_id
                        log.debug("[generate] Captured session_id: %s", event.session_id)
                    else:
                        usage_record.session_id = event.session_id

                if event.structured_output is not None:
                    structured_output = event.structured_output

                if event.usage is not None:
                    usage_record.provider = event.usage.provider
                    usage_record.session_id = event.usage.session_id
                    usage_record.total_cost_usd = event.usage.total_cost_usd
                    usage_record.duration_ms = event.usage.duration_ms
                    usage_record.duration_api_ms = event.usage.duration_api_ms
                    usage_record.num_turns = event.usage.num_turns
                    usage_record.is_error = event.usage.is_error
                    usage_record.input_tokens = event.usage.input_tokens
                    usage_record.output_tokens = event.usage.output_tokens
                    usage_record.cache_read_input_tokens = event.usage.cache_read_input_tokens
                    usage_record.cache_creation_input_tokens = event.usage.cache_creation_input_tokens
                    log.debug(
                        "[generate] ResultMessage: tokens=%d cost=$%.4f duration=%dms turns=%d session=%s",
                        usage_record.total_tokens,
                        usage_record.total_cost_usd,
                        usage_record.duration_ms,
                        usage_record.num_turns,
                        usage_record.session_id,
                    )

                if event.artifact is not None:
                    if event.artifact not in artifacts:
                        artifacts.append(event.artifact)

            if chunks:
                print()  # end the streamed output line

            ui.print_got_reply(stage_name, time.monotonic() - _query_start)

            raw_output = "".join(chunks)

            # When output_format was declared (via template), prefer structured_output
            if structured_output is not None:
                return StageResult(name="", success=True, output=structured_output, usage=usage_record, artifacts=artifacts)

            # Only write output_file when set AND the agent had no file-writing
            # tools — otherwise the agent already wrote files via Edit/Write tools
            # and the raw assistant text is prose, not code.
            # Use normalized permissions; fall back to truthy check on original
            # fields so unknown tools still disable output_file writing.
            agent_has_file_tools = (
                permissions.file_read
                or permissions.file_edit
                or permissions.terminal
                or permissions.plan_only
                or bool(effective_allowed_tools or effective_permission_mode)
            )
            if self.output_file and not agent_has_file_tools:
                code = self._extract_code(raw_output)
                out_path = pathlib.Path(self.output_file)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(code)
                return StageResult(name="", success=True, output=code, usage=usage_record, artifacts=artifacts)

            return StageResult(name="", success=True, output=raw_output, usage=usage_record, artifacts=artifacts)
        except ImportError as e:
            return StageResult(
                name="",
                success=False,
                error=str(e),
            )
        except Exception as e:
            log.debug("[generate] Exception: %s", e)
            from norn.agents.base import AgentError

            stderr_lines: list[str] = []
            original: Exception = e
            if isinstance(e, AgentError):
                stderr_lines = e.stderr_lines
                original = e.original
            return StageResult(
                name="", success=False,
                error=_render_cli_exception(original, stderr_lines, chunks),
            )
