from norn.models import PipelineContext, StageResult


def test_stage_result_defaults():
    r = StageResult(name="s1", success=True)
    assert r.output is None
    assert r.error is None
    assert r.artifacts == []


def test_pipeline_context_get():
    ctx = PipelineContext()
    ctx.results["read"] = StageResult(name="read", success=True, output="file content")
    assert ctx.get("read") == "file content"


def test_pipeline_context_stores_multiple():
    ctx = PipelineContext()
    ctx.results["a"] = StageResult(name="a", success=True, output=1)
    ctx.results["b"] = StageResult(name="b", success=True, output=2)
    assert ctx.get("a") == 1
    assert ctx.get("b") == 2
