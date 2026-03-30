"""Example pipeline: read a spec, generate a Python class, compile-check it, run tests."""
import os

from norn.alerts import MacOSChannel
from norn.dsl import Pipeline, Stage, fail
from norn.stages.generate import Generate
from norn.stages.read_file import ReadFile
from norn.stages.run_command import RunCommand

# Use absolute paths so the bundled Claude CLI writes files in the correct
# directory regardless of how it resolves its project root.
_PROJECT_DIR = os.getcwd()
_SRC = os.path.join(_PROJECT_DIR, "tmp", "hello", "src", "greeter.py")
_TEST = os.path.join(_PROJECT_DIR, "tmp", "hello", "tests", "test_greeter.py")

metadata = {
    "args": {"args": "Not used"},
}

config = (
    Pipeline("hello")
    .stage(
        "read_spec",
        ReadFile(path="examples/spec.txt"),
        on_failure=fail,
    )
    .clear_context()
    .alert(MacOSChannel())
    .loop(
        "generate_and_build",
        max_retries=3,
        on_exhaust=fail,
        stages=[
            Stage("generate", Generate(
                prompt=f"Create a Python class at {_SRC} based on this spec: {{read_spec.output}}",
                permission_mode="bypassPermissions",
                cwd=_PROJECT_DIR,
            )),
            Stage("generate_test", Generate(
                prompt=f"Create a Python test for {_SRC} at {_TEST}: {{read_spec.output}}",
                permission_mode="bypassPermissions",
                cwd=_PROJECT_DIR,
            )),
            Stage("check", RunCommand(cmd=f"python -m py_compile {_SRC}")),
            Stage("test", RunCommand(cmd=f"python -m pytest {_TEST} -v")),
        ],
    )
)
