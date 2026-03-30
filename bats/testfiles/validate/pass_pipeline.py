"""Pipeline whose Validate stage passes all checks."""

from __future__ import annotations

import os

from norn.dsl import Pipeline, Stage, fail
from norn.stages.validate import Contains, FileExists, Validate

_HERE = os.path.dirname(os.path.abspath(__file__))
_SAMPLE = os.path.join(_HERE, "sample.py")

config = (
    Pipeline("validate_pass")
    .stage(
        "validate",
        Validate(
            checks=[
                FileExists(_SAMPLE),
                Contains(_SAMPLE, patterns=["def greet", "return"]),
            ]
        ),
        on_failure=fail,
    )
)
