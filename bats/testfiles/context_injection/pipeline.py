from __future__ import annotations

import pathlib

from norn.dsl import Pipeline, Stage, fail
from norn.stages.run_command import RunCommand

# Resolve path relative to this file so the bats test can call it from any cwd
_HERE = pathlib.Path(__file__).parent

config = (
    Pipeline("context_injection")
    .context(str(_HERE / "context_file.txt"), label="test_context")
    .stage("echo", RunCommand(cmd="echo done"), on_failure=fail)
)
