"""Turn a pre-plan into a reviewed plan: draft → human Q&A loop → codex review → apply → split.

Usage:  norn run plan_with_review_v2 tmp/<slug>-preplan.md

Optional CLI-only knobs (not available under `norn ui`):
  --arg model=deep               Intent label for Claude Generate stages (default: deep)
  --arg codex_label=review       Intent label for codex review stage (default: review)
  --arg budget=50                Cost ceiling in USD before the run pauses
"""

import os
import sys

from norn.alerts import AlertEvent, MacOSChannel
from norn.dsl import Pipeline, ask_user
from norn.model_labels import ModelLabelError, labels_for, resolve_label
from norn.pipelines._plan_gates_v2 import OpenQuestionsGate, ReviewDispositionGate, StepFilesGate  # noqa: F401
from norn.pipelines._plan_review_v2 import (
    WaitForApproval,  # noqa: F401
    add_plan_review_stages,
    apply_prompt,
    draft_prompt,
    resolve_split_skill,
    revise_prompt,
    review_prompt,
    split_prompt,
)
from norn.pipelines._preplan import derive_paths, parse_arg_flags, resolve_preplan, steps_dir_of

metadata = {
    "env_vars": ["ANTHROPIC_API_KEY"],
    "args": {"args": "Path to the pre-plan markdown file (required, positional)"},
}

# --- resolve inputs at import time ------------------------------------------
REPO_DIR = os.getcwd()
_args = parse_arg_flags(sys.argv[1:])
PREPLAN = resolve_preplan(sys.argv[1:], REPO_DIR, pipeline="plan_with_review_v2")
PLAN, QUESTIONS, REVIEW, RESPONSE = derive_paths(PREPLAN)
STEPS_DIR = steps_dir_of(PLAN)

MODEL = _args.get("model", "deep")
CODEX_LABEL = _args.get("codex_label", "review")
TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]

# --- import-time knob validation -------------------------------------------
_generate_labels = set(labels_for("claude-code")) | set(labels_for("opencode"))
if MODEL not in _generate_labels:
    raise ModelLabelError(
        f"--arg model={MODEL!r} is not a valid Generate label; "
        f"known labels: {', '.join(sorted(_generate_labels))} — see norn/model_labels.toml"
    )
resolve_label("codex", CODEX_LABEL)  # fail fast if unknown

# Skill resolver (re-exported so tests can call mod._split_skill() and mod.SPLIT_SKILL).
_split_skill = resolve_split_skill
SPLIT_SKILL = _split_skill()

# Prompt constants (re-exported so existing tests can read them via mod._DRAFT_PROMPT etc.)
_DRAFT_PROMPT = draft_prompt(preplan=PREPLAN, plan=PLAN, questions=QUESTIONS)
_REVISE_PROMPT = revise_prompt(questions=QUESTIONS, plan=PLAN)
_REVIEW_PROMPT = review_prompt(plan=PLAN, preplan=PREPLAN, review=REVIEW)
_APPLY_PROMPT = apply_prompt(plan=PLAN, review=REVIEW, response=RESPONSE)
_SPLIT_PROMPT = split_prompt(plan=PLAN, steps_dir=STEPS_DIR)

# --- pipeline ---------------------------------------------------------------

_pipeline = (
    Pipeline("plan_with_review_v2")
    .alert(MacOSChannel(events={AlertEvent.ASK_USER, AlertEvent.COMPLETE, AlertEvent.FAILED}))
    .budget(max_cost_usd=float(_args.get("budget", "50")), on_exceed=ask_user)
)

config = add_plan_review_stages(
    _pipeline,
    preplan=PREPLAN,
    plan=PLAN,
    questions=QUESTIONS,
    review=REVIEW,
    response=RESPONSE,
    steps_dir=STEPS_DIR,
    repo_dir=REPO_DIR,
    model=MODEL,
    codex_label=CODEX_LABEL,
    split_skill=SPLIT_SKILL,
    pause_for_approval=False,
)
