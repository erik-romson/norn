"""Template pipeline test — uses Generate with template= (dry-run only)."""
from norn.dsl import Pipeline, Stage, fail
from norn.stages.generate import Generate

config = (
    Pipeline("template_test")
    .stage("greet", Generate(template="greeting", input="Alice"), on_failure=fail)
)
