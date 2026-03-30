"""Dogfooding pipeline: apply a change to the norn project itself.

Usage:
    bin/norn dogfooding/vanilla_change.py "Add a --dry-run flag to the CLI"
    bin/norn dogfooding/vanilla_change.py -v "Rename StageResult.error to message"

The pipeline:
1. Claude implements the change (CLAUDE.md loaded automatically via setting_sources)
2. Runs tests — if they fail, Claude fixes and retries (up to 5 attempts)
"""

import os

from norn.dsl import Pipeline, Stage, fail, stage_failed
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

PROJECT_DIR = os.getcwd()

metadata = {
    "env_vars": ["ANTHROPIC_API_KEY"],
    "args": {"args": "Free-text description of the change to implement"},
}

config = (
    Pipeline("vanilla_change")

    # Step 1: implement the change (runs once)
    .stage("implement", Generate(
        prompt=(
            f"## Working directory\n{PROJECT_DIR}\n\n"
            "IMPORTANT: When creating or editing files, always use absolute paths "
            f"based on {PROJECT_DIR}. For example, use "
            f"{PROJECT_DIR}/tmp/hello/src/greeter.py, not tmp/hello/src/greeter.py.\n\n"
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
        cwd=PROJECT_DIR,
        setting_sources=["project"],
    ))

    # Step 2: test + fix loop — inherits session from implement stage
    # fix stage only runs on retry (when tests have failed)
    .loop(
        "test_and_fix",
        max_retries=5,
        on_exhaust=fail,
        stages=[
            Stage("fix", Generate(
                prompt=(
                    f"## Working directory\n{PROJECT_DIR}\n\n"
                    "IMPORTANT: When creating or editing files, always use absolute paths "
                    f"based on {PROJECT_DIR}.\n\n"
                    "## Fix test failures\n"
                    "The tests failed. Fix the code so the tests pass.\n\n"
                    "### pytest output\n{test python.output}\n\n"
                    "### bats output\n{test bats.output}\n"
                ),
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
                permission_mode="acceptEdits",
                cwd=PROJECT_DIR,
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
