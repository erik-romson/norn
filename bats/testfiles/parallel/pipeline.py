"""Parallel stage test pipeline — no API key required (RunCommand only)."""
from norn.dsl import Pipeline, Stage
from norn.stages.run_command import RunCommand

OUT = "target/batstest/parallel"

config = (
    Pipeline("parallel_test")
    .parallel(
        "run_parallel",
        stages=[
            Stage("task_a", RunCommand(cmd=f"mkdir -p {OUT} && echo done > {OUT}/a.txt")),
            Stage("task_b", RunCommand(cmd=f"mkdir -p {OUT} && echo done > {OUT}/b.txt")),
        ],
    )
    .stage("after", RunCommand(cmd=f"echo sequential > {OUT}/after.txt"))
)
