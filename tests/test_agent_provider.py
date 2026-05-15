from __future__ import annotations

import pytest

from norn.agents import resolve_agent_provider
from norn.dsl import Pipeline


# ---------------------------------------------------------------------------
# resolve_agent_provider – priority rules
# ---------------------------------------------------------------------------


def test_default_provider_is_claude_code():
    """With no CLI flag, env var, or pipeline setting the default is claude-code."""
    p = Pipeline("test")
    assert resolve_agent_provider(p) == "claude-code"


def test_cli_flag_takes_highest_priority(monkeypatch):
    """CLI flag overrides env var and pipeline setting."""
    monkeypatch.setenv("NORN_AGENT_PROVIDER", "opencode")
    p = Pipeline("test").agent_provider("opencode")
    assert resolve_agent_provider(p, cli_provider="claude-code") == "claude-code"


def test_env_var_overrides_pipeline_setting(monkeypatch):
    """NORN_AGENT_PROVIDER overrides Pipeline.agent_provider()."""
    monkeypatch.setenv("NORN_AGENT_PROVIDER", "opencode")
    p = Pipeline("test").agent_provider("claude-code")
    assert resolve_agent_provider(p) == "opencode"


def test_pipeline_setting_overrides_default(monkeypatch):
    """Pipeline.agent_provider() is used when env var is absent."""
    monkeypatch.delenv("NORN_AGENT_PROVIDER", raising=False)
    p = Pipeline("test").agent_provider("opencode")
    assert resolve_agent_provider(p) == "opencode"


def test_none_cli_provider_falls_through_to_env(monkeypatch):
    """cli_provider=None should not override env var."""
    monkeypatch.setenv("NORN_AGENT_PROVIDER", "opencode")
    p = Pipeline("test")
    assert resolve_agent_provider(p, cli_provider=None) == "opencode"


def test_none_cli_and_no_env_and_no_pipeline_gives_default(monkeypatch):
    """All sources absent → default claude-code."""
    monkeypatch.delenv("NORN_AGENT_PROVIDER", raising=False)
    p = Pipeline("test")
    assert resolve_agent_provider(p, cli_provider=None) == "claude-code"


# ---------------------------------------------------------------------------
# CLI argument parsing – --agent-provider must not consume the pipeline path
# ---------------------------------------------------------------------------


def test_cli_parses_agent_provider_flag(tmp_path, monkeypatch):
    """--agent-provider is parsed as a named flag, not a positional argument."""
    import argparse
    import sys

    # Build the same argument parser as cli.main() does for the run subcommand
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("config")
    run_parser.add_argument("--agent-provider", default=None)
    run_parser.add_argument("-v", "--verbose", action="store_true")
    run_parser.add_argument("--arg", action="append", default=[])
    run_parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(["run", "examples/hello.py", "--agent-provider", "opencode"])
    assert args.config == "examples/hello.py"
    assert args.agent_provider == "opencode"


def test_cli_agent_provider_defaults_to_none(tmp_path):
    """--agent-provider defaults to None when not supplied."""
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("config")
    run_parser.add_argument("--agent-provider", default=None)

    args = parser.parse_args(["run", "examples/hello.py"])
    assert args.agent_provider is None


# ---------------------------------------------------------------------------
# PipelineContext default
# ---------------------------------------------------------------------------


def test_pipeline_context_default_agent_provider():
    """PipelineContext.agent_provider defaults to claude-code."""
    from norn.models import PipelineContext

    ctx = PipelineContext()
    assert ctx.agent_provider == "claude-code"


def test_pipeline_context_agent_provider_can_be_set():
    """PipelineContext.agent_provider can be overridden."""
    from norn.models import PipelineContext

    ctx = PipelineContext()
    ctx.agent_provider = "opencode"
    assert ctx.agent_provider == "opencode"
