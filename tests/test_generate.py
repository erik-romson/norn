import pytest
from unittest.mock import patch

from norn.agents.base import AgentError, AgentEvent, AgentRequest, AgentUsage
from norn.agents import registry as agent_registry
from norn.agents.models import MODEL_ALIASES, resolve_model
from norn.models import PipelineContext, StageResult
from norn.stages.generate import Generate, MODEL_MAP


# ---------------------------------------------------------------------------
# Fake provider used by most Generate integration tests
# ---------------------------------------------------------------------------


class _FakeProvider:
    """A test-only ``AgentProvider`` that captures requests and yields
    pre-configured events.
    """

    name = "_fake-test-provider"

    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []
        self.events: list[AgentEvent] = []

    async def run(self, request: AgentRequest):
        self.requests.append(request)
        for event in self.events:
            yield event


@pytest.fixture()
def fake_provider():
    """Register a ``_FakeProvider`` for the duration of a test."""
    fp = _FakeProvider()
    agent_registry.register(fp)
    try:
        yield fp
    finally:
        agent_registry._registry.pop(fp.name, None)


def _ctx_for_fake(provider_name: str = _FakeProvider.name, **kwargs) -> PipelineContext:
    """Build a ``PipelineContext`` that targets the fake provider."""
    ctx = PipelineContext(**kwargs)
    ctx.agent_provider = provider_name
    return ctx


# ---------------------------------------------------------------------------
# Prompt resolution (pure unit tests — no provider needed)
# ---------------------------------------------------------------------------


def test_resolve_prompt():
    ctx = PipelineContext()
    ctx.results["read_spec"] = StageResult(name="read_spec", success=True, output="spec content")
    gen = Generate(prompt="Based on: {read_spec.output}", output_file="out.py")
    assert gen._resolve_prompt(ctx) == "Based on: spec content"


def test_extract_code_fenced():
    text = "Here is the code:\n```python\nclass Foo:\n    pass\n```\nDone."
    assert Generate._extract_code(text) == "class Foo:\n    pass"


def test_extract_code_no_fence():
    text = "class Foo:\n    pass"
    assert Generate._extract_code(text) == "class Foo:\n    pass"


def test_resolve_prompt_param():
    ctx = PipelineContext(params={"args": "add logging"})
    gen = Generate(prompt="Do this: {param.args}", output_file="out.py")
    assert gen._resolve_prompt(ctx) == "Do this: add logging"


def test_resolve_prompt_mixed():
    ctx = PipelineContext(params={"args": "fix bug"})
    ctx.results["spec"] = StageResult(name="spec", success=True, output="the spec")
    gen = Generate(prompt="{spec.output} + {param.args}", output_file="out.py")
    assert gen._resolve_prompt(ctx) == "the spec + fix bug"


# ---------------------------------------------------------------------------
# Instance attribute storage
# ---------------------------------------------------------------------------


def test_generate_stores_permission_mode():
    gen = Generate(prompt="hi", output_file="out.py", permission_mode="acceptEdits")
    assert gen.permission_mode == "acceptEdits"


def test_generate_stores_add_dirs():
    gen = Generate(prompt="hi", output_file="out.py", add_dirs=["tmp/", "src/"])
    assert gen.add_dirs == ["tmp/", "src/"]


def test_generate_defaults_have_no_add_dirs_or_permission_mode():
    gen = Generate(prompt="hi", output_file="out.py")
    assert gen.add_dirs is None
    assert gen.permission_mode is None


def test_generate_stores_model_and_thinking():
    gen = Generate(
        prompt="hi",
        model="opus",
        thinking={"type": "enabled", "budget_tokens": 5000},
    )
    assert gen.model == "opus"
    assert gen.thinking == {"type": "enabled", "budget_tokens": 5000}


def test_model_map_short_names():
    assert MODEL_MAP["opus"] == "claude-opus-4-6"
    assert MODEL_MAP["sonnet"] == "claude-sonnet-4-6"
    assert MODEL_MAP["haiku"] == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Generate builds correct AgentRequest fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_contains_prompt(fake_provider):
    gen = Generate(prompt="hello world")
    await gen.run(_ctx_for_fake())
    assert fake_provider.requests[0].prompt == "hello world"


@pytest.mark.asyncio
async def test_request_contains_provider_name(fake_provider):
    gen = Generate(prompt="hi")
    await gen.run(_ctx_for_fake())
    assert fake_provider.requests[0].provider == _FakeProvider.name


@pytest.mark.asyncio
async def test_request_contains_stage_name(fake_provider):
    gen = Generate(prompt="hi")
    await gen.run(_ctx_for_fake(), stage_name="my_stage")
    assert fake_provider.requests[0].stage_name == "my_stage"


@pytest.mark.asyncio
async def test_request_contains_model(fake_provider):
    gen = Generate(prompt="hi", model="sonnet")
    await gen.run(_ctx_for_fake())
    assert fake_provider.requests[0].model == "sonnet"


@pytest.mark.asyncio
async def test_request_default_model_from_ctx_params(fake_provider):
    gen = Generate(prompt="hi")
    ctx = _ctx_for_fake(params={"default_model": "haiku"})
    await gen.run(ctx)
    assert fake_provider.requests[0].model == "haiku"


@pytest.mark.asyncio
async def test_request_stage_model_overrides_default(fake_provider):
    gen = Generate(prompt="hi", model="sonnet")
    ctx = _ctx_for_fake(params={"default_model": "haiku"})
    await gen.run(ctx)
    assert fake_provider.requests[0].model == "sonnet"


@pytest.mark.asyncio
async def test_request_contains_session_id(fake_provider):
    gen = Generate(prompt="hi")
    await gen.run(_ctx_for_fake(), session_id="sess-123")
    assert fake_provider.requests[0].session_id == "sess-123"


@pytest.mark.asyncio
async def test_request_contains_fork_session(fake_provider):
    gen = Generate(prompt="hi")
    await gen.run(_ctx_for_fake(), session_id="sess-1", fork_session=True)
    assert fake_provider.requests[0].fork_session is True


@pytest.mark.asyncio
async def test_request_contains_permission_mode(fake_provider):
    gen = Generate(prompt="hi", permission_mode="acceptEdits")
    await gen.run(_ctx_for_fake())
    assert fake_provider.requests[0].permission_mode == "acceptEdits"


@pytest.mark.asyncio
async def test_request_contains_allowed_tools(fake_provider):
    gen = Generate(prompt="hi", allowed_tools=["Read", "Bash"])
    await gen.run(_ctx_for_fake())
    assert fake_provider.requests[0].allowed_tools == ["Read", "Bash"]


@pytest.mark.asyncio
async def test_request_contains_max_turns(fake_provider):
    gen = Generate(prompt="hi", max_turns=5)
    await gen.run(_ctx_for_fake())
    assert fake_provider.requests[0].max_turns == 5


@pytest.mark.asyncio
async def test_request_contains_cwd(fake_provider):
    gen = Generate(prompt="hi", cwd="/tmp/test")
    await gen.run(_ctx_for_fake())
    assert fake_provider.requests[0].cwd == "/tmp/test"


@pytest.mark.asyncio
async def test_request_contains_add_dirs(fake_provider):
    gen = Generate(prompt="hi", add_dirs=["tmp/", "src/"])
    await gen.run(_ctx_for_fake())
    assert fake_provider.requests[0].add_dirs == ["tmp/", "src/"]


@pytest.mark.asyncio
async def test_request_setting_sources_project_stripped_after_guidance(fake_provider):
    """'project' is resolved by Norn and removed from setting_sources passed to the provider."""
    gen = Generate(prompt="hi", setting_sources=["project"])
    await gen.run(_ctx_for_fake())
    assert fake_provider.requests[0].setting_sources is None


@pytest.mark.asyncio
async def test_request_contains_thinking(fake_provider):
    cfg = {"type": "enabled", "budget_tokens": 10000}
    gen = Generate(prompt="hi", thinking=cfg)
    await gen.run(_ctx_for_fake())
    assert fake_provider.requests[0].thinking == cfg


