from norn.dsl import *
from norn.stages.run_command import RunCommand

config = (
    Pipeline("step_test")
    .stage("hello", RunCommand(cmd="echo hello"))
    .stage("world", RunCommand(cmd="echo world"))
)
