from __future__ import annotations

from norn.diagram import to_mermaid
from norn.dsl import Include, Loop, Parallel, Pipeline, Stage, fail
from norn.stages.base import BaseStage
from norn.models import PipelineContext, StageResult


class _Stub(BaseStage):
    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        return StageResult(name="", success=True)


def test_single_stage():
    p = Pipeline("simple").stage("greet", _Stub())
    result = to_mermaid(p)
    assert "flowchart TD" in result
    assert "greet" in result


def test_sequential_stages_connected():
    p = (
        Pipeline("seq")
        .stage("a", _Stub())
        .stage("b", _Stub())
        .stage("c", _Stub())
    )
    result = to_mermaid(p)
    assert "s1 --> s2" in result
    assert "s2 --> s3" in result


def test_loop_subgraph_and_retry_edge():
    p = Pipeline("with_loop").loop(
        "build",
        max_retries=3,
        stages=[
            Stage("compile", _Stub()),
            Stage("test", _Stub()),
        ],
    )
    result = to_mermaid(p)
    assert "subgraph" in result
    assert "loop, max 3" in result
    assert "retry" in result


def test_clear_context_node():
    p = (
        Pipeline("cc")
        .stage("a", _Stub())
        .clear_context()
        .stage("b", _Stub())
    )
    result = to_mermaid(p)
    assert "clear context" in result


def test_parallel_subgraph():
    p = Pipeline("par").parallel(
        "fan_out",
        stages=[
            Stage("x", _Stub()),
            Stage("y", _Stub()),
        ],
    )
    result = to_mermaid(p)
    assert "parallel" in result
    assert "fan_out" in result


def test_include_node():
    p = Pipeline("inc")
    p.items.append(Include(path="sub/pipeline.py"))
    result = to_mermaid(p)
    assert "sub/pipeline.py" in result
    assert "[[" in result


def test_vanilla_change_pipeline():
    """Smoke test against a real bundled pipeline."""
    from norn.catalog import load_bundled_pipeline

    pipeline = load_bundled_pipeline("vanilla_change")
    result = to_mermaid(pipeline)
    assert "flowchart TD" in result
    assert "implement" in result
    assert "test_and_fix" in result


def test_to_markdown_bundled():
    """to_markdown includes title, description, inputs, and mermaid block."""
    from norn.catalog import get_pipeline_info, load_bundled_pipeline
    from norn.diagram import to_markdown

    info = get_pipeline_info("vanilla_change")
    pipeline = load_bundled_pipeline("vanilla_change")
    result = to_markdown(pipeline, str(info.path))
    assert result.startswith("# vanilla_change")
    assert "```mermaid" in result
    assert "ANTHROPIC_API_KEY" in result
    assert "**args**" in result


def test_to_mermaid_uses_build_graph(monkeypatch):
    """to_mermaid must drive output through build_graph (proves graph is consumed)."""
    import norn.diagram as diagram_mod
    from norn.graph import build_graph as real_build_graph

    calls: list = []

    def spy(*args, **kwargs):
        calls.append(args)
        return real_build_graph(*args, **kwargs)

    monkeypatch.setattr(diagram_mod, "build_graph", spy)

    p = Pipeline("probe").stage("only", _Stub())
    result = to_mermaid(p)

    assert len(calls) == 1, "build_graph should be called exactly once"
    assert calls[0][0] is p, "build_graph should receive the pipeline"
    assert "only" in result