@pytest.mark.asyncio
async def test_request_contains_hooks():
    """Hooks flow into AgentRequest when provider is claude-code."""
    hooks = {"PreToolUse": [{"hooks": [lambda *a: {}]}]}
    gen = Generate(prompt="hi", hooks=hooks)

    class _FakeClaude:
        name = "claude-code"
        requests: list = []

        async def run(self, request):
            _FakeClaude.requests.append(request)
            return
            yield

    original = agent_registry._registry.get("claude-code")
    agent_registry.register(_FakeClaude())
    _FakeClaude.requests.clear()
    try:
        ctx = PipelineContext()
        ctx.agent_provider = "claude-code"
        await gen.run(ctx)
        assert _FakeClaude.requests[0].hooks is hooks
    finally:
        if original:
            agent_registry.register(original)
        else:
            agent_registry._registry.pop("claude-code", None)


@pytest.mark.asyncio
async def test_request_contains_mcp_servers(fake_provider):
    mcp = {"test_server": {"type": "sdk"}}
    gen = Generate(prompt="hi")
    await gen.run(_ctx_for_fake(), mcp_servers=mcp)
    assert fake_provider.requests[0].mcp_servers == mcp


@pytest.mark.asyncio
async def test_request_no_mcp_servers_when_not_provided(fake_provider):
    gen = Generate(prompt="hi")
    await gen.run(_ctx_for_fake())
    assert fake_provider.requests[0].mcp_servers is None


@pytest.mark.asyncio
async def test_request_contains_mcp_tools_from_kwargs():
    """mcp_tools kwarg passed to Generate.run() flows into AgentRequest when provider is claude-code."""
    from unittest.mock import MagicMock

    class _FakeClaude:
        name = "claude-code"
        requests: list = []

        async def run(self, request):
            _FakeClaude.requests.append(request)
            return
            yield

    fake_tool = MagicMock()
    gen = Generate(prompt="hi")

    original = agent_registry._registry.get("claude-code")
    agent_registry.register(_FakeClaude())
    _FakeClaude.requests.clear()
    try:
        ctx = PipelineContext()
        ctx.agent_provider = "claude-code"
        await gen.run(ctx, mcp_tools=[fake_tool])
        assert _FakeClaude.requests[0].mcp_tools == [fake_tool]
    finally:
        if original:
            agent_registry.register(original)
        else:
            agent_registry._registry.pop("claude-code", None)


@pytest.mark.asyncio
async def test_request_no_mcp_tools_when_not_provided(fake_provider):
    gen = Generate(prompt="hi")
    await gen.run(_ctx_for_fake())
    assert fake_provider.requests[0].mcp_tools is None


@pytest.mark.asyncio
async def test_request_contains_attempt(fake_provider):
    gen = Generate(prompt="hi")
    await gen.run(_ctx_for_fake(), attempt=3)
    assert fake_provider.requests[0].attempt == 3


@pytest.mark.asyncio
async def test_request_env_merged(fake_provider):
    """Pipeline env and stage env (with secrets) are merged into request.env."""
    ctx = _ctx_for_fake()
    ctx.env = {"NODE_ENV": "production"}
    ctx.secrets = {"MY_TOKEN": "secret_abc"}
    gen = Generate(prompt="hi", env={"API_KEY": "{secret.MY_TOKEN}"})
    await gen.run(ctx)
    env = fake_provider.requests[0].env
    assert env["NODE_ENV"] == "production"
    assert env["API_KEY"] == "secret_abc"


@pytest.mark.asyncio
async def test_request_pipeline_env_only(fake_provider):
    ctx = _ctx_for_fake()
    ctx.env = {"NODE_ENV": "production"}
    gen = Generate(prompt="hi")
    await gen.run(ctx)
    assert fake_provider.requests[0].env == {"NODE_ENV": "production"}


# ---------------------------------------------------------------------------
# Text event streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_events_stream_into_output(fake_provider):
    fake_provider.events = [
        AgentEvent(text="Hello "),
        AgentEvent(text="world"),
    ]
    gen = Generate(prompt="hi")
    result = await gen.run(_ctx_for_fake())
    assert result.success
    assert result.output == "Hello world"


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_output_becomes_result(fake_provider):
    fake_provider.events = [
        AgentEvent(structured_output={"key": "value"}),
    ]
    gen = Generate(prompt="hi")
    result = await gen.run(_ctx_for_fake())
    assert result.success
    assert result.output == {"key": "value"}


# ---------------------------------------------------------------------------
# Artifact deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifacts_deduplicated(fake_provider):
    fake_provider.events = [
        AgentEvent(artifact="src/foo.py"),
        AgentEvent(artifact="src/bar.py"),
        AgentEvent(artifact="src/foo.py"),  # duplicate
    ]
    gen = Generate(prompt="hi")
    result = await gen.run(_ctx_for_fake())
    assert result.artifacts == ["src/foo.py", "src/bar.py"]


@pytest.mark.asyncio
async def test_artifacts_collected_from_events(fake_provider):
    fake_provider.events = [
        AgentEvent(artifact="src/foo.py"),
        AgentEvent(artifact="src/bar.py"),
    ]
    gen = Generate(prompt="hi")
    result = await gen.run(_ctx_for_fake())
    assert result.artifacts == ["src/foo.py", "src/bar.py"]


# ---------------------------------------------------------------------------
# Usage mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_event_maps_to_usage_record(fake_provider):
    fake_provider.events = [
        AgentEvent(usage=AgentUsage(
            provider="test-provider",
            model="test-model",
            session_id="sess-42",
            total_cost_usd=0.05,
            duration_ms=1234,
            duration_api_ms=1000,
            num_turns=3,
            is_error=False,
            input_tokens=100,
            output_tokens=200,
            cache_read_input_tokens=10,
            cache_creation_input_tokens=5,
        )),
    ]
    gen = Generate(prompt="hi")
    result = await gen.run(_ctx_for_fake())
    assert result.usage is not None
    assert result.usage.provider == "test-provider"
    assert result.usage.session_id == "sess-42"
    assert result.usage.total_cost_usd == 0.05
    assert result.usage.duration_ms == 1234
    assert result.usage.duration_api_ms == 1000
    assert result.usage.num_turns == 3
    assert result.usage.is_error is False
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 200
    assert result.usage.cache_read_input_tokens == 10
    assert result.usage.cache_creation_input_tokens == 5


@pytest.mark.asyncio
async def test_usage_record_model_is_stage_model(fake_provider):
    """UsageRecord.model reflects the stage-level model (pre-resolution alias)."""
    gen = Generate(prompt="hi", model="sonnet")
    result = await gen.run(_ctx_for_fake())
    assert result.usage is not None
    assert result.usage.model == "sonnet"


# ---------------------------------------------------------------------------
# output_file behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_file_written_when_no_tools(fake_provider, tmp_path):
    out = tmp_path / "result.py"
    fake_provider.events = [
        AgentEvent(text="```python\nclass Foo:\n    pass\n```"),
    ]
    gen = Generate(prompt="hi", output_file=str(out))
    result = await gen.run(_ctx_for_fake())
    assert result.success
    assert out.read_text() == "class Foo:\n    pass"
    assert result.output == "class Foo:\n    pass"


@pytest.mark.asyncio
async def test_output_file_ignored_when_tools_active(fake_provider, tmp_path):
    out = tmp_path / "result.py"
    fake_provider.events = [
        AgentEvent(text="I wrote the file for you"),
    ]
    gen = Generate(prompt="hi", output_file=str(out), allowed_tools=["Write"])
    result = await gen.run(_ctx_for_fake())
    assert result.success
    assert not out.exists()
    assert result.output == "I wrote the file for you"


@pytest.mark.asyncio
async def test_output_file_ignored_when_permission_mode_set(fake_provider, tmp_path):
    out = tmp_path / "result.py"
    fake_provider.events = [
        AgentEvent(text="Done"),
    ]
    gen = Generate(prompt="hi", output_file=str(out), permission_mode="acceptEdits")
    result = await gen.run(_ctx_for_fake())
    assert result.success
    assert not out.exists()


# ---------------------------------------------------------------------------
# Context injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_injected_as_system_prompt_when_has_tools(fake_provider):
    gen = Generate(prompt="do stuff", permission_mode="acceptEdits")
    ctx = _ctx_for_fake(injected_context=[("ARCH", "Architecture docs here")])
    await gen.run(ctx)
    req = fake_provider.requests[0]
    assert req.system_prompt is not None
    assert "## ARCH" in req.system_prompt
    assert "Architecture docs here" in req.system_prompt


