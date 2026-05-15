import pytest

from norn.dsl import ClearContext, ContextSpec, Loop, OnFailure, Pipeline, Stage, ask_user, fail
from norn.stages.read_file import ReadFile
from norn.stages.run_command import RunCommand


def test_pipeline_chaining():
    p = (
        Pipeline("test")
        .stage("s1", ReadFile(path="x.txt"))
        .clear_context()
        .stage("s2", RunCommand(cmd="echo hi"))
    )
    assert p.name == "test"
    assert len(p.items) == 3
    assert isinstance(p.items[0], Stage)
    assert isinstance(p.items[1], ClearContext)
    assert isinstance(p.items[2], Stage)


def test_pipeline_loop():
    p = Pipeline("test").loop(
        "retry",
        max_retries=5,
        on_exhaust=ask_user,
        stages=[Stage("s1", RunCommand(cmd="true"))],
    )
    loop = p.items[0]
    assert isinstance(loop, Loop)
    assert loop.name == "retry"
    assert loop.max_retries == 5
    assert loop.on_exhaust == OnFailure.ASK_USER
    assert len(loop.stages) == 1


def test_fail_and_ask_user_sentinels():
    assert fail == OnFailure.FAIL
    assert ask_user == OnFailure.ASK_USER


def test_stage_default_on_failure():
    s = Stage("s1", ReadFile(path="x.txt"))
    assert s.on_failure == OnFailure.FAIL


def test_pipeline_context_file():
    p = Pipeline("test").context("ARCHITECTURE.md")
    assert len(p.contexts) == 1
    spec = p.contexts[0]
    assert spec.source == "ARCHITECTURE.md"
    assert spec.label == "ARCHITECTURE.md"
    assert spec.kind == "file"


def test_pipeline_context_file_custom_label():
    p = Pipeline("test").context("src/schema.sql", label="db_schema")
    spec = p.contexts[0]
    assert spec.label == "db_schema"
    assert spec.source == "src/schema.sql"


def test_pipeline_context_cmd():
    p = Pipeline("test").context_cmd("git log -10")
    spec = p.contexts[0]
    assert spec.source == "git log -10"
    assert spec.label == "git log -10"
    assert spec.kind == "cmd"


def test_pipeline_context_cmd_custom_label():
    p = Pipeline("test").context_cmd("git log -10", label="recent commits")
    spec = p.contexts[0]
    assert spec.label == "recent commits"


def test_pipeline_context_chaining():
    p = (
        Pipeline("test")
        .context("ARCHITECTURE.md")
        .context_cmd("git log -5", label="history")
        .stage("s1", ReadFile(path="x.txt"))
    )
    assert len(p.contexts) == 2
    assert len(p.items) == 1


# ---------------------------------------------------------------------------
# derive / skip / replace / in_loop
# ---------------------------------------------------------------------------


def _base_pipeline() -> Pipeline:
    """Build a reusable base pipeline for composition tests."""
    return (
        Pipeline("base")
        .stage("setup", ReadFile(path="setup.txt"))
        .loop(
            "build",
            stages=[
                Stage("compile", RunCommand(cmd="make")),
                Stage("test", RunCommand(cmd="pytest")),
                Stage("lint", RunCommand(cmd="ruff check")),
            ],
        )
        .stage("ship", RunCommand(cmd="make deploy"))
    )


def test_derive_copies_items():
    base = _base_pipeline()
    derived = base.derive("child")
    assert derived.name == "child"
    assert len(derived.items) == len(base.items)


def test_derive_mutation_does_not_affect_parent():
    base = _base_pipeline()
    derived = base.derive("child").skip("ship")
    # derived has one fewer item
    assert len(derived.items) == len(base.items) - 1
    # parent is untouched
    assert any(
        isinstance(item, Stage) and item.name == "ship" for item in base.items
    )


def test_top_level_skip_removes_stage():
    p = _base_pipeline().derive("d").skip("ship")
    names = [item.name for item in p.items if isinstance(item, (Stage, Loop))]
    assert "ship" not in names


def test_top_level_skip_removes_loop():
    p = _base_pipeline().derive("d").skip("build")
    names = [item.name for item in p.items if isinstance(item, (Stage, Loop))]
    assert "build" not in names


def test_top_level_replace_swaps_impl():
    new_impl = RunCommand(cmd="make fast")
    p = _base_pipeline().derive("d").replace("ship", new_impl)
    ship = next(item for item in p.items if isinstance(item, Stage) and item.name == "ship")
    assert ship.impl is new_impl


