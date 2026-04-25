"""ImplementationPlan and PlanStep — plan-specific pipeline logic.

Uses ``dogfooding.common`` for shared utilities (generate, parse_front_matter,
etc.) and only contains logic specific to implementing a plan from step files.

Orchestration (argv parsing, file discovery, resume) lives in ``pipeline.py``.
This module provides the data model and stage factories.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from norn.dsl import stage_failed
from norn.stages.run_command import RunCommand

from dogfooding.common import (
    first_h1,
    generate,
    parse_front_matter,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from norn.stages.generate import Generate


# ---------------------------------------------------------------------------
# Front-matter value helpers
# ---------------------------------------------------------------------------


def _fm_str(fm: dict, key: str, default: str) -> str:
    """Get a string value from front-matter, falling back to *default*."""
    val = fm.get(key)
    return val if isinstance(val, str) else default


def _fm_list(fm: dict, key: str) -> list[str]:
    """Get a list value from front-matter, falling back to empty list."""
    val = fm.get(key)
    return val if isinstance(val, list) else []


def _build_add_cmd(extra_paths: list[str]) -> str:
    """Build a git add command from ``git add -u`` plus any extra paths."""
    parts = ["git add -u"]
    for p in extra_paths:
        parts.append(f"git add {shlex.quote(p)}")
    return " && ".join(parts)


# ---------------------------------------------------------------------------
# PlanStep
# ---------------------------------------------------------------------------


@dataclass
class PlanStep:
    """A single step parsed from a ``step-*.md`` file."""

    name: str
    """Step name derived from the file stem, e.g. ``"step-01-hooks"``."""

    body: str
    """Markdown body after front-matter has been stripped."""

    source_path: str
    """Original file path of the step markdown file."""

    test_cmd: str
    """pytest command for this step."""

    bats_cmd: str
    """bats command for this step."""

    add_cmd: str
    """git add command (e.g. ``"git add -u"`` or ``"git add -u && git add path"``)."""

    commit_subject: str
    """Commit message subject, e.g. ``"refactor: step-01-hooks — Add hooks"``."""

    @classmethod
    def from_file(
        cls, path: str, *, default_test_cmd: str, default_bats_cmd: str,
    ) -> PlanStep:
        """Parse a step markdown file into a ``PlanStep``."""
        name = Path(path).stem
        fm, body = parse_front_matter(Path(path).read_text())
        h1 = first_h1(body) or name
        return cls(
            name=name,
            body=body,
            source_path=path,
            test_cmd=_fm_str(fm, "test_cmd", default_test_cmd),
            bats_cmd=_fm_str(fm, "bats_cmd", default_bats_cmd),
            add_cmd=_build_add_cmd(_fm_list(fm, "paths")),
            commit_subject=(
                f"refactor: {name} — {h1}" if h1 != name else f"refactor: {name}"
            ),
        )

    def test_failed(self):
        """Return a predicate for use in ``Stage(when=...)``."""
        t, b = f"test {self.name}", f"bats {self.name}"
        return lambda ctx: stage_failed(t)(ctx) or stage_failed(b)(ctx)


# ---------------------------------------------------------------------------
# ImplementationPlan
# ---------------------------------------------------------------------------


class ImplementationPlan:
    """Stage factory that turns ``PlanStep`` objects into pipeline stages.

    Takes pre-built steps and configuration. Orchestration (argv parsing,
    file discovery, resume filtering) is handled by ``pipeline.py``.
    """

    def __init__(
        self,
        steps: list[PlanStep],
        *,
        project_dir: str,
        feature_dir: str = "",
        shared_context: str = "",
    ) -> None:
        self.project_dir = project_dir
        self.feature_dir = feature_dir or project_dir
        self.shared_context = shared_context
        self._prior_summaries: str = ""
        self._steps = list(steps)

        self._all_steps_summary = ""
        for step in self._steps:
            self._all_steps_summary += (
                f"### {Path(step.source_path).name}\n\n{step.body}\n\n---\n\n"
            )

    # --- iteration ----------------------------------------------------------

    def __iter__(self) -> Iterator[PlanStep]:
        return iter(self._steps)

    # --- per-step stage factories -------------------------------------------

    def implement(self, step: PlanStep) -> Generate:
        """Generate stage: implement the step."""
        prior_context = self._prior_context()
        return generate(
            self.project_dir,
            f"{self._shared_context_block()}"
            f"{prior_context}"
            f"## Step to implement\n\n"
            f"### Source: {step.source_path}\n\n"
            f"{step.body}\n\n"
            "## Instructions\n"
            "- Read the relevant source files before making changes\n"
            "- Implement exactly what this step describes, nothing more\n"
            "- Follow the existing code style and conventions in the project\n"
            "- No fallbacks or similar — fail fast and hard\n"
            "- Do not change unrelated code\n"
            "- Tests must pass after this step\n"
            "- If this step has no tests, add a placeholder test that always succeeds\n"
        )

    def fix(self, step: PlanStep) -> Generate:
        """Generate stage: fix test failures for the step."""
        test_name = f"test {step.name}"
        bats_name = f"bats {step.name}"
        return generate(
            self.project_dir,
            f"{self._shared_context_block()}"
            "## Fix test failures\n"
            "The tests failed. Fix the code so the tests pass.\n\n"
            f"### pytest output\n{{{test_name}.output}}\n\n"
            f"### bats output\n{{{bats_name}.output}}\n"
        )

    def test(self, step: PlanStep) -> RunCommand:
        """Run the step's pytest command."""
        return RunCommand(
            cmd=f"cd {shlex.quote(self.project_dir)} && {step.test_cmd}"
        )

    def bats(self, step: PlanStep) -> RunCommand:
        """Run the step's bats command."""
        return RunCommand(
            cmd=f"cd {shlex.quote(self.project_dir)} && {step.bats_cmd}"
        )

    def commit(self, step: PlanStep) -> RunCommand:
        """Scoped git add + commit for the step."""
        return RunCommand(cmd=(
            f'cd {shlex.quote(self.project_dir)} && '
            f'{step.add_cmd} && '
            f'(git diff --cached --quiet && echo "nothing to commit") || '
            f'printf %s {shlex.quote(step.commit_subject)} | git commit -F -'
        ))

    def summarize(self, step: PlanStep) -> Generate:
        """Generate stage: summarize what was done in the step.

        Side effect: appends a ``{summarize <name>.output}`` placeholder to
        ``_prior_summaries`` so subsequent steps see earlier summaries.
        """
        summarize_name = f"summarize {step.name}"
        gen = generate(
            self.project_dir,
            f"You just implemented step `{step.name}` from:\n"
            f"### Source: {step.source_path}\n\n"
            f"{step.body}\n\n"
            "## Task\n"
            "Write a concise summary (3-5 bullet points) of what was actually "
            "implemented in this step. Focus on:\n"
            "- What files were created or modified\n"
            "- Key design decisions made\n"
            "- Any deviations from the step description\n"
            "- Important details the next step's implementer should know\n\n"
            "Output ONLY the bullet points, no preamble.\n",
            read_only=True,
        )
        # Grow the running summary for subsequent steps.
        self._prior_summaries += f"### {step.name}\n{{{summarize_name}.output}}\n\n"
        return gen

    # --- end stage factories ------------------------------------------------

    def review(self) -> Generate:
        """Generate stage: review all changes against the plan."""
        return generate(
            self.project_dir,
            "## Task: Review all implementation changes against the plan\n\n"
            "The starting commit (before any steps were implemented) is:\n"
            "{record start.output}\n\n"
            "Run `git diff {record start.output}..HEAD` and "
            "`git log --oneline {record start.output}..HEAD` "
            "to see all changes made during this pipeline run.\n\n"
            f"{self._shared_context_block()}"
            "## Plan — all steps\n\n"
            f"{self._all_steps_summary}\n\n"
            "## Instructions\n"
            "1. Read the full diff from the starting commit to HEAD\n"
            "2. For each step in the plan, verify that:\n"
            "   - The implementation matches what was requested\n"
            "   - No unrelated changes were introduced\n"
            "   - Code style and conventions are consistent\n"
            "   - Tests were added where required\n"
            "3. Check for cross-step issues: naming inconsistencies, "
            "duplicated code, missing integrations between steps\n"
            "4. Write the review to a file at "
            f"{self.feature_dir}/review.md with:\n"
            "   - A summary verdict (pass / pass with notes / needs fixes)\n"
            "   - Per-step compliance checklist\n"
            "   - Any issues found, with file paths and line numbers\n"
            "   - Suggestions for improvement (if any)\n",
            read_only=True,
        )

    def handoff(self) -> Generate:
        """Generate stage: create a handoff document."""
        return generate(
            self.project_dir,
            "## Task: Create a handoff document\n\n"
            "The starting commit (before any steps were implemented) is:\n"
            "{record start.output}\n\n"
            "Run `git diff --stat {record start.output}..HEAD` and "
            "`git log --oneline {record start.output}..HEAD` to see the scope of changes.\n\n"
            f"{self._shared_context_block()}"
            "## Instructions\n"
            "Create a handoff document at "
            f"{self.feature_dir}/handoff.md that includes:\n\n"
            "1. **Overview** — what was built and why (1-2 paragraphs)\n"
            "2. **Changes summary** — list of all files added/modified/deleted, "
            "grouped by feature area\n"
            "3. **New functionality** — what the user can now do that they couldn't before\n"
            "4. **Architecture decisions** — key design choices made during implementation\n"
            "5. **Configuration** — any new env vars, config files, or settings introduced\n"
            "6. **Testing** — what tests were added and how to run them\n"
            "7. **Known limitations** — anything deferred or requiring follow-up work\n"
            "8. **Dependencies** — any new dependencies added\n\n"
            "Read the actual changed files to understand what was built — "
            "don't just summarize the plan, summarize the implementation.\n",
            read_only=True,
        )

    # --- private helpers ----------------------------------------------------

    def _shared_context_block(self) -> str:
        """Return the shared-context block for prompt prefixes, or empty string."""
        if not self.shared_context:
            return ""
        return (
            "## Shared context (applies to every step)\n\n"
            f"{self.shared_context}\n\n"
            "---\n\n"
        )

    def _prior_context(self) -> str:
        """Return the accumulated summaries block, or empty string."""
        if not self._prior_summaries:
            return ""
        return (
            "## What was done in prior steps\n\n"
            f"{self._prior_summaries}\n\n"
        )
