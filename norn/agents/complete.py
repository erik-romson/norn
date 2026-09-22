"""Provider-neutral one-shot text completion helper.

``complete_text`` builds a minimal ``AgentRequest`` (no file or terminal
tools, single turn) and runs it through the registered provider. This
replaces direct ``claude_agent_sdk`` usage in helper stages and matchers
that need a quick LLM call for compression or classification.
"""
from __future__ import annotations

import logging

from norn.agents.base import AgentError, AgentRequest
from norn.agents.registry import get_provider
from norn.model_labels import resolve_label

log = logging.getLogger(__name__)


async def complete_text(
    prompt: str,
    *,
    provider: str = "claude-code",
    model: str = "light",
    system_prompt: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """Run a one-shot text completion through the given provider.

    Builds a minimal ``AgentRequest`` with no tools, single turn, and
    collects text chunks from the provider's event stream.

    Returns the concatenated text output, or ``None`` if the provider
    raises an error or returns empty output.

    Raises ``ModelLabelError`` (from ``norn.model_labels``) when *model*
    is not a recognised intent label for *provider*.

    Args:
        prompt: The user prompt text.
        provider: Provider name (e.g. ``"claude-code"`` or ``"opencode"``).
        model: Intent label (e.g. ``"light"``). Raw model IDs raise
            ``ModelLabelError``.
        system_prompt: Optional system prompt prepended by the provider.
        cwd: Working directory for the provider process.
        env: Environment variables for the provider process.
    """
    resolve_label(provider, model)  # raises ModelLabelError for unknown labels
    adapter = get_provider(provider)

    request = AgentRequest(
        prompt=prompt,
        stage_name="_complete_text",
        provider=provider,
        model=model,
        allowed_tools=[],
        max_turns=1,
        cwd=cwd,
        env=env or {},
        system_prompt=system_prompt,
    )

    chunks: list[str] = []
    try:
        async for event in adapter.run(request):
            if event.text is not None:
                chunks.append(event.text)
    except AgentError as exc:
        log.warning("complete_text failed: %s", exc)
        return None
    except Exception as exc:
        log.warning("complete_text failed: %s", exc)
        return None

    result = "".join(chunks).strip()
    return result or None
