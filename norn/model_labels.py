"""
Loader for norn/model_labels.toml — the single source of truth for every model
id and reasoning effort norn sends to a provider.

Public API
----------
ResolvedModel      frozen dataclass (provider, label, model, effort, since,
                   context_window)
ModelLabelError    raised on schema violations and unknown labels/providers
load_labels(path)  load + validate the TOML; cached per path
resolve_label      look up one (provider, label) pair
labels_for         sorted label names for a provider
known_labels       frozenset union across all providers
labels_path        Path to the bundled TOML

Schema rules (every violation raises ModelLabelError naming the file and table)
-------------------------------------------------------------------------------
- version must equal 1.
- Top-level tables: exactly the three known providers; any other key is an error.
- Label names must match [a-z][a-z0-9-]*.
- Each label table may only contain the keys: model, effort, since, context_window.
- model: required, non-empty string.
- effort: required for claude-code and codex, forbidden for opencode.
- context_window: optional positive int — the model's context window in tokens.
  Set it where it is known; metrics rules that measure context against the
  window skip a label that does not declare one rather than guess.
- Allowed effort values (constants commented with the CLI version they were read from):
    claude-code (CLI 2.1.263): low, medium, high, xhigh, max
      Note: SDK Literal omits xhigh — typing only, no runtime check; keep it here.
    codex (codex-cli 0.153.4): minimal, low, medium, high, xhigh
"""
from __future__ import annotations

import functools
import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

# ---------------------------------------------------------------------------
# Allowed effort values per provider — refreshed from CLI --help on upgrade.
# ---------------------------------------------------------------------------

# claude-code CLI 2.1.263 — SDK Literal omits xhigh (typing only; keep here).
_CLAUDE_CODE_EFFORT: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})
# codex-cli 0.153.4
_CODEX_EFFORT: frozenset[str] = frozenset({"minimal", "low", "medium", "high", "xhigh"})

_KNOWN_PROVIDERS: frozenset[str] = frozenset({"claude-code", "codex", "opencode"})
_EFFORT_REQUIRED: frozenset[str] = frozenset({"claude-code", "codex"})
_EFFORT_FORBIDDEN: frozenset[str] = frozenset({"opencode"})
_ALLOWED_LABEL_KEYS: frozenset[str] = frozenset({"model", "effort", "since", "context_window"})
_LABEL_RE = re.compile(r"^[a-z][a-z0-9-]*$")

_EFFORT_ALLOWED: dict[str, frozenset[str]] = {
    "claude-code": _CLAUDE_CODE_EFFORT,
    "codex": _CODEX_EFFORT,
}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedModel:
    """A fully resolved model label.

    Attributes
    ----------
    provider : str
        The provider this label belongs to (e.g. "claude-code").
    label : str
        The intent label (e.g. "standard").
    model : str
        The pinned model id sent to the provider verbatim.
    effort : str | None
        Always set for claude-code and codex; None for opencode.
    since : str | None
        ISO date string (YYYY-MM-DD) of the last update, or None.
    context_window : int | None
        The model's context window in tokens, or None when the file does not
        declare one.
    """

    provider: str
    label: str
    model: str
    effort: str | None
    since: str | None
    context_window: int | None = None


class ModelLabelError(ValueError):
    """Raised on schema violations in model_labels.toml and on unknown labels."""


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------


def labels_path() -> Path:
    """Return the Path to the bundled norn/model_labels.toml."""
    pkg = resources.files("norn")
    return Path(str(pkg.joinpath("model_labels.toml")))


# ---------------------------------------------------------------------------
# Loader (cached)
# ---------------------------------------------------------------------------


