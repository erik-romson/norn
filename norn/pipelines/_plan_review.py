"""Private helper: shared review-flow builder for plan_with_review and fix_jira_issue."""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from norn.dsl import Pipeline, Stage, ask_user, fail, stage_failed
from norn.models import PipelineContext, StageResult
from norn.pipelines._plan_gates import OpenQuestionsGate, ReviewDispositionGate, StepFilesGate
from norn.skills import Skill, resolve_skill_content
from norn.stages.base import BaseStage
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand
from norn.stages.validate import Contains, FileExists, Validate

# ---------------------------------------------------------------------------
# Skill resolver
# ---------------------------------------------------------------------------

_NORN_SPLIT_SKILL = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "split-plan" / "SKILL.md"


def resolve_split_skill(fallback: Path = _NORN_SPLIT_SKILL) -> str | Skill:
    """Resolve the split-plan skill without ever failing the import.

    A project copy wins (cwd, then ``~/.claude``); norn's own checkout is the
    fallback, so the stage still works when the pipeline runs from a repo that
    has no skill of its own.  When nothing resolves, return the bare name and
    let the stage report the miss — raising here would make the pipeline
    unloadable, and an unloadable pipeline cannot even be listed in the TUI.

    Resolving now (at import time) rather than per-stage keeps the launch
    repo's copy in force under the worktree toggle.
    """
    try:
        return Skill(name="split-plan", content=resolve_skill_content("split-plan"))
    except FileNotFoundError:
        if fallback.is_file():
            return Skill(name="split-plan", content=fallback.read_text())
        return "split-plan"


# ---------------------------------------------------------------------------
# WaitForApproval stage
# ---------------------------------------------------------------------------


class WaitForApproval(BaseStage):
    """Always fails with the approval message so the runner fires ASK_USER."""

    needs_agent = False

    def __init__(self, *, plan: str) -> None:
        super().__init__()
        self._plan = plan

    async def run(self, ctx: PipelineContext, **kwargs: Any) -> StageResult:
        return StageResult(
            name="",
            success=False,
            error=(
                f"Plan finished: {self._plan}\n"
                "Read it and edit it freely. Then choose [c]ontinue to run the "
                "codex review, apply it, split the plan into steps and implement "
                "them -- or [a]bort to stop here (a later `--resume` prompts again)."
            ),
        )


# ---------------------------------------------------------------------------
# Prompt constructors
# ---------------------------------------------------------------------------


def draft_prompt(*, preplan: str, plan: str, questions: str) -> str:
    return f"""\
Read the pre-plan at {preplan} and produce two files:

1. **The plan** — write it to {plan}.
   This is the deliverable: a complete, actionable plan that covers everything
   the pre-plan asks for.

2. **Open questions** — write them to {questions}.
   Use exactly this format:

   ```
   # Open questions — <slug>

   STATUS: NEEDS_INPUT

   ## Q1. <short question>
   **Why it matters:** <one sentence>
   **Assumption if unanswered:** <what you will assume>
   **Answer:**

   ## Q2. <short question>
   ...
   ```

   Rules for the questions file:
   - `STATUS:` is yours to write. The human fills `**Answer:**` slots only.
   - Every question MUST have `**Why it matters:**` and
     `**Assumption if unanswered:**` lines.
   - If the pre-plan is already complete and you have nothing to ask,
     write `STATUS: READY` with the body `No open questions.` —
     do NOT invent questions.
   - When there are genuine open questions, write `STATUS: NEEDS_INPUT`.
"""


def revise_prompt(*, questions: str, plan: str) -> str:
    return f"""\
Re-read the questions file at {questions}.
The human has filled in some or all `**Answer:**` slots.

1. Apply every answered question to the plan at {plan}.
2. Rewrite {questions}:
   - If everything is settled, flip to `STATUS: READY`.
   - If you need narrower follow-ups, keep `STATUS: NEEDS_INPUT`
     with new or refined questions.
   - Never blank an `**Answer:**` slot the human already filled.
   - Never invent an answer on the human's behalf.
"""


