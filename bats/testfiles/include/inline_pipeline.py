"""Inline include test pipeline — no API key required."""
from norn.dsl import Pipeline, Stage
from norn.stages.run_command import RunCommand

OUT = "target/batstest/include"

config = (
    Pipeline("inline_parent")
    .include("bats/testfiles/include/sub_pipeline.py")
    .stage("after", RunCommand(cmd=f"echo after > {OUT}/after.txt"))
)
