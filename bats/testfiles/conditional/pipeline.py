"""Conditional stage test pipeline — no API key required (RunCommand only)."""
from norn.dsl import Pipeline, Stage, stage_succeeded
from norn.stages.run_command import RunCommand

OUT = "target/batstest/conditional"

config = (
    Pipeline("conditional_test")
    .stage("setup", RunCommand(cmd=f"mkdir -p {OUT} && echo done > {OUT}/setup.txt"))
    .stage("runs", RunCommand(cmd=f"echo ran > {OUT}/ran.txt"), when=stage_succeeded("setup"))
    .stage("skipped", RunCommand(cmd=f"echo skip > {OUT}/skipped.txt"), when=lambda ctx: False)
)