def review_prompt(*, plan: str, preplan: str, review: str) -> str:
    return (
        f"Review the plan at {plan} against the pre-plan at {preplan}. "
        f"Write your review to {review} with sections `## Verdict` and "
        f"`## Findings`. Each finding is `### F<n> — title [severity]` with "
        f"`**Where:**`, `**Problem:**`, and `**Suggested change:**` sub-fields."
    )


def apply_prompt(*, plan: str, review: str, response: str) -> str:
    return f"""\
Read the plan at {plan} and the codex review at {review}.

1. For each finding you accept, apply the suggested change to {plan}.
2. Write a response to {response} with exactly this format:

   ```
   # Review response — <slug>

   - F1: accepted — <why / what changed>
   - F2: rejected — <why>
   - F3: deferred — <why>
   ```

   Rules:
   - Every `F<n>` heading in {review} MUST appear in {response},
     including findings you reject.
   - Each line must contain `accepted`, `rejected`, or `deferred`.
"""


def split_prompt(*, plan: str, steps_dir: str) -> str:
    return f"""\
Split the finished plan into the step files `norn run implement_features` runs.

Follow the split-plan skill in your system prompt, with its arguments bound to:

- `$0` (plan file)        = {plan}
- `$1` (output directory) = {steps_dir}

{plan} is reviewed and final: read it, do not rewrite it. Create {steps_dir} if
it does not exist and write into it:

- `index.md` — the context that makes any one step implementable on its own
- `step-NN-name.md` — one per step, in dependency order, front-matter first
- `README.md` — the step overview table

Every step needs a real `test_cmd:` — never `true`, never a placeholder. Look at
the repo before writing one: use the runner it already uses, and name a target
that exists today or that this same step creates.

This stage runs unattended, so a step whose test target is unclear cannot be
raised as a question. Point its `test_cmd` at the narrowest existing suite that
would still catch a regression there, and say why under that step's
`Why this test_cmd is the right contract` section.

A gate reads {steps_dir} when you stop. It fails this stage when `index.md` is
missing, when no `step-*.md` file exists, or when any step file has no real
`test_cmd:` — a retry lands you back here with the same instructions.
"""


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]