@functools.cache
def load_labels(path: Path | None = None) -> dict[str, dict[str, ResolvedModel]]:
    """Load and validate a model-labels TOML file.

    Parameters
    ----------
    path :
        Path to the TOML file.  Pass ``None`` (the default) to load the
        bundled ``norn/model_labels.toml``.  The result is cached per path.

    Returns
    -------
    dict[str, dict[str, ResolvedModel]]
        Mapping ``provider -> label -> ResolvedModel``.

    Raises
    ------
    ModelLabelError
        On any schema violation, naming the file and the offending table.
    """
    toml_path = labels_path() if path is None else path

    try:
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ModelLabelError(f"model labels file not found: {toml_path}")

    # version
    version = data.get("version")
    if version != 1:
        raise ModelLabelError(
            f"{toml_path}: 'version' must be 1, got {version!r}"
        )

    # unknown top-level tables
    top_keys = set(data.keys()) - {"version"}
    unknown_providers = top_keys - _KNOWN_PROVIDERS
    if unknown_providers:
        raise ModelLabelError(
            f"{toml_path}: unknown top-level table(s): "
            f"{', '.join(sorted(unknown_providers))}; "
            f"allowed providers: {', '.join(sorted(_KNOWN_PROVIDERS))}"
        )

    result: dict[str, dict[str, ResolvedModel]] = {}

    for provider in sorted(_KNOWN_PROVIDERS):
        if provider not in data:
            result[provider] = {}
            continue

        provider_data = data[provider]
        if not isinstance(provider_data, dict):
            raise ModelLabelError(
                f"{toml_path}: [{provider}] must be a table of label tables"
            )

        labels: dict[str, ResolvedModel] = {}
        for label, label_data in provider_data.items():
            table = f"[{provider}.{label}]"

            if not _LABEL_RE.match(label):
                raise ModelLabelError(
                    f"{toml_path}: {table}: label name {label!r} does not match "
                    f"[a-z][a-z0-9-]*"
                )

            if not isinstance(label_data, dict):
                raise ModelLabelError(
                    f"{toml_path}: {table} must be a table"
                )

            unknown_keys = set(label_data.keys()) - _ALLOWED_LABEL_KEYS
            if unknown_keys:
                raise ModelLabelError(
                    f"{toml_path}: {table}: unknown key(s): "
                    f"{', '.join(sorted(unknown_keys))}; "
                    f"allowed: {', '.join(sorted(_ALLOWED_LABEL_KEYS))}"
                )

            model = label_data.get("model")
            if not model or not isinstance(model, str):
                raise ModelLabelError(
                    f"{toml_path}: {table}: 'model' must be a non-empty string"
                )

            effort: str | None = label_data.get("effort")
            if provider in _EFFORT_REQUIRED:
                if effort is None:
                    raise ModelLabelError(
                        f"{toml_path}: {table}: 'effort' is required for "
                        f"provider '{provider}'"
                    )
                if not isinstance(effort, str):
                    raise ModelLabelError(
                        f"{toml_path}: {table}: 'effort' must be a string"
                    )
                allowed_effort = _EFFORT_ALLOWED[provider]
                if effort not in allowed_effort:
                    raise ModelLabelError(
                        f"{toml_path}: {table}: effort {effort!r} is not valid "
                        f"for provider '{provider}'; "
                        f"allowed: {', '.join(sorted(allowed_effort))}"
                    )
            elif provider in _EFFORT_FORBIDDEN:
                if effort is not None:
                    raise ModelLabelError(
                        f"{toml_path}: {table}: 'effort' is forbidden for "
                        f"provider '{provider}' (opencode --variant is not yet verified)"
                    )

            since: str | None = label_data.get("since")

            context_window: int | None = label_data.get("context_window")
            if context_window is not None and (
                not isinstance(context_window, int)
                or isinstance(context_window, bool)
                or context_window <= 0
            ):
                raise ModelLabelError(
                    f"{toml_path}: {table}: 'context_window' must be a positive "
                    f"integer, got {context_window!r}"
                )

            labels[label] = ResolvedModel(
                provider=provider,
                label=label,
                model=model,
                effort=effort,
                since=since,
                context_window=context_window,
            )

        result[provider] = labels

    return result


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


def resolve_label(provider: str, label: str) -> ResolvedModel:
    """Return the ``ResolvedModel`` for *(provider, label)*.

    Raises
    ------
    ModelLabelError
        If the provider is unknown, or if the label is not defined for that
        provider (message lists the valid labels).
    """
    if provider not in _KNOWN_PROVIDERS:
        raise ModelLabelError(
            f"unknown provider {provider!r}; "
            f"known providers: {', '.join(sorted(_KNOWN_PROVIDERS))}"
        )
    all_labels = load_labels()
    provider_labels = all_labels.get(provider, {})
    if label not in provider_labels:
        valid = ", ".join(sorted(provider_labels))
        raise ModelLabelError(
            f"unknown label {label!r} for provider {provider!r}; "
            f"labels: {valid} — see norn/model_labels.toml"
        )
    return provider_labels[label]


def labels_for(provider: str) -> list[str]:
    """Return sorted label names available for *provider*."""
    return sorted(load_labels().get(provider, {}).keys())


def known_labels() -> frozenset[str]:
    """Return the frozenset union of all label names across every provider."""
    result: set[str] = set()
    for provider_labels in load_labels().values():
        result.update(provider_labels.keys())
    return frozenset(result)