def test_top_level_replace_unknown_raises():
    with pytest.raises(KeyError, match="ghost"):
        _base_pipeline().derive("d").replace("ghost", RunCommand(cmd="x"))


def test_in_loop_skip():
    p = _base_pipeline().derive("d").in_loop("build").skip("lint").end_loop()
    loop = next(item for item in p.items if isinstance(item, Loop) and item.name == "build")
    stage_names = [s.name for s in loop.stages]
    assert "lint" not in stage_names
    assert "compile" in stage_names


def test_in_loop_replace():
    new_impl = RunCommand(cmd="pytest -x")
    p = _base_pipeline().derive("d").in_loop("build").replace("test", new_impl).end_loop()
    loop = next(item for item in p.items if isinstance(item, Loop) and item.name == "build")
    test_stage = next(s for s in loop.stages if s.name == "test")
    assert test_stage.impl is new_impl


def test_in_loop_insert_after():
    extra = Stage("coverage", RunCommand(cmd="coverage run"))
    p = _base_pipeline().derive("d").in_loop("build").insert_after("test", extra).end_loop()
    loop = next(item for item in p.items if isinstance(item, Loop) and item.name == "build")
    names = [s.name for s in loop.stages]
    assert names.index("coverage") == names.index("test") + 1


def test_in_loop_insert_before():
    extra = Stage("format", RunCommand(cmd="ruff format"))
    p = _base_pipeline().derive("d").in_loop("build").insert_before("compile", extra).end_loop()
    loop = next(item for item in p.items if isinstance(item, Loop) and item.name == "build")
    names = [s.name for s in loop.stages]
    assert names.index("format") == names.index("compile") - 1


def test_in_loop_skip_unknown_stage_is_noop():
    p = _base_pipeline().derive("d")
    loop = next(item for item in p.items if isinstance(item, Loop) and item.name == "build")
    count_before = len(loop.stages)
    p.in_loop("build").skip("ghost").end_loop()
    assert len(loop.stages) == count_before


def test_in_loop_unknown_loop_raises():
    with pytest.raises(KeyError, match="phantom"):
        _base_pipeline().derive("d").in_loop("phantom").end_loop()


def test_find_loop_dot_notation():
    """Dot-notation finds a loop nested inside another loop's stages."""
    inner_loop = Loop(
        name="inner",
        stages=[Stage("s1", RunCommand(cmd="echo hi"))],
    )
    outer_loop = Loop(name="outer", stages=[inner_loop])  # type: ignore[arg-type]
    p = Pipeline("nested")
    p.items.append(outer_loop)

    found = p._find_loop("outer.inner")
    assert found is inner_loop


def test_end_loop_returns_pipeline():
    p = _base_pipeline().derive("d")
    result = p.in_loop("build").end_loop()
    assert result is p


# ---------------------------------------------------------------------------
# agent_provider DSL
# ---------------------------------------------------------------------------


def test_agent_provider_default_is_none():
    """agent_provider_name defaults to None (resolved later to claude-code)."""
    p = Pipeline("test")
    assert p.agent_provider_name is None


def test_agent_provider_stores_name():
    """Pipeline.agent_provider() stores the provider name and returns self."""
    p = Pipeline("test").agent_provider("opencode")
    assert p.agent_provider_name == "opencode"


def test_agent_provider_returns_pipeline_for_chaining():
    """Pipeline.agent_provider() returns the pipeline instance for chaining."""
    p = Pipeline("test")
    result = p.agent_provider("opencode")
    assert result is p


def test_derive_preserves_agent_provider():
    """derive() copies agent_provider_name to the derived pipeline."""
    base = Pipeline("base").agent_provider("opencode")
    derived = base.derive("child")
    assert derived.agent_provider_name == "opencode"


def test_derive_preserves_none_agent_provider():
    """derive() copies None agent_provider_name (default) to the derived pipeline."""
    base = Pipeline("base")
    derived = base.derive("child")
    assert derived.agent_provider_name is None


def test_derive_agent_provider_is_independent():
    """Changing agent_provider_name on derived pipeline does not affect parent."""
    base = Pipeline("base").agent_provider("opencode")
    derived = base.derive("child")
    derived.agent_provider("claude-code")
    assert base.agent_provider_name == "opencode"
    assert derived.agent_provider_name == "claude-code"
