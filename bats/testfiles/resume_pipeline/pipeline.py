"""Minimal pipeline for --resume session resumption testing.

The prompt is driven entirely by {param.args} so tests can control
what Claude is asked to do on each run.
"""

from norn.dsl import Pipeline, Stage, fail
from norn.stages.generate import Generate

config = (
    Pipeline("resume_test")
    .stage(
        "respond",
        Generate(
            prompt="{param.args}",
            output_file="tmp/resume_test/output.txt",
        ),
        on_failure=fail,
    )
)
