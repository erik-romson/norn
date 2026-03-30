"""Budget pipeline test — no API key required (RunCommand only)."""
from norn.dsl import OnFailure, Pipeline, Stage, fail
from norn.stages.run_command import RunCommand

OUT = "target/batstest/budget"

config = (
    Pipeline("budget_test")
    .budget(max_cost_usd=5.00, on_exceed=fail)
    .stage("task", RunCommand(cmd=f"mkdir -p {OUT} && echo done > {OUT}/result.txt"))
)
