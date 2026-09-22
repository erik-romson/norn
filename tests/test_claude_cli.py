"""Which `claude` binary norn spawns, and whether it accepts our efforts.

The SDK's own search prefers the CLI bundled inside the installed
claude-agent-sdk wheel and only falls back to PATH when that file is absent.
The bundle is frozen at the wheel's release, so it drifts behind the Claude
Code the user actually installed — and the drift is invisible until a flag
stops working mid-run. That is how a run died at step 05: sdk 0.1.50 bundles
CLI 2.1.81, which rejects the `--effort xhigh` that the `deep` label sends.
"""
from __future__ import annotations

import subprocess
import sys
import types as stdlib_types
from unittest.mock import MagicMock, patch

import pytest

from norn.agents.base import AgentRequest
from norn.agents.claude_code import (
    CLI_ENV_VAR,
    ClaudeCodeProvider,
    _bundled_cli,
    cli_version,
    resolve_cli_path,
)


@pytest.fixture(autouse=True)
def _clear_cli_cache():
    """resolve_cli_path caches its probes for the process; each test starts fresh."""
    resolve_cli_path.cache_clear()
    yield
    resolve_cli_path.cache_clear()


# ---------------------------------------------------------------------------
# cli_version
# ---------------------------------------------------------------------------


def test_cli_version_parses_the_version_line(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "2.1.263 (Claude Code)\n", ""),
    )
    assert cli_version("/bin/claude") == (2, 1, 263)


def test_cli_version_is_none_when_the_binary_fails(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "boom")
    )
    assert cli_version("/bin/claude") is None


def test_cli_version_is_none_when_the_binary_is_missing(monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert cli_version("/bin/claude") is None


def test_cli_version_is_none_when_the_binary_hangs(monkeypatch):
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired("claude", 30)

    monkeypatch.setattr(subprocess, "run", _timeout)
    assert cli_version("/bin/claude") is None


def test_cli_version_is_none_when_the_output_has_no_version(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "hello\n", "")
    )
    assert cli_version("/bin/claude") is None


# ---------------------------------------------------------------------------
# _bundled_cli
# ---------------------------------------------------------------------------


def test_bundled_lookup_finds_the_real_sdk_bundle():
    """The lookup must mirror the SDK's own, or the version comparison is a lie."""
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    expected = SubprocessCLITransport.__dict__["_find_bundled_cli"](None)
    assert _bundled_cli() == expected


def test_bundled_lookup_survives_a_stubbed_sdk():
    """Suites that stub claude_agent_sdk must not break CLI resolution."""
    with patch.dict(sys.modules, {"claude_agent_sdk": MagicMock()}):
        assert _bundled_cli() is None


# ---------------------------------------------------------------------------
# resolve_cli_path
# ---------------------------------------------------------------------------


def _fake_probes(monkeypatch, *, on_path, bundled, versions):
    monkeypatch.setattr("norn.agents.claude_code.shutil.which", lambda _: on_path)
    monkeypatch.setattr("norn.agents.claude_code._bundled_cli", lambda: bundled)
    monkeypatch.setattr("norn.agents.claude_code.cli_version", versions.get)


def test_env_override_wins_without_a_version_probe(monkeypatch):
    """An operator naming a binary means that binary, whatever it reports."""
    monkeypatch.setenv(CLI_ENV_VAR, "/opt/my/claude")

    def _explode(_path):
        raise AssertionError("the override must not be version-probed")

    monkeypatch.setattr("norn.agents.claude_code.cli_version", _explode)

    assert resolve_cli_path() == "/opt/my/claude"


def test_newer_path_cli_beats_the_sdk_bundle(monkeypatch):
    """The reported failure: the bundle rejects an effort the PATH CLI accepts."""
    monkeypatch.delenv(CLI_ENV_VAR, raising=False)
    _fake_probes(
        monkeypatch,
        on_path="/usr/bin/claude",
        bundled="/sdk/_bundled/claude",
        versions={"/usr/bin/claude": (2, 1, 263), "/sdk/_bundled/claude": (2, 1, 81)},
    )
    assert resolve_cli_path() == "/usr/bin/claude"


