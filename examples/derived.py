"""Example: config composition via Pipeline.derive().

Derives a leaner pipeline from the hello example by:
- skipping the slow pytest stage inside the loop
- inserting a fast syntax-only check after the compile step
- replacing the top-level read_spec stage with a different file path
"""
from norn.dsl import Pipeline, Stage, fail
from norn.stages.read_file import ReadFile
from norn.stages.run_command import RunCommand

from examples.hello import config as base

config = (
    base.derive("hello-fast")
    # Replace the spec file used by the read stage
    .replace("read_spec", ReadFile(path="examples/spec.txt"))
    # Mutate the loop: drop the full test run, insert a quick compile check
    .in_loop("generate_and_build")
        .skip("test")
        .insert_after("check", Stage("typecheck", RunCommand(cmd="python -m py_compile tmp/hello/src/greeter.py")))
    .end_loop()
)
