"""Pipeline for checkpoint integration testing.

Stage 1 always succeeds.
Stage 2 reads a trigger file — fails when the file is absent, succeeds when present.
This lets BATS tests control which stage fails by creating/removing the trigger file.
"""

from norn.dsl import Pipeline, fail
from norn.stages.run_command import RunCommand

config = (
    Pipeline("checkpoint_test")
    .stage("step1", RunCommand(cmd="echo step1"), on_failure=fail)
    .stage("step2", RunCommand(cmd="cat target/batstest/checkpoint/trigger.txt"), on_failure=fail)
)
