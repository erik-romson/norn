"""Sub-pipeline used by include integration tests — no API key required."""
from norn.dsl import Pipeline, Stage
from norn.stages.run_command import RunCommand

OUT = "target/batstest/include"

config = (
    Pipeline("sub")
    .stage("sub_task", RunCommand(cmd=f"mkdir -p {OUT} && echo sub > {OUT}/sub.txt"))
)
