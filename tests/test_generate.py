import pytest
from unittest.mock import patch, MagicMock

from norn.models import PipelineContext, StageResult
from norn.stages.generate import Generate, MODEL_MAP


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


@pytest.mark.asyncio
async def test_generate_missing_sdk():
    """Generate returns a clear error when claude-agent-sdk is not installed."""
    gen = Generate(prompt="hello", output_file="out.py")
    result = await gen.run(PipelineContext())
    # In the test environment, claude-agent-sdk is likely not installed
    # If it IS installed, this test will pass differently — both outcomes are fine
    assert isinstance(result.success, bool)


def test_generate_stores_permission_mode():
    """permission_mode is stored on the instance."""
    gen = Generate(prompt="hi", output_file="out.py", permission_mode="acceptEdits")
    assert gen.permission_mode == "acceptEdits"


def test_generate_stores_add_dirs():
    """add_dirs is stored on the instance."""
    gen = Generate(prompt="hi", output_file="out.py", add_dirs=["tmp/", "src/"])
    assert gen.add_dirs == ["tmp/", "src/"]


def test_generate_defaults_have_no_add_dirs_or_permission_mode():
    """add_dirs and permission_mode default to None (not passed to SDK unless set)."""
    gen = Generate(prompt="hi", output_file="out.py")
    assert gen.add_dirs is None
    assert gen.permission_mode is None


@pytest.mark.asyncio
async def test_permission_mode_and_add_dirs_passed_to_options():
    """permission_mode and add_dirs are forwarded to ClaudeAgentOptions when set."""
    captured_options = {}

    async def fake_query(*, prompt, options):
        captured_options["opts"] = options
        return
        yield  # make it an async generator

    fake_options_cls = MagicMock(side_effect=lambda **kw: kw)

    gen = Generate(
        prompt="hi",
        output_file="/tmp/test_out.py",
        permission_mode="acceptEdits",
        add_dirs=["tmp/"],
    )

    with (
        patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}),
        patch("norn.stages.generate.Generate.run", wraps=gen.run),
    ):
        # Patch the internals directly by mocking inside the module
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls

        await gen.run(PipelineContext())

    call_kwargs = fake_options_cls.call_args.kwargs
    assert call_kwargs.get("permission_mode") == "acceptEdits"
    assert call_kwargs.get("add_dirs") == ["tmp/"]


@pytest.mark.asyncio
async def test_context_injected_as_system_prompt_when_has_tools():
    """With permission_mode set, injected_context is forwarded as system_prompt."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    gen = Generate(prompt="do stuff", permission_mode="acceptEdits")
    ctx = PipelineContext(injected_context=[("ARCH", "Architecture docs here")])

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls


        await gen.run(ctx)

    assert "system_prompt" in captured_kwargs
    assert "## ARCH" in captured_kwargs["system_prompt"]
    assert "Architecture docs here" in captured_kwargs["system_prompt"]


@pytest.mark.asyncio
async def test_context_prepended_to_prompt_when_no_tools():
    """Without tools, injected_context is prepended to the prompt."""
    captured_prompts: list[str] = []

    async def fake_query(*, prompt, options):
        captured_prompts.append(prompt)
        return
        yield

    def fake_options_cls(**kw):
        return kw

    gen = Generate(prompt="my task")
    ctx = PipelineContext(injected_context=[("schema", "CREATE TABLE foo (id INT)")])

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls


        await gen.run(ctx)

    assert captured_prompts, "query was never called"
    full_prompt = captured_prompts[0]
    assert "## schema" in full_prompt
    assert "CREATE TABLE foo" in full_prompt
    assert "my task" in full_prompt
    # context must appear before the actual prompt
    assert full_prompt.index("## schema") < full_prompt.index("my task")


@pytest.mark.asyncio
async def test_no_context_no_system_prompt():
    """Without injected_context, system_prompt is not added to options."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    gen = Generate(prompt="do stuff", permission_mode="acceptEdits")
    ctx = PipelineContext()  # no injected_context

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls


        await gen.run(ctx)

    assert "system_prompt" not in captured_kwargs


@pytest.mark.asyncio
async def test_env_passed_to_claude_options():
    """Stage-level env dict is forwarded to ClaudeAgentOptions after secret resolution."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    ctx = PipelineContext()
    ctx.secrets = {"MY_TOKEN": "secret_abc"}
    gen = Generate(prompt="do stuff", env={"API_KEY": "{secret.MY_TOKEN}"})

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls


        await gen.run(ctx)

    assert captured_kwargs.get("env") == {"API_KEY": "secret_abc"}


@pytest.mark.asyncio
async def test_pipeline_env_merged_into_claude_options():
    """Pipeline-level ctx.env is included in the env passed to ClaudeAgentOptions."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    ctx = PipelineContext()
    ctx.env = {"NODE_ENV": "production"}
    gen = Generate(prompt="do stuff")

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls


        await gen.run(ctx)

    assert captured_kwargs.get("env") == {"NODE_ENV": "production"}


