from __future__ import annotations

from norn.dsl import Pipeline, fail
from norn.stages.run_command import RunCommand

# Secret resolved from the TEST_PIPELINE_SECRET environment variable.
# Stage uses {secret.TEST_PIPELINE_SECRET} to inject it.
config = (
    Pipeline("secret_env_test")
    .secret("TEST_PIPELINE_SECRET", source="env")
    .stage(
        "check",
        RunCommand(
            cmd='test -n "$MY_SECRET"',
            env={"MY_SECRET": "{secret.TEST_PIPELINE_SECRET}"},
        ),
        on_failure=fail,
    )
)
