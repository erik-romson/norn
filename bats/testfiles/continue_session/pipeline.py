"""Pipeline for testing --continue flag.

Uses RunCommand (no API needed). Writes a counter to a file to prove
stages re-run on --continue (not skipped).
"""
import os

from norn.dsl import Pipeline, Stage, fail
from norn.stages.run_command import RunCommand

OUTPUT_DIR = os.path.join(os.getcwd(), "tmp", "continue_test")

config = (
    Pipeline("continue_test")
    .stage(
        "count",
        RunCommand(
            cmd=f'mkdir -p {OUTPUT_DIR} && '
                f'COUNT=$(cat {OUTPUT_DIR}/counter.txt 2>/dev/null || echo 0) && '
                f'echo $(( COUNT + 1 )) > {OUTPUT_DIR}/counter.txt && '
                f'cat {OUTPUT_DIR}/counter.txt',
        ),
        on_failure=fail,
    )
)