def add_plan_review_stages(
    pipeline: Pipeline,
    *,
    preplan: str,
    plan: str,
    questions: str,
    review: str,
    response: str,
    steps_dir: str,
    repo_dir: str,
    model: str,
    codex_model: str | None = None,
    split_skill: str | Skill,
    pause_for_approval: bool = False,
) -> Pipeline:
    """Add the full review-flow stage sequence to *pipeline* and return it.

    Stage names are identical to the historical ``plan_with_review`` names so
    that checkpoint files and test assertions remain stable.

    Parameters
    ----------
    pipeline:
        The ``Pipeline`` to extend.  The caller owns wiring up alerts, budget,
        and metadata; this function only adds stages.
    preplan:
        Absolute path to the pre-plan markdown file.
    plan:
        Absolute path for the output plan file.
    questions:
        Absolute path for the questions file.
    review:
        Absolute path for the codex review output.
    response:
        Absolute path for the review response.
    steps_dir:
        Absolute path for the step-files directory.
    repo_dir:
        Absolute repo root passed to ``codex exec -C``.
    model:
        Claude model shorthand for Generate stages.
    codex_model:
        Model passed to ``codex exec -m``; ``None`` omits the flag.
    split_skill:
        Resolved skill object (or bare name) for the split-plan stage.
    pause_for_approval:
        When ``True``, insert a ``WaitForApproval`` gate after ``open
        questions resolved`` so a human can inspect and edit the plan before
        the codex review runs.
    """
    _draft_p = draft_prompt(preplan=preplan, plan=plan, questions=questions)
    _revise_p = revise_prompt(questions=questions, plan=plan)
    _review_p = review_prompt(plan=plan, preplan=preplan, review=review)
    _apply_p = apply_prompt(plan=plan, review=review, response=response)
    _split_p = split_prompt(plan=plan, steps_dir=steps_dir)

    codex_model_flag = f"-m {shlex.quote(codex_model)} " if codex_model else ""
    codex_cmd = (
        "codex exec --sandbox read-only --skip-git-repo-check "
        + codex_model_flag
        + f"-C {shlex.quote(repo_dir)} -o {shlex.quote(review)} "
        + shlex.quote(_review_p)
    )

    # 0. Fail fast before spending a single token.
    pipeline.stage(
        "preflight",
        RunCommand(cmd=(
            f'command -v codex >/dev/null || {{ echo "codex CLI not found — brew install codex"; exit 1; }}; '
            f'test -f {shlex.quote(preplan)} || {{ echo "pre-plan not found: {preplan}"; exit 1; }}'
        )),
        on_failure=fail,
    )

    # 1. Draft the plan + the questions file.
    pipeline.stage(
        "draft plan",
        Generate(
            prompt=_draft_p,
            model=model,
            allowed_tools=_TOOLS,
            permission_mode="acceptEdits",
            setting_sources=["project"],
        ),
        on_failure=ask_user,
    )

    # 2. Human-in-the-loop until no open questions remain.
    pipeline.loop(
        "resolve open questions",
        max_retries=1,
        on_exhaust=ask_user,
        stages=[
            Stage(
                "revise plan",
                Generate(
                    prompt=_revise_p,
                    model=model,
                    allowed_tools=_TOOLS,
                    permission_mode="acceptEdits",
                    setting_sources=["project"],
                ),
                when=stage_failed("check open questions"),
            ),
            Stage("check open questions", OpenQuestionsGate(path=questions)),
        ],
    )

    # 2b. Hard gate: `c` at the pause above lands here and stops the run.
    pipeline.stage(
        "open questions resolved",
        OpenQuestionsGate(path=questions),
        on_failure=fail,
    )

    # 2c. Optional human approval pause before codex review.
    if pause_for_approval:
        pipeline.stage(
            "wait for plan approval",
            WaitForApproval(plan=plan),
            on_failure=ask_user,
        )

    # 3. Codex reviews the plan (read-only).
    pipeline.stage(
        "codex review",
        RunCommand(cmd=codex_cmd, timeout=1800),
        on_failure=ask_user,
    )

    pipeline.stage(
        "check review shape",
        Validate(checks=[
            FileExists(review),
            Contains(review, patterns=["## Verdict", "## Findings"]),
        ]),
        on_failure=fail,
    )

    # 4. Claude applies the review and logs a disposition per finding.
    pipeline.clear_context()
    pipeline.loop(
        "apply review",
        max_retries=2,
        on_exhaust=ask_user,
        stages=[
            Stage(
                "apply findings",
                Generate(
                    prompt=_apply_p,
                    model=model,
                    allowed_tools=_TOOLS,
                    permission_mode="acceptEdits",
                    setting_sources=["project"],
                ),
            ),
            Stage(
                "check dispositions",
                ReviewDispositionGate(review=review, response=response),
            ),
        ],
    )

    # 4b. Hard gate, same reason as 2b.
    pipeline.stage(
        "dispositions recorded",
        ReviewDispositionGate(review=review, response=response),
        on_failure=fail,
    )

    # 5. Split the reviewed plan into step files implement_features can run.
    pipeline.clear_context()
    pipeline.loop(
        "split plan",
        max_retries=2,
        on_exhaust=ask_user,
        stages=[
            Stage(
                "write step files",
                Generate(
                    prompt=_split_p,
                    skills=[split_skill],
                    model=model,
                    allowed_tools=_TOOLS,
                    permission_mode="acceptEdits",
                    setting_sources=["project"],
                ),
            ),
            Stage("check step files", StepFilesGate(steps_dir=steps_dir)),
        ],
    )

    # 5b. Hard gate, same reason as 2b.
    pipeline.stage(
        "step files written",
        StepFilesGate(steps_dir=steps_dir),
        on_failure=fail,
    )

    pipeline.stage(
        "summary",
        RunCommand(cmd=(
            f"ls -l {shlex.quote(plan)} {shlex.quote(review)} {shlex.quote(response)}; "
            f"ls -l {shlex.quote(steps_dir)}"
        )),
    )

    return pipeline
