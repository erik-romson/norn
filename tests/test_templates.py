from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from norn.models import PipelineContext, StageResult
from norn.stages.generate import Generate
from norn.templates import PromptTemplate, load_template


# ---------------------------------------------------------------------------
# PromptTemplate dataclass
# ---------------------------------------------------------------------------


def test_prompt_template_required_fields():
    t = PromptTemplate(name="greet", template="Hello {input}")
    assert t.name == "greet"
    assert t.template == "Hello {input}"
    assert t.system_prompt is None
    assert t.output_format is None


def test_prompt_template_all_fields():
    fmt = {"type": "object", "properties": {"result": {"type": "string"}}}
    t = PromptTemplate(
        name="review",
        template="Review: {input}",
        system_prompt="You are a reviewer.",
        output_format=fmt,
    )
    assert t.system_prompt == "You are a reviewer."
    assert t.output_format == fmt


# ---------------------------------------------------------------------------
# load_template
# ---------------------------------------------------------------------------


def test_load_template_missing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="templates/missing.py"):
        load_template("missing")


def test_load_template_no_matching_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "review.py").write_text(
        textwrap.dedent("""\
            from norn.templates import PromptTemplate
            wrong = PromptTemplate(name="other", template="x")
        """)
    )
    with pytest.raises(ValueError, match="No PromptTemplate with name 'review'"):
        load_template("review")


def test_load_template_success(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "code_review.py").write_text(
        textwrap.dedent("""\
            from norn.templates import PromptTemplate
            review = PromptTemplate(
                name="code_review",
                template="Review this code:\\n{input}",
                system_prompt="You are a senior code reviewer.",
                output_format={"type": "object"},
            )
        """)
    )
    t = load_template("code_review")
    assert t.name == "code_review"
    assert "{input}" in t.template
    assert t.system_prompt == "You are a senior code reviewer."
    assert t.output_format == {"type": "object"}


# ---------------------------------------------------------------------------
# Generate with template
# ---------------------------------------------------------------------------


def test_generate_requires_prompt_or_template():
    with pytest.raises(ValueError, match="prompt.*template"):
        Generate()


def test_generate_stores_template_and_input():
    gen = Generate(template="code_review", input="{spec.output}")
    assert gen.template == "code_review"
    assert gen.input == "{spec.output}"
    assert gen.prompt is None


def test_generate_resolve_input_placeholder():
    ctx = PipelineContext()
    ctx.results["spec"] = StageResult(name="spec", success=True, output="def foo(): pass")
    gen = Generate(template="review", input="{spec.output}")
    resolved = gen._resolve(gen.input, ctx)
    assert resolved == "def foo(): pass"


@pytest.mark.asyncio
async def test_template_system_prompt_passed_to_options(tmp_path, monkeypatch):
    """template.system_prompt is forwarded to ClaudeAgentOptions.system_prompt."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "review.py").write_text(
        textwrap.dedent("""\
            from norn.templates import PromptTemplate
            review = PromptTemplate(
                name="review",
                template="Review: {input}",
                system_prompt="You are a reviewer.",
            )
        """)
    )

    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    gen = Generate(template="review", input="some code")

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.UserMessage = type("UserMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls
        sdk_mod.HookMatcher = lambda *, hooks: object()

        await gen.run(PipelineContext())

    assert captured_kwargs.get("system_prompt") == "You are a reviewer."


@pytest.mark.asyncio
async def test_template_output_format_passed_to_options(tmp_path, monkeypatch):
    """template.output_format is forwarded to ClaudeAgentOptions.output_format."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "templates").mkdir()
    fmt = {"type": "object", "required": ["issues"]}
    (tmp_path / "templates" / "review.py").write_text(
        textwrap.dedent(f"""\
            from norn.templates import PromptTemplate
            review = PromptTemplate(
                name="review",
                template="Review: {{input}}",
                output_format={fmt!r},
            )
        """)
    )

    captured_kwargs: dict = {}

    async def fake_query(*, prompt, options):
        return
        yield

    def fake_options_cls(**kw):
        captured_kwargs.update(kw)
        return kw

    gen = Generate(template="review", input="code here")

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.UserMessage = type("UserMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls
        sdk_mod.HookMatcher = lambda *, hooks: object()

        await gen.run(PipelineContext())

    assert captured_kwargs.get("output_format") == fmt


@pytest.mark.asyncio
async def test_template_prompt_built_from_input(tmp_path, monkeypatch):
    """Generate builds the prompt by substituting input into template.template."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "review.py").write_text(
        textwrap.dedent("""\
            from norn.templates import PromptTemplate
            review = PromptTemplate(name="review", template="Review this:\\n{input}")
        """)
    )

    captured_prompts: list[str] = []

    async def fake_query(*, prompt, options):
        captured_prompts.append(prompt)
        return
        yield

    def fake_options_cls(**kw):
        return kw

    gen = Generate(template="review", input="my code")

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.UserMessage = type("UserMessage", (), {})
        sdk_mod.ResultMessage = type("ResultMessage", (), {})
        sdk_mod.ClaudeAgentOptions = fake_options_cls
        sdk_mod.HookMatcher = lambda *, hooks: object()

        await gen.run(PipelineContext())

    assert captured_prompts, "query was never called"
    assert captured_prompts[0] == "Review this:\nmy code"


@pytest.mark.asyncio
async def test_template_structured_output_used_as_stage_output(tmp_path, monkeypatch):
    """When ResultMessage.structured_output is set, it becomes the stage output."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "review.py").write_text(
        textwrap.dedent("""\
            from norn.templates import PromptTemplate
            review = PromptTemplate(
                name="review",
                template="Review: {input}",
                output_format={"type": "object"},
            )
        """)
    )

    structured = {"issues": [], "summary": "looks good", "score": 9}

    async def fake_query(*, prompt, options):
        msg = MagicMock()
        msg.session_id = "s1"
        msg.total_cost_usd = 0.001
        msg.duration_ms = 100
        msg.duration_api_ms = 90
        msg.num_turns = 1
        msg.is_error = False
        msg.usage = {}
        msg.structured_output = structured
        yield type("ResultMessage", (), {})().__class__
        # yield actual ResultMessage-like object
        return

    # Rebuild fake_query to yield a proper ResultMessage-like
    ResultMessage = type("ResultMessage", (), {})

    async def fake_query2(*, prompt, options):
        msg = MagicMock(spec=ResultMessage)
        msg.session_id = "s1"
        msg.total_cost_usd = 0.001
        msg.duration_ms = 100
        msg.duration_api_ms = 90
        msg.num_turns = 1
        msg.is_error = False
        msg.usage = {}
        msg.structured_output = structured
        yield msg

    gen = Generate(template="review", input="some code")

    with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
        import claude_agent_sdk as sdk_mod

        sdk_mod.query = fake_query2
        sdk_mod.AssistantMessage = type("AssistantMessage", (), {})
        sdk_mod.UserMessage = type("UserMessage", (), {})
        sdk_mod.ResultMessage = ResultMessage
        sdk_mod.ClaudeAgentOptions = lambda **kw: kw
        sdk_mod.HookMatcher = lambda *, hooks: object()

        result = await gen.run(PipelineContext())

    assert result.success is True
    assert result.output == structured
