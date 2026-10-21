"""Private helper: the plan-review flow builder for fix_jira_issue_v3 — not a pipeline.

The ONE home of ``add_plan_review_stages_v3``.  It adds stages 11-30 of
fix_jira_issue_v3's stage table.  Compared with ``_plan_review_v2``:

* gate 1 (the open questions) gains ``**Recommended answer:**`` lines and an
  ``expand recommended answers`` stage;
* gate 2 (plan approval) moves to after the codex review, just before the split.

``_plan_review_v2.add_plan_review_stages`` has a fixed pause position, so only
its stage wiring is duplicated here; prompts, gates and helpers are imported.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

from norn.dsl import Pipeline, Stage, ask_user, fail
from norn.model_labels import resolve_label
from norn.models import PipelineContext
from norn.pipelines._decisions_v3 import (
    AUTO_RECOMMENDED_SUFFIX,
    RECOMMENDED_SUFFIX,
    AutoDecide,
    DecisionGate,
    ExpandRecommendedAnswers,
    QuestionsGate,
    WriteDecision,
    questions_need_revision,
    recommendation_problems,
)
from norn.pipelines._plan_gates_v2 import (
    PlanSectionsGate,
    ReviewDispositionGate,
    StepFilesGate,
    missing_plan_sections,
    parse_questions,
)
from norn.pipelines._plan_review_v2 import (
    _DRAFT_TOOLS,
    _SUBAGENT_LABEL,
    _TOOLS,
    FINDER,
    CheckLearningStore,
    apply_prompt,
    draft_prompt,
    resolve_learning_store,
    review_prompt,
    revise_prompt,
    sections_prompt,
    split_prompt,
)
from norn.runner import resolve_run_path
from norn.skills import Skill
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand
from norn.stages.validate import Contains, FileExists, Validate

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

RECOMMENDATION_ADDENDUM = """

## Amendment to the questions-file format: recommended answers

The format shown above is amended. Every `## Q<n>` section in the questions
file carries one more line, placed **directly above** `**Answer:**`:

```
## Q1. <short question>
**Why it matters:** <one sentence>
**Assumption if unanswered:** <what you will assume>
**Recommended answer:** <one line>
**Answer:**
```

- `**Recommended answer:**` is the answer you would pick yourself. Keep it to
  one line, short enough that the human can paste it into `**Answer:**`.
- When only a human can decide (money, access, a policy choice, an outside
  fact you cannot read from the repo), write
  `**Recommended answer:** none — <why only a human can decide>`.
- The recommendation line always sits above `**Answer:**`, never below it:
  every line after `**Answer:**` is read as the answer.
- Leave `**Answer:**` itself empty.
"""

REVISE_ADDENDUM = """

When you rewrite the questions file:

- Keep every `**Recommended answer:**` line, directly above `**Answer:**`.
- Give each new or refined question a `**Recommended answer:**` line in the
  same place (or `none — <why only a human can decide>`).
- Never edit an answer that ends in `_(auto: recommended answer)_` or
  `_(recommended answer)_`: the plan already absorbs it as the human's answer.
"""


def draft_prompt_v3(**kw: str) -> str:
    return draft_prompt(**kw) + RECOMMENDATION_ADDENDUM


def revise_prompt_v3(**kw: str) -> str:
    return revise_prompt(**kw) + REVISE_ADDENDUM


def recommendations_prompt(*, questions: str) -> str:
    return f"""\
The open-questions file at {questions} has questions without a valid
`**Recommended answer:**` line.

Read it and add the missing lines only:

- Each `## Q<n>` section needs `**Recommended answer:** <one line>` placed
  **directly above** `**Answer:**`: the answer you would pick, short enough to
  paste into `**Answer:**`.
- When only a human can decide, write
  `**Recommended answer:** none — <why only a human can decide>`.
- Move a recommendation that sits below `**Answer:**` to above it, and replace
  an empty one.
- Change nothing else: not the `STATUS:` line, not a question, and never an
  `**Answer:**` slot.
