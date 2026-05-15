from __future__ import annotations

from dataclasses import dataclass

# Tool name → category mappings
_FILE_READ_TOOLS: frozenset[str] = frozenset({"Read"})
_FILE_EDIT_TOOLS: frozenset[str] = frozenset({"Write", "Edit", "NotebookEdit"})
_TERMINAL_TOOLS: frozenset[str] = frozenset({"Bash"})


@dataclass(frozen=True)
class AgentPermissions:
    """Provider-neutral representation of what an agent is allowed to do.

    Computed from ``allowed_tools`` and ``permission_mode``; used by providers
    and ``Generate`` to make permission-related decisions without knowing the
    original mode names or tool lists.

    Attributes:
        file_read: Agent may read files.
        file_edit: Agent may write or edit files.
        terminal: Agent may run terminal/shell commands.
        plan_only: Agent is in plan-only mode; no file edits or terminal access.
    """

    file_read: bool = False
    file_edit: bool = False
    terminal: bool = False
    plan_only: bool = False


def normalize_permissions(
    allowed_tools: list[str] | None,
    permission_mode: str | None,
) -> AgentPermissions:
    """Convert Claude-style permission fields to provider-neutral ``AgentPermissions``.

    Tool mapping:

    - ``Read`` → ``file_read``
    - ``Write``, ``Edit``, ``NotebookEdit`` → ``file_edit``
    - ``Bash`` → ``terminal``

    Permission mode mapping:

    - ``None`` or ``"default"``: provider default; categories derived from ``allowed_tools``.
    - ``"acceptEdits"``: ``file_edit=True``; ``terminal`` only if ``Bash`` is in ``allowed_tools``.
    - ``"bypassPermissions"``: ``file_edit=True`` and ``terminal=True``.
    - ``"plan"``: ``plan_only=True``; ``file_edit`` and ``terminal`` forced ``False``.

    Args:
        allowed_tools: List of pre-approved tool names (may be ``None``).
        permission_mode: Agent permission level string (may be ``None``).

    Returns:
        An immutable ``AgentPermissions`` instance.
    """
    tools: frozenset[str] = frozenset(allowed_tools) if allowed_tools else frozenset()

    # Derive per-category flags from explicit tool names
    file_read: bool = bool(tools & _FILE_READ_TOOLS)
    file_edit: bool = bool(tools & _FILE_EDIT_TOOLS)
    terminal: bool = bool(tools & _TERMINAL_TOOLS)
    plan_only: bool = False

    # permission_mode overrides/extends the per-tool flags
    if permission_mode == "bypassPermissions":
        file_edit = True
        terminal = True
    elif permission_mode == "acceptEdits":
        file_edit = True
        # terminal is only True when Bash was explicitly listed in allowed_tools
    elif permission_mode == "plan":
        file_edit = False
        terminal = False
        plan_only = True
    # None / "default" → use only what allowed_tools implied above

    return AgentPermissions(
        file_read=file_read,
        file_edit=file_edit,
        terminal=terminal,
        plan_only=plan_only,
    )