@pytest.mark.asyncio
async def test_context_prepended_to_prompt_when_no_tools(fake_provider):
    gen = Generate(prompt="my task")
    ctx = _ctx_for_fake(injected_context=[("schema", "CREATE TABLE foo (id INT)")])
    await gen.run(ctx)
    req = fake_provider.requests[0]
    assert "## schema" in req.prompt
    assert "CREATE TABLE foo" in req.prompt
    assert "my task" in req.prompt
    assert req.prompt.index("## schema") < req.prompt.index("my task")


@pytest.mark.asyncio
async def test_no_context_no_system_prompt(fake_provider):
    gen = Generate(prompt="do stuff", permission_mode="acceptEdits")
    await gen.run(_ctx_for_fake())
    assert fake_provider.requests[0].system_prompt is None


# ---------------------------------------------------------------------------
# Session ID propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_id_from_event(fake_provider):
    fake_provider.events = [
        AgentEvent(session_id="sess-abc"),
    ]
    gen = Generate(prompt="hi")
    result = await gen.run(_ctx_for_fake())
    assert result.usage is not None
    assert result.usage.session_id == "sess-abc"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_error_surfaces_stderr(fake_provider):
    """AgentError stderr_lines are rendered in StageResult.error."""
    class FailingProvider:
        name = "_fake-test-provider"

        async def run(self, request):
            raise AgentError(
                RuntimeError("Command failed: Check stderr output for details"),
                stderr_lines=["You've hit your limit · resets 6:30pm"],
            )
            yield  # make it an async generator

    agent_registry.register(FailingProvider())
    gen = Generate(prompt="do stuff")
    result = await gen.run(_ctx_for_fake())
    assert not result.success
    assert result.error is not None
    assert "You've hit your limit" in result.error
    assert "Check stderr output for details" not in result.error


@pytest.mark.asyncio
async def test_non_agent_error_handled(fake_provider):
    """Non-AgentError exceptions are still rendered."""
    class FailingProvider:
        name = "_fake-test-provider"

        async def run(self, request):
            raise RuntimeError("Something went wrong")
            yield

    agent_registry.register(FailingProvider())
    gen = Generate(prompt="do stuff")
    result = await gen.run(_ctx_for_fake())
    assert not result.success
    assert "Something went wrong" in result.error


@pytest.mark.asyncio
async def test_import_error_returns_clear_message(fake_provider):
    """ImportError from provider is surfaced as StageResult error."""
    class FailingProvider:
        name = "_fake-test-provider"

        async def run(self, request):
            raise ImportError("some-sdk is not installed")
            yield

    agent_registry.register(FailingProvider())
    gen = Generate(prompt="hi")
    result = await gen.run(_ctx_for_fake())
    assert not result.success
    assert "some-sdk is not installed" in result.error


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_selects_provider_from_context():
    received_requests: list = []

    class FakeProvider:
        name = "fake-ctx-provider"

        async def run(self, request):
            received_requests.append(request)
            return
            yield

    fake = FakeProvider()
    agent_registry.register(fake)
    try:
        gen = Generate(prompt="hello from fake")
        ctx = PipelineContext()
        ctx.agent_provider = "fake-ctx-provider"
        await gen.run(ctx)
        assert received_requests, "Provider was never called"
        assert received_requests[0].provider == "fake-ctx-provider"
    finally:
        agent_registry._registry.pop("fake-ctx-provider", None)


@pytest.mark.asyncio
async def test_generate_uses_claude_code_provider_by_default():
    ctx = PipelineContext()
    assert ctx.agent_provider == "claude-code"
    provider = agent_registry.get_provider("claude-code")
    assert provider is not None


# ---------------------------------------------------------------------------
# norn.agents.models – resolve_model
# ---------------------------------------------------------------------------


def test_resolve_model_none_returns_none():
    assert resolve_model("claude-code", None) is None
    assert resolve_model("opencode", None) is None


def test_resolve_model_claude_code_aliases():
    assert resolve_model("claude-code", "opus") == MODEL_MAP["opus"]
    assert resolve_model("claude-code", "sonnet") == MODEL_MAP["sonnet"]
    assert resolve_model("claude-code", "haiku") == MODEL_MAP["haiku"]


def test_resolve_model_claude_code_full_id_passthrough():
    assert resolve_model("claude-code", "claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert resolve_model("claude-code", "claude-opus-4-6") == "claude-opus-4-6"


def test_resolve_model_opencode_aliases():
    assert resolve_model("opencode", "opus") == "github-copilot/claude-opus-4.7"
    assert resolve_model("opencode", "sonnet") == "github-copilot/claude-sonnet-4.6"
    assert resolve_model("opencode", "haiku") == "github-copilot/claude-sonnet-4.6"


def test_resolve_model_opencode_normalises_claude_prefix():
    assert resolve_model("opencode", "claude-sonnet-4-6") == "anthropic/claude-sonnet-4-6"
    assert resolve_model("opencode", "claude-opus-4-6") == "anthropic/claude-opus-4-6"


def test_resolve_model_opencode_anthropic_prefixed_passthrough():
    assert resolve_model("opencode", "anthropic/claude-sonnet-4-6") == "anthropic/claude-sonnet-4-6"


def test_resolve_model_opencode_unknown_id_passthrough():
    assert resolve_model("opencode", "gpt-4o") == "gpt-4o"


def test_resolve_model_unknown_provider_passthrough():
    assert resolve_model("some-future-provider", "sonnet") == "sonnet"
    assert resolve_model("some-future-provider", "my-custom-model") == "my-custom-model"


def test_model_aliases_claude_code_consistent_with_model_map():
    assert MODEL_ALIASES["claude-code"] == MODEL_MAP


# ---------------------------------------------------------------------------
# norn.agents.registry – get_provider
# ---------------------------------------------------------------------------


def test_registry_raises_for_unknown_provider():
    with pytest.raises(ValueError, match="Unknown agent provider"):
        agent_registry.get_provider("nonexistent-provider")


def test_registry_raises_includes_provider_name():
    with pytest.raises(ValueError, match="nonexistent-provider"):
        agent_registry.get_provider("nonexistent-provider")


def test_registry_returns_registered_provider():
    class FakeProvider:
        name = "fake-test-provider"

        async def run(self, request):
            return
            yield

    fake = FakeProvider()
    agent_registry.register(fake)
    try:
        result = agent_registry.get_provider("fake-test-provider")
        assert result is fake
    finally:
        agent_registry._registry.pop("fake-test-provider", None)


# ---------------------------------------------------------------------------
# ClaudeCodeProvider – MCP tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_provider_creates_mcp_server_from_mcp_tools():
    """ClaudeCodeProvider calls create_sdk_mcp_server when request.mcp_tools is set."""
    import sys
    from unittest.mock import MagicMock, patch

    from norn.agents.claude_code import ClaudeCodeProvider
    from norn.agents.base import AgentRequest

    fake_tool = MagicMock()
    fake_server = MagicMock()

    mock_sdk = MagicMock()
    mock_sdk.create_sdk_mcp_server.return_value = fake_server

    async def fake_query(prompt, options):
        return
        yield  # make it an async generator

    mock_sdk.query = fake_query

    request = AgentRequest(
        prompt="test",
        stage_name="my_stage",
        provider="claude-code",
        mcp_tools=[fake_tool],
    )

    mock_types = MagicMock()
    with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk, "claude_agent_sdk.types": mock_types}):
        provider = ClaudeCodeProvider()
        _events = [event async for event in provider.run(request)]

    mock_sdk.create_sdk_mcp_server.assert_called_once_with("my_stage", tools=[fake_tool])
    options_kwargs = mock_sdk.ClaudeAgentOptions.call_args.kwargs
    assert options_kwargs.get("mcp_servers") == {"my_stage": fake_server}


# ---------------------------------------------------------------------------
# Project guidance injection (setting_sources=["project"])
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_md_injected_into_system_prompt(fake_provider, tmp_path):
    """AGENTS.md content is injected into system_prompt for setting_sources=["project"]."""
    (tmp_path / "AGENTS.md").write_text("Use pytest for all tests.")
    gen = Generate(prompt="do stuff", setting_sources=["project"], cwd=str(tmp_path))
    await gen.run(_ctx_for_fake())
    req = fake_provider.requests[0]
    assert req.system_prompt is not None
    assert "Use pytest for all tests." in req.system_prompt
    assert "AGENTS.md" in req.system_prompt


