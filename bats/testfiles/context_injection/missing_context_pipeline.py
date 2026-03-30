from __future__ import annotations

from norn.dsl import Pipeline, Stage, fail
from norn.stages.run_command import RunCommand

config = (
    Pipeline("missing_context")
    .context("nonexistent_file_that_does_not_exist.txt", label="missing")
    .stage("echo", RunCommand(cmd="echo done"), on_failure=fail)
)
