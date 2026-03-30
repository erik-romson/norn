"""Example pipeline: demonstrates FileChannel alerts without requiring macOS or Slack.

Runs a trivial shell command so no ANTHROPIC_API_KEY is needed.
Alerts are written to ``tmp/alert_demo/alerts.jsonl`` as JSON lines.
"""
from norn.alerts import FileChannel
from norn.dsl import Pipeline, Stage, fail
from norn.stages.run_command import RunCommand

ALERT_FILE = "tmp/alert_demo/alerts.jsonl"

config = (
    Pipeline("alert_demo")
    .alert(FileChannel(path=ALERT_FILE))
    .stage("greet", RunCommand(cmd="echo 'Hello from alert_demo pipeline'"), on_failure=fail)
)
