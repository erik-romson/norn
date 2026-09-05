"""Apply a free-text change to the repo you launched from.

Usage:
    norn run vanilla_change "Add a --dry-run flag to the CLI"
    norn run vanilla_change -v "Rename StageResult.error to message"

The pipeline:
1. Claude implements the change (CLAUDE.md loaded automatically via setting_sources)
2. Runs tests — if they fail, Claude fixes and retries (up to 5 attempts)
"""

from norn.dsl import Pipeline, Stage, ask_user, fail, stage_failed
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

metadata = {
    "env_vars": ["ANTHROPIC_API_KEY"],
    "args": {"args": "Free-text description of the change to implement"},
}

config = (
    Pipeline("vanilla_change")

    # Step 1: implement the change (runs once)
    .stage("implement", Generate(
        prompt=(
            "## Change requested\n"
            "{param.args}\n\n"
            "## Instructions\n"
            "- Read the relevant source files before making changes\n"
            "- Implement the requested change\n"
            "- Follow the existing code style and conventions\n"
            "- Update or add tests as needed\n"
            "- Do not change unrelated code\n"
        ),
        allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        permission_mode="acceptEdits",
        setting_sources=["project"],
    ),
    # A failed implement (e.g. a transient agent/SDK error like a 529 overload)
    # prompts for retry/continue/abort rather than killing the run before the
    # test loop ever gets a chance.
    on_failure=ask_user)

    # Step 2: test + fix loop — inherits session from implement stage
    # fix stage only runs on retry (when tests have failed)
    .loop(
        "test_and_fix",
        max_retries=5,
        on_exhaust=fail,
        stages=[
            Stage("fix", Generate(
                prompt=(
                    "## Fix test failures\n"
                    "The tests failed. Fix the code so the tests pass.\n\n"
                    "### pytest output\n{test python.output}\n\n"
                    "### bats output\n{test bats.output}\n"
                ),
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
                permission_mode="acceptEdits",
                setting_sources=["project"],
            ), when=lambda ctx: stage_failed("test python")(ctx) or stage_failed("test bats")(ctx)),
            Stage("test python", RunCommand(
                cmd="uv run python -m pytest tests/ -v",
            )),
            Stage("test bats", RunCommand(
                cmd="bats -r bats/ -v",
            )),
        ],
    )
)
