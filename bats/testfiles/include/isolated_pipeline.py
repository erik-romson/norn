"""Isolated include test pipeline — no API key required."""
from norn.dsl import Pipeline, Stage
from norn.stages.run_command import RunCommand

OUT = "target/batstest/include"

config = (
    Pipeline("isolated_parent")
    .include("bats/testfiles/include/sub_pipeline.py", isolated=True, outputs=["sub_task"])
    .stage("after", RunCommand(cmd=f"echo after > {OUT}/after.txt"))
)
