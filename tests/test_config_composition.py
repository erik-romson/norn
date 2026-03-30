from __future__ import annotations

import pytest

from norn.dsl import Loop, OnFailure, Pipeline, Stage, draft_pr, fail
from norn.stages.read_file import ReadFile
from norn.stages.run_command import RunCommand


def _make_pipeline() -> Pipeline:
    return (
        Pipeline("base")
        .stage("read", ReadFile(path="spec.txt"))
        .stage("build", RunCommand(cmd="make"))
        .loop(
            "retry_loop",
            max_retries=3,
            stages=[
                Stage("compile", RunCommand(cmd="make compile")),
                Stage("test", RunCommand(cmd="make test")),
            ],
        )
    )


# ---------------------------------------------------------------------------
# derive()
# ---------------------------------------------------------------------------


def test_derive_copies_name():
    p = _make_pipeline()
    d = p.derive("derived")
    assert d.name == "derived"
    assert p.name == "base"


def test_derive_copies_items():
    p = _make_pipeline()
    d = p.derive("derived")
    assert len(d.items) == len(p.items)


def test_derive_items_are_independent():
    p = _make_pipeline()
    d = p.derive("derived")
    # Mutating the derived pipeline should not affect the parent
    d.items.clear()
    assert len(p.items) == 3


def test_derive_deep_copies_loop_stages():
    p = _make_pipeline()
    d = p.derive("derived")
    # Mutate a loop stage name in the derived pipeline
    loop = next(item for item in d.items if isinstance(item, Loop))
    loop.stages[0].name = "mutated"
    # Parent should be unaffected
    parent_loop = next(item for item in p.items if isinstance(item, Loop))
    assert parent_loop.stages[0].name == "compile"


# ---------------------------------------------------------------------------
# skip()
# ---------------------------------------------------------------------------


def test_skip_removes_top_level_stage():
    p = _make_pipeline()
    p.skip("read")
    names = [item.name for item in p.items if isinstance(item, Stage)]
    assert "read" not in names


def test_skip_removes_loop():
    p = _make_pipeline()
    p.skip("retry_loop")
    assert not any(isinstance(item, Loop) for item in p.items)


def test_skip_nonexistent_is_noop():
    p = _make_pipeline()
    original_count = len(p.items)
    p.skip("does_not_exist")
    assert len(p.items) == original_count


# ---------------------------------------------------------------------------
# replace()
# ---------------------------------------------------------------------------


def test_replace_swaps_impl():
    p = _make_pipeline()
    new_impl = RunCommand(cmd="make new")
    p.replace("build", new_impl)
    stage = next(item for item in p.items if isinstance(item, Stage) and item.name == "build")
    assert stage.impl is new_impl


def test_replace_unknown_raises():
    p = _make_pipeline()
    with pytest.raises(KeyError, match="nonexistent"):
        p.replace("nonexistent", RunCommand(cmd="x"))


# ---------------------------------------------------------------------------
# in_loop()
# ---------------------------------------------------------------------------


def test_in_loop_skip():
    p = _make_pipeline()
    p.in_loop("retry_loop").skip("test")
    loop = next(item for item in p.items if isinstance(item, Loop))
    names = [s.name for s in loop.stages]
    assert "test" not in names
    assert "compile" in names


def test_in_loop_replace():
    p = _make_pipeline()
    new_impl = RunCommand(cmd="make fast-compile")
    p.in_loop("retry_loop").replace("compile", new_impl)
    loop = next(item for item in p.items if isinstance(item, Loop))
    stage = next(s for s in loop.stages if s.name == "compile")
    assert stage.impl is new_impl


def test_in_loop_insert_after():
    p = _make_pipeline()
    new_stage = Stage("lint", RunCommand(cmd="make lint"))
    p.in_loop("retry_loop").insert_after("compile", new_stage)
    loop = next(item for item in p.items if isinstance(item, Loop))
    names = [s.name for s in loop.stages]
    assert names == ["compile", "lint", "test"]


def test_in_loop_insert_before():
    p = _make_pipeline()
    new_stage = Stage("setup", RunCommand(cmd="make setup"))
    p.in_loop("retry_loop").insert_before("compile", new_stage)
    loop = next(item for item in p.items if isinstance(item, Loop))
    names = [s.name for s in loop.stages]
    assert names == ["setup", "compile", "test"]


def test_in_loop_end_loop_returns_pipeline():
    p = _make_pipeline()
    result = p.in_loop("retry_loop").end_loop()
    assert result is p


def test_in_loop_unknown_loop_raises():
    p = _make_pipeline()
    with pytest.raises(KeyError, match="no_such_loop"):
        p.in_loop("no_such_loop")


def test_in_loop_skip_unknown_stage_is_noop():
    p = _make_pipeline()
    loop_before = next(item for item in p.items if isinstance(item, Loop))
    count_before = len(loop_before.stages)
    p.in_loop("retry_loop").skip("nonexistent_stage")
    loop_after = next(item for item in p.items if isinstance(item, Loop))
    assert len(loop_after.stages) == count_before


def test_in_loop_replace_unknown_raises():
    p = _make_pipeline()
    with pytest.raises(KeyError, match="nonexistent"):
        p.in_loop("retry_loop").replace("nonexistent", RunCommand(cmd="x"))


def test_in_loop_insert_after_unknown_raises():
    p = _make_pipeline()
    with pytest.raises(KeyError, match="nonexistent"):
        p.in_loop("retry_loop").insert_after("nonexistent", Stage("x", RunCommand(cmd="x")))


def test_in_loop_insert_before_unknown_raises():
    p = _make_pipeline()
    with pytest.raises(KeyError, match="nonexistent"):
        p.in_loop("retry_loop").insert_before("nonexistent", Stage("x", RunCommand(cmd="x")))


# ---------------------------------------------------------------------------
# draft_pr sentinel
# ---------------------------------------------------------------------------


def test_draft_pr_sentinel():
    assert draft_pr == OnFailure.DRAFT_PR


# ---------------------------------------------------------------------------
# projects() and credentials() and session_profile()
# ---------------------------------------------------------------------------


def test_projects_adds_keys():
    p = Pipeline("p").projects("PROJ", "ACME")
    assert p.project_keys == ["PROJ", "ACME"]


def test_credentials_sets_provider():
    p = Pipeline("p").credentials(provider="vault", url="http://vault:8200")
    assert p._credential_provider == "vault"
    assert p._credential_kwargs["url"] == "http://vault:8200"


def test_session_profile_sets_profile():
    from norn.profiles import ANALYSIS
    p = Pipeline("p").session_profile(ANALYSIS)
    assert p._session_profile is ANALYSIS
