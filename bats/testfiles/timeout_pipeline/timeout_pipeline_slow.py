"""Timeout pipeline that triggers a stage timeout (sleep longer than timeout)."""
from norn.dsl import Pipeline, Stage, fail
from norn.stages.run_command import RunCommand

config = (
    Pipeline("timeout_slow_test")
    .stage("slow", RunCommand(cmd="sleep 30"), timeout=0.1)
)
