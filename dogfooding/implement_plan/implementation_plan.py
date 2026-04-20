"""ImplementationPlan and PlanStep — reusable, testable extraction of implement_features.py logic.

Encapsulates step discovery, front-matter parsing, prompt generation, and stage
factories so that pipeline configs become thin wiring.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path
from typing import TYPE_CHECKING

from norn.dsl import stage_failed
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

if TYPE_CHECKING:
    from collections.abc import Iterator


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
    """Commit message subject, e.g. ``"refactor: step-01-hooks \u2014 Add hooks"``."""

    def test_failed(self):
        """Return a predicate for use in ``Stage(when=...)``."""
        t, b = f"test {self.name}", f"bats {self.name}"
        return lambda ctx: stage_failed(t)(ctx) or stage_failed(b)(ctx)


# ---------------------------------------------------------------------------
# Helpers (same logic as implement_features.py)
# ---------------------------------------------------------------------------


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """Parse a tiny subset of YAML front-matter: ``key: value`` and ``key:``
    + indented ``- item`` lists.  Returns ``(dict, body)``."""
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    block, body = m.group(1), m.group(2)
    data: dict = {}
    current_list_key: str | None = None
    for raw_line in block.splitlines():
        if not raw_line.strip():
            current_list_key = None
            continue
        if raw_line.lstrip().startswith("- ") and current_list_key:
            data[current_list_key].append(raw_line.lstrip()[2:].strip())
            continue
        if ":" in raw_line:
            k, v = raw_line.split(":", 1)
            k, v = k.strip(), v.strip()
            if v == "":
                data[k] = []
                current_list_key = k
            else:
                data[k] = v
                current_list_key = None
    return data, body


def _first_h1(text: str) -> str | None:
    """Return the text of the first ``# H1`` heading, or ``None``."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _already_committed_steps(project_dir: str) -> set[str]:
    """Return step names whose ``refactor: <name>`` commit is already on HEAD.

    Used for resume support \u2014 those steps are skipped at build time.
    """
    try:
        out = subprocess.check_output(
            ["git", "-C", project_dir, "log", "--pretty=%s", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return set()
    done = set()
    for subject in out.splitlines():
        m = re.match(r"^refactor:\s+(\S+)", subject)
        if m:
            done.add(m.group(1))
    return done


# ---------------------------------------------------------------------------
# ImplementationPlan
# ---------------------------------------------------------------------------


class ImplementationPlan:
    """Stage factory that reproduces the behaviour of ``implement_features.py``.

    Discovers step files, parses front-matter, and exposes factory methods
    that return ``Generate`` / ``RunCommand`` instances with identical prompts
    and commands to the original pipeline.
    """

    def __init__(
        self,
        *,
        project_dir: str,
        test_cmd: str,
        bats_cmd: str,
        feature_dir: str | None = None,
        argv: list[str] | None = None,
    ) -> None:
        self.project_dir = project_dir
        self._prior_summaries: str = ""

        # --- resolve the target directory from argv -------------------------
        if feature_dir is None:
            raw_args = argv if argv is not None else sys.argv[1:]
            for arg in raw_args:
                candidate = Path(arg)
                if candidate.is_dir():
                    feature_dir = str(candidate)
                    break
                candidate = Path(project_dir) / arg
                if candidate.is_dir():
                    feature_dir = str(candidate)
                    break
            if feature_dir is None:
                feature_dir = os.path.join(project_dir, "tmp")

        self.feature_dir = feature_dir

        # --- load shared context (index.md) ---------------------------------
        index_path = Path(feature_dir) / "index.md"
        feature_test_cmd = test_cmd
        feature_bats_cmd = bats_cmd
        self._shared_context = ""

        if index_path.exists():
            fm, body = _parse_front_matter(index_path.read_text())
            if isinstance(fm.get("test_cmd"), str):
                feature_test_cmd = fm["test_cmd"]
            if isinstance(fm.get("bats_cmd"), str):
                feature_bats_cmd = fm["bats_cmd"]
            self._shared_context = (
                "## Shared context (from index.md \u2014 applies to every step)\n\n"
                f"{body}\n\n"
                "---\n\n"
            )

        # --- discover step files --------------------------------------------
        step_files = sorted(glob(os.path.join(feature_dir, "step-*.md")))
        if not step_files:
            step_files = sorted(
                f for f in glob(os.path.join(feature_dir, "*.md"))
                if Path(f).name not in ("index.md", "README.md", "refactor-plan.md")
            )
        if not step_files:
            from norn.runner import PipelineError

            raise PipelineError(
                f"No step-*.md files found in {feature_dir}\n"
                "Usage: norn run dogfooding/implement_features.py <directory>"
            )

        # --- resume: drop steps already committed ---------------------------
        done = _already_committed_steps(project_dir)
        skipped_for_resume = [f for f in step_files if Path(f).stem in done]
        step_files = [f for f in step_files if Path(f).stem not in done]

        if skipped_for_resume:
            print(
                f"[implement-features] resume: skipping {len(skipped_for_resume)} "
                f"already-committed steps: "
                + ", ".join(Path(f).stem for f in skipped_for_resume),
                file=sys.stderr,
            )

        # --- build PlanStep list --------------------------------------------
        self._steps: list[PlanStep] = []
        for step_file in step_files:
            name = Path(step_file).stem
            raw_step_text = Path(step_file).read_text()
            step_fm, step_body = _parse_front_matter(raw_step_text)

            step_test_cmd = (
                step_fm.get("test_cmd")
                if isinstance(step_fm.get("test_cmd"), str)
                else None
            )
            step_test_cmd = step_test_cmd or feature_test_cmd

            step_bats_cmd = (
                step_fm.get("bats_cmd")
                if isinstance(step_fm.get("bats_cmd"), str)
                else None
            )
            step_bats_cmd = step_bats_cmd or feature_bats_cmd

            extra_paths = (
                step_fm.get("paths")
                if isinstance(step_fm.get("paths"), list)
                else []
            )
            add_cmd_parts = ["git add -u"]
            for p in extra_paths:
                add_cmd_parts.append(f"git add {shlex.quote(p)}")
            add_cmd = " && ".join(add_cmd_parts)

            h1 = _first_h1(step_body) or name
            commit_subject = (
                f"refactor: {name} \u2014 {h1}" if h1 != name else f"refactor: {name}"
            )

            self._steps.append(
                PlanStep(
                    name=name,
                    body=step_body,
                    source_path=step_file,
                    test_cmd=step_test_cmd,
                    bats_cmd=step_bats_cmd,
                    add_cmd=add_cmd,
                    commit_subject=commit_subject,
                )
            )

        # --- collect all step contents for review/handoff -------------------
        self._all_steps_summary = ""
        for step in self._steps:
            self._all_steps_summary += (
                f"### {Path(step.source_path).name}\n\n"
                f"{Path(step.source_path).read_text()}\n\n---\n\n"
            )

    # --- classmethod for testing without file I/O ---------------------------

    @classmethod
    def from_steps(
        cls,
        steps: list[PlanStep],
        *,
        project_dir: str = "/tmp",
        shared_context: str = "",
    ) -> ImplementationPlan:
        """Create an instance without file I/O, for testing."""
        instance = object.__new__(cls)
        instance.project_dir = project_dir
        instance.feature_dir = project_dir
        instance._steps = list(steps)
        instance._prior_summaries = ""
        if shared_context:
            instance._shared_context = (
                "## Shared context (from index.md \u2014 applies to every step)\n\n"
                f"{shared_context}\n\n"
                "---\n\n"
            )
        else:
            instance._shared_context = ""
        instance._all_steps_summary = ""
        for step in steps:
            instance._all_steps_summary += (
                f"### {Path(step.source_path).name}\n\n{step.body}\n\n---\n\n"
            )
        return instance

    # --- iteration ----------------------------------------------------------

    def __iter__(self) -> Iterator[PlanStep]:
        return iter(self._steps)

    # --- gate stage factories -----------------------------------------------

    def clean_worktree(self) -> RunCommand:
        """Gate: fail if the working tree is dirty."""
        return RunCommand(cmd=(
            f'cd {shlex.quote(self.project_dir)} && '
            'if [ -n "$(git status --porcelain)" ]; then '
            'echo "ERROR: Working tree is not clean. Commit or .gitignore these files:" && '
            'git status --short && exit 1; fi'
        ))

    def preflight(self, *tools: str) -> RunCommand:
        """Gate: check that each tool is on PATH."""
        parts = [f'cd {shlex.quote(self.project_dir)}']
        for tool in tools:
            parts.append(
                f'command -v {shlex.quote(tool)} >/dev/null || '
                f'{{ echo "ERROR: {tool} not on PATH"; exit 1; }}'
            )
        # Print versions for the last two tools (matching original behaviour)
        for tool in tools:
            parts.append(f'{shlex.quote(tool)} --version')
        return RunCommand(cmd=" && ".join(parts))

    def record_start(self) -> RunCommand:
        """Capture the starting commit SHA."""
        return RunCommand(
            cmd=f"cd {shlex.quote(self.project_dir)} && git rev-parse HEAD"
        )

    # --- per-step stage factories -------------------------------------------

    def implement(self, step: PlanStep) -> Generate:
        """Generate stage: implement the step."""
        prior_context = self._prior_context()
        return self._generate(
            f"{self._shared_context}"
            f"{prior_context}"
            f"## Step to implement\n\n"
            f"### Source: {step.source_path}\n\n"
            f"{step.body}\n\n"
            "## Instructions\n"
            "- Read the relevant source files before making changes\n"
            "- Implement exactly what this step describes, nothing more\n"
            "- Follow the existing code style and conventions in the project\n"
            "- No fallbacks or similar \u2014 fail fast and hard\n"
            "- Do not change unrelated code\n"
            "- Tests must pass after this step\n"
            "- If this step has no tests, add a placeholder test that always succeeds\n"
        )

    def fix(self, step: PlanStep) -> Generate:
        """Generate stage: fix test failures for the step."""
        test_name = f"test {step.name}"
        bats_name = f"bats {step.name}"
        return self._generate(
            f"{self._shared_context}"
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
        gen = self._generate(
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
        return self._generate(
            "## Task: Review all implementation changes against the plan\n\n"
            "The starting commit (before any steps were implemented) is:\n"
            "{record start.output}\n\n"
            "Run `git diff {record start.output}..HEAD` and "
            "`git log --oneline {record start.output}..HEAD` "
            "to see all changes made during this pipeline run.\n\n"
            f"{self._shared_context}"
            "## Plan \u2014 all steps\n\n"
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
        return self._generate(
            "## Task: Create a handoff document\n\n"
            "The starting commit (before any steps were implemented) is:\n"
            "{record start.output}\n\n"
            "Run `git diff --stat {record start.output}..HEAD` and "
            "`git log --oneline {record start.output}..HEAD` to see the scope of changes.\n\n"
            f"{self._shared_context}"
            "## Instructions\n"
            "Create a handoff document at "
            f"{self.feature_dir}/handoff.md that includes:\n\n"
            "1. **Overview** \u2014 what was built and why (1-2 paragraphs)\n"
            "2. **Changes summary** \u2014 list of all files added/modified/deleted, "
            "grouped by feature area\n"
            "3. **New functionality** \u2014 what the user can now do that they couldn't before\n"
            "4. **Architecture decisions** \u2014 key design choices made during implementation\n"
            "5. **Configuration** \u2014 any new env vars, config files, or settings introduced\n"
            "6. **Testing** \u2014 what tests were added and how to run them\n"
            "7. **Known limitations** \u2014 anything deferred or requiring follow-up work\n"
            "8. **Dependencies** \u2014 any new dependencies added\n\n"
            "Read the actual changed files to understand what was built \u2014 "
            "don't just summarize the plan, summarize the implementation.\n",
            read_only=True,
        )

    # --- private helpers ----------------------------------------------------

    def _generate(self, prompt: str, *, read_only: bool = False) -> Generate:
        """Build a ``Generate`` with the standard working-dir preamble and tools."""
        full_prompt = (
            f"## Working directory\n{self.project_dir}\n\n"
            "IMPORTANT: When creating or editing files, always use absolute paths "
            f"based on {self.project_dir}.\n\n"
            f"{prompt}"
        )
        if read_only:
            allowed_tools = ["Read", "Glob", "Grep", "Bash"]
        else:
            allowed_tools = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
        return Generate(
            prompt=full_prompt,
            allowed_tools=allowed_tools,
            permission_mode="acceptEdits",
            cwd=self.project_dir,
            setting_sources=["project"],
        )

    def _prior_context(self) -> str:
        """Return the accumulated summaries block, or empty string."""
        if not self._prior_summaries:
            return ""
        return (
            "## What was done in prior steps\n\n"
            f"{self._prior_summaries}\n\n"
        )
