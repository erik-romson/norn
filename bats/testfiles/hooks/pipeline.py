from norn.dsl import Pipeline, Stage, fail
from norn.stages.run_command import RunCommand

OUTPUT_DIR = "target/batstest/hooks"

config = (
    Pipeline("hooks_test")
    .hook("pre_stage", RunCommand(cmd=f"mkdir -p {OUTPUT_DIR} && echo pre >> {OUTPUT_DIR}/events.txt"))
    .hook("post_stage", RunCommand(cmd=f"echo post >> {OUTPUT_DIR}/events.txt"))
    .hook("on_failure", RunCommand(cmd=f"echo on_failure >> {OUTPUT_DIR}/events.txt"))
    .stage("step1", RunCommand(cmd="echo step1"))
    .stage("step2", RunCommand(cmd="echo step2"))
)
