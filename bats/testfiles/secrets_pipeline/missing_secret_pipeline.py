from __future__ import annotations

from norn.dsl import Pipeline, fail
from norn.stages.run_command import RunCommand

# Declares a secret from an env var that is expected to be unset.
# The pipeline should fail fast at startup with a clear error.
config = (
    Pipeline("missing_secret_test")
    .secret("TEST_MISSING_SECRET_XYZ", source="env")
    .stage("check", RunCommand(cmd="echo should_not_reach"), on_failure=fail)
)