@pytest.mark.asyncio
async def test_claude_md_injected_when_present(fake_provider, tmp_path):
    """CLAUDE.md content is injected into system_prompt for setting_sources=["project"]."""
    (tmp_path / "CLAUDE.md").write_text("Always use type hints.")
    gen = Generate(prompt="do stuff", setting_sources=["project"], cwd=str(tmp_path))
    await gen.run(_ctx_for_fake())
    req = fake_provider.requests[0]
    assert req.system_prompt is not None
    assert "Always use type hints." in req.system_prompt
    assert "CLAUDE.md" in req.system_prompt


@pytest.mark.asyncio
async def test_both_agents_and_claude_md_injected(fake_provider, tmp_path):
    """Both AGENTS.md and CLAUDE.md are injected when present."""
    (tmp_path / "AGENTS.md").write_text("Agents guidance here.")
    (tmp_path / "CLAUDE.md").write_text("Claude guidance here.")
    gen = Generate(prompt="do stuff", setting_sources=["project"], cwd=str(tmp_path))
    await gen.run(_ctx_for_fake())
    req = fake_provider.requests[0]
    assert req.system_prompt is not None
    assert "Agents guidance here." in req.system_prompt
    assert "Claude guidance here." in req.system_prompt


@pytest.mark.asyncio
async def test_opencode_json_instructions_injected(fake_provider, tmp_path):
    """opencode.json instructions file paths are read and injected."""
    import json

    (tmp_path / "instructions.md").write_text("OpenCode project rules.")
    (tmp_path / "opencode.json").write_text(json.dumps({
        "instructions": ["instructions.md"],
    }))
    gen = Generate(prompt="do stuff", setting_sources=["project"], cwd=str(tmp_path))
    await gen.run(_ctx_for_fake())
    req = fake_provider.requests[0]
    assert req.system_prompt is not None
    assert "OpenCode project rules." in req.system_prompt


@pytest.mark.asyncio
async def test_opencode_json_instructions_glob(fake_provider, tmp_path):
    """opencode.json instructions with glob patterns resolve matching files."""
    import json

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "style.md").write_text("Style guide content.")
    (docs / "arch.md").write_text("Architecture content.")
    (tmp_path / "opencode.json").write_text(json.dumps({
        "instructions": ["docs/*.md"],
    }))
    gen = Generate(prompt="do stuff", setting_sources=["project"], cwd=str(tmp_path))
    await gen.run(_ctx_for_fake())
    req = fake_provider.requests[0]
    assert req.system_prompt is not None
    assert "Style guide content." in req.system_prompt
    assert "Architecture content." in req.system_prompt


@pytest.mark.asyncio
async def test_settings_local_json_not_read(fake_provider, tmp_path):
    """.claude/settings.local.json is never read or injected."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text('{"secret": "do-not-inject"}')
    gen = Generate(prompt="do stuff", setting_sources=["project"], cwd=str(tmp_path))
    await gen.run(_ctx_for_fake())
    req = fake_provider.requests[0]
    # No guidance files exist, so system_prompt should be None
    assert req.system_prompt is None


@pytest.mark.asyncio
async def test_unsupported_setting_source_non_claude_fails():
    """Non-portable setting_sources fail for non-Claude providers as StageResult."""
    gen = Generate(prompt="do stuff", setting_sources=["user"])
    # opencode is a registered provider that does not support non-project setting_sources
    ctx = PipelineContext()
    ctx.agent_provider = "opencode"
    result = await gen.run(ctx)
    assert not result.success
    assert result.error is not None
    assert "setting_sources" in result.error.lower() or "'user'" in result.error
    assert "opencode" in result.error


@pytest.mark.asyncio
async def test_unsupported_setting_source_claude_code_passes(fake_provider):
    """Non-portable setting_sources pass through for claude-code provider."""
    gen = Generate(prompt="do stuff", setting_sources=["user"])
    ctx = PipelineContext()
    ctx.agent_provider = "claude-code"
    # Register a provider that responds to "claude-code"
    class FakeClaude:
        name = "claude-code"

        async def run(self, request):
            return
            yield

    original = agent_registry._registry.get("claude-code")
    agent_registry.register(FakeClaude())
    try:
        result = await gen.run(ctx)
        # Should not raise — claude-code can handle non-portable sources
        assert result is not None
    finally:
        if original:
            agent_registry.register(original)


@pytest.mark.asyncio
async def test_guidance_composes_with_skills(fake_provider, tmp_path):
    """Project guidance and skills both appear in system_prompt."""
    (tmp_path / "AGENTS.md").write_text("Project rules here.")
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    (skill_dir / "review.md").write_text("Review skill content.")

    import os
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        gen = Generate(
            prompt="review code",
            setting_sources=["project"],
            cwd=str(tmp_path),
            skills=["review"],
        )
        await gen.run(_ctx_for_fake())
    finally:
        os.chdir(old_cwd)

    req = fake_provider.requests[0]
    assert req.system_prompt is not None
    assert "Project rules here." in req.system_prompt
    assert "Review skill content." in req.system_prompt


@pytest.mark.asyncio
async def test_guidance_composes_with_template_system_prompt(fake_provider, tmp_path):
    """Project guidance and template system_prompt both appear in system_prompt."""
    (tmp_path / "AGENTS.md").write_text("Agents rules.")

    from norn.templates import PromptTemplate

    tmpl = PromptTemplate(
        name="review",
        template="{input}",
        system_prompt="You are a code reviewer.",
    )
    with patch("norn.stages.generate.load_template", return_value=tmpl):
        gen = Generate(
            template="review",
            input="review this",
            setting_sources=["project"],
            cwd=str(tmp_path),
        )
        await gen.run(_ctx_for_fake())

    req = fake_provider.requests[0]
    assert req.system_prompt is not None
    assert "Agents rules." in req.system_prompt
    assert "You are a code reviewer." in req.system_prompt


@pytest.mark.asyncio
async def test_no_guidance_files_no_extra_system_prompt(fake_provider, tmp_path):
    """When no guidance files exist, setting_sources=["project"] doesn't add system_prompt."""
    gen = Generate(prompt="do stuff", setting_sources=["project"], cwd=str(tmp_path))
    await gen.run(_ctx_for_fake())
    req = fake_provider.requests[0]
    assert req.system_prompt is None


@pytest.mark.asyncio
async def test_setting_sources_project_stripped_from_request(fake_provider, tmp_path):
    """'project' is removed from setting_sources in the request after Norn resolves it."""
    (tmp_path / "AGENTS.md").write_text("guidance")
    gen = Generate(prompt="hi", setting_sources=["project"], cwd=str(tmp_path))
    await gen.run(_ctx_for_fake())
    req = fake_provider.requests[0]
    assert req.setting_sources is None


@pytest.mark.asyncio
async def test_mixed_setting_sources_project_stripped_others_kept(fake_provider, tmp_path):
    """'project' is stripped but other sources are preserved for claude-code."""
    (tmp_path / "AGENTS.md").write_text("guidance")
    gen = Generate(prompt="hi", setting_sources=["project", "user"], cwd=str(tmp_path))
    ctx = PipelineContext()
    ctx.agent_provider = "claude-code"

    class FakeClaude:
        name = "claude-code"

        async def run(self, request):
            self.last_request = request
            return
            yield

    fc = FakeClaude()
    agent_registry.register(fc)
    try:
        await gen.run(ctx)
        assert fc.last_request.setting_sources == ["user"]
    finally:
        agent_registry.register(original) if (original := agent_registry._registry.get("claude-code")) else None
        # Restore original claude-code provider
        from norn.agents.claude_code import ClaudeCodeProvider
        agent_registry.register(ClaudeCodeProvider())


# ---------------------------------------------------------------------------
# Permission normalization (norn.agents.permissions)
# ---------------------------------------------------------------------------


from norn.agents.permissions import AgentPermissions, normalize_permissions


def test_normalize_no_tools_no_mode():
    """No tools and no mode → all False."""
    p = normalize_permissions(None, None)
    assert p == AgentPermissions(file_read=False, file_edit=False, terminal=False, plan_only=False)


def test_normalize_read_tool_gives_file_read():
    p = normalize_permissions(["Read"], None)
    assert p.file_read is True
    assert p.file_edit is False
    assert p.terminal is False
    assert p.plan_only is False


def test_normalize_write_tool_gives_file_edit():
    p = normalize_permissions(["Write"], None)
    assert p.file_edit is True
    assert p.file_read is False
    assert p.terminal is False


def test_normalize_edit_tool_gives_file_edit():
    p = normalize_permissions(["Edit"], None)
    assert p.file_edit is True


