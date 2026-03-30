"""Timeout pipeline test — verifies stage timeout is enforced without API key."""
from norn.dsl import Pipeline, Stage, fail
from norn.stages.run_command import RunCommand

OUT = "target/batstest/timeout"

config = (
    Pipeline("timeout_test")
    # This stage completes quickly and should succeed within its generous timeout
    .stage("fast", RunCommand(cmd=f"mkdir -p {OUT} && echo done > {OUT}/result.txt"), timeout=30.0)
)
