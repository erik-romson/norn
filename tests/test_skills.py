from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from norn.models import PipelineContext
from norn.skills import Skill, resolve_skill_content


# ---------------------------------------------------------------------------
# resolve_skill_content
# ---------------------------------------------------------------------------


def test_resolve_inline_skill():
    skill = Skill(name="my-skill", content="Always be concise.")
    assert resolve_skill_content(skill) == "Always be concise."


def test_resolve_named_skill_from_local_skills_dir(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "review-pr.md").write_text("Review code carefully.")
    monkeypatch.chdir(tmp_path)

    content = resolve_skill_content("review-pr")
    assert content == "Review code carefully."


def test_resolve_qualified_skill(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills" / "my-pkg"
    skills_dir.mkdir(parents=True)
    (skills_dir / "tool.md").write_text("Qualified skill content.")
    monkeypatch.chdir(tmp_path)

    content = resolve_skill_content("my-pkg:tool")
    assert content == "Qualified skill content."


def test_resolve_skill_not_found():
    with pytest.raises(FileNotFoundError, match="nonexistent-skill"):
        resolve_skill_content("nonexistent-skill")


def test_resolve_skill_project_level(tmp_path, monkeypatch):
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "commit.md").write_text("Write good commit messages.")
    monkeypatch.chdir(tmp_path)

    content = resolve_skill_content("commit")
    assert content == "Write good commit messages."


# ---------------------------------------------------------------------------
# Generate + skills
# ---------------------------------------------------------------------------


def _make_fake_sdk(captured: dict[str, Any]) -> MagicMock:
    """Build a minimal fake claude_agent_sdk that records ClaudeAgentOptions kwargs."""

    class FakeOptions:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    class FakeResultMessage:
        session_id = "test-session"
        total_cost_usd = 0.0
        duration_ms = 0
        duration_api_ms = 0
        num_turns = 1
        is_error = False
        usage: dict = {}
        structured_output = None

    async def fake_query(prompt: str, options: Any):  # noqa: ANN001
        yield FakeResultMessage()

    sdk = MagicMock()
    sdk.query = fake_query
    sdk.ClaudeAgentOptions = FakeOptions
    sdk.AssistantMessage = type("AssistantMessage", (), {})
    sdk.ResultMessage = FakeResultMessage
    sdk.HookMatcher = MagicMock(return_value=MagicMock())
    return sdk


@pytest.mark.asyncio
async def test_generate_stage_skills_injected_into_system_prompt(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _make_fake_sdk(captured))

    from norn.stages.generate import Generate

    stage = Generate(
        prompt="Do something.",
        skills=[Skill(name="concise", content="Be very concise.")],
    )
    result = await stage.run(PipelineContext())

    assert result.success
    assert "Be very concise." in captured.get("system_prompt", "")


@pytest.mark.asyncio
async def test_generate_multiple_skills_all_injected(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _make_fake_sdk(captured))

    from norn.stages.generate import Generate

    stage = Generate(
        prompt="Test.",
        skills=[
            Skill(name="s1", content="Skill one content."),
            Skill(name="s2", content="Skill two content."),
        ],
    )
    result = await stage.run(PipelineContext())

    assert result.success
    system_prompt = captured.get("system_prompt", "")
    assert "Skill one content." in system_prompt
    assert "Skill two content." in system_prompt


@pytest.mark.asyncio
async def test_generate_pipeline_skills_merged_with_stage_skills(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _make_fake_sdk(captured))

    from norn.stages.generate import Generate

    stage = Generate(
        prompt="Test.",
        skills=[Skill(name="stage-skill", content="Stage skill.")],
    )
    ctx = PipelineContext()
    ctx.pipeline_skills = [Skill(name="pipeline-skill", content="Pipeline skill.")]

    result = await stage.run(ctx)

    assert result.success
    system_prompt = captured.get("system_prompt", "")
    assert "Pipeline skill." in system_prompt
    assert "Stage skill." in system_prompt


@pytest.mark.asyncio
async def test_generate_no_system_prompt_when_no_skills(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _make_fake_sdk(captured))

    from norn.stages.generate import Generate

    stage = Generate(prompt="Test.")
    result = await stage.run(PipelineContext())

    assert result.success
    # No system_prompt key or empty when no skills/template/context
    assert "system_prompt" not in captured