def test_normalize_notebook_edit_gives_file_edit():
    p = normalize_permissions(["NotebookEdit"], None)
    assert p.file_edit is True


def test_normalize_bash_tool_gives_terminal():
    p = normalize_permissions(["Bash"], None)
    assert p.terminal is True
    assert p.file_read is False
    assert p.file_edit is False


def test_normalize_multiple_tools():
    p = normalize_permissions(["Read", "Write", "Edit", "NotebookEdit", "Bash"], None)
    assert p.file_read is True
    assert p.file_edit is True
    assert p.terminal is True
    assert p.plan_only is False


def test_normalize_default_mode_uses_tools_only():
    """'default' mode behaves the same as None — only tools determine categories."""
    p_none = normalize_permissions(["Read"], None)
    p_default = normalize_permissions(["Read"], "default")
    assert p_none == p_default


def test_normalize_accept_edits_implies_file_edit():
    p = normalize_permissions(None, "acceptEdits")
    assert p.file_edit is True
    assert p.terminal is False
    assert p.plan_only is False


def test_normalize_accept_edits_does_not_imply_terminal():
    """acceptEdits alone must not grant terminal access."""
    p = normalize_permissions(None, "acceptEdits")
    assert p.terminal is False


def test_normalize_accept_edits_with_bash_grants_terminal():
    """acceptEdits + Bash in allowed_tools → terminal is True."""
    p = normalize_permissions(["Bash"], "acceptEdits")
    assert p.file_edit is True
    assert p.terminal is True


def test_normalize_bypass_permissions_implies_file_edit_and_terminal():
    p = normalize_permissions(None, "bypassPermissions")
    assert p.file_edit is True
    assert p.terminal is True
    assert p.plan_only is False


def test_normalize_bypass_permissions_overrides_tool_list():
    """bypassPermissions grants file_edit/terminal regardless of tool list."""
    p = normalize_permissions([], "bypassPermissions")
    assert p.file_edit is True
    assert p.terminal is True


def test_normalize_plan_mode_is_plan_only():
    p = normalize_permissions(None, "plan")
    assert p.plan_only is True
    assert p.file_edit is False
    assert p.terminal is False


def test_normalize_plan_mode_disables_file_edit_even_with_write_tool():
    """plan mode overrides allowed file-edit tools."""
    p = normalize_permissions(["Write", "Edit"], "plan")
    assert p.file_edit is False
    assert p.plan_only is True


def test_normalize_plan_mode_disables_terminal_even_with_bash():
    """plan mode overrides Bash in allowed_tools."""
    p = normalize_permissions(["Bash"], "plan")
    assert p.terminal is False
    assert p.plan_only is True


# ---------------------------------------------------------------------------
# Permissions flow through to AgentRequest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_permissions_set_with_allowed_tools(fake_provider):
    gen = Generate(prompt="hi", allowed_tools=["Read", "Write", "Bash"])
    await gen.run(_ctx_for_fake())
    req = fake_provider.requests[0]
    assert req.permissions is not None
    assert req.permissions.file_read is True
    assert req.permissions.file_edit is True
    assert req.permissions.terminal is True


@pytest.mark.asyncio
async def test_request_permissions_set_with_permission_mode(fake_provider):
    gen = Generate(prompt="hi", permission_mode="bypassPermissions")
    await gen.run(_ctx_for_fake())
    req = fake_provider.requests[0]
    assert req.permissions is not None
    assert req.permissions.file_edit is True
    assert req.permissions.terminal is True


@pytest.mark.asyncio
async def test_request_permissions_none_when_no_tools_or_mode(fake_provider):
    """With no allowed_tools and no permission_mode, permissions has all False."""
    gen = Generate(prompt="hi")
    await gen.run(_ctx_for_fake())
    req = fake_provider.requests[0]
    assert req.permissions is not None
    assert req.permissions == AgentPermissions()


# ---------------------------------------------------------------------------
# ClaudeCodeProvider still receives original Claude permission fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_provider_receives_original_allowed_tools():
    """ClaudeCodeProvider passes allowed_tools unchanged to ClaudeAgentOptions."""
    import sys

    from norn.agents.claude_code import ClaudeCodeProvider
    from norn.agents.base import AgentRequest
    from norn.agents.permissions import normalize_permissions

    mock_sdk = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()

    async def fake_query(prompt, options):
        return
        yield

    mock_sdk.query = fake_query

    request = AgentRequest(
        prompt="test",
        stage_name="s",
        provider="claude-code",
        allowed_tools=["Read", "Bash"],
        permission_mode="acceptEdits",
        permissions=normalize_permissions(["Read", "Bash"], "acceptEdits"),
    )

    from unittest.mock import MagicMock as _MagicMock  # noqa: PLC0415
    mock_types = _MagicMock()
    with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk, "claude_agent_sdk.types": mock_types}):
        provider = ClaudeCodeProvider()
        _events = [event async for event in provider.run(request)]

    options_kwargs = mock_sdk.ClaudeAgentOptions.call_args.kwargs
    assert options_kwargs.get("allowed_tools") == ["Read", "Bash"]
    assert options_kwargs.get("permission_mode") == "acceptEdits"


@pytest.mark.asyncio
async def test_claude_provider_receives_bypass_permissions_mode():
    """ClaudeCodeProvider passes bypassPermissions permission_mode unchanged."""
    import sys

    from norn.agents.claude_code import ClaudeCodeProvider
    from norn.agents.base import AgentRequest
    from norn.agents.permissions import normalize_permissions

    mock_sdk = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()

    async def fake_query(prompt, options):
        return
        yield

    mock_sdk.query = fake_query

    request = AgentRequest(
        prompt="test",
        stage_name="s",
        provider="claude-code",
        permission_mode="bypassPermissions",
        permissions=normalize_permissions(None, "bypassPermissions"),
    )

    from unittest.mock import MagicMock as _MagicMock  # noqa: PLC0415
    mock_types = _MagicMock()
    with patch.dict(sys.modules, {"claude_agent_sdk": mock_sdk, "claude_agent_sdk.types": mock_types}):
        provider = ClaudeCodeProvider()
        _events = [event async for event in provider.run(request)]

    options_kwargs = mock_sdk.ClaudeAgentOptions.call_args.kwargs
    assert options_kwargs.get("permission_mode") == "bypassPermissions"


# ---------------------------------------------------------------------------
# OpenCodeProvider
# ---------------------------------------------------------------------------

import asyncio
import json as json_mod

from norn.agents.opencode import OpenCodeError, OpenCodeProvider
from norn.agents.permissions import normalize_permissions as _normalize_permissions


def _opencode_request(**overrides) -> AgentRequest:
    """Build a minimal ``AgentRequest`` targeting the opencode provider."""
    defaults = dict(
        prompt="hello",
        stage_name="test_stage",
        provider="opencode",
    )
    defaults.update(overrides)
    return AgentRequest(**defaults)


def _json_line(data: dict) -> bytes:
    """Encode a dict as a JSON line terminated with newline."""
    return (json_mod.dumps(data) + "\n").encode()


def _step_start_event(session_id: str = "ses_abc123") -> dict:
    return {
        "type": "step_start",
        "timestamp": 1000,
        "sessionID": session_id,
        "part": {"id": "prt_1", "type": "step-start", "sessionID": session_id},
    }


def _text_event(text: str, session_id: str = "ses_abc123") -> dict:
    return {
        "type": "text",
        "timestamp": 1001,
        "sessionID": session_id,
        "part": {
            "id": "prt_2",
            "type": "text",
            "text": text,
            "sessionID": session_id,
        },
    }


def _tool_use_event(
    tool: str = "apply_patch",
    files: list[dict] | None = None,
    session_id: str = "ses_abc123",
) -> dict:
    return {
        "type": "tool_use",
        "timestamp": 1002,
        "sessionID": session_id,
        "part": {
            "type": "tool",
            "tool": tool,
            "state": {
                "status": "completed",
                "metadata": {
                    "files": files or [],
                },
            },
            "sessionID": session_id,
        },
    }


def _step_finish_event(
    reason: str = "stop",
    tokens: dict | None = None,
    cost: float = 0.0,
    session_id: str = "ses_abc123",
) -> dict:
    return {
        "type": "step_finish",
        "timestamp": 1003,
        "sessionID": session_id,
        "part": {
            "id": "prt_3",
            "type": "step-finish",
            "reason": reason,
            "sessionID": session_id,
            "tokens": tokens or {"total": 100, "input": 80, "output": 20, "reasoning": 0, "cache": {"write": 0, "read": 0}},
            "cost": cost,
        },
    }


