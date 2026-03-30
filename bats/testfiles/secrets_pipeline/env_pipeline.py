from __future__ import annotations

from norn.dsl import Pipeline, fail
from norn.stages.run_command import RunCommand

# Pipeline-level env var injected into all stages via ctx.env
config = (
    Pipeline("env_test")
    .env("GREETING", "hello_from_pipeline_env")
    .stage(
        "check",
        RunCommand(cmd='test "$GREETING" = "hello_from_pipeline_env"'),
        on_failure=fail,
    )
)
