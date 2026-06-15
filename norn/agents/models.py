from __future__ import annotations

# Provider-specific model alias tables.
#
# ``claude-code`` aliases mirror the existing MODEL_MAP in
# ``norn.stages.generate`` exactly so the two stay in sync during the
# migration period.
#
# ``opencode`` aliases use the Anthropic-prefixed form required by OpenCode's
# model selector.  Plain ``claude-*`` IDs (no ``/`` separator) are
# automatically normalised to ``anthropic/claude-*`` for OpenCode.

MODEL_ALIASES: dict[str, dict[str, str]] = {
    "claude-code": {
        "opus": "claude-opus-4-6",
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5-20251001",
    },
    "opencode": {
        "opus": "github-copilot/claude-opus-4.7",
        "sonnet": "github-copilot/claude-sonnet-4.6",
        "haiku": "github-copilot/claude-sonnet-4.6",
    },
}


def resolve_model(provider: str, model: str | None) -> str | None:
    """Resolve a model alias or raw ID to the provider-facing model string.

    Resolution rules:

    - ``None`` is returned as ``None`` (no model override).
    - If the provider has an alias table and ``model`` is a known short name
      (e.g. ``"sonnet"``), return the mapped full ID.
    - For ``opencode``, plain ``claude-*`` IDs that do not already contain
      ``/`` are normalised to ``anthropic/claude-*``.
    - All other values pass through unchanged.

    Args:
        provider: Provider name (e.g. ``"claude-code"`` or ``"opencode"``).
        model: Model alias, full provider-specific ID, or ``None``.

    Returns:
        Resolved model string, or ``None`` when ``model`` is ``None``.
    """
    if model is None:
        return None

    aliases = MODEL_ALIASES.get(provider, {})

    # Check explicit alias first.
    if model in aliases:
        return aliases[model]

    # For opencode, normalise bare ``claude-*`` IDs.
    if provider == "opencode" and model.startswith("claude-") and "/" not in model:
        return f"anthropic/{model}"

    return model