"""


# ---------------------------------------------------------------------------
# Gate 2 file
# ---------------------------------------------------------------------------

_RESPONSE_LINE_RE = re.compile(r"\bF\d+\b.*?\b(accepted|rejected|deferred)\b", re.IGNORECASE)


def review_counts(response_text: str) -> dict[str, int]:
    """Count ``F<n> … accepted|rejected|deferred`` lines in the response file."""
    counts = {"accepted": 0, "rejected": 0, "deferred": 0}
    for line in response_text.splitlines():
        m = _RESPONSE_LINE_RE.search(line)
        if m:
            counts[m.group(1).lower()] += 1
    return counts


def render_plan_approval(
    *, key: str, plan: str, review: str, response: str, questions: str, ctx: PipelineContext
) -> str:
    counts = review_counts(resolve_run_path(ctx, response).read_text())
    total = sum(counts.values())
    qtext = resolve_run_path(ctx, questions).read_text()
    _status, parsed = parse_questions(qtext)
    answered = sum(1 for q in parsed if q.answered)
    from_rec = sum(
        1
        for line in qtext.splitlines()
        if line.rstrip().endswith((RECOMMENDED_SUFFIX.strip(), AUTO_RECOMMENDED_SUFFIX.strip()))
    )
    return (
        f"# Plan approval — {key}\n"
        "\n"
        "DECISION: pending\n"
        "\n"
        "The run waits until you change the line above to exactly `DECISION: approve` "
        "and save this file.\n"
        f"Edit the plan freely first: {plan}\n"
        f"- Codex review: {review} — {total} findings: {counts['accepted']} accepted, "
        f"{counts['rejected']} rejected, {counts['deferred']} deferred\n"
        f"- Questions: {questions} — {answered} answered, {from_rec} from a recommendation\n"
    )


# ---------------------------------------------------------------------------
# `when` predicates (computed from files, never from stage results)
# ---------------------------------------------------------------------------


def _read_or_none(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return None


def _needs_recommendations(questions: str):
    def when(ctx: PipelineContext) -> bool:
        text = _read_or_none(questions)
        return True if text is None else bool(recommendation_problems(text))

    return when


def _needs_revision(questions: str):
    return lambda ctx: questions_need_revision(questions)


def _needs_plan_sections(plan: str):
    def when(ctx: PipelineContext) -> bool:
        text = _read_or_none(plan)
        return True if text is None else bool(missing_plan_sections(text))

    return when


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def add_plan_review_stages_v3(
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
    codex_label: str,
    split_skill: str | Skill,
    approval: str,
    auto: bool,
    plan_gate: bool,
) -> Pipeline:
    """Add stages 11-30 (``preflight`` through ``plan summary``) to *pipeline*.

    *approval* is the absolute path of ``<KEY>-plan-approval.md``.  With *auto*
    the open-questions loop gets three attempts, blanks are filled from the
    recommendations, and ``auto approve plan`` decides gate 2; with
    ``plan_gate=False`` the same stage approves gate 2 as ``plan_gate=off``.
    """
    store_rel, store_error = resolve_learning_store(repo_dir)
    research_dir = (plan[:-3] if plan.endswith(".md") else plan) + "-research"
    key = Path(approval).name.removesuffix("-plan-approval.md")

    _draft_p = draft_prompt_v3(
        preplan=preplan,
        plan=plan,
        questions=questions,
        store_rel=store_rel,
        finder=FINDER,
        research_dir=research_dir,
    )
    _revise_p = revise_prompt_v3(questions=questions, plan=plan)
    _recs_p = recommendations_prompt(questions=questions)
    _sections_p = sections_prompt(plan=plan)
    _review_p = review_prompt(plan=plan, preplan=preplan, review=review)
    _apply_p = apply_prompt(plan=plan, review=review, response=response)
    _split_p = split_prompt(plan=plan, steps_dir=steps_dir)

    _subagent_env = {
        "CLAUDE_CODE_SUBAGENT_MODEL": resolve_label("claude-code", _SUBAGENT_LABEL).model,
        "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
    }

    def _generate(prompt: str, *, skills: list | None = None, tools: list[str] = _TOOLS) -> Generate:
        kwargs = {"skills": skills} if skills is not None else {}
        return Generate(
            prompt=prompt,
            model=model,
            allowed_tools=tools,
            permission_mode="acceptEdits",
            setting_sources=["project"],
            env=_subagent_env,
            **kwargs,
        )

    # Copied from _plan_review_v2.py (the codex command; the builder builds it
    # inline there), with `rm -f` first so a stale file cannot pass the shape check.
    _codex_resolved = resolve_label("codex", codex_label)
    _effort_token = shlex.quote(f'model_reasoning_effort="{_codex_resolved.effort}"')
    codex_cmd = (
        f"rm -f {shlex.quote(review)} && "
        "codex exec --sandbox read-only --skip-git-repo-check "
        + f"-m {shlex.quote(_codex_resolved.model)} "
        + f"-c {_effort_token} "
        + f"-C {shlex.quote(repo_dir)} -o {shlex.quote(review)} "
        + shlex.quote(_review_p)
    )

    # 11. Fail fast before spending a single token.
    pipeline.stage(
        "preflight",
        RunCommand(cmd=(
            f'command -v codex >/dev/null || {{ echo "codex CLI not found — brew install codex"; exit 1; }}; '
            f'test -f {shlex.quote(preplan)} || {{ echo "pre-plan not found: {preplan}"; exit 1; }}'
        )),
        on_failure=fail,
    )

    # 12. A bad `docs_root` is reported by a stage, never raised at import.
    pipeline.stage(
        "check learning store",
        CheckLearningStore(error=store_error),
        on_failure=fail,
    )

    # 13. Draft the plan + the questions file.
    pipeline.stage(
        "draft plan",
        _generate(_draft_p, tools=_DRAFT_TOOLS),
        on_failure=ask_user,
    )

    # 14. Gate 1.  Every repairing stage is gated on files, so a resumed process
    # (which sees only checkpointed results) behaves like the first one.
    pipeline.loop(
        "resolve open questions",
        max_retries=3 if auto else 1,
        on_exhaust=ask_user,
        stages=[
            Stage(
                "add recommendations",
                _generate(_recs_p),
                when=_needs_recommendations(questions),
            ),
            Stage(
                "expand recommended answers",
                ExpandRecommendedAnswers(path=questions, fill_blanks=auto),
            ),
            Stage(
                "revise plan",
                _generate(_revise_p),
                when=_needs_revision(questions),
            ),
            Stage("check open questions", QuestionsGate(path=questions)),
        ],
    )

    # 15. Hard twin: `c` at an exhausted loop counts as success.
    pipeline.stage(
        "open questions resolved",
        QuestionsGate(path=questions),
        on_failure=fail,
    )

    # 16-17. The four required sections.
    pipeline.loop(
        "complete plan sections",
        max_retries=2,
        on_exhaust=ask_user,
        stages=[
            Stage(
                "add plan sections",
                _generate(_sections_p),
                when=_needs_plan_sections(plan),
            ),
            Stage("check plan sections", PlanSectionsGate(path=plan)),
        ],
    )
    pipeline.stage(
        "plan sections present",
        PlanSectionsGate(path=plan),
        on_failure=fail,
    )

    # 18-19. Codex reviews the plan (read-only); re-running is the only repair.
    _review_checks = [
        FileExists(review),
        Contains(review, patterns=["## Verdict", "## Findings"]),
    ]
    pipeline.loop(
        "review plan",
        max_retries=2,
        on_exhaust=ask_user,
        stages=[
            Stage("codex review", RunCommand(cmd=codex_cmd, timeout=1800)),
            Stage("check review shape", Validate(checks=_review_checks)),
        ],
    )
    pipeline.stage(
        "review shape ok",
        Validate(checks=_review_checks),
        on_failure=fail,
    )

    # 20-22. Claude applies the review and logs a disposition per finding.
    pipeline.clear_context()
    pipeline.loop(
        "apply review",
        max_retries=2,
        on_exhaust=ask_user,
        stages=[
            Stage("apply findings", _generate(_apply_p)),
            Stage(
                "check dispositions",
                ReviewDispositionGate(review=review, response=response),
            ),
        ],
    )
    pipeline.stage(
        "dispositions recorded",
        ReviewDispositionGate(review=review, response=response),
        on_failure=fail,
    )
    pipeline.stage(
        "plan sections kept",
        PlanSectionsGate(path=plan),
        on_failure=fail,
    )

    # 23-27. Gate 2: plan approval, after the review and before the split.
    pipeline.stage(
        "write plan approval",
        WriteDecision(
            path=approval,
            render=lambda ctx: render_plan_approval(
                key=key,
                plan=plan,
                review=review,
                response=response,
                questions=questions,
                ctx=ctx,
            ),
        ),
        on_failure=fail,
    )
    if auto or not plan_gate:
        pipeline.stage(
            "auto approve plan",
            AutoDecide(
                path=approval,
                field="DECISION",
                value="approve",
                note="plan_gate=off" if not auto else "auto",
            ),
            on_failure=fail,
        )
    _approve_action = (
        f"To approve the plan, edit {approval}: change the DECISION line to exactly "
        "`DECISION: approve`, save the file, then [r]etry."
    )
    _approve_values = frozenset({"approve", "approved"})
    pipeline.stage(
        "plan approved",
        DecisionGate(
            path=approval, field="DECISION", accept=_approve_values, action=_approve_action
        ),
        on_failure=ask_user,
    )
    pipeline.stage(
        "plan approval recorded",
        DecisionGate(
            path=approval, field="DECISION", accept=_approve_values, action=_approve_action
        ),
        on_failure=fail,
        always_run=True,
    )
    # The human may have edited the plan at the gate.
    pipeline.stage(
        "plan sections approved",
        PlanSectionsGate(path=plan),
        on_failure=fail,
    )

    # 28-30. Split the approved plan into step files implement_features_v3 can run.
    pipeline.clear_context()
    pipeline.loop(
        "split plan",
        max_retries=2,
        on_exhaust=ask_user,
        stages=[
            Stage("write step files", _generate(_split_p, skills=[split_skill])),
            Stage("check step files", StepFilesGate(steps_dir=steps_dir)),
        ],
    )
    pipeline.stage(
        "step files written",
        StepFilesGate(steps_dir=steps_dir),
        on_failure=fail,
    )
    pipeline.stage(
        "plan summary",
        RunCommand(cmd=(
            f"ls -l {shlex.quote(plan)} {shlex.quote(review)} {shlex.quote(response)}; "
            f"ls -l {shlex.quote(steps_dir)}"
        )),
    )

    return pipeline