class _FakeProcess:
    """Mock asyncio subprocess for OpenCode CLI tests."""

    def __init__(self, events: list[dict], returncode: int = 0, stderr_text: str = ""):
        self.returncode = returncode
        self._events = events
        self._stderr_text = stderr_text

        # Build stdout as an async iterable of JSON lines
        self.stdout = self._make_stdout()
        self.stderr = self._make_stderr()

    def _make_stdout(self):
        lines = [_json_line(e) for e in self._events]

        class _AsyncLineIter:
            def __init__(self, data):
                self._lines = data
                self._index = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._index >= len(self._lines):
                    raise StopAsyncIteration
                line = self._lines[self._index]
                self._index += 1
                return line

            async def readline(self):
                if self._index >= len(self._lines):
                    return b""
                line = self._lines[self._index]
                self._index += 1
                return line

        return _AsyncLineIter(lines)

    def _make_stderr(self):
        text = self._stderr_text

        class _AsyncStderr:
            async def read(self):
                return text.encode()

        return _AsyncStderr()

    async def wait(self):
        pass

    def kill(self):
        # Mirror asyncio.subprocess.Process.kill for the watchdog test path.
        self.killed = True


def _patch_subprocess(events, returncode=0, stderr_text=""):
    """Return a patch context manager for ``asyncio.create_subprocess_exec``."""
    proc = _FakeProcess(events, returncode, stderr_text)

    async def _fake_exec(*args, **kwargs):
        return proc

    return patch("asyncio.create_subprocess_exec", side_effect=_fake_exec), proc


def _patch_which(found: bool = True):
    """Patch ``shutil.which`` to control opencode availability."""
    return patch("shutil.which", return_value="/usr/local/bin/opencode" if found else None)


# -- New session creation --


@pytest.mark.asyncio
async def test_opencode_new_session_creation():
    """When no session_id, a new session is created and its ID is emitted."""
    events = [
        _step_start_event("ses_new"),
        _text_event("Hi there", "ses_new"),
        _step_finish_event(session_id="ses_new"),
    ]
    sub_patch, _ = _patch_subprocess(events)
    with sub_patch, _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request()
        collected = [e async for e in provider.run(request)]

    session_events = [e for e in collected if e.session_id is not None]
    assert len(session_events) >= 1
    assert session_events[0].session_id == "ses_new"


# -- Existing session continuation --


@pytest.mark.asyncio
async def test_opencode_existing_session_continuation():
    """When session_id is present, --session flag is passed to CLI."""
    events = [
        _step_start_event("ses_existing"),
        _text_event("Continued", "ses_existing"),
        _step_finish_event(session_id="ses_existing"),
    ]
    sub_patch, _ = _patch_subprocess(events)
    captured_cmd: list = []

    async def _capture_exec(*args, **kwargs):
        captured_cmd.extend(args)
        return _FakeProcess(events)

    with patch("asyncio.create_subprocess_exec", side_effect=_capture_exec), _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request(session_id="ses_existing")
        _ = [e async for e in provider.run(request)]

    assert "--session" in captured_cmd
    idx = captured_cmd.index("--session")
    assert captured_cmd[idx + 1] == "ses_existing"


# -- cwd is passed into the CLI --


@pytest.mark.asyncio
async def test_opencode_cwd_passed_to_cli():
    """cwd from request is passed as --dir flag and subprocess cwd."""
    events = [_step_finish_event()]
    captured_cmd: list = []
    captured_kwargs: dict = {}

    async def _capture_exec(*args, **kwargs):
        captured_cmd.extend(args)
        captured_kwargs.update(kwargs)
        return _FakeProcess(events)

    with patch("asyncio.create_subprocess_exec", side_effect=_capture_exec), _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request(cwd="/my/project")
        _ = [e async for e in provider.run(request)]

    assert "--dir" in captured_cmd
    idx = captured_cmd.index("--dir")
    assert captured_cmd[idx + 1] == "/my/project"
    assert captured_kwargs.get("cwd") == "/my/project"


# -- Alias model resolution and Claude-style ID normalization --


@pytest.mark.asyncio
async def test_opencode_alias_model_resolution():
    """Model alias 'sonnet' resolves to github-copilot/claude-sonnet-4.6."""
    events = [_step_finish_event()]
    captured_cmd: list = []

    async def _capture_exec(*args, **kwargs):
        captured_cmd.extend(args)
        return _FakeProcess(events)

    with patch("asyncio.create_subprocess_exec", side_effect=_capture_exec), _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request(model="sonnet")
        _ = [e async for e in provider.run(request)]

    assert "-m" in captured_cmd
    idx = captured_cmd.index("-m")
    assert captured_cmd[idx + 1] == "github-copilot/claude-sonnet-4.6"


@pytest.mark.asyncio
async def test_opencode_claude_id_normalization():
    """Bare claude-* IDs are normalized to anthropic/ prefix."""
    events = [_step_finish_event()]
    captured_cmd: list = []

    async def _capture_exec(*args, **kwargs):
        captured_cmd.extend(args)
        return _FakeProcess(events)

    with patch("asyncio.create_subprocess_exec", side_effect=_capture_exec), _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request(model="claude-opus-4-6")
        _ = [e async for e in provider.run(request)]

    assert "-m" in captured_cmd
    idx = captured_cmd.index("-m")
    assert captured_cmd[idx + 1] == "anthropic/claude-opus-4-6"


# -- Permission mapping --


@pytest.mark.asyncio
async def test_opencode_file_edit_and_terminal_uses_skip_permissions():
    """file_edit + terminal → --dangerously-skip-permissions."""
    events = [_step_finish_event()]
    captured_cmd: list = []

    async def _capture_exec(*args, **kwargs):
        captured_cmd.extend(args)
        return _FakeProcess(events)

    with patch("asyncio.create_subprocess_exec", side_effect=_capture_exec), _patch_which():
        provider = OpenCodeProvider()
        perms = _normalize_permissions(["Write", "Bash"], "bypassPermissions")
        request = _opencode_request(
            allowed_tools=["Write", "Bash"],
            permission_mode="bypassPermissions",
            permissions=perms,
        )
        _ = [e async for e in provider.run(request)]

    assert "--dangerously-skip-permissions" in captured_cmd


@pytest.mark.asyncio
async def test_opencode_file_edit_without_terminal_fails():
    """file_edit without terminal → fails because OpenCode can't represent it."""
    with _patch_which():
        provider = OpenCodeProvider()
        perms = _normalize_permissions(["Write"], "acceptEdits")
        request = _opencode_request(
            allowed_tools=["Write"],
            permission_mode="acceptEdits",
            permissions=perms,
        )
        with pytest.raises(OpenCodeError, match="cannot grant file-edit"):
            _ = [e async for e in provider.run(request)]


@pytest.mark.asyncio
async def test_opencode_no_permissions_no_skip_flag():
    """No permissions → no --dangerously-skip-permissions."""
    events = [_step_finish_event()]
    captured_cmd: list = []

    async def _capture_exec(*args, **kwargs):
        captured_cmd.extend(args)
        return _FakeProcess(events)

    with patch("asyncio.create_subprocess_exec", side_effect=_capture_exec), _patch_which():
        provider = OpenCodeProvider()
        perms = _normalize_permissions(None, None)
        request = _opencode_request(permissions=perms)
        _ = [e async for e in provider.run(request)]

    assert "--dangerously-skip-permissions" not in captured_cmd


@pytest.mark.asyncio
async def test_opencode_plan_only_no_skip_flag():
    """plan_only → no --dangerously-skip-permissions."""
    events = [_step_finish_event()]
    captured_cmd: list = []

    async def _capture_exec(*args, **kwargs):
        captured_cmd.extend(args)
        return _FakeProcess(events)

    with patch("asyncio.create_subprocess_exec", side_effect=_capture_exec), _patch_which():
        provider = OpenCodeProvider()
        perms = _normalize_permissions(None, "plan")
        request = _opencode_request(permissions=perms)
        _ = [e async for e in provider.run(request)]

    assert "--dangerously-skip-permissions" not in captured_cmd


# -- Unsupported structured output --


@pytest.mark.asyncio
async def test_opencode_structured_output_fails():
    """Structured output (output_format) is rejected."""
    with _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request(output_format={"type": "json_schema", "schema": {}})
        with pytest.raises(OpenCodeError, match="Structured output"):
            _ = [e async for e in provider.run(request)]