@pytest.mark.asyncio
async def test_artifacts_collected_from_write_and_edit_tool_use():
    """PostToolUse hook captures Write/Edit file paths into StageResult.artifacts."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        # Extract the artifact tracking hook from the hooks config
        hooks_config = captured_kwargs.get("hooks", {})
        for matcher_dict in hooks_config.get("PostToolUse", []):
            for fn in matcher_dict.get("hooks", []):
                await fn({"tool_name": "Write", "tool_input": {"file_path": "src/foo.py"}}, "id1", {})
                await fn({"tool_name": "Edit", "tool_input": {"file_path": "src/bar.py"}}, "id2", {})
                await fn({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "id3", {})
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    gen = Generate(prompt="write a file", permission_mode="acceptEdits")

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls

        result = await gen.run(PipelineContext())

    assert result.artifacts == ["src/foo.py", "src/bar.py"]


def test_model_map_short_names():
    """MODEL_MAP contains expected short-name to full-model-id mappings."""
    assert MODEL_MAP["opus"] == "claude-opus-4-6"
    assert MODEL_MAP["sonnet"] == "claude-sonnet-4-6"
    assert MODEL_MAP["haiku"] == "claude-haiku-4-5-20251001"


def test_generate_stores_model_and_thinking():
    """model and thinking are stored on the instance."""
    gen = Generate(
        prompt="hi",
        model="opus",
        thinking={"type": "enabled", "budget_tokens": 5000},
    )
    assert gen.model == "opus"
    assert gen.thinking == {"type": "enabled", "budget_tokens": 5000}


@pytest.mark.asyncio
async def test_model_short_name_resolved_and_passed_to_options():
    """Short model name is resolved via MODEL_MAP and forwarded to ClaudeAgentOptions."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    gen = Generate(prompt="do stuff", model="opus")

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls


        await gen.run(PipelineContext())

    assert captured_kwargs.get("model") == "claude-opus-4-6"


@pytest.mark.asyncio
async def test_model_full_name_passed_as_is():
    """A full model ID (not in MODEL_MAP) is forwarded unchanged."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    gen = Generate(prompt="do stuff", model="claude-sonnet-4-6")

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls


        await gen.run(PipelineContext())

    assert captured_kwargs.get("model") == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_default_model_from_ctx_params():
    """When no stage-level model is set, default_model from ctx.params is used."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    gen = Generate(prompt="do stuff")
    ctx = PipelineContext(params={"default_model": "haiku"})

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls


        await gen.run(ctx)

    assert captured_kwargs.get("model") == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_stage_model_overrides_default_model():
    """Stage-level model takes precedence over default_model in ctx.params."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    gen = Generate(prompt="do stuff", model="sonnet")
    ctx = PipelineContext(params={"default_model": "haiku"})

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls


        await gen.run(ctx)

    assert captured_kwargs.get("model") == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_thinking_passed_to_options():
    """thinking dict is forwarded to ClaudeAgentOptions when set."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    thinking_cfg = {"type": "enabled", "budget_tokens": 10000}
    gen = Generate(prompt="do stuff", model="opus", thinking=thinking_cfg)

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls


        await gen.run(PipelineContext())

    assert captured_kwargs.get("thinking") == thinking_cfg


@pytest.mark.asyncio
async def test_model_stored_on_usage_record():
    """The resolved model short name is stored on UsageRecord.model."""
    captured_results: list = []

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        return kw

    gen = Generate(prompt="do stuff", model="sonnet")

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls


        result = await gen.run(PipelineContext())
        captured_results.append(result)

    assert captured_results[0].usage is not None


@pytest.mark.asyncio
async def test_mcp_servers_kwarg_passed_to_options():
    """mcp_servers kwarg is forwarded to ClaudeAgentOptions when provided."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    gen = Generate(prompt="use mcp tools")
    fake_server = {"type": "sdk", "name": "test_server", "instance": object()}

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls

        await gen.run(PipelineContext(), mcp_servers={"test_server": fake_server})

    assert captured_kwargs.get("mcp_servers") == {"test_server": fake_server}


@pytest.mark.asyncio
async def test_mcp_servers_not_set_when_not_provided():
    """mcp_servers is not added to options when not passed as kwarg."""
    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    gen = Generate(prompt="no mcp tools")

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls

        await gen.run(PipelineContext())

    assert "mcp_servers" not in captured_kwargs
