"""Private helper: the comment-style rule shared by ``merge_branch`` and
``check_and_fix_ci`` (from step 12).

Not a pipeline module — ``norn/catalog.py`` skips anything under
``norn/pipelines/`` whose name starts with ``_``. Answers plan Q5: every
prompt that writes code, commits or PR text appends ``COMMENT_RULE``, and
every commit these pipelines make is checked against ``ATTRIBUTION_ERE`` —
either halting (``merge_branch``) or refusing to commit (``check_and_fix_ci``)
when it matches. The check never strips a matching line; it only stops.
"""
from __future__ import annotations

import shlex

COMMENT_RULE = (
    "- Keep everything you write short and plain: code comments, commit messages\n"
    "  and PR text. Use one line where one line will do. Never mention Claude,\n"
    "  Anthropic, an AI or an assistant, and never add a \"Generated with\" or\n"
    "  \"Co-Authored-By\" line."
)

# POSIX ERE, used case-insensitively by both `grep -iE` in shell gates and
# `re.compile(ATTRIBUTION_ERE, re.I)` in Python. Matches attribution lines
# only, so "claude_code.py" or "the Claude provider" does not trip it.
ATTRIBUTION_ERE = r"co-authored-by:.*(claude|anthropic)|generated (with|by) \[?claude"


def attribution_guard_sh(messages_range: str | None = None) -> str:
    """Return a POSIX ``sh`` fragment that guards against assistant attribution.

    Greps the staged added lines (``git diff --cached -U0``) and, when
    *messages_range* is given, the commit messages in that range
    (``git log --format=%B <range>``), both against ``ATTRIBUTION_ERE``. On a
    match it prints the matching lines and an error to stderr and exits 1;
    otherwise it falls through with exit 0. One compound command (``{ ...; }``)
    so it composes inside a ``git add -u && ... && git commit`` chain; it ends
    at the closing brace, because a newline before the next ``&&`` is a shell
    syntax error that only surfaces when the guard passes. Never
    checks the human's own commits: callers pass only a range an agent alone
    could have filled.
    """
    pattern = shlex.quote(ATTRIBUTION_ERE)
    range_block = ""
    if messages_range:
        range_block = (
            f"  if LOG_MATCHES=$(git log --format=%B {shlex.quote(messages_range)} "
            f"| grep -iE {pattern}); then\n"
            "    printf '%s\\n' \"$LOG_MATCHES\" >&2\n"
            "    FOUND=1\n"
            "  fi\n"
        )
    return (
        "{\n"
        "  FOUND=0\n"
        f"  if DIFF_MATCHES=$(git diff --cached -U0 | grep '^+' | grep -iE {pattern}); then\n"
        "    printf '%s\\n' \"$DIFF_MATCHES\" >&2\n"
        "    FOUND=1\n"
        "  fi\n"
        f"{range_block}"
        "  if [ \"$FOUND\" = 1 ]; then\n"
        "    echo 'ERROR: assistant attribution; refusing to commit' >&2\n"
        "    exit 1\n"
        "  fi\n"
        "  true\n"
        "}"
    )