# -- Unsupported fork_session --


@pytest.mark.asyncio
async def test_opencode_fork_session_fails():
    """fork_session is rejected."""
    with _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request(fork_session=True)
        with pytest.raises(OpenCodeError, match="fork_session"):
            _ = [e async for e in provider.run(request)]


# -- Unsupported hooks --


@pytest.mark.asyncio
async def test_opencode_hooks_fails():
    """hooks are rejected."""
    with _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request(hooks={"PostToolUse": []})
        with pytest.raises(OpenCodeError, match="hooks"):
            _ = [e async for e in provider.run(request)]


# -- Unsupported mcp_servers --


@pytest.mark.asyncio
async def test_opencode_mcp_servers_fails():
    """mcp_servers are rejected."""
    with _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request(mcp_servers={"test": {}})
        with pytest.raises(OpenCodeError, match="mcp_servers"):
            _ = [e async for e in provider.run(request)]


# -- Unsupported mcp_tools --


@pytest.mark.asyncio
async def test_opencode_mcp_tools_fails():
    """mcp_tools are rejected."""
    with _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request(mcp_tools=["some_tool"])
        with pytest.raises(OpenCodeError, match="mcp_tools"):
            _ = [e async for e in provider.run(request)]


# -- Unsupported thinking --


@pytest.mark.asyncio
async def test_opencode_thinking_fails():
    """thinking budget is rejected."""
    with _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request(thinking={"type": "enabled", "budget_tokens": 5000})
        with pytest.raises(OpenCodeError, match="thinking"):
            _ = [e async for e in provider.run(request)]


# -- Text, session ID, usage, and artifacts mapped to events --


@pytest.mark.asyncio
async def test_opencode_text_events_streamed():
    """Text events are mapped to AgentEvent.text."""
    events = [
        _step_start_event(),
        _text_event("Hello "),
        _text_event("world"),
        _step_finish_event(),
    ]
    sub_patch, _ = _patch_subprocess(events)
    with sub_patch, _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request()
        collected = [e async for e in provider.run(request)]

    text_events = [e for e in collected if e.text is not None]
    assert len(text_events) == 2
    assert text_events[0].text == "Hello "
    assert text_events[1].text == "world"


@pytest.mark.asyncio
async def test_opencode_usage_mapped():
    """Token and cost data from step_finish is mapped to AgentUsage."""
    tokens = {"total": 500, "input": 400, "output": 100, "reasoning": 0, "cache": {"write": 10, "read": 50}}
    events = [
        _step_start_event(),
        _step_finish_event(tokens=tokens, cost=0.02),
    ]
    sub_patch, _ = _patch_subprocess(events)
    with sub_patch, _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request(model="sonnet")
        collected = [e async for e in provider.run(request)]

    usage_events = [e for e in collected if e.usage is not None]
    assert len(usage_events) == 1
    usage = usage_events[0].usage
    assert usage.provider == "opencode"
    assert usage.model == "sonnet"
    assert usage.input_tokens == 400
    assert usage.output_tokens == 100
    assert usage.cache_read_input_tokens == 50
    assert usage.cache_creation_input_tokens == 10
    assert usage.total_cost_usd == 0.02
    assert usage.num_turns == 1
    assert usage.is_error is False


@pytest.mark.asyncio
async def test_opencode_multi_step_tokens_accumulated():
    """Tokens are accumulated across multiple step_finish events."""
    tokens1 = {"total": 100, "input": 80, "output": 20, "reasoning": 0, "cache": {"write": 0, "read": 0}}
    tokens2 = {"total": 200, "input": 150, "output": 50, "reasoning": 0, "cache": {"write": 5, "read": 10}}
    events = [
        _step_start_event(),
        _step_finish_event(tokens=tokens1, cost=0.01, reason="tool-calls"),
        _step_start_event(),
        _step_finish_event(tokens=tokens2, cost=0.03),
    ]
    sub_patch, _ = _patch_subprocess(events)
    with sub_patch, _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request()
        collected = [e async for e in provider.run(request)]

    usage_events = [e for e in collected if e.usage is not None]
    assert len(usage_events) == 1
    usage = usage_events[0].usage
    assert usage.input_tokens == 230
    assert usage.output_tokens == 70
    assert usage.cache_read_input_tokens == 10
    assert usage.cache_creation_input_tokens == 5
    assert usage.total_cost_usd == pytest.approx(0.04)
    assert usage.num_turns == 2


@pytest.mark.asyncio
async def test_opencode_artifacts_from_tool_events():
    """File paths from tool_use events are emitted as artifact events."""
    events = [
        _step_start_event(),
        _tool_use_event(files=[
            {"filePath": "/proj/src/foo.py", "type": "modify"},
            {"filePath": "/proj/src/bar.py", "type": "add"},
        ]),
        _step_finish_event(),
    ]
    sub_patch, _ = _patch_subprocess(events)
    with sub_patch, _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request()
        collected = [e async for e in provider.run(request)]

    artifact_events = [e for e in collected if e.artifact is not None]
    assert len(artifact_events) == 2
    assert artifact_events[0].artifact == "/proj/src/foo.py"
    assert artifact_events[1].artifact == "/proj/src/bar.py"


@pytest.mark.asyncio
async def test_opencode_duplicate_artifacts_deduplicated():
    """Duplicate artifact paths are deduplicated."""
    events = [
        _step_start_event(),
        _tool_use_event(files=[{"filePath": "/proj/foo.py", "type": "modify"}]),
        _tool_use_event(files=[{"filePath": "/proj/foo.py", "type": "modify"}]),
        _step_finish_event(),
    ]
    sub_patch, _ = _patch_subprocess(events)
    with sub_patch, _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request()
        collected = [e async for e in provider.run(request)]

    artifact_events = [e for e in collected if e.artifact is not None]
    assert len(artifact_events) == 1


@pytest.mark.asyncio
async def test_opencode_session_id_in_usage():
    """Session ID from events is included in the usage record."""
    events = [
        _step_start_event("ses_usage_test"),
        _step_finish_event(session_id="ses_usage_test"),
    ]
    sub_patch, _ = _patch_subprocess(events)
    with sub_patch, _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request()
        collected = [e async for e in provider.run(request)]

    usage_events = [e for e in collected if e.usage is not None]
    assert usage_events[0].usage.session_id == "ses_usage_test"


# -- Error handling --


@pytest.mark.asyncio
async def test_opencode_nonzero_exit_raises():
    """Non-zero exit code raises OpenCodeError with stderr."""
    events = [_step_start_event()]
    sub_patch, _ = _patch_subprocess(events, returncode=1, stderr_text="Authentication failed")
    with sub_patch, _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request()
        with pytest.raises(OpenCodeError, match="Authentication failed"):
            _ = [e async for e in provider.run(request)]


@pytest.mark.asyncio
async def test_opencode_error_event_raises_even_on_zero_exit():
    """`error` events surface as OpenCodeError even when the CLI exits 0.

    OpenCode emits e.g. `{"type":"error",...,"error":{"data":{"message":"Model not found: ..."}}}`
    and exits with status 0. Without this guard the stage would silently
    report success with 0 tokens — see the model-not-found case that
    motivated the fix.
    """
    error_event = {
        "type": "error",
        "timestamp": 1000,
        "sessionID": "ses_err",
        "error": {
            "name": "UnknownError",
            "data": {"message": "Model not found: github-copilot/claude-sonnet-4.6."},
        },
    }
    sub_patch, _ = _patch_subprocess([error_event], returncode=0)
    with sub_patch, _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request()
        with pytest.raises(OpenCodeError, match="Model not found"):
            _ = [e async for e in provider.run(request)]


@pytest.mark.asyncio
async def test_opencode_watchdog_kills_silent_subprocess(monkeypatch):
    """If opencode stops producing output, the watchdog kills it and raises.

    Models a wedged upstream API call: stdout returns nothing for longer than
    the configured idle timeout. The provider should `proc.kill()` and raise
    `OpenCodeError(TimeoutError(...))` instead of hanging forever.
    """
    monkeypatch.setenv("NORN_OPENCODE_IDLE_TIMEOUT", "0.05")

    class _SilentStdout:
        async def readline(self):
            # Sleep longer than the watchdog window without yielding a line.
            await asyncio.sleep(5)
            return b""

    class _SilentStderr:
        async def read(self):
            return b""

    class _SilentProc:
        returncode = 0
        stdout = _SilentStdout()
        stderr = _SilentStderr()
        killed = False

        async def wait(self):
            return None

        def kill(self):
            self.killed = True

    silent = _SilentProc()

    async def _fake_exec(*args, **kwargs):
        return silent

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec), _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request()
        with pytest.raises(OpenCodeError, match="produced no output"):
            _ = [e async for e in provider.run(request)]

    assert silent.killed, "watchdog must kill the wedged subprocess"