def test_newer_sdk_bundle_beats_a_stale_path_cli(monkeypatch):
    """The comparison runs both ways — no blanket preference for either source."""
    monkeypatch.delenv(CLI_ENV_VAR, raising=False)
    _fake_probes(
        monkeypatch,
        on_path="/usr/bin/claude",
        bundled="/sdk/_bundled/claude",
        versions={"/usr/bin/claude": (2, 0, 5), "/sdk/_bundled/claude": (2, 1, 81)},
    )
    assert resolve_cli_path() == "/sdk/_bundled/claude"


def test_the_only_candidate_that_answers_is_chosen(monkeypatch):
    monkeypatch.delenv(CLI_ENV_VAR, raising=False)
    _fake_probes(
        monkeypatch,
        on_path="/usr/bin/claude",
        bundled="/sdk/_bundled/claude",
        versions={"/sdk/_bundled/claude": (2, 1, 81)},
    )
    assert resolve_cli_path() == "/sdk/_bundled/claude"


def test_no_candidate_defers_to_the_sdk(monkeypatch):
    """Returning None lets the SDK run its own search and print its install help."""
    monkeypatch.delenv(CLI_ENV_VAR, raising=False)
    _fake_probes(monkeypatch, on_path=None, bundled=None, versions={})
    assert resolve_cli_path() is None


def test_probes_run_once_per_process(monkeypatch):
    monkeypatch.delenv(CLI_ENV_VAR, raising=False)
    calls: list[str] = []

    monkeypatch.setattr("norn.agents.claude_code.shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("norn.agents.claude_code._bundled_cli", lambda: None)

    def _counted(path):
        calls.append(path)
        return (2, 1, 263)

    monkeypatch.setattr("norn.agents.claude_code.cli_version", _counted)

    assert resolve_cli_path() == resolve_cli_path() == "/usr/bin/claude"
    assert calls == ["/usr/bin/claude"]


# ---------------------------------------------------------------------------
# The provider hands the path to the SDK
# ---------------------------------------------------------------------------


async def _options_used(monkeypatch) -> dict:
    """Run the provider against a mocked SDK and return the options it built."""
    sdk = MagicMock()

    async def _query(prompt, options):
        return
        yield  # pragma: no cover - makes this an async generator

    sdk.query = _query
    types_mod = stdlib_types.SimpleNamespace(
        TextBlock=type("T", (), {}),
        ThinkingBlock=type("K", (), {}),
        ToolUseBlock=type("U", (), {}),
        ToolResultBlock=type("R", (), {}),
    )
    request = AgentRequest(prompt="p", stage_name="s", provider="claude-code")
    with patch.dict(
        sys.modules, {"claude_agent_sdk": sdk, "claude_agent_sdk.types": types_mod}
    ):
        async for _ in ClaudeCodeProvider().run(request):
            pass
    return sdk.ClaudeAgentOptions.call_args.kwargs


@pytest.mark.asyncio
async def test_provider_pins_the_resolved_cli(monkeypatch):
    monkeypatch.setenv(CLI_ENV_VAR, "/opt/my/claude")
    assert (await _options_used(monkeypatch))["cli_path"] == "/opt/my/claude"


@pytest.mark.asyncio
async def test_provider_omits_cli_path_when_nothing_resolves(monkeypatch):
    """Absent a resolution, the SDK must be left to its own search."""
    monkeypatch.delenv(CLI_ENV_VAR, raising=False)
    _fake_probes(monkeypatch, on_path=None, bundled=None, versions={})
    assert "cli_path" not in await _options_used(monkeypatch)


# ---------------------------------------------------------------------------
# The guard that would have caught the reported failure
# ---------------------------------------------------------------------------


def test_resolved_cli_accepts_every_effort_we_send():
    """Every effort in model_labels.toml must be one this CLI actually parses.

    `--effort` is validated by the CLI at spawn time, so a value it does not
    know kills the stage — after the run has already paid for the steps before
    it. Probing with `--version` exercises the same argument parsing without
    reaching the API.
    """
    from norn.model_labels import labels_for, resolve_label

    cli = resolve_cli_path()
    if cli is None:
        pytest.skip("no Claude Code CLI available to probe")

    efforts = {
        resolve_label("claude-code", label).effort for label in labels_for("claude-code")
    }
    for effort in sorted(e for e in efforts if e):
        result = subprocess.run(
            [cli, "--effort", effort, "--version"], capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"{cli} rejects --effort {effort}: {result.stderr.strip()}\n"
            "Update Claude Code, or set the effort to a value this CLI knows."
        )