@pytest.mark.asyncio
async def test_opencode_watchdog_disabled_when_zero(monkeypatch):
    """Setting NORN_OPENCODE_IDLE_TIMEOUT=0 disables the watchdog."""
    monkeypatch.setenv("NORN_OPENCODE_IDLE_TIMEOUT", "0")
    events = [_step_finish_event()]
    sub_patch, _ = _patch_subprocess(events)
    with sub_patch, _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request()
        collected = [e async for e in provider.run(request)]
    assert any(e.usage is not None for e in collected)


@pytest.mark.asyncio
async def test_opencode_not_installed_raises():
    """Missing opencode binary raises OpenCodeError."""
    with _patch_which(found=False):
        provider = OpenCodeProvider()
        request = _opencode_request()
        with pytest.raises(OpenCodeError, match="not installed"):
            _ = [e async for e in provider.run(request)]


# -- System prompt is prepended to prompt --


@pytest.mark.asyncio
async def test_opencode_system_prompt_prepended():
    """System prompt is prepended to the user prompt."""
    events = [_step_finish_event()]
    captured_cmd: list = []

    async def _capture_exec(*args, **kwargs):
        captured_cmd.extend(args)
        return _FakeProcess(events)

    with patch("asyncio.create_subprocess_exec", side_effect=_capture_exec), _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request(
            prompt="Do the task",
            system_prompt="You are a code reviewer.",
        )
        _ = [e async for e in provider.run(request)]

    # The last positional arg is the prompt
    prompt_arg = captured_cmd[-1]
    assert "You are a code reviewer." in prompt_arg
    assert "Do the task" in prompt_arg
    assert prompt_arg.index("You are a code reviewer.") < prompt_arg.index("Do the task")


# -- env is passed to subprocess --


@pytest.mark.asyncio
async def test_opencode_env_passed_to_subprocess():
    """Request env is merged into subprocess environment."""
    events = [_step_finish_event()]
    captured_kwargs: dict = {}

    async def _capture_exec(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return _FakeProcess(events)

    with patch("asyncio.create_subprocess_exec", side_effect=_capture_exec), _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request(env={"MY_VAR": "my_value"})
        _ = [e async for e in provider.run(request)]

    proc_env = captured_kwargs.get("env", {})
    assert proc_env.get("MY_VAR") == "my_value"


# -- OpenCode registered in registry --


def test_opencode_registered_in_registry():
    """OpenCodeProvider is registered and retrievable."""
    provider = agent_registry.get_provider("opencode")
    assert provider is not None
    assert provider.name == "opencode"


# -- Error finish reason marks is_error --


@pytest.mark.asyncio
async def test_opencode_error_finish_marks_is_error():
    """step_finish with reason 'error' sets is_error=True in usage."""
    events = [
        _step_start_event(),
        _step_finish_event(reason="error"),
    ]
    sub_patch, _ = _patch_subprocess(events)
    with sub_patch, _patch_which():
        provider = OpenCodeProvider()
        request = _opencode_request()
        collected = [e async for e in provider.run(request)]

    usage_events = [e for e in collected if e.usage is not None]
    assert usage_events[0].usage.is_error is True


# ---------------------------------------------------------------------------
# Non-portable feature validation (step-10) – Generate.run() early reject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hooks_rejected_for_non_claude_provider():
    """SDK hooks are rejected for non-claude-code providers with an informative message."""
    hooks = {"PreToolUse": [{"matcher": {}, "hook": {"type": "command", "command": "exit 0"}}]}
    gen = Generate(prompt="do stuff", hooks=hooks)
    ctx = PipelineContext()
    ctx.agent_provider = "opencode"
    result = await gen.run(ctx)
    assert not result.success
    assert result.error is not None
    assert "hooks" in result.error.lower()
    assert "opencode" in result.error
    assert "claude-code" in result.error


@pytest.mark.asyncio
async def test_stage_profile_blocked_patterns_rejected_for_non_claude_provider():
    """Blocked patterns compiled to hooks via stage-level profile are rejected for non-claude-code."""
    from norn.profiles import SessionProfile

    profile = SessionProfile(
        name="coding",
        permission_mode="bypassPermissions",
        blocked_patterns=["rm -rf /"],
    )
    gen = Generate(prompt="do stuff", profile=profile)
    ctx = PipelineContext()
    ctx.agent_provider = "opencode"
    result = await gen.run(ctx)
    assert not result.success
    assert result.error is not None
    assert "hooks" in result.error.lower()
    assert "opencode" in result.error


@pytest.mark.asyncio
async def test_pipeline_profile_blocked_patterns_rejected_for_non_claude_provider():
    """Blocked patterns from pipeline-level profile compiled to hooks are rejected for non-claude-code."""
    from norn.profiles import CODING

    gen = Generate(prompt="do stuff")
    ctx = PipelineContext()
    ctx.agent_provider = "opencode"
    ctx.pipeline_profile = CODING
    result = await gen.run(ctx)
    assert not result.success
    assert result.error is not None
    assert "hooks" in result.error.lower()
    assert "opencode" in result.error


@pytest.mark.asyncio
async def test_mcp_tools_rejected_for_non_claude_provider_in_generate():
    """mcp_tools kwarg to Generate.run() is rejected for non-claude-code providers."""
    from unittest.mock import MagicMock

    gen = Generate(prompt="do stuff")
    ctx = PipelineContext()
    ctx.agent_provider = "opencode"
    result = await gen.run(ctx, mcp_tools=[MagicMock()])
    assert not result.success
    assert result.error is not None
    assert "mcp_tools" in result.error.lower()
    assert "opencode" in result.error
    assert "claude-code" in result.error


@pytest.mark.asyncio
async def test_setting_sources_non_project_rejected_for_non_claude_provider():
    """Non-project setting_sources return StageResult failure for non-claude-code providers."""
    gen = Generate(prompt="do stuff", setting_sources=["user"])
    ctx = PipelineContext()
    ctx.agent_provider = "opencode"
    result = await gen.run(ctx)
    assert not result.success
    assert result.error is not None
    assert "'user'" in result.error
    assert "opencode" in result.error
    assert "claude-code" in result.error


@pytest.mark.asyncio
async def test_hooks_allowed_for_claude_code():
    """SDK hooks pass through for claude-code provider without early rejection."""
    hooks = {"PreToolUse": [{"matcher": {}, "hook": {"type": "command", "command": "exit 0"}}]}
    gen = Generate(prompt="do stuff", hooks=hooks)
    ctx = PipelineContext()
    ctx.agent_provider = "claude-code"

    class _FakeClaude:
        name = "claude-code"
        last_request = None

        async def run(self, request):
            _FakeClaude.last_request = request
            return
            yield

    original = agent_registry._registry.get("claude-code")
    agent_registry.register(_FakeClaude())
    try:
        result = await gen.run(ctx)
        # Validation must not have fired — the fake provider was reached
        assert _FakeClaude.last_request is not None
        assert _FakeClaude.last_request.hooks is hooks
    finally:
        if original:
            agent_registry.register(original)
        else:
            agent_registry._registry.pop("claude-code", None)


@pytest.mark.asyncio
async def test_mcp_tools_allowed_for_claude_code():
    """mcp_tools pass through for claude-code provider without early rejection."""
    from unittest.mock import MagicMock

    fake_tool = MagicMock()
    gen = Generate(prompt="do stuff")
    ctx = PipelineContext()
    ctx.agent_provider = "claude-code"

    class _FakeClaude:
        name = "claude-code"
        last_request = None

        async def run(self, request):
            _FakeClaude.last_request = request
            return
            yield

    original = agent_registry._registry.get("claude-code")
    agent_registry.register(_FakeClaude())
    try:
        result = await gen.run(ctx, mcp_tools=[fake_tool])
        assert _FakeClaude.last_request is not None
        assert _FakeClaude.last_request.mcp_tools == [fake_tool]
    finally:
        if original:
            agent_registry.register(original)
        else:
            agent_registry._registry.pop("claude-code", None)
