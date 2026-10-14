"""Structural and import-time tests for norn/pipelines/implement_features_v3.py.

Import tests patch ``sys.argv`` and reload the module (the same idiom used in
``tests/test_pipelines.py`` for v1) so every fixture directory produces a
stable, repeatable result regardless of what sits in the repo's ``tmp/``.

Shell-stage tests use a temporary git repo built with gitpython so the real
repo is never touched.
"""
from __future__ import annotations

import importlib
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from git import Repo

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "implement_features_v2"
GOOD_DIR = FIXTURE_DIR / "good"


# ---------------------------------------------------------------------------
# Import helper
# ---------------------------------------------------------------------------


def _import_v2(feature_dir: Path | str, *extra_argv: str):
    """Import implement_features_v3 against *feature_dir*.

    Patches sys.argv so the module's import-time argv scan finds exactly
    *feature_dir* as the one positional token.  *extra_argv* tokens are
    appended after the feature dir (e.g. ``"--arg", "max_retries=5"``).
    """
    saved = list(sys.argv)
    sys.argv = [saved[0], str(feature_dir), *extra_argv]
    try:
        from norn.pipelines import implement_features_v3
        return importlib.reload(implement_features_v3)
    finally:
        sys.argv = saved


# ---------------------------------------------------------------------------
# Good-fixture baseline
# ---------------------------------------------------------------------------


def test_good_fixture_imports_cleanly() -> None:
    mod = _import_v2(GOOD_DIR)
    assert mod.config is not None
    assert mod.feature_dir == str(GOOD_DIR.resolve())


def test_metadata_structure() -> None:
    mod = _import_v2(GOOD_DIR)
    assert isinstance(mod.metadata, dict)
    assert "env_vars" in mod.metadata
    assert "ANTHROPIC_API_KEY" in mod.metadata["env_vars"]
    assert list(mod.metadata["args"].keys()) == ["args"]


def test_snapshot_version() -> None:
    mod = _import_v2(GOOD_DIR)
    assert mod.SNAPSHOT_VERSION == "v4"
    assert "v4" in mod.snapshot_root
    parts = Path(mod.snapshot_root).parts
    assert parts[-3:-1] == ("v4", "implement_features_v3"), (
        "snapshot root must carry the pipeline component so v2 and v3 never share markers"
    )


def test_config_items_start_with_required_stages() -> None:
    from norn.dsl import OnFailure, Stage
    mod = _import_v2(GOOD_DIR)
    items = mod.config.items
    assert len(items) >= 3

    first = items[0]
    assert isinstance(first, Stage)
    assert first.name == "assert launch tree"
    assert first.on_failure == OnFailure.FAIL

    second = items[1]
    assert isinstance(second, Stage)
    assert second.name == "preflight repository"
    assert second.on_failure == OnFailure.FAIL

    third = items[2]
    assert isinstance(third, Stage)
    assert third.name == "record start"
    assert third.on_failure == OnFailure.FAIL


def test_all_step_names_contains_all_steps() -> None:
    """all_step_names must cover every step even when resume skips some."""
    # Monkeypatch already_committed_steps to pretend step 01 is done.
    saved = list(sys.argv)
    sys.argv = [saved[0], str(GOOD_DIR)]
    try:
        from norn.pipelines import implement_features_v3
        with patch.object(
            implement_features_v3,
            "already_committed_steps",
            return_value={"step-01-v2-fixture-alpha": "abc123"},
        ):
            mod = importlib.reload(implement_features_v3)
    finally:
        sys.argv = saved

    for stem in [
        "step-01-v2-fixture-alpha",
        "step-02-v2-fixture-beta",
        "step-03-v2-fixture-gamma",
        "step-04-v2-fixture-delta",
    ]:
        assert stem in mod.all_step_names, f"{stem} missing from all_step_names"


def test_feature_test_cmd_loaded_from_index() -> None:
    mod = _import_v2(GOOD_DIR)
    # good/index.md has final_test_cmd but no test_cmd — feature_test_cmd is None
    assert mod.FINAL_TEST_CMD == 'python3 -c "pass"'


# ---------------------------------------------------------------------------
# Knob tests
# ---------------------------------------------------------------------------


def test_default_knobs() -> None:
    mod = _import_v2(GOOD_DIR)
    steps = len(mod._all_step_files)
    # The ceilings scale with the plan: per-step rates plus the review rounds,
    # which cost the same however many steps came before them.
    assert mod.BUDGET == mod.BUDGET_USD_PER_STEP * steps + mod.BUDGET_USD_REVIEW
    assert mod.TOKEN_BUDGET == mod.BUDGET_TOKENS_PER_STEP * steps + mod.BUDGET_TOKENS_REVIEW
    assert mod.MAX_TURNS_REVIEW == mod.MAX_TURNS_REVIEW_BASE + mod.MAX_TURNS_REVIEW_PER_STEP * steps
    assert mod.REVIEW_MODEL == "standard"
    assert mod.AGGREGATE_MODEL == "standard"
    assert mod.MAX_RETRIES == 3
    assert mod.ALLOW_DIRTY_INDEX is False


def test_custom_knobs_land_in_module() -> None:
    mod = _import_v2(
        GOOD_DIR,
        "--arg", "budget=50",
        "--arg", "token_budget=100000",
        "--arg", "review_model=deep",
        "--arg", "aggregate_model=deep",
        "--arg", "max_retries=5",
        "--arg", "allow_dirty_index=1",
        "--arg", "allow_dirty_worktree=1",
    )
    assert mod.BUDGET == 50.0
    assert mod.TOKEN_BUDGET == 100_000
    assert mod.REVIEW_MODEL == "deep"
    assert mod.AGGREGATE_MODEL == "deep"
    assert mod.MAX_RETRIES == 5
    assert mod.ALLOW_DIRTY_INDEX is True
    assert mod.ALLOW_DIRTY_WORKTREE is True


def test_bad_max_retries_zero_raises() -> None:
    with pytest.raises(ValueError, match="max_retries"):
        _import_v2(GOOD_DIR, "--arg", "max_retries=0")


def test_bad_review_model_haiku_raises() -> None:
    with pytest.raises(ValueError, match="review_model"):
        _import_v2(GOOD_DIR, "--arg", "review_model=haiku")


def test_bad_aggregate_model_raises() -> None:
    with pytest.raises(ValueError, match="aggregate_model"):
        _import_v2(GOOD_DIR, "--arg", "aggregate_model=flash")


def test_allow_dirty_index_removes_cached_check() -> None:
    mod = _import_v2(GOOD_DIR, "--arg", "allow_dirty_index=1")
    from norn.dsl import Stage
    preflight_stage = next(
        item for item in mod.config.items
        if isinstance(item, Stage) and item.name == "preflight repository"
    )
    assert "git diff --cached" not in preflight_stage.impl.cmd


def test_allow_dirty_index_false_includes_cached_check() -> None:
    mod = _import_v2(GOOD_DIR)
    from norn.dsl import Stage
    preflight_stage = next(
        item for item in mod.config.items
        if isinstance(item, Stage) and item.name == "preflight repository"
    )
    assert "git diff --cached --quiet" in preflight_stage.impl.cmd
    assert "allow_dirty_index=1" in preflight_stage.impl.cmd


def test_allow_dirty_worktree_removes_worktree_check() -> None:
    mod = _import_v2(GOOD_DIR, "--arg", "allow_dirty_worktree=1")
    from norn.dsl import Stage
    preflight_stage = next(
        item for item in mod.config.items
        if isinstance(item, Stage) and item.name == "preflight repository"
    )
    assert "clean worktree" not in preflight_stage.impl.cmd


def test_allow_dirty_worktree_false_includes_worktree_check() -> None:
    mod = _import_v2(GOOD_DIR)
    from norn.dsl import Stage
    preflight_stage = next(
        item for item in mod.config.items
        if isinstance(item, Stage) and item.name == "preflight repository"
    )
    assert "=== clean worktree ===" in preflight_stage.impl.cmd
    assert "allow_dirty_worktree=1" in preflight_stage.impl.cmd


# ---------------------------------------------------------------------------
# Feature directory resolution
# ---------------------------------------------------------------------------


def test_nonexistent_path_raises() -> None:
    with pytest.raises(ValueError, match="implement_features_v3"):
        _import_v2("/tmp/nonexistent-v2-fixture-xyzzy-99999")


def test_two_directory_candidates_raises(tmp_path) -> None:
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    dir_b = tmp_path / "b"
    dir_b.mkdir()
    saved = list(sys.argv)
    sys.argv = [saved[0], str(dir_a), str(dir_b)]
    try:
        from norn.pipelines import implement_features_v3
        with pytest.raises(ValueError, match="multiple directory candidates"):
            importlib.reload(implement_features_v3)
    finally:
        sys.argv = saved


# ---------------------------------------------------------------------------
# Bad-fixture tests
# ---------------------------------------------------------------------------


def test_bad_no_index_raises() -> None:
    with pytest.raises(ValueError, match="index.md"):
        _import_v2(FIXTURE_DIR / "bad-no-index")


def test_bad_no_steps_raises() -> None:
    with pytest.raises(ValueError, match="step-\\*\\.md"):
        _import_v2(FIXTURE_DIR / "bad-no-steps")


def test_bad_gap_raises() -> None:
    with pytest.raises(ValueError, match="contiguous"):
        _import_v2(FIXTURE_DIR / "bad-gap")


def test_bad_duplicate_raises() -> None:
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        _import_v2(FIXTURE_DIR / "bad-duplicate")


def test_bad_unknown_key_raises() -> None:
    with pytest.raises(ValueError, match="tset_cmd"):
        _import_v2(FIXTURE_DIR / "bad-unknown-key")


def test_bad_unknown_key_mentions_typo() -> None:
    with pytest.raises(ValueError, match="typo"):
        _import_v2(FIXTURE_DIR / "bad-unknown-key")


def test_bad_effort_raises() -> None:
    with pytest.raises(ValueError, match="effort"):
        _import_v2(FIXTURE_DIR / "bad-effort")


def test_bad_effort_mentions_label() -> None:
    with pytest.raises(ValueError, match="label"):
        _import_v2(FIXTURE_DIR / "bad-effort")


def test_bad_placeholder_cmd_raises() -> None:
    with pytest.raises(ValueError, match="no-op"):
        _import_v2(FIXTURE_DIR / "bad-placeholder-cmd")


def test_bad_model_raises() -> None:
    with pytest.raises(ValueError, match="haiku"):
        _import_v2(FIXTURE_DIR / "bad-model")


def test_bad_timeout_raises() -> None:
    with pytest.raises(ValueError, match="boolean"):
        _import_v2(FIXTURE_DIR / "bad-timeout")


def test_bad_allow_noop_raises() -> None:
    with pytest.raises(ValueError, match="allow_noop"):
        _import_v2(FIXTURE_DIR / "bad-allow-noop")


def test_bad_allow_noop_mentions_boolean() -> None:
    with pytest.raises(ValueError, match="boolean"):
        _import_v2(FIXTURE_DIR / "bad-allow-noop")


def test_allow_noop_fixture_imports() -> None:
    """`allow_noop: true` is an accepted step key, not a typo."""
    mod = _import_v2(FIXTURE_DIR / "allow-noop")
    assert _stage(mod, "assert owned diff step-02-v2-noop-gate") is not None


# ---------------------------------------------------------------------------
# _AssertLaunchTree stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_launch_tree_succeeds_with_no_working_dir() -> None:
    from norn.models import PipelineContext
    from norn.pipelines.implement_features_v3 import _AssertLaunchTree

    stage = _AssertLaunchTree()
    ctx = PipelineContext()
    ctx.working_dir = None
    result = await stage.run(ctx)
    assert result.success


@pytest.mark.asyncio
async def test_assert_launch_tree_succeeds_with_project_dir() -> None:
    from norn.models import PipelineContext
    from norn.pipelines.implement_features_v3 import PROJECT_DIR, _AssertLaunchTree

    stage = _AssertLaunchTree()
    ctx = PipelineContext()
    ctx.working_dir = PROJECT_DIR
    result = await stage.run(ctx)
    assert result.success


@pytest.mark.asyncio
async def test_assert_launch_tree_fails_with_different_dir(tmp_path) -> None:
    from norn.models import PipelineContext
    from norn.pipelines.implement_features_v3 import _AssertLaunchTree

    stage = _AssertLaunchTree()
    ctx = PipelineContext()
    ctx.working_dir = str(tmp_path)
    result = await stage.run(ctx)
    assert not result.success
    assert "worktree" in result.error.lower()


# ---------------------------------------------------------------------------
# Catalog discovery
# ---------------------------------------------------------------------------


def test_catalog_lists_implement_features_v3() -> None:
    from norn.catalog import list_pipelines
    names = [info.name for info in list_pipelines()]
    assert "implement_features_v3" in names


def test_catalog_excludes_step_snapshot() -> None:
    from norn.catalog import list_pipelines
    names = [info.name for info in list_pipelines()]
    assert "_step_snapshot" not in names
    assert not any(n.startswith("_") for n in names)


# ---------------------------------------------------------------------------
# Helpers used by step-03 structural tests
# ---------------------------------------------------------------------------


def _stage(module, name: str):
    """Walk config.items (including Loop bodies) and return the named Stage.

    Raises KeyError when no stage with that name exists.
    """
    from norn.dsl import Loop, Stage

    for item in module.config.items:
        if isinstance(item, Stage) and item.name == name:
            return item
        if isinstance(item, Loop):
            for s in item.stages:
                if isinstance(s, Stage) and s.name == name:
                    return s
    raise KeyError(f"Stage not found: {name!r}")


def _loop(module, name: str):
    """Return the Loop with *name* from config.items."""
    from norn.dsl import Loop

    for item in module.config.items:
        if isinstance(item, Loop) and item.name == name:
            return item
    raise KeyError(f"Loop not found: {name!r}")


def _all_generates(module):
    """Yield every Generate instance across top-level stages and loop bodies."""
    from norn.dsl import Loop, Stage
    from norn.stages.generate import Generate

    for item in module.config.items:
        if isinstance(item, Stage) and isinstance(item.impl, Generate):
            yield item
        elif isinstance(item, Loop):
            for s in item.stages:
                if isinstance(s, Stage) and isinstance(s.impl, Generate):
                    yield s


def _step_items(module, stem: str):
    """Return the 13 pipeline items for *stem* in pipeline order."""
    from norn.dsl import ClearContext, Loop, Stage

    expected_names = {
        f"record baseline {stem}",
        f"implement {stem}",
        f"assert head unchanged {stem}",
        f"assert owned diff {stem}",
        f"preflight command {stem}",
        f"validate {stem}",
        f"validation passed {stem}",
        f"closeout {stem}",
        f"commit {stem}",
        f"revalidate {stem}",
        f"assert committed {stem}",
        f"facts {stem}",
    }
    result = []
    for item in module.config.items:
        n = getattr(item, "name", None)
        if n in expected_names:
            result.append(item)
        # ClearContext has no name; find the one after "facts <stem>"
        elif isinstance(item, ClearContext) and result and isinstance(result[-1], Stage) \
                and result[-1].name == f"facts {stem}":
            result.append(item)
    return result


# ---------------------------------------------------------------------------
# Per-step structural tests (against good/ fixture)
# ---------------------------------------------------------------------------

# Stage stems present in the good fixture (after resume filtering — good/ has
# no already-committed steps in this repo, so all four appear).
_GOOD_STEMS = [
    "step-01-v2-fixture-alpha",
    "step-02-v2-fixture-beta",
    "step-03-v2-fixture-gamma",
    "step-04-v2-fixture-delta",
]

_EXPECTED_ITEM_NAMES = [
    "record baseline {stem}",
    "implement {stem}",
    "assert head unchanged {stem}",
    "assert owned diff {stem}",
    "preflight command {stem}",
    "validate {stem}",            # Loop
    "validation passed {stem}",
    "closeout {stem}",            # Generate (semantic handoff)
    "commit {stem}",
    "revalidate {stem}",
    "assert committed {stem}",
    "facts {stem}",               # ReadFile
    None,                         # ClearContext (no name)
]


@pytest.mark.parametrize("stem", _GOOD_STEMS)
def test_per_step_items_exist_in_order(stem: str) -> None:
    """All 13 items for each step exist in config.items in the correct order."""
    from norn.dsl import ClearContext, Loop, Stage

    mod = _import_v2(GOOD_DIR)
    items = _step_items(mod, stem)
    assert len(items) == 13, f"Expected 13 items for {stem}, got {len(items)}: {[getattr(i,'name','?') for i in items]}"
    for item, tpl in zip(items, _EXPECTED_ITEM_NAMES):
        if tpl is None:
            assert isinstance(item, ClearContext), f"Expected ClearContext, got {type(item).__name__}"
            continue
        expected_name = tpl.format(stem=stem)
        assert item.name == expected_name, f"Unexpected name {item.name!r}, wanted {expected_name!r}"

    # Positional checks for type
    assert isinstance(items[0], Stage)          # record baseline
    assert isinstance(items[1], Stage)          # implement
    assert isinstance(items[2], Stage)          # assert head unchanged
    assert isinstance(items[3], Stage)          # assert owned diff
    assert isinstance(items[4], Stage)          # preflight command
    assert isinstance(items[5], Loop)           # validate loop
    assert isinstance(items[6], Stage)          # validation passed gate
    assert isinstance(items[7], Stage)          # closeout
    assert isinstance(items[8], Stage)          # commit
    assert isinstance(items[9], Stage)          # revalidate
    assert isinstance(items[10], Stage)         # assert committed
    assert isinstance(items[11], Stage)         # facts
    assert isinstance(items[12], ClearContext)  # clear context


def test_loop_max_retries_and_exhaust() -> None:
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        lp = _loop(mod, f"validate {stem}")
        assert lp.max_retries == mod.MAX_RETRIES, f"{stem}: max_retries mismatch"
        assert lp.on_exhaust == OnFailure.ASK_USER, f"{stem}: on_exhaust mismatch"


def test_loop_body_order_is_compress_ledger_fix_run() -> None:
    """Each validation loop is exactly: compress, retry ledger, fix, run validation."""
    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        lp = _loop(mod, f"validate {stem}")
        names = [s.name for s in lp.stages]
        assert names == [
            f"compress {stem}",
            f"retry ledger {stem}",
            f"fix {stem}",
            f"run validation {stem}",
        ], f"{stem}: loop stage names {names!r}"


def test_gate_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        gate = _stage(mod, f"validation passed {stem}")
        assert gate.on_failure == OnFailure.FAIL, f"{stem}: gate on_failure should be FAIL"


def test_gate_name_differs_from_all_loop_names() -> None:
    """Gate stage name must not collide with any in-loop stage name."""
    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        lp = _loop(mod, f"validate {stem}")
        loop_names = {s.name for s in lp.stages}
        gate_name = f"validation passed {stem}"
        assert gate_name not in loop_names, f"{stem}: gate name {gate_name!r} collides with loop"


def test_implement_on_failure_is_ask_user() -> None:
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        s = _stage(mod, f"implement {stem}")
        assert s.on_failure == OnFailure.ASK_USER, f"{stem}: implement on_failure should be ASK_USER"


# ---------------------------------------------------------------------------
# Prompt content tests
# ---------------------------------------------------------------------------

_FORBIDDEN_VERBS = [
    "git add", "git commit", "git amend", "git reset",
    "git checkout", "git restore", "git rebase", "git stash",
    "git clean", "git push",
]


def _get_implement_prompt(module, stem: str) -> str:
    from norn.stages.generate import Generate
    s = _stage(module, f"implement {stem}")
    assert isinstance(s.impl, Generate)
    return s.impl.prompt


def _get_fix_prompt(module, stem: str) -> str:
    from norn.stages.generate import Generate
    s = _stage(module, f"fix {stem}")
    assert isinstance(s.impl, Generate)
    return s.impl.prompt


@pytest.mark.parametrize("stem", _GOOD_STEMS)
def test_implement_prompt_contains_validation_cmd(stem: str) -> None:
    mod = _import_v2(GOOD_DIR)
    prompt = _get_implement_prompt(mod, stem)
    # step-02 has both test_cmd and bats_cmd; check combined command markers
    if stem == "step-02-v2-fixture-beta":
        assert "=== test_cmd ===" in prompt, f"{stem}: missing test_cmd marker"
        assert "=== bats_cmd ===" in prompt, f"{stem}: missing bats_cmd marker"
        assert 'python3 -c "pass"' in prompt  # the actual cmd appears
    else:
        assert 'python3 -c "pass"' in prompt, f"{stem}: validation cmd missing"


@pytest.mark.parametrize("stem", _GOOD_STEMS)
def test_implement_prompt_contains_step_path(stem: str) -> None:
    mod = _import_v2(GOOD_DIR)
    prompt = _get_implement_prompt(mod, stem)
    step_path = str(GOOD_DIR / f"{stem}.md")
    assert step_path in prompt, f"{stem}: step_path {step_path!r} missing from implement prompt"


@pytest.mark.parametrize("stem", _GOOD_STEMS)
@pytest.mark.parametrize("verb", _FORBIDDEN_VERBS)
def test_implement_prompt_contains_forbidden_verb(stem: str, verb: str) -> None:
    mod = _import_v2(GOOD_DIR)
    prompt = _get_implement_prompt(mod, stem)
    assert verb in prompt, f"{stem}: implement prompt missing forbidden verb {verb!r}"


@pytest.mark.parametrize("stem", _GOOD_STEMS)
def test_fix_prompt_contains_validation_cmd(stem: str) -> None:
    mod = _import_v2(GOOD_DIR)
    prompt = _get_fix_prompt(mod, stem)
    if stem == "step-02-v2-fixture-beta":
        assert "=== test_cmd ===" in prompt
        assert "=== bats_cmd ===" in prompt
    else:
        assert 'python3 -c "pass"' in prompt, f"{stem}: validation cmd missing from fix prompt"


@pytest.mark.parametrize("stem", _GOOD_STEMS)
def test_fix_prompt_contains_step_path(stem: str) -> None:
    mod = _import_v2(GOOD_DIR)
    prompt = _get_fix_prompt(mod, stem)
    step_path = str(GOOD_DIR / f"{stem}.md")
    assert step_path in prompt, f"{stem}: step_path missing from fix prompt"


@pytest.mark.parametrize("stem", _GOOD_STEMS)
@pytest.mark.parametrize("verb", _FORBIDDEN_VERBS)
def test_fix_prompt_contains_forbidden_verb(stem: str, verb: str) -> None:
    mod = _import_v2(GOOD_DIR)
    prompt = _get_fix_prompt(mod, stem)
    assert verb in prompt, f"{stem}: fix prompt missing forbidden verb {verb!r}"


def test_no_prompt_mentions_context7() -> None:
    """No agent prompt in the pipeline may mention 'context7'."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    for s in _all_generates(mod):
        assert isinstance(s.impl, Generate)
        prompt = s.impl.prompt or ""
        assert "context7" not in prompt.lower(), (
            f"Stage {s.name!r} prompt mentions 'context7'"
        )


def test_no_generate_lists_task_tool() -> None:
    """No Generate stage may include 'Task' in allowed_tools."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    for s in _all_generates(mod):
        assert isinstance(s.impl, Generate)
        tools = s.impl.allowed_tools or []
        assert "Task" not in tools, (
            f"Stage {s.name!r} lists 'Task' in allowed_tools"
        )


def test_every_generate_has_timeout() -> None:
    """Every Generate stage carries a timeout.

    The learn agent is the one stage whose timeout is applied inside the stage
    (LearnGenerate) rather than by the runner, because a runner timeout would
    fail a run that passed review (decision 5).
    """
    from norn.pipelines._learnings import LearnGenerate

    mod = _import_v2(GOOD_DIR)
    for s in _all_generates(mod):
        if isinstance(s.impl, LearnGenerate):
            assert s.timeout is None and s.impl.timeout == 300, (
                f"{s.name!r}: the learn agent's timeout belongs inside the stage"
            )
            continue
        assert s.timeout is not None, (
            f"Generate stage {s.name!r} has no timeout"
        )


def test_no_generate_sets_cwd() -> None:
    """Generate stages must not set cwd (they follow ctx.working_dir)."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    for s in _all_generates(mod):
        assert isinstance(s.impl, Generate)
        assert s.impl.cwd is None, (
            f"Generate stage {s.name!r} sets cwd={s.impl.cwd!r}; "
            "Generate stages must follow ctx.working_dir"
        )


# ---------------------------------------------------------------------------
# Timeout and model tests
# ---------------------------------------------------------------------------


def test_implement_timeout_deep_2700() -> None:
    """Step-02 uses deep model → implement timeout must be 2700."""
    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "implement step-02-v2-fixture-beta")
    assert s.timeout == 2700, f"deep implement timeout should be 2700, got {s.timeout}"


@pytest.mark.parametrize("stem", [
    "step-01-v2-fixture-alpha",
    "step-03-v2-fixture-gamma",
    "step-04-v2-fixture-delta",
])
def test_implement_timeout_standard(stem: str) -> None:
    """Non-deep steps → the standard implement timeout."""
    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, f"implement {stem}")
    assert s.timeout == mod.TIMEOUT_IMPLEMENT_SONNET == 2700, (
        f"{stem}: standard implement timeout should be 2700, got {s.timeout}"
    )


# ---------------------------------------------------------------------------
# Fix-stage `when` predicate tests
# ---------------------------------------------------------------------------


def test_fix_when_truthy_after_failed_run_validation() -> None:
    """fix stage `when` predicate returns True after a failed run validation."""
    from norn.models import PipelineContext, StageResult

    mod = _import_v2(GOOD_DIR)
    stem = "step-01-v2-fixture-alpha"
    fix_stage = _stage(mod, f"fix {stem}")
    assert fix_stage.when is not None

    ctx = PipelineContext()
    ctx.results[f"run validation {stem}"] = StageResult(
        name=f"run validation {stem}", success=False, output="failed"
    )
    assert fix_stage.when(ctx) is True


def test_fix_when_falsy_after_successful_run_validation() -> None:
    """fix stage `when` predicate returns False after a successful run validation."""
    from norn.models import PipelineContext, StageResult

    mod = _import_v2(GOOD_DIR)
    stem = "step-01-v2-fixture-alpha"
    fix_stage = _stage(mod, f"fix {stem}")
    assert fix_stage.when is not None

    ctx = PipelineContext()
    ctx.results[f"run validation {stem}"] = StageResult(
        name=f"run validation {stem}", success=True, output="passed"
    )
    assert fix_stage.when(ctx) is False


def test_fix_when_falsy_with_no_prior_result() -> None:
    """fix stage `when` predicate returns False when run validation has not run yet."""
    from norn.models import PipelineContext

    mod = _import_v2(GOOD_DIR)
    stem = "step-01-v2-fixture-alpha"
    fix_stage = _stage(mod, f"fix {stem}")
    assert fix_stage.when is not None

    ctx = PipelineContext()
    assert fix_stage.when(ctx) is False


# ---------------------------------------------------------------------------
# CompressTestLog configuration test
# ---------------------------------------------------------------------------


def test_compress_stages_have_summarize_with_haiku_false() -> None:
    """All CompressTestLog instances in the pipeline must have summarize_with_haiku=False."""
    from norn.dsl import Loop, Stage
    from norn.stages.compress_test_log import CompressTestLog

    mod = _import_v2(GOOD_DIR)
    found = 0
    for item in mod.config.items:
        stages_to_check = []
        if isinstance(item, Stage):
            stages_to_check = [item]
        elif isinstance(item, Loop):
            stages_to_check = item.stages
        for s in stages_to_check:
            if isinstance(s, Stage) and isinstance(s.impl, CompressTestLog):
                assert s.impl.summarize_with_haiku is False, (
                    f"Stage {s.name!r}: summarize_with_haiku should be False"
                )
                found += 1
    # good/ has 4 steps + 1 aggregate compress → 5 compress stages total
    assert found == 5, f"Expected 5 CompressTestLog stages (4 per-step + 1 aggregate), found {found}"


# ---------------------------------------------------------------------------
# Temp-repo shell-stage tests
# ---------------------------------------------------------------------------


def _init_temp_repo(path: Path) -> Repo:
    """Create a minimal git repo with identity and an initial commit."""
    repo = Repo.init(path)
    repo.config_writer().set_value("user", "name", "Test User").release()
    repo.config_writer().set_value("user", "email", "test@example.com").release()
    readme = path / "README.md"
    readme.write_text("# test\n")
    repo.index.add(["README.md"])
    repo.index.commit("initial commit")
    return repo


def _import_v2_in_repo(feature_dir: Path, monkeypatch) -> object:
    """Reload implement_features_v3 with PROJECT_DIR = cwd (a temp repo)."""
    saved = list(sys.argv)
    sys.argv = [saved[0], str(feature_dir)]
    try:
        from norn.pipelines import implement_features_v3
        return importlib.reload(implement_features_v3)
    finally:
        sys.argv = saved


@pytest.mark.asyncio
async def test_record_baseline_succeeds(tmp_path, monkeypatch) -> None:
    """record baseline RunCommand writes head file and pre snapshot."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"

    # Copy good fixture into the repo
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    stage = _stage(mod, f"record baseline {stem}")
    ctx = PipelineContext()
    result = await stage.impl.run(ctx)
    assert result.success, f"record baseline failed: {result.error}"

    head_file = Path(mod.snapshot_root) / f"{stem}.head"
    pre_file = Path(mod.snapshot_root) / f"{stem}.pre.json"
    assert head_file.exists(), "head file not written"
    assert pre_file.exists(), "pre snapshot not written"
    # head file must contain the current HEAD sha (40 hex chars + newline)
    assert len(head_file.read_text().strip()) == 40


@pytest.mark.asyncio
async def test_assert_head_unchanged_fails_when_head_moves(tmp_path, monkeypatch) -> None:
    """assert head unchanged fails when a commit happens after record baseline."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    # Record baseline first
    baseline_stage = _stage(mod, f"record baseline {stem}")
    r = await baseline_stage.impl.run(ctx)
    assert r.success

    # Now make a new commit so HEAD moves
    new_file = repo_path / "new.txt"
    new_file.write_text("new\n")
    repo.index.add(["new.txt"])
    repo.index.commit("move HEAD")

    # assert head unchanged should now fail
    check_stage = _stage(mod, f"assert head unchanged {stem}")
    r2 = await check_stage.impl.run(ctx)
    assert not r2.success, "assert head unchanged should fail after HEAD moved"


@pytest.mark.asyncio
async def test_head_moved_error_names_both_shas(tmp_path, monkeypatch) -> None:
    """The failure must say which SHA was recorded and which is current.

    HEAD moves forward on a commit and backward on a reset; the gate cannot
    tell them apart, so the message names both SHAs instead of guessing that
    the agent committed.
    """
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    baseline_stage = _stage(mod, f"record baseline {stem}")
    assert (await baseline_stage.impl.run(ctx)).success
    recorded = repo.head.commit.hexsha

    # Move HEAD backwards, the way a `git reset` in another terminal would.
    (repo_path / "new.txt").write_text("new\n")
    repo.index.add(["new.txt"])
    repo.index.commit("move HEAD")
    moved = repo.head.commit.hexsha
    repo.git.reset(recorded)

    check_stage = _stage(mod, f"assert head unchanged {stem}")
    r2 = await check_stage.impl.run(ctx)
    assert r2.success, "reset back to the recorded SHA must satisfy the gate"

    # And forward again: the message carries both SHAs.
    repo.git.reset(moved)
    r3 = await check_stage.impl.run(ctx)
    assert not r3.success
    output = f"{r3.output or ''}{r3.error or ''}"
    assert recorded in output
    assert moved in output
    assert "recorded:" in output


@pytest.mark.asyncio
async def test_assert_head_unchanged_fails_when_files_staged(tmp_path, monkeypatch) -> None:
    """assert head unchanged fails when the index is dirty."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    # Record baseline
    baseline_stage = _stage(mod, f"record baseline {stem}")
    r = await baseline_stage.impl.run(ctx)
    assert r.success

    # Stage a file without committing
    staged_file = repo_path / "staged.txt"
    staged_file.write_text("staged\n")
    repo.index.add(["staged.txt"])

    check_stage = _stage(mod, f"assert head unchanged {stem}")
    r2 = await check_stage.impl.run(ctx)
    assert not r2.success, "assert head unchanged should fail when files are staged"

    # Clean up staged file for isolation
    repo.index.reset()


@pytest.mark.asyncio
async def test_assert_head_unchanged_succeeds_when_clean(tmp_path, monkeypatch) -> None:
    """assert head unchanged succeeds when HEAD and index are unmodified."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    baseline_stage = _stage(mod, f"record baseline {stem}")
    r = await baseline_stage.impl.run(ctx)
    assert r.success

    check_stage = _stage(mod, f"assert head unchanged {stem}")
    r2 = await check_stage.impl.run(ctx)
    assert r2.success, f"assert head unchanged should succeed on clean repo: {r2.error}"


def test_validation_gate_reruns_only_when_the_loop_left_no_proof_for_this_tree(
    tmp_path, monkeypatch
) -> None:
    """The gate trusts a passing in-loop run on an unchanged tree and reruns otherwise."""
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    shutil.copytree(GOOD_DIR, repo_path / "feat")
    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(repo_path / "feat", monkeypatch)

    runs = tmp_path / "runs.txt"
    fail_flag = tmp_path / "fail"
    cmd = f"echo run >> {runs} && test ! -f {fail_flag}"
    in_loop, gate = mod._proven_validation("step-x", cmd)

    def sh(script: str) -> subprocess.CompletedProcess:
        return subprocess.run(script, shell=True, cwd=repo_path, capture_output=True, text=True)

    def run_count() -> int:
        return len(runs.read_text().splitlines())

    assert sh(in_loop).returncode == 0
    passed_gate = sh(gate)
    assert passed_gate.returncode == 0, passed_gate.stderr
    assert "not rerun" in passed_gate.stdout
    assert run_count() == 1

    (repo_path / "README.md").write_text("# changed after validation\n")
    assert sh(gate).returncode == 0
    assert run_count() == 2

    assert sh(in_loop).returncode == 0
    fail_flag.touch()
    assert sh(in_loop).returncode != 0
    assert sh(gate).returncode != 0, "a failed last loop run must not leave a proof"
    assert run_count() == 5


def test_validation_is_not_rerun_in_the_same_loop_pass_on_the_tree_it_failed_on(
    tmp_path, monkeypatch
) -> None:
    """A later attempt fails at once when the fix changed nothing; attempt 1 always runs."""
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    shutil.copytree(GOOD_DIR, repo_path / "feat")
    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(repo_path / "feat", monkeypatch)

    runs = tmp_path / "runs.txt"
    fail_flag = tmp_path / "fail"
    fail_flag.touch()
    cmd = f"echo run >> {runs} && test ! -f {fail_flag}"
    in_loop, _ = mod._proven_validation("step-y", cmd)

    def sh(attempt: int) -> subprocess.CompletedProcess:
        env = {**os.environ, "NORN_ATTEMPT": str(attempt)}
        return subprocess.run(in_loop, shell=True, cwd=repo_path, env=env,
                              capture_output=True, text=True)

    def run_count() -> int:
        return len(runs.read_text().splitlines())

    assert sh(1).returncode != 0
    assert run_count() == 1

    unchanged = sh(2)
    assert unchanged.returncode != 0
    assert "fix changed nothing" in unchanged.stderr
    assert run_count() == 1, "an unchanged tree must not be revalidated within the loop pass"

    assert sh(1).returncode != 0, "attempt 1 is an operator retry and always reruns"
    assert run_count() == 2

    (repo_path / "README.md").write_text("# the fix changed this\n")
    fail_flag.unlink()
    assert sh(2).returncode == 0
    assert run_count() == 3


@pytest.mark.asyncio
async def test_assert_owned_diff_fails_with_no_changes(tmp_path, monkeypatch) -> None:
    """assert owned diff fails when no worktree changes occurred after baseline."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    baseline_stage = _stage(mod, f"record baseline {stem}")
    r = await baseline_stage.impl.run(ctx)
    assert r.success

    # Run assert owned diff immediately — nothing changed
    diff_stage = _stage(mod, f"assert owned diff {stem}")
    r2 = await diff_stage.impl.run(ctx)
    assert not r2.success, "assert owned diff should fail when nothing changed"
    assert "changed nothing" in (r2.output or {}).get("stdout", "") or \
           "changed nothing" in (r2.output or {}).get("stderr", ""), \
           f"Expected 'changed nothing' in output: {r2.output}"


@pytest.mark.asyncio
async def test_assert_owned_diff_succeeds_after_file_written(tmp_path, monkeypatch) -> None:
    """assert owned diff succeeds when a new file was written after baseline."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    baseline_stage = _stage(mod, f"record baseline {stem}")
    r = await baseline_stage.impl.run(ctx)
    assert r.success

    # Write a new file to the repo
    new_file = repo_path / "new_feature.py"
    new_file.write_text("# new\n")

    diff_stage = _stage(mod, f"assert owned diff {stem}")
    r2 = await diff_stage.impl.run(ctx)
    assert r2.success, f"assert owned diff should succeed after file written: {r2.output}"


# ---------------------------------------------------------------------------
# Recoverable no-op: assert owned diff prompts, and [c]ontinue stays coherent
# ---------------------------------------------------------------------------


def test_assert_owned_diff_on_failure_is_ask_user() -> None:
    """A step that changed nothing must prompt, not kill the run."""
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        s = _stage(mod, f"assert owned diff {stem}")
        assert s.on_failure == OnFailure.ASK_USER, (
            f"{stem}: assert owned diff on_failure should be ASK_USER"
        )


@pytest.mark.asyncio
async def test_assert_owned_diff_writes_noop_marker(tmp_path, monkeypatch) -> None:
    """The no-op branch drops the marker that gates commit and assert committed."""
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()
    marker = Path(mod.snapshot_root) / f"{stem}.noop"

    r = await _stage(mod, f"record baseline {stem}").impl.run(ctx)
    assert r.success

    diff_stage = _stage(mod, f"assert owned diff {stem}")
    r2 = await diff_stage.impl.run(ctx)
    assert not r2.success
    assert marker.exists(), "no-op branch should drop the .noop marker"

    # The commit and assert-committed stages are skipped while it exists.
    assert not _stage(mod, f"commit {stem}").when(ctx)
    assert not _stage(mod, f"assert committed {stem}").when(ctx)
    assert _stage(mod, f"record no-op {stem}").when(ctx)

    # A retry after real work clears the marker and re-enables the commit.
    (repo_path / "new_feature.py").write_text("# new\n")
    r3 = await diff_stage.impl.run(ctx)
    assert r3.success, f"assert owned diff should pass after a real change: {r3.output}"
    assert not marker.exists(), "a passing re-run must clear the stale marker"
    assert _stage(mod, f"commit {stem}").when(ctx)
    assert not _stage(mod, f"record no-op {stem}").when(ctx)


# ---------------------------------------------------------------------------
# Declared no-op: `allow_noop: true` passes the gate without prompting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_noop_step_passes_owned_diff_with_no_changes(tmp_path, monkeypatch) -> None:
    """A verification-only step must survive a run that cannot prompt.

    Without the key the gate exits 1, and `--non-interactive` answers abort —
    which is what killed a fix_jira_issue child run on a regression-gate step.
    """
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(FIXTURE_DIR / "allow-noop", feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-02-v2-noop-gate"
    ctx = PipelineContext()
    marker = Path(mod.snapshot_root) / f"{stem}.noop"

    r = await _stage(mod, f"record baseline {stem}").impl.run(ctx)
    assert r.success

    r2 = await _stage(mod, f"assert owned diff {stem}").impl.run(ctx)
    assert r2.success, f"allow_noop step should pass with no changes: {r2.output}"
    assert "changed nothing" in (r2.output or {}).get("stdout", "")

    # The marker still drives the downstream wiring: nothing to commit, and the
    # no-op facts file stands in for the commit's.
    assert marker.exists(), "allow_noop must still drop the .noop marker"
    assert not _stage(mod, f"commit {stem}").when(ctx)
    assert not _stage(mod, f"assert committed {stem}").when(ctx)
    assert _stage(mod, f"record no-op {stem}").when(ctx)


@pytest.mark.asyncio
async def test_noop_step_commits_files_the_fix_loop_changed(tmp_path, monkeypatch) -> None:
    """Regression: the marker is set before validation, so a fix the loop made
    in a gate step was never committed and the next run refused the dirty tree."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(FIXTURE_DIR / "allow-noop", feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-02-v2-noop-gate"
    ctx = PipelineContext()
    marker = Path(mod.snapshot_root) / f"{stem}.noop"

    assert (await _stage(mod, f"record baseline {stem}").impl.run(ctx)).success
    assert (await _stage(mod, f"assert owned diff {stem}").impl.run(ctx)).success
    assert marker.exists()
    head_before = repo.head.commit.hexsha

    # The fix loop edits a file after the owned-diff gate.
    (repo_path / "fixed_by_loop.py").write_text("# fix\n")

    recheck = _stage(mod, f"recheck no-op {stem}")
    assert recheck.when(ctx)
    r = await recheck.impl.run(ctx)
    assert r.success, f"recheck no-op failed: {r.output}"
    assert "fixed_by_loop.py" in (r.output or {}).get("stdout", "")
    assert not marker.exists(), "a changed tree must clear the no-op marker"
    assert not _stage(mod, f"record no-op {stem}").when(ctx)
    assert _stage(mod, f"commit {stem}").when(ctx)

    r = await _stage(mod, f"commit {stem}").impl.run(ctx)
    assert r.success, f"commit failed: {r.output}"
    assert repo.head.commit.parents[0].hexsha == head_before
    assert f"refactor: {stem}" in repo.head.commit.message


@pytest.mark.asyncio
async def test_noop_step_with_unchanged_tree_stays_a_noop(tmp_path, monkeypatch) -> None:
    """The recheck keeps the marker when validation left the tree as it was."""
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(FIXTURE_DIR / "allow-noop", feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-02-v2-noop-gate"
    ctx = PipelineContext()
    marker = Path(mod.snapshot_root) / f"{stem}.noop"

    assert (await _stage(mod, f"record baseline {stem}").impl.run(ctx)).success
    assert (await _stage(mod, f"assert owned diff {stem}").impl.run(ctx)).success

    r = await _stage(mod, f"recheck no-op {stem}").impl.run(ctx)
    assert r.success, f"recheck no-op failed: {r.output}"
    assert marker.exists(), "an unchanged tree must keep the no-op marker"
    assert not _stage(mod, f"commit {stem}").when(ctx)
    assert _stage(mod, f"record no-op {stem}").when(ctx)


@pytest.mark.asyncio
async def test_allow_noop_does_not_leak_to_other_steps(tmp_path, monkeypatch) -> None:
    """The waiver is per step: a sibling without the key still fails the gate."""
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(FIXTURE_DIR / "allow-noop", feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-noop-alpha"
    ctx = PipelineContext()

    r = await _stage(mod, f"record baseline {stem}").impl.run(ctx)
    assert r.success

    r2 = await _stage(mod, f"assert owned diff {stem}").impl.run(ctx)
    assert not r2.success, "a step without allow_noop must still stop on a no-op"


@pytest.mark.asyncio
async def test_allow_noop_step_still_fails_on_a_real_error(tmp_path, monkeypatch) -> None:
    """Only the no-op branch is waived; a broken snapshot still fails the stage."""
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(FIXTURE_DIR / "allow-noop", feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-02-v2-noop-gate"
    ctx = PipelineContext()

    # No baseline snapshot was recorded, so the diff has nothing to compare to.
    r = await _stage(mod, f"assert owned diff {stem}").impl.run(ctx)
    assert not r.success, "a missing pre-snapshot must still fail the gate"


@pytest.mark.asyncio
async def test_record_noop_writes_three_line_facts(tmp_path, monkeypatch) -> None:
    """The no-op facts file has the same three-line shape the commit stage writes."""
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    r = await _stage(mod, f"record no-op {stem}").impl.run(ctx)
    assert r.success, f"record no-op should succeed: {r.output}"

    facts = (Path(mod.snapshot_root) / f"{stem}.facts").read_text().splitlines()
    assert len(facts) == 3, f"expected three facts lines, got {facts}"
    assert facts[0].startswith("Commit: (none)")
    assert facts[1] == "Changed: (none)"
    assert facts[2].startswith("Validation: PASS - ")


# ---------------------------------------------------------------------------
# Step-04 structural tests: commit, revalidate, assert committed, clear context
# ---------------------------------------------------------------------------


def test_commit_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        s = _stage(mod, f"commit {stem}")
        assert s.on_failure == OnFailure.FAIL, f"{stem}: commit on_failure should be FAIL"


def test_assert_committed_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        s = _stage(mod, f"assert committed {stem}")
        assert s.on_failure == OnFailure.FAIL, f"{stem}: assert committed on_failure should be FAIL"


def test_revalidate_has_when_predicate() -> None:
    """revalidate stage has a `when` predicate that is true only when the hookfix marker exists."""
    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        s = _stage(mod, f"revalidate {stem}")
        assert s.when is not None, f"{stem}: revalidate must have a `when` predicate"


def test_revalidate_when_truthy_with_marker(tmp_path) -> None:
    """revalidate when predicate returns True when the hookfix marker file exists."""
    from norn.models import PipelineContext

    mod = _import_v2(GOOD_DIR)
    stem = "step-01-v2-fixture-alpha"
    s = _stage(mod, f"revalidate {stem}")

    # Create the marker file at the path the module expects
    marker_path = os.path.join(mod.snapshot_root, f"{stem}.hookfix.marker")
    os.makedirs(os.path.dirname(marker_path), exist_ok=True)
    Path(marker_path).touch()
    try:
        ctx = PipelineContext()
        assert s.when(ctx) is True
    finally:
        os.unlink(marker_path)


def test_revalidate_when_falsy_without_marker() -> None:
    """revalidate when predicate returns False when no marker file exists."""
    from norn.models import PipelineContext

    mod = _import_v2(GOOD_DIR)
    stem = "step-01-v2-fixture-alpha"
    s = _stage(mod, f"revalidate {stem}")

    marker_path = os.path.join(mod.snapshot_root, f"{stem}.hookfix.marker")
    # Ensure the marker does not exist
    if os.path.exists(marker_path):
        os.unlink(marker_path)
    ctx = PipelineContext()
    assert s.when(ctx) is False


def test_revalidate_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        s = _stage(mod, f"revalidate {stem}")
        assert s.on_failure == OnFailure.FAIL, f"{stem}: revalidate on_failure should be FAIL"


def test_revalidate_emits_amend_guidance_on_failure() -> None:
    """revalidate wraps the validation command so a failure explains the amend path."""
    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        cmd = _stage(mod, f"revalidate {stem}").impl.cmd
        assert "already committed" in cmd, f"{stem}: revalidate must say the code is committed"
        assert "amend the last commit" in cmd, f"{stem}: revalidate must name the amend fix"
        assert "skipped on resume" in cmd, f"{stem}: revalidate must mention resume skipping"


def test_loop_body_ends_with_clear_context() -> None:
    """The last item of each step's 13-item segment is a ClearContext."""
    from norn.dsl import ClearContext

    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        items = _step_items(mod, stem)
        assert len(items) == 13, f"{stem}: expected 13 items, got {len(items)}"
        assert isinstance(items[-1], ClearContext), (
            f"{stem}: last item should be ClearContext, got {type(items[-1]).__name__}"
        )


# ---------------------------------------------------------------------------
# Step-05 structural tests: closeout and facts stages
# ---------------------------------------------------------------------------


def test_closeout_stage_exists_between_gate_and_commit() -> None:
    """closeout stage exists and sits between validation passed and commit."""
    mod = _import_v2(GOOD_DIR)
    items = mod.config.items
    # Walk items looking for the triplet [validation passed, closeout, commit]
    names = [getattr(i, "name", None) for i in items]
    for stem in _GOOD_STEMS:
        vp_idx = names.index(f"validation passed {stem}")
        co_idx = names.index(f"closeout {stem}")
        cm_idx = names.index(f"commit {stem}")
        assert vp_idx < co_idx < cm_idx, (
            f"{stem}: expected 'validation passed' < 'closeout' < 'commit', "
            f"got indices {vp_idx}, {co_idx}, {cm_idx}"
        )


def test_facts_stage_exists_between_assert_committed_and_clear_context() -> None:
    """facts stage sits between assert committed and the ClearContext item."""
    from norn.dsl import ClearContext

    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        items = _step_items(mod, stem)
        ac_idx = next(i for i, it in enumerate(items) if getattr(it, "name", None) == f"assert committed {stem}")
        facts_idx = next(i for i, it in enumerate(items) if getattr(it, "name", None) == f"facts {stem}")
        cc_idx = next(i for i, it in enumerate(items) if isinstance(it, ClearContext))
        assert ac_idx < facts_idx < cc_idx, (
            f"{stem}: expected 'assert committed' < 'facts' < ClearContext, "
            f"got indices {ac_idx}, {facts_idx}, {cc_idx}"
        )


@pytest.mark.parametrize("stem", _GOOD_STEMS)
def test_closeout_generate_config(stem: str) -> None:
    """Closeout Generate has plan mode, 1 turn, falsy tools/sources, step model, timeout 120."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, f"closeout {stem}")
    assert isinstance(s.impl, Generate), f"{stem}: closeout impl should be Generate"
    assert s.impl.permission_mode == "plan", f"{stem}: closeout must use plan mode"
    assert s.impl.max_turns == 1, f"{stem}: closeout max_turns must be 1"
    assert not s.impl.allowed_tools, f"{stem}: closeout allowed_tools must be falsy"
    assert not s.impl.setting_sources, f"{stem}: closeout setting_sources must be falsy"
    assert s.timeout == 120, f"{stem}: closeout timeout must be 120"


@pytest.mark.parametrize("stem", _GOOD_STEMS)
def test_closeout_model_matches_step_model(stem: str) -> None:
    """Closeout model matches the step's model and is never 'haiku'."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    closeout = _stage(mod, f"closeout {stem}")
    implement = _stage(mod, f"implement {stem}")
    assert isinstance(closeout.impl, Generate)
    assert isinstance(implement.impl, Generate)
    assert closeout.impl.model == implement.impl.model, (
        f"{stem}: closeout model {closeout.impl.model!r} != implement model {implement.impl.model!r}"
    )
    assert closeout.impl.model != "haiku", f"{stem}: closeout model must not be 'haiku'"


def test_no_generate_uses_haiku_model() -> None:
    """No Generate stage in the pipeline may use the 'haiku' model."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    for s in _all_generates(mod):
        assert isinstance(s.impl, Generate)
        assert s.impl.model != "haiku", (
            f"Stage {s.name!r} uses model 'haiku', which is not allowed"
        )


@pytest.mark.parametrize("stem", _GOOD_STEMS)
def test_closeout_on_failure_is_ask_user(stem: str) -> None:
    """Closeout on_failure must be ASK_USER (failure is recoverable)."""
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, f"closeout {stem}")
    assert s.on_failure == OnFailure.ASK_USER, (
        f"{stem}: closeout on_failure should be ASK_USER, got {s.on_failure}"
    )


def test_facts_stage_uses_readfile() -> None:
    """facts stages use ReadFile (not Generate or RunCommand)."""
    from norn.stages.read_file import ReadFile

    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        s = _stage(mod, f"facts {stem}")
        assert isinstance(s.impl, ReadFile), (
            f"{stem}: facts impl should be ReadFile, got {type(s.impl).__name__}"
        )


# ---------------------------------------------------------------------------
# Prior-context composition tests
# ---------------------------------------------------------------------------


def test_implement_prompt_step01_has_no_prior_steps() -> None:
    """Step 01 is the first step — its implement prompt has no '## Prior steps'."""
    mod = _import_v2(GOOD_DIR)
    prompt = _get_implement_prompt(mod, "step-01-v2-fixture-alpha")
    assert "## Prior steps" not in prompt, "Step 01 must not have a '## Prior steps' section"


def test_implement_prompt_has_build_what_was_asked_section() -> None:
    """The implement prompt tells the agent to build only what was asked."""
    mod = _import_v2(GOOD_DIR)
    prompt = _get_implement_prompt(mod, "step-01-v2-fixture-alpha")
    assert "## Build what was asked" in prompt
    assert "NOT_BUILT" in prompt


def test_closeout_prompt_fields_and_limit() -> None:
    """The closeout prompt asks for the richer fields, in order, capped at 900."""
    mod = _import_v2(GOOD_DIR)
    prompt = mod._CLOSEOUT_PROMPT
    fields = ["EVIDENCE:", "SETTLED:", "DEVIATIONS:", "NOT_BUILT:", "NEXT_CONSTRAINTS:", "RISKS:"]
    positions = [prompt.index(f) for f in fields]
    assert positions == sorted(positions)
    assert "DECISIONS:" not in prompt
    assert "Maximum 900 characters" in prompt
    assert "700" not in prompt
    assert "No preamble. Do not use tools." in prompt
    assert "{" not in prompt and "}" not in prompt


def test_implement_prompt_step02_has_facts_and_closeout_for_step01() -> None:
    """Step 02 prompt includes facts and closeout placeholders for step 01."""
    mod = _import_v2(GOOD_DIR)
    stem01 = "step-01-v2-fixture-alpha"
    prompt = _get_implement_prompt(mod, "step-02-v2-fixture-beta")
    assert "## Prior steps" in prompt, "Step 02 must have a '## Prior steps' section"
    assert f"{{facts {stem01}.output}}" in prompt, (
        f"Step 02 prompt must include {{facts {stem01}.output}}"
    )
    assert f"{{closeout {stem01}.output}}" in prompt, (
        f"Step 02 prompt must include {{closeout {stem01}.output}}"
    )


def test_implement_prompt_step04_has_facts_for_all_prior_steps() -> None:
    """Step 04 prompt includes facts for steps 01, 02, 03."""
    mod = _import_v2(GOOD_DIR)
    prompt = _get_implement_prompt(mod, "step-04-v2-fixture-delta")
    for stem in ["step-01-v2-fixture-alpha", "step-02-v2-fixture-beta", "step-03-v2-fixture-gamma"]:
        assert f"{{facts {stem}.output}}" in prompt, (
            f"Step 04 prompt must include {{facts {stem}.output}}"
        )


def test_implement_prompt_step04_closeout_only_for_last_two() -> None:
    """Step 04 closeout window covers steps 02 and 03 only (not step 01)."""
    mod = _import_v2(GOOD_DIR)
    prompt = _get_implement_prompt(mod, "step-04-v2-fixture-delta")
    stem01 = "step-01-v2-fixture-alpha"
    stem02 = "step-02-v2-fixture-beta"
    stem03 = "step-03-v2-fixture-gamma"
    assert f"{{closeout {stem01}.output}}" not in prompt, (
        "Step 04 must NOT include closeout for step 01 (outside two-step window)"
    )
    assert f"{{closeout {stem02}.output}}" in prompt, (
        "Step 04 must include closeout for step 02"
    )
    assert f"{{closeout {stem03}.output}}" in prompt, (
        "Step 04 must include closeout for step 03"
    )


def test_build_prior_context_skipped_step_shows_commit_line() -> None:
    """_build_prior_context includes 'Commit: <short-sha>' for resume-skipped steps."""
    from norn.pipelines.implement_features_v3 import _build_prior_context

    skipped_files = ["/some/dir/step-01-v2-fixture-alpha.md"]
    done_shas = {"step-01-v2-fixture-alpha": "abc1234567890abcdef1234567890abcdef12345"}
    result = _build_prior_context([], skipped_files, done_shas)

    assert "## Prior steps" in result
    short_sha = "abc1234"  # first 7 chars
    assert f"Commit: {short_sha}" in result, (
        f"Expected 'Commit: {short_sha}' in prior context: {result!r}"
    )
    # Skipped step must NOT appear as a placeholder (its session is gone).
    assert "{facts step-01-v2-fixture-alpha.output}" not in result
    assert "{closeout step-01-v2-fixture-alpha.output}" not in result


@pytest.mark.asyncio
async def test_resume_skipped_step_excluded_from_pipeline(tmp_path, monkeypatch) -> None:
    """A step with a matching refactor commit is skipped and its prompt uses the ledger line."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    # Create a commit whose subject matches the resume pattern for step 01.
    stem01 = "step-01-v2-fixture-alpha"
    (repo_path / "dummy.py").write_text("# dummy\n")
    repo.index.add(["dummy.py"])
    repo.index.commit(f"refactor: {stem01} \u2014 Alpha fixture")

    commit_sha = repo.head.commit.hexsha
    short_sha = commit_sha[:7]

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    # Pipeline must have no stages for the skipped step.
    names = [getattr(i, "name", None) for i in mod.config.items]
    assert f"implement {stem01}" not in names, "Skipped step must not generate pipeline stages"

    # Step 02's implement prompt must show the short-sha ledger line.
    prompt = _get_implement_prompt(mod, "step-02-v2-fixture-beta")
    assert f"Commit: {short_sha}" in prompt, (
        f"Step 02 prompt must contain 'Commit: {short_sha}' for skipped step 01"
    )
    # Must NOT contain the dynamic placeholder for a skipped step.
    assert f"{{facts {stem01}.output}}" not in prompt, (
        "Skipped step must not appear as a facts placeholder"
    )
    assert f"{{closeout {stem01}.output}}" not in prompt, (
        "Skipped step must not appear as a closeout placeholder"
    )


# ---------------------------------------------------------------------------
# Commit shell structural tests (v1 carry-over)
# ---------------------------------------------------------------------------


def test_commit_shell_has_two_git_commit_attempts() -> None:
    """The commit command must contain exactly two `git commit -F -` invocations."""
    mod = _import_v2(GOOD_DIR)
    stem = "step-01-v2-fixture-alpha"
    cmd = _stage(mod, f"commit {stem}").impl.cmd
    assert cmd.count("git commit -F -") == 2, (
        f"Expected exactly 2 'git commit -F -', got {cmd.count('git commit -F -')}"
    )


def test_commit_shell_has_two_hook_fixes() -> None:
    """hook-fixes must appear twice (before first commit and before retry)."""
    mod = _import_v2(GOOD_DIR)
    stem = "step-01-v2-fixture-alpha"
    cmd = _stage(mod, f"commit {stem}").impl.cmd
    assert cmd.count("hook-fixes") == 2, (
        f"Expected 2 'hook-fixes', got {cmd.count('hook-fixes')}"
    )


def test_commit_shell_hook_fixes_before_first_commit() -> None:
    """The first hook-fixes invocation must come before the first git commit."""
    mod = _import_v2(GOOD_DIR)
    stem = "step-01-v2-fixture-alpha"
    cmd = _stage(mod, f"commit {stem}").impl.cmd
    first_hook = cmd.index("hook-fixes")
    first_commit = cmd.index("git commit -F -")
    assert first_hook < first_commit, "hook-fixes must precede the first commit attempt"


def test_commit_shell_retry_reads_git_state() -> None:
    """The retry block re-runs hook-fixes (reads git state), not the changed list."""
    mod = _import_v2(GOOD_DIR)
    stem = "step-01-v2-fixture-alpha"
    cmd = _stage(mod, f"commit {stem}").impl.cmd
    # After the first commit attempt, the retry block should call hook-fixes again
    first_commit_pos = cmd.index("git commit -F -")
    after_first = cmd[first_commit_pos + len("git commit -F -"):]
    assert "hook-fixes" in after_first, "retry block must re-read git state via hook-fixes"


def test_commit_shell_git_add_failure_aborts() -> None:
    """A failing `git add` must abort the commit (exit 1), not fall through."""
    mod = _import_v2(GOOD_DIR)
    stem = "step-01-v2-fixture-alpha"
    cmd = _stage(mod, f"commit {stem}").impl.cmd
    assert "git add failed" in cmd, "commit shell must abort on git add failure"


# ---------------------------------------------------------------------------
# Temp-repo commit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_happy_path(tmp_path, monkeypatch) -> None:
    """Happy path: record baseline → write file → commit → HEAD advances."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    # Record baseline
    r = await _stage(mod, f"record baseline {stem}").impl.run(ctx)
    assert r.success, f"record baseline failed: {r.error}"
    head_before = repo.head.commit.hexsha

    # Write a new file
    new_file = repo_path / "feature.py"
    new_file.write_text("# feature\n")

    # Commit
    r = await _stage(mod, f"commit {stem}").impl.run(ctx)
    assert r.success, f"commit failed: {r.output}"

    # HEAD advanced by one
    assert repo.head.commit.hexsha != head_before
    assert repo.head.commit.parents[0].hexsha == head_before

    # Subject matches
    assert "refactor:" in repo.head.commit.message
    assert stem in repo.head.commit.message

    # Facts file exists with three lines
    facts_path = Path(mod.snapshot_root) / f"{stem}.facts"
    assert facts_path.exists()
    facts_lines = facts_path.read_text().strip().splitlines()
    assert len(facts_lines) == 3
    assert facts_lines[0].startswith("Commit:")
    assert facts_lines[1].startswith("Changed:")
    assert facts_lines[2].startswith("Validation:")

    # Hookfix marker should not exist (no hooks in this repo)
    marker = Path(mod.snapshot_root) / f"{stem}.hookfix.marker"
    assert not marker.exists()

    # Assert committed should succeed
    r = await _stage(mod, f"assert committed {stem}").impl.run(ctx)
    assert r.success, f"assert committed failed: {r.output}"


@pytest.mark.asyncio
async def test_commit_dirty_file_modified_during_step(tmp_path, monkeypatch) -> None:
    """P0-2b regression: a pre-existing dirty file edited during the step is committed."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)

    # Create a tracked file, commit it, then modify it (making it ` M`)
    tracked = repo_path / "tracked.py"
    tracked.write_text("# original\n")
    repo.index.add(["tracked.py"])
    repo.index.commit("add tracked.py")

    tracked.write_text("# modified before step\n")
    # Now tracked.py is ` M` (worktree modified, not staged)

    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    # Record baseline (captures the ` M` state with content hash)
    r = await _stage(mod, f"record baseline {stem}").impl.run(ctx)
    assert r.success

    # Now modify tracked.py again (still ` M` but different content — the P0-2b gap)
    tracked.write_text("# modified during step\n")

    # Commit
    r = await _stage(mod, f"commit {stem}").impl.run(ctx)
    assert r.success, f"commit failed: {r.output}"

    # The file should be in the commit
    committed_files = list(repo.head.commit.stats.files.keys())
    assert "tracked.py" in committed_files, (
        f"tracked.py should be in the commit; got {committed_files}"
    )


@pytest.mark.asyncio
async def test_commit_dirty_file_not_touched_excluded(tmp_path, monkeypatch) -> None:
    """A pre-existing dirty file NOT touched during the step is NOT committed."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)

    # Create and track a file, then dirty it
    tracked = repo_path / "untouched.py"
    tracked.write_text("# original\n")
    repo.index.add(["untouched.py"])
    repo.index.commit("add untouched.py")
    tracked.write_text("# dirty but not touched\n")

    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    r = await _stage(mod, f"record baseline {stem}").impl.run(ctx)
    assert r.success

    # Write a DIFFERENT file (so there's something to commit)
    (repo_path / "other.py").write_text("# other\n")

    r = await _stage(mod, f"commit {stem}").impl.run(ctx)
    assert r.success, f"commit failed: {r.output}"

    committed_files = list(repo.head.commit.stats.files.keys())
    assert "untouched.py" not in committed_files, (
        f"untouched.py should NOT be in the commit; got {committed_files}"
    )
    assert "other.py" in committed_files


@pytest.mark.asyncio
async def test_commit_no_changes_fails(tmp_path, monkeypatch) -> None:
    """Commit fails when require_changes=True and nothing changed."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    r = await _stage(mod, f"record baseline {stem}").impl.run(ctx)
    assert r.success

    # Don't write anything → commit should fail
    r = await _stage(mod, f"commit {stem}").impl.run(ctx)
    assert not r.success, "commit should fail when nothing changed"
    out = r.output if isinstance(r.output, str) else str(r.output)
    assert "no step-owned changes" in out.lower() or "no step-owned changes" in str(r.output).lower(), (
        f"Expected 'no step-owned changes' in output: {r.output}"
    )


@pytest.mark.asyncio
async def test_commit_fails_when_head_moved(tmp_path, monkeypatch) -> None:
    """Commit fails when HEAD moved between baseline and commit."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    r = await _stage(mod, f"record baseline {stem}").impl.run(ctx)
    assert r.success

    # Make a commit to move HEAD
    (repo_path / "move.txt").write_text("move\n")
    repo.index.add(["move.txt"])
    repo.index.commit("move HEAD")

    # Write a file and try to commit — should fail
    (repo_path / "feature.py").write_text("# feature\n")
    r = await _stage(mod, f"commit {stem}").impl.run(ctx)
    assert not r.success, "commit should fail when HEAD moved"


@pytest.mark.asyncio
async def test_commit_with_hook_rewrite(tmp_path, monkeypatch) -> None:
    """A pre-commit hook that rewrites → first attempt fails, retry succeeds, marker created."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)

    # Write a pre-commit hook that appends a line on first call and exits 1,
    # then exits 0 on subsequent calls (uses a flag file).
    hooks_dir = repo_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    flag = tmp_path / "hook_ran_once"
    hook.write_text(
        f'#!/bin/sh\n'
        f'if [ ! -f {shlex.quote(str(flag))} ]; then\n'
        f'  touch {shlex.quote(str(flag))}\n'
        f'  # Append a line to a staged file to simulate auto-formatting\n'
        f'  for f in $(git diff --cached --name-only); do\n'
        f'    echo "# hook-added" >> "$f"\n'
        f'  done\n'
        f'  exit 1\n'
        f'fi\n'
        f'exit 0\n'
    )
    hook.chmod(0o755)

    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    r = await _stage(mod, f"record baseline {stem}").impl.run(ctx)
    assert r.success

    # Write a new file
    (repo_path / "feature.py").write_text("# feature\n")

    r = await _stage(mod, f"commit {stem}").impl.run(ctx)
    assert r.success, f"commit should succeed on retry: {r.output}"

    # The hookfix marker should exist
    marker = Path(mod.snapshot_root) / f"{stem}.hookfix.marker"
    assert marker.exists(), "hookfix marker should exist after hook rewrite"

    # Revalidate's `when` should be true
    rev_stage = _stage(mod, f"revalidate {stem}")
    assert rev_stage.when(PipelineContext()) is True


@pytest.mark.asyncio
async def test_commit_unicode_filename(tmp_path, monkeypatch) -> None:
    """A file with spaces and Unicode in its name is committed correctly."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    r = await _stage(mod, f"record baseline {stem}").impl.run(ctx)
    assert r.success

    # Create a file with spaces and Unicode
    special = repo_path / "hello w\u00f6rld.py"
    special.write_text("# special\n")

    r = await _stage(mod, f"commit {stem}").impl.run(ctx)
    assert r.success, f"commit failed with special filename: {r.output}"

    # Verify the file is in the commit (git may quote non-ASCII filenames)
    committed_files = list(repo.head.commit.stats.files.keys())
    assert any("hello" in f and "rld" in f for f in committed_files), (
        f"Unicode filename not in commit: {committed_files}"
    )


@pytest.mark.asyncio
async def test_assert_committed_fails_when_path_dirtied(tmp_path, monkeypatch) -> None:
    """assert committed fails when a committed path is modified afterwards."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    # Record baseline, write a file, commit
    r = await _stage(mod, f"record baseline {stem}").impl.run(ctx)
    assert r.success
    feat_file = repo_path / "feature.py"
    feat_file.write_text("# feature\n")
    r = await _stage(mod, f"commit {stem}").impl.run(ctx)
    assert r.success

    # Now dirty the committed file
    feat_file.write_text("# dirtied after commit\n")

    # assert committed should fail
    r = await _stage(mod, f"assert committed {stem}").impl.run(ctx)
    assert not r.success, "assert committed should fail when a committed path is dirtied"


@pytest.mark.asyncio
async def test_facts_readfile_returns_three_line_block(tmp_path, monkeypatch) -> None:
    """After commit succeeds, the facts ReadFile stage returns the three-line block."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    # Record baseline, write a file, commit
    r = await _stage(mod, f"record baseline {stem}").impl.run(ctx)
    assert r.success, f"record baseline failed: {r.error}"
    (repo_path / "feature.py").write_text("# feature\n")
    r = await _stage(mod, f"commit {stem}").impl.run(ctx)
    assert r.success, f"commit failed: {r.output}"

    # Run the facts ReadFile stage
    facts_stage = _stage(mod, f"facts {stem}")
    r = await facts_stage.impl.run(ctx)
    assert r.success, f"facts stage failed: {r.error}"
    assert isinstance(r.output, str), "facts output must be a str"
    lines = r.output.strip().splitlines()
    assert len(lines) == 3, f"facts output must be three lines, got {len(lines)}: {r.output!r}"
    assert lines[0].startswith("Commit:"), f"line 0 must start with 'Commit:': {lines[0]!r}"
    assert lines[1].startswith("Changed:"), f"line 1 must start with 'Changed:': {lines[1]!r}"
    assert lines[2].startswith("Validation:"), f"line 2 must start with 'Validation:': {lines[2]!r}"


# ---------------------------------------------------------------------------
# Step-06 structural tests: aggregate validation phase
# ---------------------------------------------------------------------------


def _make_no_final_test_cmd_dir(tmp_path: Path) -> Path:
    """Return a fixture dir identical to good/ but with index.md that has no front matter."""
    feat_dir = tmp_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)
    # Strip front matter from index.md — removes final_test_cmd
    (feat_dir / "index.md").write_text("# Fixture feature (v2)\n\nNo front matter.\n")
    return feat_dir


def test_aggregate_cmd_equals_final_test_cmd_for_good_fixture() -> None:
    """good/ declares final_test_cmd → AGGREGATE_CMD must equal it."""
    mod = _import_v2(GOOD_DIR)
    assert mod.AGGREGATE_CMD == 'python3 -c "pass"', (
        f"AGGREGATE_CMD should equal final_test_cmd, got {mod.AGGREGATE_CMD!r}"
    )


def test_aggregate_timeout_equals_default_when_final_test_cmd(tmp_path) -> None:
    """When final_test_cmd is set, AGGREGATE_TIMEOUT is DEFAULT_TEST_TIMEOUT."""
    mod = _import_v2(GOOD_DIR)
    assert mod.AGGREGATE_TIMEOUT == mod.DEFAULT_TEST_TIMEOUT, (
        f"AGGREGATE_TIMEOUT should be DEFAULT_TEST_TIMEOUT when final_test_cmd is declared, "
        f"got {mod.AGGREGATE_TIMEOUT}"
    )


def test_aggregate_cmd_fallback_contains_first_step_marker(tmp_path) -> None:
    """Fallback AGGREGATE_CMD contains the first step's echo marker."""
    feat_dir = _make_no_final_test_cmd_dir(tmp_path)
    mod = _import_v2(feat_dir)
    assert mod.FINAL_TEST_CMD is None, "sanity: no final_test_cmd for this fixture copy"
    assert "step-01-v2-fixture-alpha" in mod.AGGREGATE_CMD, (
        f"Fallback AGGREGATE_CMD must contain step-01 marker, got: {mod.AGGREGATE_CMD!r}"
    )


def test_aggregate_cmd_fallback_contains_step_with_bats_marker(tmp_path) -> None:
    """Fallback AGGREGATE_CMD contains the step-02 marker (bats_cmd makes it unique)."""
    feat_dir = _make_no_final_test_cmd_dir(tmp_path)
    mod = _import_v2(feat_dir)
    assert "step-02-v2-fixture-beta" in mod.AGGREGATE_CMD, (
        f"Fallback AGGREGATE_CMD must contain step-02 marker, got: {mod.AGGREGATE_CMD!r}"
    )


def test_aggregate_cmd_fallback_deduplicates_identical_commands(tmp_path) -> None:
    """Fallback AGGREGATE_CMD deduplicates: steps with same command appear only once."""
    feat_dir = _make_no_final_test_cmd_dir(tmp_path)
    mod = _import_v2(feat_dir)
    # step-01, -03, -04 all have test_cmd='python3 -c "pass"' and no bats_cmd.
    # They deduplicate to one entry under step-01's marker.
    assert "step-03-v2-fixture-gamma" not in mod.AGGREGATE_CMD, (
        "step-03 command is identical to step-01 — it must be deduplicated"
    )
    assert "step-04-v2-fixture-delta" not in mod.AGGREGATE_CMD, (
        "step-04 command is identical to step-01 — it must be deduplicated"
    )


def test_aggregate_cmd_fallback_in_file_order(tmp_path) -> None:
    """Fallback AGGREGATE_CMD has step-01 marker before step-02 marker."""
    feat_dir = _make_no_final_test_cmd_dir(tmp_path)
    mod = _import_v2(feat_dir)
    idx01 = mod.AGGREGATE_CMD.find("step-01-v2-fixture-alpha")
    idx02 = mod.AGGREGATE_CMD.find("step-02-v2-fixture-beta")
    assert idx01 >= 0, "step-01 marker missing"
    assert idx02 >= 0, "step-02 marker missing"
    assert idx01 < idx02, "step-01 must appear before step-02 in AGGREGATE_CMD"


def test_aggregate_cmd_fallback_includes_resume_skipped_step_command(
    tmp_path, monkeypatch
) -> None:
    """Aggregate fallback command includes all steps even when some are resume-skipped."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)
    # No final_test_cmd → trigger fallback aggregation
    (feat_dir / "index.md").write_text("# Fixture feature (v2)\n\nNo front matter.\n")

    # Commit matching step-01's resume pattern so it appears in already_committed_steps()
    (repo_path / "dummy.py").write_text("# dummy\n")
    repo.index.add(["dummy.py"])
    repo.index.commit("refactor: step-01-v2-fixture-alpha \u2014 Alpha fixture")

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    # step-01 should have been resume-skipped (excluded from step_files)
    assert any("step-01-v2-fixture-alpha" in f for f in mod.skipped_for_resume), (
        "step-01 should be resume-skipped"
    )
    # AGGREGATE_CMD must still contain the skipped step's marker
    assert "step-01-v2-fixture-alpha" in mod.AGGREGATE_CMD, (
        "AGGREGATE_CMD must include resume-skipped step's command "
        f"(got: {mod.AGGREGATE_CMD!r})"
    )


# ---------------------------------------------------------------------------
# Aggregate loop configuration
# ---------------------------------------------------------------------------


def test_loop_aggregate_max_retries() -> None:
    """loop aggregate has max_retries == 3."""
    mod = _import_v2(GOOD_DIR)
    lp = _loop(mod, "loop aggregate")
    assert lp.max_retries == 3, f"loop aggregate max_retries should be 3, got {lp.max_retries}"


def test_loop_aggregate_new_session() -> None:
    """loop aggregate has new_session=True."""
    mod = _import_v2(GOOD_DIR)
    lp = _loop(mod, "loop aggregate")
    assert lp.new_session is True, "loop aggregate must have new_session=True"


def test_loop_aggregate_on_exhaust_ask_user() -> None:
    """loop aggregate on_exhaust is ask_user."""
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    lp = _loop(mod, "loop aggregate")
    assert lp.on_exhaust == OnFailure.ASK_USER, (
        f"loop aggregate on_exhaust should be ASK_USER, got {lp.on_exhaust}"
    )


def test_loop_aggregate_has_three_stages() -> None:
    """loop aggregate has exactly: aggregate compress, aggregate fix, aggregate run."""
    mod = _import_v2(GOOD_DIR)
    lp = _loop(mod, "loop aggregate")
    names = [s.name for s in lp.stages]
    assert names == ["aggregate compress", "aggregate fix", "aggregate run"], (
        f"Unexpected loop aggregate stage names: {names!r}"
    )


def test_aggregate_fix_when_truthy_after_failed_aggregate_run() -> None:
    """aggregate fix `when` returns True after a failed aggregate run."""
    from norn.models import PipelineContext, StageResult

    mod = _import_v2(GOOD_DIR)
    fix_stage = _stage(mod, "aggregate fix")
    assert fix_stage.when is not None

    ctx = PipelineContext()
    ctx.results["aggregate run"] = StageResult(
        name="aggregate run", success=False, output="failed"
    )
    assert fix_stage.when(ctx) is True


def test_aggregate_fix_when_falsy_after_successful_aggregate_run() -> None:
    """aggregate fix `when` returns False after a successful aggregate run."""
    from norn.models import PipelineContext, StageResult

    mod = _import_v2(GOOD_DIR)
    fix_stage = _stage(mod, "aggregate fix")

    ctx = PipelineContext()
    ctx.results["aggregate run"] = StageResult(
        name="aggregate run", success=True, output="passed"
    )
    assert fix_stage.when(ctx) is False


def test_aggregate_fix_when_falsy_with_no_prior_result() -> None:
    """aggregate fix `when` returns False when aggregate run has not run yet."""
    from norn.models import PipelineContext

    mod = _import_v2(GOOD_DIR)
    fix_stage = _stage(mod, "aggregate fix")

    ctx = PipelineContext()
    assert fix_stage.when(ctx) is False


def test_aggregate_fix_model_follows_aggregate_model_knob() -> None:
    """aggregate fix model follows --arg aggregate_model=deep."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR, "--arg", "aggregate_model=deep")
    fix_stage = _stage(mod, "aggregate fix")
    assert isinstance(fix_stage.impl, Generate)
    assert fix_stage.impl.model == "deep", (
        f"aggregate fix model should be 'deep', got {fix_stage.impl.model!r}"
    )


def test_aggregate_compress_has_summarize_with_haiku_false() -> None:
    """aggregate compress CompressTestLog has summarize_with_haiku=False."""
    from norn.stages.compress_test_log import CompressTestLog

    mod = _import_v2(GOOD_DIR)
    compress_stage = _stage(mod, "aggregate compress")
    assert isinstance(compress_stage.impl, CompressTestLog)
    assert compress_stage.impl.summarize_with_haiku is False


# ---------------------------------------------------------------------------
# Aggregate gate and postcondition stages
# ---------------------------------------------------------------------------


def test_aggregate_passed_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "aggregate passed")
    assert s.on_failure == OnFailure.FAIL, (
        f"aggregate passed on_failure should be FAIL, got {s.on_failure}"
    )


def test_aggregate_commit_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "aggregate commit")
    assert s.on_failure == OnFailure.FAIL, (
        f"aggregate commit on_failure should be FAIL, got {s.on_failure}"
    )


def test_aggregate_committed_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "aggregate committed")
    assert s.on_failure == OnFailure.FAIL, (
        f"aggregate committed on_failure should be FAIL, got {s.on_failure}"
    )


def test_aggregate_stages_exist_after_loop_in_order() -> None:
    """aggregate passed → aggregate commit → aggregate committed exist after loop aggregate."""
    mod = _import_v2(GOOD_DIR)
    names = [getattr(i, "name", None) for i in mod.config.items]
    lp_idx = next(
        i for i, item in enumerate(mod.config.items)
        if getattr(item, "name", None) == "loop aggregate"
    )
    ap_idx = names.index("aggregate passed")
    ac_idx = names.index("aggregate commit")
    acd_idx = names.index("aggregate committed")
    assert lp_idx < ap_idx < ac_idx < acd_idx, (
        f"Expected loop aggregate < aggregate passed < aggregate commit < aggregate committed, "
        f"got indices {lp_idx}, {ap_idx}, {ac_idx}, {acd_idx}"
    )


def test_aggregate_passed_not_named_like_any_loop_stage() -> None:
    """aggregate passed must not collide with any stage name in loop aggregate."""
    mod = _import_v2(GOOD_DIR)
    lp = _loop(mod, "loop aggregate")
    loop_names = {s.name for s in lp.stages}
    assert "aggregate passed" not in loop_names, (
        "aggregate passed must not collide with loop aggregate stage names"
    )


def test_aggregate_commit_shell_require_changes_false() -> None:
    """aggregate commit shell includes the 'nothing to commit' no-change branch."""
    mod = _import_v2(GOOD_DIR)
    cmd = _stage(mod, "aggregate commit").impl.cmd
    assert "nothing to commit" in cmd, (
        "aggregate commit shell must include 'nothing to commit' (require_changes=False)"
    )
    # Must NOT include the require_changes=True error
    assert "no step-owned changes to commit" not in cmd, (
        "aggregate commit must not use require_changes=True logic"
    )


# ---------------------------------------------------------------------------
# Aggregate fix prompt content
# ---------------------------------------------------------------------------


def test_aggregate_fix_prompt_contains_aggregate_cmd() -> None:
    """aggregate fix prompt contains the exact AGGREGATE_CMD verbatim."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    fix_stage = _stage(mod, "aggregate fix")
    assert isinstance(fix_stage.impl, Generate)
    prompt = fix_stage.impl.prompt
    assert mod.AGGREGATE_CMD in prompt, (
        f"aggregate fix prompt must contain AGGREGATE_CMD verbatim.\n"
        f"AGGREGATE_CMD: {mod.AGGREGATE_CMD!r}\n"
        f"Prompt excerpt: {prompt[:300]!r}"
    )


@pytest.mark.parametrize("verb", _FORBIDDEN_VERBS)
def test_aggregate_fix_prompt_contains_forbidden_verb(verb: str) -> None:
    """aggregate fix prompt contains every forbidden git verb."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    fix_stage = _stage(mod, "aggregate fix")
    assert isinstance(fix_stage.impl, Generate)
    assert verb in fix_stage.impl.prompt, (
        f"aggregate fix prompt missing forbidden verb {verb!r}"
    )


def test_aggregate_fix_prompt_references_failure_output_placeholder() -> None:
    """aggregate fix prompt has the {aggregate compress.output} placeholder."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    fix_stage = _stage(mod, "aggregate fix")
    assert isinstance(fix_stage.impl, Generate)
    assert "{aggregate compress.output}" in fix_stage.impl.prompt


def test_aggregate_fix_prompt_repairs_stale_test_files_and_reports_stale_inline_checks() -> None:
    """A stale assertion in a test file is re-targeted; one inline in the frozen command is reported."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    fix_stage = _stage(mod, "aggregate fix")
    assert isinstance(fix_stage.impl, Generate)
    prompt = fix_stage.impl.prompt
    assert "frozen for the rest of the run" in prompt
    assert "In a test file in the repository: update the assertion" in prompt
    assert "never delete it or loosen it" in prompt
    assert "Inline in the command above" in prompt
    assert "make no change and say so" in prompt


def test_aggregate_fix_timeout_is_1200() -> None:
    """aggregate fix stage-level timeout is 1200."""
    mod = _import_v2(GOOD_DIR)
    fix_stage = _stage(mod, "aggregate fix")
    assert fix_stage.timeout == 1200, (
        f"aggregate fix timeout should be 1200, got {fix_stage.timeout}"
    )


# ---------------------------------------------------------------------------
# Temp-repo aggregate shell-stage tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregate_baseline_writes_files(tmp_path, monkeypatch) -> None:
    """aggregate baseline writes agg_head and aggregate.pre.json."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    ctx = PipelineContext()
    r = await _stage(mod, "aggregate baseline").impl.run(ctx)
    assert r.success, f"aggregate baseline failed: {r.error}"

    agg_head_path = Path(mod.snapshot_root) / "aggregate.head"
    agg_pre_path = Path(mod.snapshot_root) / "aggregate.pre.json"
    assert agg_head_path.exists(), "aggregate.head must be written"
    assert agg_pre_path.exists(), "aggregate.pre.json must be written"
    assert len(agg_head_path.read_text().strip()) == 40, "aggregate.head must contain a 40-char SHA"


@pytest.mark.asyncio
async def test_aggregate_commit_no_changes_succeeds(tmp_path, monkeypatch) -> None:
    """aggregate commit with no changes exits 0 ('nothing to commit', require_changes=False)."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    ctx = PipelineContext()
    head_before = repo.head.commit.hexsha

    r = await _stage(mod, "aggregate baseline").impl.run(ctx)
    assert r.success, f"aggregate baseline failed: {r.error}"

    # No changes made — commit should succeed with "nothing to commit"
    r = await _stage(mod, "aggregate commit").impl.run(ctx)
    assert r.success, f"aggregate commit (no changes) failed: {r.output}"
    stdout = (r.output or {}).get("stdout", "") if isinstance(r.output, dict) else str(r.output)
    assert "nothing to commit" in stdout.lower(), (
        f"Expected 'nothing to commit' in output, got: {stdout!r}"
    )

    # HEAD must not have moved
    assert repo.head.commit.hexsha == head_before, "HEAD must not move when nothing to commit"

    # aggregate committed must succeed
    r = await _stage(mod, "aggregate committed").impl.run(ctx)
    assert r.success, f"aggregate committed failed after no-change commit: {r.output}"


@pytest.mark.asyncio
async def test_aggregate_commit_with_changes_creates_fix_commit(tmp_path, monkeypatch) -> None:
    """aggregate commit with a changed file creates a 'fix: aggregate' commit."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    ctx = PipelineContext()
    head_before = repo.head.commit.hexsha

    r = await _stage(mod, "aggregate baseline").impl.run(ctx)
    assert r.success, f"aggregate baseline failed: {r.error}"

    # Write a file to simulate an aggregate fix
    (repo_path / "agg_fix.py").write_text("# aggregate fix\n")

    r = await _stage(mod, "aggregate commit").impl.run(ctx)
    assert r.success, f"aggregate commit (with changes) failed: {r.output}"

    # HEAD advanced by one
    assert repo.head.commit.hexsha != head_before, "HEAD must advance after aggregate commit"
    assert repo.head.commit.parents[0].hexsha == head_before, "Parent must be the baseline"

    # Subject must be the fix: pattern
    assert repo.head.commit.message.startswith("fix: aggregate validation repair"), (
        f"Unexpected commit subject: {repo.head.commit.message!r}"
    )

    # aggregate committed must succeed
    r = await _stage(mod, "aggregate committed").impl.run(ctx)
    assert r.success, f"aggregate committed failed after fix commit: {r.output}"


@pytest.mark.asyncio
async def test_aggregate_committed_fails_when_owned_path_dirtied(tmp_path, monkeypatch) -> None:
    """aggregate committed fails when an aggregate-owned path is dirtied after commit."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    ctx = PipelineContext()

    r = await _stage(mod, "aggregate baseline").impl.run(ctx)
    assert r.success

    fix_file = repo_path / "agg_fix.py"
    fix_file.write_text("# aggregate fix\n")

    r = await _stage(mod, "aggregate commit").impl.run(ctx)
    assert r.success, f"aggregate commit failed: {r.output}"

    # Dirty the committed file
    fix_file.write_text("# dirtied after commit\n")

    r = await _stage(mod, "aggregate committed").impl.run(ctx)
    assert not r.success, "aggregate committed should fail when owned path is dirtied"


# ===========================================================================
# Step-07 structural tests: review gate
# ===========================================================================


# ---------------------------------------------------------------------------
# Review round wiring and stage ordering
# ---------------------------------------------------------------------------


def test_review_preflight_exists_after_aggregate_committed() -> None:
    """review preflight exists after aggregate committed and before round 1."""
    mod = _import_v2(GOOD_DIR)
    names = [getattr(i, "name", None) for i in mod.config.items]
    acd_idx = names.index("aggregate committed")
    rp_idx = names.index("review preflight")
    r1_idx = names.index("review manifest 1")
    assert acd_idx < rp_idx < r1_idx


def test_review_preflight_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure
    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "review preflight")
    assert s.on_failure == OnFailure.FAIL


def test_review_rounds_exist_in_order() -> None:
    """Rounds 1, 2, 3 appear in order, with fix rounds only for 1 and 2."""
    mod = _import_v2(GOOD_DIR)
    names = [getattr(i, "name", None) for i in mod.config.items]

    # All review round stages
    for n in range(1, 4):
        for sn in [
            f"review manifest {n}",
            f"read review manifest {n}",
            f"review {n}",
            f"review postcondition {n}",
            f"check review {n}",
        ]:
            assert sn in names, f"Stage {sn!r} missing from pipeline"

    # Fix rounds only for 1 and 2
    for n in range(1, 3):
        for sn in [
            f"review fix baseline {n}",
            f"review fix {n}",
            f"review fix validated {n}",
            f"review fix commit {n}",
            f"review fix committed {n}",
        ]:
            assert sn in names, f"Stage {sn!r} missing from pipeline"

    # No fix round 3
    assert "review fix baseline 3" not in names
    assert "review fix 3" not in names

    # review passed at the end
    assert "review passed" in names

    # Ordering: round 1 before round 2 before round 3 before review passed
    idx1 = names.index("review manifest 1")
    idx2 = names.index("review manifest 2")
    idx3 = names.index("review manifest 3")
    idxp = names.index("review passed")
    assert idx1 < idx2 < idx3 < idxp


def test_review_passed_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure
    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "review passed")
    assert s.on_failure == OnFailure.FAIL


def test_review_on_failure_is_ask_user() -> None:
    """review Generate stages have on_failure=ask_user (transient SDK errors)."""
    from norn.dsl import OnFailure
    mod = _import_v2(GOOD_DIR)
    for n in range(1, 4):
        s = _stage(mod, f"review {n}")
        assert s.on_failure == OnFailure.ASK_USER, (
            f"review {n} on_failure should be ASK_USER"
        )


def test_review_fix_on_failure_is_ask_user() -> None:
    """review fix Generate stages have on_failure=ask_user."""
    from norn.dsl import OnFailure
    mod = _import_v2(GOOD_DIR)
    for n in range(1, 3):
        s = _stage(mod, f"review fix {n}")
        assert s.on_failure == OnFailure.ASK_USER, (
            f"review fix {n} on_failure should be ASK_USER"
        )


def test_check_review_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure
    mod = _import_v2(GOOD_DIR)
    for n in range(1, 4):
        s = _stage(mod, f"check review {n}")
        assert s.on_failure == OnFailure.FAIL


def test_review_postcondition_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure
    mod = _import_v2(GOOD_DIR)
    for n in range(1, 4):
        s = _stage(mod, f"review postcondition {n}")
        assert s.on_failure == OnFailure.FAIL


def test_review_fix_validated_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure
    mod = _import_v2(GOOD_DIR)
    for n in range(1, 3):
        s = _stage(mod, f"review fix validated {n}")
        assert s.on_failure == OnFailure.FAIL


def test_review_fix_commit_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure
    mod = _import_v2(GOOD_DIR)
    for n in range(1, 3):
        s = _stage(mod, f"review fix commit {n}")
        assert s.on_failure == OnFailure.FAIL


def test_review_fix_committed_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure
    mod = _import_v2(GOOD_DIR)
    for n in range(1, 3):
        s = _stage(mod, f"review fix committed {n}")
        assert s.on_failure == OnFailure.FAIL


# ---------------------------------------------------------------------------
# Round `when` predicates
# ---------------------------------------------------------------------------


def test_round_1_stages_have_when_none() -> None:
    """Round 1 stages have when=None (always run)."""
    mod = _import_v2(GOOD_DIR)
    for sn in [
        "review manifest 1",
        "read review manifest 1",
        "review 1",
        "review postcondition 1",
        "check review 1",
    ]:
        s = _stage(mod, sn)
        assert s.when is None, f"{sn}: when should be None for round 1"


def test_round_2_stages_have_when_truthy_when_marker_exists(tmp_path) -> None:
    """Round 2 stages' when is truthy only when review-1.needs-fixes exists."""
    from norn.models import PipelineContext
    mod = _import_v2(GOOD_DIR)
    # Find the marker path from the module
    marker_1 = os.path.join(mod.snapshot_root, "review-1.needs-fixes")
    os.makedirs(os.path.dirname(marker_1), exist_ok=True)

    ctx = PipelineContext()

    # Without marker → falsy
    if os.path.exists(marker_1):
        os.unlink(marker_1)
    s = _stage(mod, "review manifest 2")
    assert s.when is not None, "round 2 must have a when predicate"
    assert s.when(ctx) is False, "round 2 when should be False without marker"

    # With marker → truthy
    Path(marker_1).touch()
    try:
        assert s.when(ctx) is True, "round 2 when should be True with marker"
    finally:
        os.unlink(marker_1)


def test_round_3_stages_gated_on_round_2_marker() -> None:
    """Round 3 stages are gated on review-2.needs-fixes marker."""
    from norn.models import PipelineContext
    mod = _import_v2(GOOD_DIR)
    marker_2 = os.path.join(mod.snapshot_root, "review-2.needs-fixes")
    os.makedirs(os.path.dirname(marker_2), exist_ok=True)

    ctx = PipelineContext()

    s = _stage(mod, "review manifest 3")
    assert s.when is not None

    if os.path.exists(marker_2):
        os.unlink(marker_2)
    assert s.when(ctx) is False

    Path(marker_2).touch()
    try:
        assert s.when(ctx) is True
    finally:
        os.unlink(marker_2)


def test_review_fix_stages_gated_on_needs_fixes_marker() -> None:
    """review fix stages are gated on file_exists(needs_fixes marker)."""
    from norn.models import PipelineContext
    mod = _import_v2(GOOD_DIR)

    for n in range(1, 3):
        marker = os.path.join(mod.snapshot_root, f"review-{n}.needs-fixes")
        os.makedirs(os.path.dirname(marker), exist_ok=True)

        s = _stage(mod, f"review fix baseline {n}")
        assert s.when is not None, f"review fix baseline {n}: must have when predicate"

        ctx = PipelineContext()
        if os.path.exists(marker):
            os.unlink(marker)
        assert s.when(ctx) is False

        Path(marker).touch()
        try:
            assert s.when(ctx) is True
        finally:
            os.unlink(marker)


# ---------------------------------------------------------------------------
# Review Generate configuration
# ---------------------------------------------------------------------------


def test_review_generate_config() -> None:
    """review Generate has max_turns scaled to the plan, timeout=1800, no Edit, no Task, model follows knob."""
    from norn.stages.generate import Generate
    mod = _import_v2(GOOD_DIR)
    for n in range(1, 4):
        s = _stage(mod, f"review {n}")
        assert isinstance(s.impl, Generate)
        assert s.impl.max_turns == mod.MAX_TURNS_REVIEW, f"review {n}: max_turns should be MAX_TURNS_REVIEW"
        assert s.timeout == 1800, f"review {n}: timeout should be 1800"
        tools = s.impl.allowed_tools or []
        assert "Edit" not in tools, f"review {n}: should not have Edit tool"
        assert "Task" not in tools, f"review {n}: should not have Task tool"
        assert s.impl.model == "standard", f"review {n}: default model should be standard"


def test_review_model_follows_review_model_knob() -> None:
    """review stages model follows --arg review_model=deep."""
    from norn.stages.generate import Generate
    mod = _import_v2(GOOD_DIR, "--arg", "review_model=deep")
    for n in range(1, 4):
        s = _stage(mod, f"review {n}")
        assert isinstance(s.impl, Generate)
        assert s.impl.model == "deep", f"review {n}: model should be deep"


# ---------------------------------------------------------------------------
# Review prompt content
# ---------------------------------------------------------------------------


def test_review_prompt_names_base_sha() -> None:
    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "review 1")
    prompt = s.impl.prompt
    assert "base.sha" in prompt or mod.BASE_SHA in prompt


def test_review_prompt_names_review_md() -> None:
    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "review 1")
    prompt = s.impl.prompt
    assert "review.md" in prompt


def test_review_prompt_contains_both_verdict_spellings() -> None:
    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "review 1")
    prompt = s.impl.prompt
    assert "VERDICT: PASS" in prompt
    assert "VERDICT: NEEDS_FIXES" in prompt


def test_review_prompt_contains_manifest_placeholder() -> None:
    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "review 1")
    prompt = s.impl.prompt
    assert "{read review manifest 1.output}" in prompt


def test_review_prompt_contains_all_step_filenames() -> None:
    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "review 1")
    prompt = s.impl.prompt
    for stem in _GOOD_STEMS:
        assert f"{stem}.md" in prompt, f"review prompt must reference {stem}.md"


@pytest.mark.parametrize("n", [1, 2, 3])
def test_review_prompt_names_steps_without_pasting_them(n: int) -> None:
    """The plan is a directory the reviewer can read, not prompt payload."""
    mod = _import_v2(GOOD_DIR)
    prompt = _stage(mod, f"review {n}").impl.prompt
    body = (GOOD_DIR / "step-01-v2-fixture-alpha.md").read_text()
    quoted = next(ln for ln in body.splitlines() if len(ln.strip()) > 40)
    assert quoted not in prompt
    assert str(GOOD_DIR) in prompt


def test_review_round_2_prompt_mentions_round_number() -> None:
    """Round 2+ review prompt mentions the round number."""
    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "review 2")
    prompt = s.impl.prompt
    assert "round 2" in prompt.lower()


def test_review_round_1_reviews_the_whole_range() -> None:
    prompt = _stage(_import_v2(GOOD_DIR), "review 1").impl.prompt
    assert "every commit from that base to HEAD" in prompt
    assert "bounded" not in prompt


@pytest.mark.parametrize("n", [2, 3])
def test_later_rounds_verify_instead_of_re_reviewing(n: int) -> None:
    """An unbounded re-review never converges, so later rounds are bounded."""
    mod = _import_v2(GOOD_DIR)
    prompt = _stage(mod, f"review {n}").impl.prompt
    assert "Do **not** start a fresh review of the full range" in prompt
    assert f"Re-check every blocking finding from round {n - 1}" in prompt
    assert "Review the fix commits themselves" in prompt


@pytest.mark.parametrize("n", [2, 3])
def test_later_rounds_read_the_previous_review(n: int) -> None:
    mod = _import_v2(GOOD_DIR)
    read_stage = _stage(mod, f"read previous review {n}")
    assert read_stage.impl.path == mod._review_paths[n]["prev_review"]
    prompt = _stage(mod, f"review {n}").impl.prompt
    assert f"{{read previous review {n}.output}}" in prompt


def test_round_1_has_no_previous_review_stage() -> None:
    mod = _import_v2(GOOD_DIR)
    with pytest.raises(KeyError):
        _stage(mod, "read previous review 1")


@pytest.mark.parametrize("n", [2, 3])
def test_later_manifests_archive_the_previous_review_and_list_fix_commits(n: int) -> None:
    mod = _import_v2(GOOD_DIR)
    cmd = _stage(mod, f"review manifest {n}").impl.cmd
    prev = mod._review_paths[n - 1]
    assert f"cp {shlex.quote(mod.REVIEW_MD)} {shlex.quote(mod._review_paths[n]['prev_review'])}" in cmd
    assert shlex.quote(prev["head"]) in cmd
    assert f"## Fix commits since round {n - 1}" in cmd


def test_round_1_manifest_has_no_fix_commit_section() -> None:
    cmd = _stage(_import_v2(GOOD_DIR), "review manifest 1").impl.cmd
    assert "Fix commits" not in cmd


@pytest.mark.parametrize("n", [1, 2, 3])
def test_review_prompt_gates_on_blocking_severities(n: int) -> None:
    """Minor findings and nits must not hold the verdict."""
    prompt = _stage(_import_v2(GOOD_DIR), f"review {n}").impl.prompt
    assert "`VERDICT: PASS` when no critical or major finding remains" in prompt
    assert "## Blocking findings" in prompt
    assert "## Non-blocking" in prompt


@pytest.mark.parametrize("n", [1, 2, 3])
def test_review_prompt_sends_out_of_scope_work_to_the_human(n: int) -> None:
    """The fix step has no git commands, so it cannot commit untouched files."""
    prompt = _stage(_import_v2(GOOD_DIR), f"review {n}").impl.prompt
    assert "## For the human" in prompt
    assert "Only the commits from the base SHA to HEAD are in scope" in prompt


def test_review_fix_prompt_applies_blocking_findings_only() -> None:
    prompt = _stage(_import_v2(GOOD_DIR), "review fix 1").impl.prompt
    assert "## Blocking findings" in prompt
    assert "`## Non-blocking` findings are" in prompt


# ---------------------------------------------------------------------------
# Review fix prompt content
# ---------------------------------------------------------------------------


def test_review_fix_prompt_names_review_md() -> None:
    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "review fix 1")
    prompt = s.impl.prompt
    assert "review.md" in prompt


def test_review_fix_prompt_contains_aggregate_cmd() -> None:
    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "review fix 1")
    prompt = s.impl.prompt
    assert mod.AGGREGATE_CMD in prompt


@pytest.mark.parametrize("verb", _FORBIDDEN_VERBS)
def test_review_fix_prompt_contains_forbidden_verb(verb: str) -> None:
    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "review fix 1")
    prompt = s.impl.prompt
    assert verb in prompt, f"review fix prompt missing forbidden verb {verb!r}"


def test_review_fix_timeout_is_1200() -> None:
    mod = _import_v2(GOOD_DIR)
    for n in range(1, 3):
        s = _stage(mod, f"review fix {n}")
        assert s.timeout == 1200, f"review fix {n}: timeout should be 1200"


def test_review_fix_model_follows_aggregate_model_knob() -> None:
    from norn.stages.generate import Generate
    mod = _import_v2(GOOD_DIR, "--arg", "aggregate_model=deep")
    for n in range(1, 3):
        s = _stage(mod, f"review fix {n}")
        assert isinstance(s.impl, Generate)
        assert s.impl.model == "deep"


# ---------------------------------------------------------------------------
# CompressTestLog count update (now 5 per-step + 1 aggregate = 6 total)
# Note: review does NOT have CompressTestLog; this is unchanged from step 6.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Temp-repo review shell-stage tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_manifest_writes_manifest(tmp_path, monkeypatch) -> None:
    """review manifest 1 writes a manifest whose first two lines are Base and Head SHAs."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()

    # Record start to write base.sha
    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success, f"record start failed: {r.output}"

    # Review manifest
    r = await _stage(mod, "review manifest 1").impl.run(ctx)
    assert r.success, f"review manifest 1 failed: {r.output}"

    manifest_path = os.path.join(mod.snapshot_root, "review-manifest-1.md")
    assert os.path.exists(manifest_path), "manifest file must exist"
    text = Path(manifest_path).read_text()
    lines = text.splitlines()
    assert lines[0].startswith("Base: "), f"line 0 must start with 'Base:', got {lines[0]!r}"
    assert lines[1].startswith("Head: "), f"line 1 must start with 'Head:', got {lines[1]!r}"

    # read review manifest 1 should return the manifest text
    r = await _stage(mod, "read review manifest 1").impl.run(ctx)
    assert r.success
    assert isinstance(r.output, str)
    assert "Base:" in r.output


@pytest.mark.asyncio
async def test_check_review_pass_no_marker(tmp_path, monkeypatch) -> None:
    """check review 1 with a well-formed PASS file: success, no marker."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    head_sha = repo.head.commit.hexsha
    base_sha = Path(mod.BASE_SHA).read_text().strip()

    # Write a well-formed PASS review.md
    review_md = os.path.join(str(feat_dir), "review.md")
    Path(review_md).write_text(
        f"VERDICT: PASS\n"
        f"Base: {base_sha}\n"
        f"Head: {head_sha}\n"
        f"\nNo issues found.\n"
    )

    r = await _stage(mod, "check review 1").impl.run(ctx)
    assert r.success, f"check review 1 should succeed on PASS: {r.output}"

    marker = os.path.join(mod.snapshot_root, "review-1.needs-fixes")
    assert not os.path.exists(marker), "PASS should not create needs-fixes marker"


@pytest.mark.asyncio
async def test_check_review_needs_fixes_creates_marker(tmp_path, monkeypatch) -> None:
    """check review 1 with NEEDS_FIXES: success and marker created."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    head_sha = repo.head.commit.hexsha
    base_sha = Path(mod.BASE_SHA).read_text().strip()

    review_md = os.path.join(str(feat_dir), "review.md")
    Path(review_md).write_text(
        f"VERDICT: NEEDS_FIXES\n"
        f"Base: {base_sha}\n"
        f"Head: {head_sha}\n"
        f"\n## Findings\n- Fix foo\n"
    )

    r = await _stage(mod, "check review 1").impl.run(ctx)
    assert r.success, f"check review 1 should succeed on NEEDS_FIXES: {r.output}"

    marker = os.path.join(mod.snapshot_root, "review-1.needs-fixes")
    assert os.path.exists(marker), "NEEDS_FIXES should create needs-fixes marker"


@pytest.mark.asyncio
async def test_review_passed_rejects_a_stale_head(tmp_path, monkeypatch) -> None:
    """Staleness is caught at the end of the phase, against the real HEAD.

    `check review` no longer looks at the SHAs — `stamp review` writes them one
    stage earlier, so checking them there would test norn against itself. The
    case that matters is a review whose head no longer matches HEAD, and that
    is what `review passed` guards.
    """
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    assert (await _stage(mod, "record start").impl.run(ctx)).success

    base_sha = Path(mod.BASE_SHA).read_text().strip()
    Path(mod.REVIEW_MD).write_text(
        "VERDICT: PASS\n"
        f"Base: {base_sha}\n"
        "Head: 0000000000000000000000000000000000000000\n"
    )

    r = await _stage(mod, "review passed").impl.run(ctx)
    assert not r.success, "review passed should reject a head that is not HEAD"
    assert "stale" in str(r.output).lower(), f"failure should mention 'stale': {r.output}"


@pytest.mark.asyncio
async def test_check_review_malformed_fails(tmp_path, monkeypatch) -> None:
    """check review 1 fails with a bad first line, mentioning malformed."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    review_md = os.path.join(str(feat_dir), "review.md")
    Path(review_md).write_text("WRONG HEADER\nBase: x\nHead: y\n")

    r = await _stage(mod, "check review 1").impl.run(ctx)
    assert not r.success, "check review 1 should fail with malformed header"
    out = str(r.output)
    assert "malformed" in out.lower(), f"failure should mention 'malformed': {out}"


@pytest.mark.asyncio
async def test_review_postcondition_succeeds_with_only_review_md(tmp_path, monkeypatch) -> None:
    """review postcondition 1 succeeds when only review.md was written."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    # Run review manifest to take the pre-snapshot
    r = await _stage(mod, "review manifest 1").impl.run(ctx)
    assert r.success

    # Write only review.md
    review_md = os.path.join(str(feat_dir), "review.md")
    Path(review_md).write_text("VERDICT: PASS\nBase: abc\nHead: def\n")

    # Review postcondition should succeed (review.md is ignored in diff)
    r = await _stage(mod, "review postcondition 1").impl.run(ctx)
    assert r.success, f"postcondition should succeed with only review.md: {r.output}"


@pytest.mark.asyncio
async def test_review_postcondition_fails_when_code_edited(tmp_path, monkeypatch) -> None:
    """review postcondition 1 fails when another file was also written."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    r = await _stage(mod, "review manifest 1").impl.run(ctx)
    assert r.success

    # Write review.md AND another file
    review_md = os.path.join(str(feat_dir), "review.md")
    Path(review_md).write_text("VERDICT: PASS\nBase: abc\nHead: def\n")
    (repo_path / "sneaky_edit.py").write_text("# sneaky\n")

    r = await _stage(mod, "review postcondition 1").impl.run(ctx)
    assert not r.success, "postcondition should fail when code was edited"


@pytest.mark.asyncio
async def test_review_postcondition_names_paths_not_culprit(tmp_path, monkeypatch) -> None:
    """The failure must list the paths and stay neutral about who wrote them.

    The snapshot diff sees that the worktree moved, not who moved it: the review
    agent is constrained by its prompt, but an editor saving in another terminal
    produces the identical diff.
    """
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    assert (await _stage(mod, "record start").impl.run(ctx)).success
    assert (await _stage(mod, "review manifest 1").impl.run(ctx)).success

    Path(os.path.join(str(feat_dir), "review.md")).write_text(
        "VERDICT: PASS\nBase: abc\nHead: def\n"
    )
    (repo_path / "sneaky_edit.py").write_text("# sneaky\n")

    r = await _stage(mod, "review postcondition 1").impl.run(ctx)
    assert not r.success
    output = f"{r.output or ''}{r.error or ''}"
    assert "files other than review.md changed" in output
    assert "sneaky_edit.py" in output, "the offending path must be named"
    assert "another terminal" in output, "the message must name the other cause"


@pytest.mark.asyncio
async def test_review_postcondition_names_staged_paths(tmp_path, monkeypatch) -> None:
    """A dirty index fails the gate and the staged paths are named."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    assert (await _stage(mod, "record start").impl.run(ctx)).success
    assert (await _stage(mod, "review manifest 1").impl.run(ctx)).success

    (repo_path / "staged_by_someone.py").write_text("# staged\n")
    repo.index.add(["staged_by_someone.py"])

    r = await _stage(mod, "review postcondition 1").impl.run(ctx)
    assert not r.success
    output = f"{r.output or ''}{r.error or ''}"
    assert "staged changes" in output, "the index gate must be the one that tripped"
    assert "staged_by_someone.py" in output, "the staged path must be named"


@pytest.mark.asyncio
async def test_review_passed_fails_on_needs_fixes(tmp_path, monkeypatch) -> None:
    """review passed fails when verdict is NEEDS_FIXES."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    head_sha = repo.head.commit.hexsha
    base_sha = Path(mod.BASE_SHA).read_text().strip()

    review_md = os.path.join(str(feat_dir), "review.md")
    Path(review_md).write_text(
        f"VERDICT: NEEDS_FIXES\n"
        f"Base: {base_sha}\n"
        f"Head: {head_sha}\n"
    )

    r = await _stage(mod, "review passed").impl.run(ctx)
    assert not r.success, "review passed should fail on NEEDS_FIXES"


@pytest.mark.asyncio
async def test_review_passed_fails_on_stale_head(tmp_path, monkeypatch) -> None:
    """review passed fails when Head: line doesn't match current HEAD."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    base_sha = Path(mod.BASE_SHA).read_text().strip()

    review_md = os.path.join(str(feat_dir), "review.md")
    Path(review_md).write_text(
        f"VERDICT: PASS\n"
        f"Base: {base_sha}\n"
        f"Head: 0000000000000000000000000000000000000000\n"
    )

    r = await _stage(mod, "review passed").impl.run(ctx)
    assert not r.success, "review passed should fail on stale head"


@pytest.mark.asyncio
async def test_review_passed_succeeds_and_writes_reviewed_head(tmp_path, monkeypatch) -> None:
    """review passed succeeds on fresh PASS and writes reviewed.head."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    head_sha = repo.head.commit.hexsha
    base_sha = Path(mod.BASE_SHA).read_text().strip()

    review_md = os.path.join(str(feat_dir), "review.md")
    Path(review_md).write_text(
        f"VERDICT: PASS\n"
        f"Base: {base_sha}\n"
        f"Head: {head_sha}\n"
        f"\nAll good.\n"
    )

    r = await _stage(mod, "review passed").impl.run(ctx)
    assert r.success, f"review passed should succeed on fresh PASS: {r.output}"

    reviewed_head_path = mod.REVIEWED_HEAD
    assert os.path.exists(reviewed_head_path), "reviewed.head must be written"
    assert Path(reviewed_head_path).read_text().strip() == head_sha


# ===========================================================================
# Step-08 structural tests: handoff and final audit
# ===========================================================================


# ---------------------------------------------------------------------------
# Stage ordering and presence
# ---------------------------------------------------------------------------


def test_handoff_and_final_audit_are_last_items() -> None:
    """handoff and final audit are the very last two items in config.items."""
    from norn.dsl import Stage

    mod = _import_v2(GOOD_DIR)
    items = mod.config.items
    assert isinstance(items[-1], Stage) and items[-1].name == "final audit", (
        f"Last item should be 'final audit', got {getattr(items[-1], 'name', type(items[-1]).__name__)}"
    )
    assert isinstance(items[-2], Stage) and items[-2].name == "handoff postcondition", (
        f"Second-to-last should be 'handoff postcondition', "
        f"got {getattr(items[-2], 'name', type(items[-2]).__name__)}"
    )


def test_no_generate_between_review_passed_and_handoff_except_handoff() -> None:
    """Between review passed and handoff the only agent is the learn stage.

    `learn` is a LearnGenerate restricted to the Write tool on its scratch
    files; with --arg learn=off there is no agent there at all.
    """
    from norn.dsl import Stage
    from norn.pipelines._learnings import LearnGenerate
    from norn.stages.generate import Generate

    for extra in ((), ("--arg", "learn=off")):
        mod = _import_v2(GOOD_DIR, *extra)
        items = mod.config.items
        names = [getattr(i, "name", None) for i in items]

        rp_idx = names.index("review passed")
        handoff_idx = names.index("handoff")

        agents = [
            item for item in items[rp_idx + 1 : handoff_idx]
            if isinstance(item, Stage) and isinstance(item.impl, Generate)
        ]
        if extra:
            assert agents == [], f"learn=off: unexpected agents {[a.name for a in agents]}"
        else:
            assert [a.name for a in agents] == ["learn"]
            assert isinstance(agents[0].impl, LearnGenerate)
            assert agents[0].impl.allowed_tools == ["Write"]


def test_handoff_follows_review_passed_with_only_non_generate_between() -> None:
    """handoff follows review passed; only ClearContext, RunCommand, ReadFile in between."""
    from norn.dsl import ClearContext, Stage
    from norn.stages.generate import Generate
    from norn.stages.read_file import ReadFile
    from norn.stages.run_command import RunCommand

    mod = _import_v2(GOOD_DIR)
    items = mod.config.items
    names = [getattr(i, "name", None) for i in items]

    rp_idx = names.index("review passed")
    handoff_idx = names.index("handoff")

    allowed_types = (ClearContext, type(None))  # for ClearContext
    between = items[rp_idx + 1 : handoff_idx]
    between_names = [getattr(i, "name", None) for i in between]

    # Exactly: ClearContext, handoff manifest, read handoff manifest, read review
    assert "handoff manifest" in between_names, "'handoff manifest' must be between review passed and handoff"
    assert "read handoff manifest" in between_names, "'read handoff manifest' must be between review passed and handoff"
    assert "read review" in between_names, "'read review' must be between review passed and handoff"


def test_handoff_postcondition_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "handoff postcondition")
    assert s.on_failure == OnFailure.FAIL, (
        f"handoff postcondition on_failure should be FAIL, got {s.on_failure}"
    )


def test_final_audit_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "final audit")
    assert s.on_failure == OnFailure.FAIL, (
        f"final audit on_failure should be FAIL, got {s.on_failure}"
    )


# ---------------------------------------------------------------------------
# handoff Generate configuration
# ---------------------------------------------------------------------------


def test_handoff_allowed_tools_is_write_only() -> None:
    """handoff Generate has allowed_tools == ['Write']."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "handoff")
    assert isinstance(s.impl, Generate)
    assert s.impl.allowed_tools == ["Write"], (
        f"handoff allowed_tools should be ['Write'], got {s.impl.allowed_tools!r}"
    )


def test_handoff_max_turns_is_4() -> None:
    """handoff Generate has max_turns == 4."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "handoff")
    assert isinstance(s.impl, Generate)
    assert s.impl.max_turns == 4, (
        f"handoff max_turns should be 4, got {s.impl.max_turns}"
    )


def test_handoff_timeout_is_300() -> None:
    """handoff stage-level timeout is 300."""
    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "handoff")
    assert s.timeout == 300, f"handoff timeout should be 300, got {s.timeout}"


def test_handoff_on_failure_is_ask_user() -> None:
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "handoff")
    assert s.on_failure == OnFailure.ASK_USER, (
        f"handoff on_failure should be ASK_USER, got {s.on_failure}"
    )


# ---------------------------------------------------------------------------
# handoff prompt content
# ---------------------------------------------------------------------------


def test_handoff_prompt_contains_read_handoff_manifest_placeholder() -> None:
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "handoff")
    assert isinstance(s.impl, Generate)
    assert "{read handoff manifest.output}" in s.impl.prompt, (
        "handoff prompt must contain '{read handoff manifest.output}' placeholder"
    )


def test_handoff_prompt_contains_read_review_placeholder() -> None:
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "handoff")
    assert isinstance(s.impl, Generate)
    assert "{read review.output}" in s.impl.prompt, (
        "handoff prompt must contain '{read review.output}' placeholder"
    )


def test_handoff_prompt_contains_facts_placeholder_for_first_step() -> None:
    """handoff prompt includes {facts step-01-v2-fixture-alpha.output} (window=0 facts only)."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "handoff")
    assert isinstance(s.impl, Generate)
    assert "{facts step-01-v2-fixture-alpha.output}" in s.impl.prompt, (
        "handoff prompt must contain facts placeholder for first step"
    )


def test_handoff_prompt_contains_no_closeout_placeholders() -> None:
    """handoff prompt uses window=0 — no closeout placeholders."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "handoff")
    assert isinstance(s.impl, Generate)
    assert "{closeout " not in s.impl.prompt, (
        "handoff prompt must not contain closeout placeholders (window=0)"
    )


def test_handoff_prompt_contains_aggregate_cmd() -> None:
    """handoff prompt includes the exact validation command."""
    from norn.stages.generate import Generate

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "handoff")
    assert isinstance(s.impl, Generate)
    assert mod.AGGREGATE_CMD in s.impl.prompt, (
        "handoff prompt must contain the AGGREGATE_CMD for the Testing section"
    )


def test_handoff_manifest_on_failure_is_fail() -> None:
    from norn.dsl import OnFailure

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "handoff manifest")
    assert s.on_failure == OnFailure.FAIL


# ---------------------------------------------------------------------------
# Temp-repo handoff shell-stage tests
# ---------------------------------------------------------------------------


def _setup_reviewed_repo(tmp_path: Path, monkeypatch) -> tuple:
    """Create a temp repo, copy the good fixture, record start, and write reviewed.head.

    Returns (repo, repo_path, feat_dir, mod, base_sha, head_sha).
    """
    from norn.models import PipelineContext

    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    ctx = PipelineContext()
    return repo, repo_path, feat_dir, mod, ctx


@pytest.mark.asyncio
async def test_handoff_manifest_writes_manifest_with_commits_section(tmp_path, monkeypatch) -> None:
    """handoff manifest produces a manifest whose ## Commits section lists a commit after record start."""
    repo, repo_path, feat_dir, mod, ctx = _setup_reviewed_repo(tmp_path, monkeypatch)
    from norn.models import PipelineContext

    # Record start
    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success, f"record start failed: {r.output}"

    base_sha = Path(mod.BASE_SHA).read_text().strip()

    # Make a commit after record start
    (repo_path / "feature.py").write_text("# feature\n")
    repo.index.add(["feature.py"])
    repo.index.commit("refactor: step-01-v2-fixture-alpha — Alpha")
    head_sha = repo.head.commit.hexsha

    # Write reviewed.head (normally written by review passed)
    Path(mod.REVIEWED_HEAD).parent.mkdir(parents=True, exist_ok=True)
    Path(mod.REVIEWED_HEAD).write_text(head_sha + "\n")

    # Run handoff manifest
    r = await _stage(mod, "handoff manifest").impl.run(ctx)
    assert r.success, f"handoff manifest failed: {r.output}"

    manifest_path = Path(mod.snapshot_root) / "handoff-manifest.md"
    assert manifest_path.exists(), "handoff-manifest.md must be written"
    text = manifest_path.read_text()

    # Check structure
    assert text.startswith("Base:"), f"manifest must start with 'Base:', got: {text[:50]!r}"
    assert "## Commits" in text, "manifest must have ## Commits section"
    assert "## Files" in text, "manifest must have ## Files section"
    assert "## Stat" in text, "manifest must have ## Stat section"
    assert "## Dependency and configuration changes" in text

    # The commit made after record start must appear in ## Commits
    commits_start = text.index("## Commits")
    commits_section = text[commits_start:]
    assert "step-01-v2-fixture-alpha" in commits_section, (
        f"## Commits section must list the commit made after record start; got:\n{commits_section[:300]}"
    )

    # handoff.pre.json must be written for postcondition
    pre_path = Path(mod.snapshot_root) / "handoff.pre.json"
    assert pre_path.exists(), "handoff.pre.json must be written by handoff manifest"

    # read handoff manifest ReadFile stage should return manifest text
    r = await _stage(mod, "read handoff manifest").impl.run(ctx)
    assert r.success
    assert isinstance(r.output, str)
    assert "Base:" in r.output


@pytest.mark.asyncio
async def test_handoff_postcondition_succeeds_when_only_handoff_md_written(tmp_path, monkeypatch) -> None:
    """handoff postcondition succeeds when only handoff.md was written."""
    repo, repo_path, feat_dir, mod, ctx = _setup_reviewed_repo(tmp_path, monkeypatch)

    # record start + handoff manifest (for pre-snapshot)
    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    head_sha = repo.head.commit.hexsha
    Path(mod.REVIEWED_HEAD).parent.mkdir(parents=True, exist_ok=True)
    Path(mod.REVIEWED_HEAD).write_text(head_sha + "\n")

    r = await _stage(mod, "handoff manifest").impl.run(ctx)
    assert r.success, f"handoff manifest failed: {r.output}"

    # Write only handoff.md
    Path(mod.HANDOFF_MD).write_text("# Handoff\n\nAll done.\n")

    r = await _stage(mod, "handoff postcondition").impl.run(ctx)
    assert r.success, f"handoff postcondition should succeed with only handoff.md: {r.output}"


@pytest.mark.asyncio
async def test_handoff_postcondition_fails_when_head_moved(tmp_path, monkeypatch) -> None:
    """handoff postcondition fails when HEAD moved (handoff committed something)."""
    repo, repo_path, feat_dir, mod, ctx = _setup_reviewed_repo(tmp_path, monkeypatch)

    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    head_sha = repo.head.commit.hexsha
    Path(mod.REVIEWED_HEAD).parent.mkdir(parents=True, exist_ok=True)
    Path(mod.REVIEWED_HEAD).write_text(head_sha + "\n")

    r = await _stage(mod, "handoff manifest").impl.run(ctx)
    assert r.success

    # Move HEAD past reviewed.head (simulate a rogue commit)
    (repo_path / "rogue.py").write_text("# rogue\n")
    repo.index.add(["rogue.py"])
    repo.index.commit("rogue commit")

    Path(mod.HANDOFF_MD).write_text("# Handoff\n")

    r = await _stage(mod, "handoff postcondition").impl.run(ctx)
    assert not r.success, "handoff postcondition should fail when HEAD moved"
    out = str(r.output)
    assert "reviewed.head" in out or "HEAD" in out, (
        f"failure should mention HEAD/reviewed.head mismatch: {out}"
    )


@pytest.mark.asyncio
async def test_final_audit_succeeds_on_clean_run(tmp_path, monkeypatch) -> None:
    """final audit succeeds: HEAD == reviewed.head, index clean, no owned paths dirty, no new untracked."""
    repo, repo_path, feat_dir, mod, ctx = _setup_reviewed_repo(tmp_path, monkeypatch)

    # record start → reviewed.head
    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    head_sha = repo.head.commit.hexsha
    Path(mod.REVIEWED_HEAD).parent.mkdir(parents=True, exist_ok=True)
    Path(mod.REVIEWED_HEAD).write_text(head_sha + "\n")

    r = await _stage(mod, "final audit").impl.run(ctx)
    assert r.success, f"final audit should succeed on clean run: {r.output}"


@pytest.mark.asyncio
async def test_final_audit_fails_when_committed_path_dirtied(tmp_path, monkeypatch) -> None:
    """final audit fails when a path from a .changed list is dirty after the run."""
    repo, repo_path, feat_dir, mod, ctx = _setup_reviewed_repo(tmp_path, monkeypatch)

    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    stem = "step-01-v2-fixture-alpha"

    # Record baseline and commit a file (writes .changed list)
    r = await _stage(mod, f"record baseline {stem}").impl.run(ctx)
    assert r.success

    committed_file = repo_path / "committed.py"
    committed_file.write_text("# committed\n")

    r = await _stage(mod, f"commit {stem}").impl.run(ctx)
    assert r.success, f"commit failed: {r.output}"

    # HEAD is now one past the original start; update reviewed.head
    head_sha = repo.head.commit.hexsha
    Path(mod.REVIEWED_HEAD).parent.mkdir(parents=True, exist_ok=True)
    Path(mod.REVIEWED_HEAD).write_text(head_sha + "\n")

    # Dirty the committed file
    committed_file.write_text("# dirtied after commit\n")

    r = await _stage(mod, "final audit").impl.run(ctx)
    assert not r.success, "final audit should fail when committed path is dirtied"
    out = str(r.output)
    assert "owned paths" in out.lower() or "dirty" in out.lower(), (
        f"failure should mention owned paths / dirty: {out}"
    )


@pytest.mark.asyncio
async def test_final_audit_fails_when_new_untracked_file_appeared(tmp_path, monkeypatch) -> None:
    """final audit fails when a new untracked file appeared during the run (not in start snapshot)."""
    repo, repo_path, feat_dir, mod, ctx = _setup_reviewed_repo(tmp_path, monkeypatch)

    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    head_sha = repo.head.commit.hexsha
    Path(mod.REVIEWED_HEAD).parent.mkdir(parents=True, exist_ok=True)
    Path(mod.REVIEWED_HEAD).write_text(head_sha + "\n")

    # Create a new untracked file AFTER record start
    new_file = repo_path / "appeared_during_run.py"
    new_file.write_text("# appeared during run\n")

    r = await _stage(mod, "final audit").impl.run(ctx)
    assert not r.success, "final audit should fail when new untracked file appeared"
    out = str(r.output)
    assert "untracked" in out.lower() or "committed" in out.lower() or "appeared" in out.lower(), (
        f"failure should mention untracked/appeared: {out}"
    )


@pytest.mark.asyncio
async def test_final_audit_does_not_fail_for_preexisting_untracked(tmp_path, monkeypatch) -> None:
    """final audit does NOT fail for a file that was already untracked at record start."""
    repo, repo_path, feat_dir, mod, ctx = _setup_reviewed_repo(tmp_path, monkeypatch)

    # Create the untracked file BEFORE record start
    preexisting = repo_path / "preexisting_untracked.py"
    preexisting.write_text("# pre-existing untracked\n")

    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    head_sha = repo.head.commit.hexsha
    Path(mod.REVIEWED_HEAD).parent.mkdir(parents=True, exist_ok=True)
    Path(mod.REVIEWED_HEAD).write_text(head_sha + "\n")

    # The pre-existing untracked file still exists, was there before start snapshot
    r = await _stage(mod, "final audit").impl.run(ctx)
    assert r.success, (
        f"final audit should not flag pre-existing untracked files: {r.output}"
    )


@pytest.mark.asyncio
async def test_final_audit_does_not_fail_for_newly_ignored_file(tmp_path, monkeypatch) -> None:
    """final audit does NOT flag a file that became gitignored during the run.

    The path was untracked when the run-start snapshot was taken and a
    ``.gitignore`` committed by a step now covers it, so it has vanished from
    ``git status``.  ``git add`` refuses such a path, which is why the commit
    stage drops it; flagging it here would end an otherwise clean run with
    nothing the operator could do about it.
    """
    repo, repo_path, feat_dir, mod, ctx = _setup_reviewed_repo(tmp_path, monkeypatch)

    # Untracked before record start, so it lands in the run-start snapshot.
    scratch = repo_path / "editor-scratch.tmp"
    scratch.write_text("# editor scratch\n")

    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    # A step commits a .gitignore that covers it — the file is still on disk
    # but no longer appears in git status.
    (repo_path / ".gitignore").write_text("editor-scratch.tmp\n")
    repo.index.add([".gitignore"])
    repo.index.commit("refactor: step-01-v2-fixture-alpha — ignore editor scratch")

    head_sha = repo.head.commit.hexsha
    Path(mod.REVIEWED_HEAD).parent.mkdir(parents=True, exist_ok=True)
    Path(mod.REVIEWED_HEAD).write_text(head_sha + "\n")

    r = await _stage(mod, "final audit").impl.run(ctx)
    assert r.success, (
        f"final audit should not flag a path that became gitignored: {r.output}"
    )


@pytest.mark.asyncio
async def test_final_audit_prints_one_offending_path_per_line(tmp_path, monkeypatch) -> None:
    """The audit's delta list is NUL-separated; each path must print on its own line."""
    repo, repo_path, feat_dir, mod, ctx = _setup_reviewed_repo(tmp_path, monkeypatch)

    r = await _stage(mod, "record start").impl.run(ctx)
    assert r.success

    head_sha = repo.head.commit.hexsha
    Path(mod.REVIEWED_HEAD).parent.mkdir(parents=True, exist_ok=True)
    Path(mod.REVIEWED_HEAD).write_text(head_sha + "\n")

    (repo_path / "appeared_one.py").write_text("# one\n")
    (repo_path / "appeared_two.py").write_text("# two\n")

    r = await _stage(mod, "final audit").impl.run(ctx)
    assert not r.success
    stderr = r.output["stderr"] if isinstance(r.output, dict) else str(r.output)
    lines = [line.strip() for line in stderr.splitlines()]
    assert "appeared_one.py" in lines, f"path must be on its own line, got:\n{stderr}"
    assert "appeared_two.py" in lines, f"path must be on its own line, got:\n{stderr}"


# ===========================================================================
# Step-09 tests: docstring and catalog contract
# ===========================================================================


def test_docstring_mentions_budget_knob() -> None:
    """Module docstring documents the --arg budget knob."""
    mod = _import_v2(GOOD_DIR)
    assert "budget" in (mod.__doc__ or ""), (
        "docstring must mention 'budget' knob"
    )


def test_docstring_mentions_allow_dirty_index_knob() -> None:
    """Module docstring documents the --arg allow_dirty_index knob."""
    mod = _import_v2(GOOD_DIR)
    assert "allow_dirty_index" in (mod.__doc__ or ""), (
        "docstring must mention 'allow_dirty_index' knob"
    )


def test_docstring_mentions_allow_dirty_worktree_knob() -> None:
    """Module docstring documents the --arg allow_dirty_worktree knob."""
    mod = _import_v2(GOOD_DIR)
    assert "allow_dirty_worktree" in (mod.__doc__ or ""), (
        "docstring must mention 'allow_dirty_worktree' knob"
    )


def test_docstring_mentions_review_md() -> None:
    """Module docstring documents that review.md is written."""
    mod = _import_v2(GOOD_DIR)
    assert "review.md" in (mod.__doc__ or ""), (
        "docstring must mention 'review.md'"
    )


def test_docstring_mentions_handoff_md() -> None:
    """Module docstring documents that handoff.md is written."""
    mod = _import_v2(GOOD_DIR)
    assert "handoff.md" in (mod.__doc__ or ""), (
        "docstring must mention 'handoff.md'"
    )


def test_docstring_mentions_not_transactional() -> None:
    """Module docstring states the 'not transactional' honest limit."""
    mod = _import_v2(GOOD_DIR)
    assert "not transactional" in (mod.__doc__ or ""), (
        "docstring must include 'not transactional' in Honest limits"
    )


def test_catalog_get_pipeline_info_returns_correct_short() -> None:
    """get_pipeline_info('implement_features_v3').short is the first docstring line."""
    from norn.catalog import get_pipeline_info

    info = get_pipeline_info("implement_features_v3")
    assert info is not None, "implement_features_v3 must be discoverable by the catalog"
    mod = _import_v2(GOOD_DIR)
    expected_short = (mod.__doc__ or "").split("\n", 1)[0].strip()
    assert info.short == expected_short, (
        f"catalog short should be {expected_short!r}, got {info.short!r}"
    )


def test_catalog_get_pipeline_info_has_one_positional_arg() -> None:
    """get_pipeline_info('implement_features_v3') reports exactly one positional arg ('args')."""
    from norn.catalog import get_pipeline_info

    info = get_pipeline_info("implement_features_v3")
    assert info is not None
    assert list(info.args.keys()) == ["args"], (
        f"implement_features_v3 should expose exactly one positional arg 'args', "
        f"got {list(info.args.keys())!r}"
    )


# ---------------------------------------------------------------------------
# Plan-controlled text in the commit shell
# ---------------------------------------------------------------------------

MULTILINE_DIR = FIXTURE_DIR / "multiline-cmd"


def test_facts_validation_label_is_one_line() -> None:
    """A block-scalar test_cmd collapses to a single facts line."""
    mod = _import_v2(GOOD_DIR)
    label = mod._facts_validation_label("sh -c '\n  echo one\n  echo two\n'")
    assert "\n" not in label
    assert label == "sh -c ' echo one echo two '"


def test_facts_validation_label_is_truncated() -> None:
    """The facts file is pasted into later prompts, so the label is bounded."""
    mod = _import_v2(GOOD_DIR)
    label = mod._facts_validation_label("x" * 500)
    assert len(label) == mod.FACTS_VALIDATION_MAX_CHARS
    assert label.endswith("…")


def test_every_generated_command_is_valid_shell() -> None:
    """Every RunCommand the pipeline builds must parse as POSIX shell.

    A `test_cmd:` block scalar used to be interpolated into the commit shell as
    code, so a plan carrying newlines or parentheses produced a command that
    /bin/sh refused before the commit was attempted.
    """
    import subprocess

    mod = _import_v2(MULTILINE_DIR)

    def walk(items):
        for it in items:
            yield it
            for attr in ("stages", "items", "branches"):
                sub = getattr(it, attr, None)
                if isinstance(sub, list):
                    yield from walk(sub)

    checked = 0
    for item in walk(mod.config.items):
        cmd = getattr(getattr(item, "impl", None), "cmd", None)
        if not isinstance(cmd, str):
            continue
        checked += 1
        proc = subprocess.run(["/bin/sh", "-n"], input=cmd, text=True, capture_output=True)
        assert proc.returncode == 0, (
            f"stage {getattr(item, 'name', '?')!r} builds invalid shell: {proc.stderr.strip()}"
        )
    assert checked > 10, f"expected many RunCommand stages, checked {checked}"


@pytest.mark.asyncio
async def test_commit_does_not_execute_the_test_cmd(tmp_path, monkeypatch) -> None:
    """The commit stage records the test_cmd as data, never runs it.

    The fixture's test_cmd contains a command substitution. Interpolated as
    code it would run during the commit; passed as data it cannot.
    """
    repo_path = tmp_path / "repo"
    _init_temp_repo(repo_path)

    feat_dir = repo_path / "feat"
    shutil.copytree(MULTILINE_DIR, feat_dir)

    canary = tmp_path / "canary"
    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-multiline"
    ctx = PipelineContext()

    baseline = _stage(mod, f"record baseline {stem}")
    assert (await baseline.impl.run(ctx)).success

    # A change for the step to own, so the commit has something to commit.
    (repo_path / "owned.txt").write_text("work\n")

    commit = _stage(mod, f"commit {stem}")
    result = await commit.impl.run(ctx)
    assert result.success, f"commit failed: {result.output}"

    assert not canary.exists(), "the test_cmd's command substitution was executed"
    assert not Path("/tmp/norn-injection-canary").exists(), (
        "the test_cmd's command substitution was executed"
    )

    facts = Path(mod.snapshot_root) / f"{stem}.facts"
    lines = facts.read_text().splitlines()
    assert len(lines) == 3, f"facts file must stay three lines, got: {lines}"
    assert lines[2].startswith("Validation: PASS - sh -ex -c")


# ---------------------------------------------------------------------------
# norn's own run state
#
# norn writes `<key>.runs/<n>/events.jsonl`, `<key>.checkpoint` and
# `<key>.history` into the launch directory and keeps writing to them for the
# whole run. When the launch directory is the target repo, the snapshot diff
# used to see them as step-owned changes: step 01 committed the journal, the
# next journal write dirtied it again, and `assert committed` failed.
# ---------------------------------------------------------------------------


def test_no_state_key_leaves_the_diff_flags_empty(tmp_path, monkeypatch) -> None:
    """Importing outside a run (TUI, tests, --dry-run) must not change the shell."""
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    monkeypatch.delenv("NORN_STATE_KEY", raising=False)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    assert mod.NORN_STATE_IGNORE_FLAGS == ""


def test_state_key_produces_ignore_flags_on_every_diff(tmp_path, monkeypatch) -> None:
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    monkeypatch.setenv("NORN_STATE_KEY", str(repo_path / "implement_features_v3"))
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    flags = mod.NORN_STATE_IGNORE_FLAGS
    assert "--ignore-prefix implement_features_v3.runs" in flags
    assert "--ignore implement_features_v3.checkpoint" in flags
    assert "--ignore implement_features_v3.history" in flags

    stem = "step-01-v2-fixture-alpha"
    for stage_name in (f"commit {stem}", f"assert owned diff {stem}"):
        cmd = _stage(mod, stage_name).impl.cmd
        for line in cmd.split("_step_snapshot.py diff")[1:]:
            assert "--ignore-prefix implement_features_v3.runs" in line, (
                f"{stage_name}: a diff invocation is missing the norn-state flags"
            )


@pytest.mark.asyncio
async def test_journal_writes_alone_do_not_satisfy_assert_owned_diff(
    tmp_path, monkeypatch
) -> None:
    """A step that wrote nothing must still fail, even while the journal grows."""
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    monkeypatch.setenv("NORN_STATE_KEY", str(repo_path / "implement_features_v3"))
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    assert (await _stage(mod, f"record baseline {stem}").impl.run(ctx)).success

    journal = repo_path / "implement_features_v3.runs" / "2" / "events.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"event": "stage_start"}\n')

    r = await _stage(mod, f"assert owned diff {stem}").impl.run(ctx)
    assert not r.success, "the journal is not the step's work"


@pytest.mark.asyncio
async def test_run_state_is_not_committed_and_assert_committed_survives_it(
    tmp_path, monkeypatch
) -> None:
    """Regression: journal committed by step 01, dirtied again by the next write."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    monkeypatch.setenv("NORN_STATE_KEY", str(repo_path / "implement_features_v3"))
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    assert (await _stage(mod, f"record baseline {stem}").impl.run(ctx)).success

    # The runner writes its state while the step runs.
    journal = repo_path / "implement_features_v3.runs" / "2" / "events.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"event": "stage_start"}\n')
    checkpoint = repo_path / "implement_features_v3.checkpoint"
    checkpoint.write_text('{"stages": []}\n')
    history = repo_path / "implement_features_v3.history"
    history.write_text("[]\n")

    # The step's own work.
    (repo_path / "feature.py").write_text("# feature\n")

    r = await _stage(mod, f"commit {stem}").impl.run(ctx)
    assert r.success, f"commit failed: {r.output}"

    committed = repo.git.show("--name-only", "--format=", "HEAD").split()
    assert "feature.py" in committed
    assert not [p for p in committed if p.startswith("implement_features_v3.")], (
        f"norn's own run state was committed: {committed}"
    )

    # The run continues: more events, a new checkpoint.
    journal.write_text('{"event": "stage_start"}\n{"event": "stage_end"}\n')
    checkpoint.write_text('{"stages": ["commit"]}\n')

    r = await _stage(mod, f"assert committed {stem}").impl.run(ctx)
    assert r.success, f"assert committed failed: {r.output}"


# ---------------------------------------------------------------------------
# A dirty index must be caught at run start and named where it is asserted
#
# A run resumed into a repo with a staged change used to skip the preflight
# (restored from the checkpoint) and then die at the first postcondition that
# asserts a clean index — reporting it as "index is not clean after review fix
# commit 2", naming neither the files nor the fact that they predated the stage.
# ---------------------------------------------------------------------------


def test_preflight_is_never_restored_from_a_checkpoint() -> None:
    mod = _import_v2(GOOD_DIR)
    stage = _stage(mod, "preflight repository")
    assert stage.always_run, "a resume must re-check the repo, not trust a cached verdict"


def _combined_output(result) -> str:
    out = result.output or {}
    return f"{out.get('stdout', '')}\n{out.get('stderr', '')}"


@pytest.mark.asyncio
async def test_preflight_names_the_staged_paths(tmp_path, monkeypatch) -> None:
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    (repo_path / "leftover.txt").write_text("staged before the run\n")
    repo.index.add(["leftover.txt"])

    result = await _stage(mod, "preflight repository").impl.run(PipelineContext())

    assert not result.success, "preflight must reject a dirty index"
    assert "leftover.txt" in _combined_output(result), (
        f"preflight must name what is staged: {result.output}"
    )


@pytest.mark.asyncio
async def test_preflight_names_the_modified_tracked_files(tmp_path, monkeypatch) -> None:
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    tracked = Path(repo.working_tree_dir) / "README.md"
    tracked.write_text("modified before the run\n")

    result = await _stage(mod, "preflight repository").impl.run(PipelineContext())

    assert not result.success
    assert "README.md" in _combined_output(result)


@pytest.mark.asyncio
async def test_preflight_accepts_the_work_of_the_step_a_resume_stopped_inside(
    tmp_path, monkeypatch
) -> None:
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    shutil.copytree(GOOD_DIR, repo_path / "feat")
    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(repo_path / "feat", monkeypatch)

    from norn.models import PipelineContext

    (Path(repo.working_tree_dir) / "README.md").write_text("the interrupted step's work\n")
    stem = "step-01-v2-fixture-alpha"
    preflight = _stage(mod, "preflight repository").impl

    mid_step = PipelineContext()
    mid_step.checkpoint_stages = {f"record baseline {stem}", f"implement {stem}"}
    resumed = await preflight.run(mid_step)
    assert resumed.success, _combined_output(resumed)
    assert "README.md" in _combined_output(resumed)

    between_steps = PipelineContext()
    between_steps.checkpoint_stages = {f"record baseline {stem}", f"facts {stem}"}
    assert not (await preflight.run(between_steps)).success, (
        "an edit no interrupted step owns must still be rejected"
    )


@pytest.mark.asyncio
async def test_head_moved_names_the_subject_that_skips_the_step(tmp_path, monkeypatch) -> None:
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    shutil.copytree(GOOD_DIR, repo_path / "feat")
    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(repo_path / "feat", monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()
    assert (await _stage(mod, f"record baseline {stem}").impl.run(ctx)).success
    (repo_path / "work.txt").write_text("committed by hand\n")
    repo.index.add(["work.txt"])
    repo.index.commit("tmp")

    result = await _stage(mod, f"assert head unchanged {stem}").impl.run(ctx)

    assert not result.success
    assert f'"refactor: {stem}"' in _combined_output(result)


@pytest.mark.asyncio
async def test_assert_committed_names_the_staged_paths(tmp_path, monkeypatch) -> None:
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    stem = "step-01-v2-fixture-alpha"
    ctx = PipelineContext()

    assert (await _stage(mod, f"record baseline {stem}").impl.run(ctx)).success
    (repo_path / "feature.py").write_text("# feature\n")
    assert (await _stage(mod, f"commit {stem}").impl.run(ctx)).success

    # Something stages a file after the commit — another terminal, a hook.
    (repo_path / "sneaked.txt").write_text("staged behind our back\n")
    repo.index.add(["sneaked.txt"])

    result = await _stage(mod, f"assert committed {stem}").impl.run(ctx)

    assert not result.success
    assert "sneaked.txt" in _combined_output(result), (
        f"the postcondition must name what is staged: {result.output}"
    )


@pytest.mark.asyncio
async def test_review_fix_postcondition_names_the_staged_paths(tmp_path, monkeypatch) -> None:
    """The reported failure: 'index is not clean after review fix commit 2', no paths."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    (repo_path / "leftover.txt").write_text("staged by an earlier run\n")
    repo.index.add(["leftover.txt"])

    result = await _stage(mod, "review fix committed 2").impl.run(PipelineContext())

    assert not result.success
    assert "leftover.txt" in _combined_output(result), (
        f"the postcondition must name what is staged: {result.output}"
    )


# ---------------------------------------------------------------------------
# The base SHA when steps are resume-skipped
#
# Every later phase reads base.sha: the review contract's `Base:` line, the
# aggregate and handoff manifests, the final diff. Recording HEAD on a run
# where the steps are already committed names a base that CONTAINS the work,
# so the review is handed an empty range — and then rejected for not echoing
# a base it could not sensibly use.
# ---------------------------------------------------------------------------


def _commit_step(repo, repo_path: Path, stem: str, filename: str):
    """Commit a file under the subject v2's resume scan matches."""
    (repo_path / filename).write_text(f"# {stem}\n")
    repo.index.add([filename])
    return repo.index.commit(f"refactor: {stem} — {stem} title")


async def _record_start_base(mod, ctx) -> str:
    result = await _stage(mod, "record start").impl.run(ctx)
    assert result.success, f"record start failed: {result.error}"
    return Path(mod.BASE_SHA).read_text().strip()


@pytest.mark.asyncio
async def test_base_is_head_when_no_step_is_committed(tmp_path, monkeypatch) -> None:
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    assert await _record_start_base(mod, PipelineContext()) == repo.head.commit.hexsha


@pytest.mark.asyncio
async def test_base_backs_up_past_the_committed_steps(tmp_path, monkeypatch) -> None:
    """The reported failure: base == HEAD left the review an empty range."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    true_base = repo.head.commit.hexsha
    _commit_step(repo, repo_path, "step-01-v2-fixture-alpha", "alpha.py")
    _commit_step(repo, repo_path, "step-02-v2-fixture-beta", "beta.py")
    # An unrelated commit on top, as a cleanup or a review fix would be.
    (repo_path / "chore.txt").write_text("chore\n")
    repo.index.add(["chore.txt"])
    repo.index.commit("chore: untrack something")

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    base = await _record_start_base(mod, PipelineContext())

    assert base == true_base, "base must be the parent of the earliest step commit"
    assert base != repo.head.commit.hexsha, "a base equal to HEAD is an empty range"


@pytest.mark.asyncio
async def test_base_matches_the_review_contract_after_a_partial_run(
    tmp_path, monkeypatch
) -> None:
    """`check review` compares review.md line 2 to base.sha — they must agree."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    true_base = repo.head.commit.hexsha
    _commit_step(repo, repo_path, "step-01-v2-fixture-alpha", "alpha.py")

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    ctx = PipelineContext()
    assert await _record_start_base(mod, ctx) == true_base

    # Write the review the agent would write against that base, then let the
    # contract check read it.
    Path(mod.REVIEW_MD).write_text(
        "VERDICT: PASS\n"
        f"Base: {true_base}\n"
        f"Head: {repo.head.commit.hexsha}\n\n"
        "Looks fine.\n"
    )
    result = await _stage(mod, "check review 1").impl.run(ctx)

    assert result.success, f"check review rejected a correct base: {result.output}"


@pytest.mark.asyncio
async def test_base_falls_back_when_the_step_commit_is_the_root(
    tmp_path, monkeypatch
) -> None:
    """A root step commit has no parent; record the commit rather than fail the run."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo = Repo.init(repo_path)
    repo.config_writer().set_value("user", "name", "Test User").release()
    repo.config_writer().set_value("user", "email", "test@example.com").release()

    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    root = _commit_step(repo, repo_path, "step-01-v2-fixture-alpha", "alpha.py")

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    from norn.models import PipelineContext

    assert await _record_start_base(mod, PipelineContext()) == root.hexsha


# ---------------------------------------------------------------------------
# The review artifact: agent owns the verdict, norn owns the SHAs
#
# The old contract asked the agent to transcribe two 40-character SHAs into
# review.md and threw away the whole review when they did not match — including
# when the agent was right and the manifest's range was what was wrong.
# ---------------------------------------------------------------------------


def test_review_prompt_asks_only_for_the_verdict() -> None:
    mod = _import_v2(GOOD_DIR)
    prompt = mod._review_prompt(1)

    assert "VERDICT: PASS" in prompt
    assert "VERDICT: NEEDS_FIXES" in prompt
    assert "Base: <sha>" not in prompt, "the agent must not be asked to write SHAs"
    assert "Head: <sha>" not in prompt


@pytest.mark.asyncio
async def test_review_manifest_deletes_a_previous_review(tmp_path, monkeypatch) -> None:
    """Freshness: a round starts with no artifact, so a silent agent cannot pass."""
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    assert (await _stage(mod, "record start").impl.run(ctx)).success
    Path(mod.REVIEW_MD).write_text("VERDICT: PASS\nfrom an earlier run\n")

    assert (await _stage(mod, "review manifest 1").impl.run(ctx)).success
    assert not Path(mod.REVIEW_MD).exists(), "a stale review.md must not survive"


@pytest.mark.asyncio
async def test_stamp_fails_when_the_review_wrote_nothing(tmp_path, monkeypatch) -> None:
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    assert (await _stage(mod, "record start").impl.run(ctx)).success
    assert (await _stage(mod, "review manifest 1").impl.run(ctx)).success

    result = await _stage(mod, "stamp review 1").impl.run(ctx)

    assert not result.success, "a round that wrote no review.md must fail"
    assert "wrote no review.md" in _combined_output(result)


@pytest.mark.asyncio
async def test_stamp_writes_the_shas_and_keeps_the_findings(tmp_path, monkeypatch) -> None:
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    assert (await _stage(mod, "record start").impl.run(ctx)).success
    assert (await _stage(mod, "review manifest 1").impl.run(ctx)).success

    # What the agent now writes: a verdict and findings, no SHAs.
    Path(mod.REVIEW_MD).write_text(
        "VERDICT: NEEDS_FIXES\n\n## Findings\n\n- major: something is wrong\n"
    )

    assert (await _stage(mod, "stamp review 1").impl.run(ctx)).success

    lines = Path(mod.REVIEW_MD).read_text().splitlines()
    assert lines[0] == "VERDICT: NEEDS_FIXES"
    assert lines[1] == f"Base: {Path(mod.BASE_SHA).read_text().strip()}"
    assert lines[2] == f"Head: {repo.head.commit.hexsha}"
    assert "- major: something is wrong" in "\n".join(lines)


@pytest.mark.asyncio
async def test_stamp_does_not_duplicate_shas_the_agent_wrote(tmp_path, monkeypatch) -> None:
    """An agent writing the old header out of habit must not end up with two."""
    repo = _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    assert (await _stage(mod, "record start").impl.run(ctx)).success
    assert (await _stage(mod, "review manifest 1").impl.run(ctx)).success

    Path(mod.REVIEW_MD).write_text(
        "VERDICT: PASS\n"
        "Base: deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
        "Head: deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"
        "\nAll good.\n"
    )

    assert (await _stage(mod, "stamp review 1").impl.run(ctx)).success

    text = Path(mod.REVIEW_MD).read_text()
    assert text.count("Base: ") == 1, f"Base written twice:\n{text}"
    assert text.count("Head: ") == 1, f"Head written twice:\n{text}"
    assert "deadbeef" not in text, "the agent's SHAs must be replaced, not kept"
    assert text.splitlines()[1] == f"Base: {Path(mod.BASE_SHA).read_text().strip()}"
    assert text.splitlines()[2] == f"Head: {repo.head.commit.hexsha}"
    assert "All good." in text


@pytest.mark.asyncio
async def test_check_review_accepts_a_verdict_only_artifact(tmp_path, monkeypatch) -> None:
    """The whole point: a review is not discarded over missing or wrong SHAs."""
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)
    from norn.models import PipelineContext

    ctx = PipelineContext()
    Path(mod.REVIEW_MD).write_text("VERDICT: PASS\n\nNothing to fix.\n")

    result = await _stage(mod, "check review 1").impl.run(ctx)

    assert result.success, f"check review must judge the verdict only: {result.output}"


@pytest.mark.asyncio
async def test_review_round_1_stamps_before_it_checks(tmp_path, monkeypatch) -> None:
    """Stage order: the SHAs must be on the file before the verdict is read."""
    _init_temp_repo(tmp_path / "repo")
    repo_path = tmp_path / "repo"
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)

    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    names = [i.name for i in mod.config.items if hasattr(i, "name")]
    assert names.index("review 1") < names.index("review postcondition 1")
    assert names.index("review postcondition 1") < names.index("stamp review 1")
    assert names.index("stamp review 1") < names.index("check review 1")


# ---------------------------------------------------------------------------
# Review write-guard
#
# The review prompt has always said "do not edit any file except review.md",
# but under permission_mode="acceptEdits" that is guidance an agent can decide
# to override.  One review round did: it fixed a real jq bug in
# _service-build.yml, and the run died at the review postcondition with the
# review already paid for.
# ---------------------------------------------------------------------------


def test_review_stage_declares_the_write_guard() -> None:
    mod = _import_v2(GOOD_DIR)
    stage = _stage(mod, "review 1")
    hooks = stage.impl.hooks
    assert hooks, "review 1 must declare the write-guard hook"
    matchers = [m["matcher"] for m in hooks["PreToolUse"]]
    assert matchers == ["Write|Edit|MultiEdit|NotebookEdit"]


def test_review_fix_stage_has_no_write_guard() -> None:
    """The fix stage exists to edit code — guarding it would break the loop."""
    mod = _import_v2(GOOD_DIR)
    assert _stage(mod, "review fix 1").impl.hooks is None


@pytest.mark.asyncio
async def test_write_guard_allows_review_md_and_denies_everything_else() -> None:
    mod = _import_v2(GOOD_DIR)
    hook = _stage(mod, "review 1").impl.hooks["PreToolUse"][0]["hooks"][0]

    allowed = await hook({"tool_input": {"file_path": mod.REVIEW_MD}}, "t1", None)
    assert allowed == {}, "review.md must stay writable"

    denied = await hook(
        {"tool_input": {"file_path": os.path.join(mod.PROJECT_DIR, "src/app.py")}}, "t2", None
    )
    decision = denied["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "finding" in decision["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_write_guard_is_not_fooled_by_a_dotdot_path() -> None:
    """realpath comparison: `<feature>/../review.md` is not review.md."""
    mod = _import_v2(GOOD_DIR)
    hook = _stage(mod, "review 1").impl.hooks["PreToolUse"][0]["hooks"][0]

    sneaky = os.path.join(os.path.dirname(mod.REVIEW_MD), "..", "review.md")
    denied = await hook({"tool_input": {"file_path": sneaky}}, "t3", None)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    # The same detour that lands back on review.md is allowed.
    round_trip = os.path.join(os.path.dirname(mod.REVIEW_MD), "..",
                              os.path.basename(os.path.dirname(mod.REVIEW_MD)), "review.md")
    assert await hook({"tool_input": {"file_path": round_trip}}, "t4", None) == {}


@pytest.mark.asyncio
async def test_write_guard_ignores_tools_without_a_file_path() -> None:
    mod = _import_v2(GOOD_DIR)
    hook = _stage(mod, "review 1").impl.hooks["PreToolUse"][0]["hooks"][0]
    assert await hook({"tool_input": {}}, "t5", None) == {}


def test_write_guard_is_omitted_for_providers_without_hook_support() -> None:
    """SDK hooks are Claude Code only; declaring them elsewhere fails the stage."""
    mod = _import_v2(GOOD_DIR)
    assert mod._effective_agent_provider(["--agent-provider", "codex"]) == "codex"
    assert mod._effective_agent_provider(["--agent-provider=codex"]) == "codex"
    assert mod._effective_agent_provider([]) == "claude-code"


# ---------------------------------------------------------------------------
# Retry ledger, oscillation stop and retry prompt (step 09)
# ---------------------------------------------------------------------------

_LEDGER_STEP = "step-01-v2-fixture-alpha"


def test_failing_tests_pytest() -> None:
    mod = _import_v2(GOOD_DIR)
    text = (
        "tests/test_a.py F.E                                     [100%]\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_a.py::test_one - AssertionError: assert 1 == 2\n"
        "ERROR tests/test_b.py::test_two - RuntimeError: boom\n"
        "ERROR: file or directory not found: tests/missing.py\n"
        "========================= 1 failed, 1 error in 0.12s =========================\n"
    )
    result = mod.failing_tests(text)
    assert isinstance(result, frozenset)
    assert result == {"tests/test_a.py::test_one", "tests/test_b.py::test_two"}


def test_failing_tests_bats() -> None:
    mod = _import_v2(GOOD_DIR)
    text = (
        "1..3\n"
        "ok 1 greets the user\n"
        "not ok 2 rejects an empty name\n"
        "# (in test file bats/greet.bats, line 12)\n"
        "not ok 3 prints the version\r\n"
    )
    assert mod.failing_tests(text) == {"rejects an empty name", "prints the version"}


def test_failing_tests_go() -> None:
    mod = _import_v2(GOOD_DIR)
    text = (
        "=== RUN   TestParse\n"
        "--- FAIL: TestParse (0.00s)\n"
        "    parse_test.go:12: got 1, want 2\n"
        "=== RUN   TestFormat\n"
        "--- PASS: TestFormat (0.00s)\n"
        "--- FAIL: TestRender (0.01s)\n"
        "FAIL\n"
    )
    assert mod.failing_tests(text) == {"TestParse", "TestRender"}


def test_failing_tests_maven() -> None:
    mod = _import_v2(GOOD_DIR)
    text = (
        "[ERROR] Failures: \n"
        "[ERROR]   CalculatorTest.testAdd:15 expected:<3> but was:<4>\n"
        "[ERROR]   com.acme.ParserTest.testEmpty:42 NullPointer\n"
        "[ERROR] Tests run: 9, Failures: 2, Errors: 0, Skipped: 0\n"
        "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.2.5:test\n"
    )
    assert mod.failing_tests(text) == {"CalculatorTest.testAdd", "com.acme.ParserTest.testEmpty"}


def test_failing_tests_unknown_output_is_empty() -> None:
    mod = _import_v2(GOOD_DIR)
    assert mod.failing_tests("") == frozenset()
    assert mod.failing_tests(
        "Traceback (most recent call last):\n  File \"x.py\", line 1\nSyntaxError: bad\nexit 1\n"
    ) == frozenset()


def _ledger_entries(*failsets, rnd: int = 1, start: int = 1) -> list[dict]:
    return [
        {"attempt": start + i, "round": rnd, "failset": sorted(fs)}
        for i, fs in enumerate(failsets)
    ]


_A = {"tests/test_x.py::test_a"}
_B = {"tests/test_x.py::test_b"}
_C = {"tests/test_x.py::test_c"}


def test_detect_oscillation_a_b_a() -> None:
    mod = _import_v2(GOOD_DIR)
    assert mod.detect_oscillation(_ledger_entries(_A, _B, _A)) == (frozenset(_A), frozenset(_B))
    # Only the trailing window counts, so a longer round still detects.
    assert mod.detect_oscillation(_ledger_entries(_C, _A, _B, _A)) == (frozenset(_A), frozenset(_B))


@pytest.mark.parametrize("failsets", [
    (_A, _A, _A),
    (_A, _B, _C),
    (_A, _B),
    (_A,),
    (),
    (_A, set(), _A),
    (set(), _B, set()),
], ids=["AAA", "ABC", "too-short", "one", "empty-ledger", "empty-middle", "empty-ends"])
def test_detect_oscillation_negative(failsets) -> None:
    mod = _import_v2(GOOD_DIR)
    assert mod.detect_oscillation(_ledger_entries(*failsets)) is None


def test_detect_oscillation_round_boundary_resets() -> None:
    mod = _import_v2(GOOD_DIR)
    entries = _ledger_entries(_A, _B) + _ledger_entries(_A, rnd=2, start=0)
    assert mod.detect_oscillation(entries) is None
    entries = _ledger_entries(_A) + _ledger_entries(_B, _A, rnd=2, start=0)
    assert mod.detect_oscillation(entries) is None
    # A full A,B,A inside the new round still detects.
    entries = _ledger_entries(_C, _C) + _ledger_entries(_A, _B, _A, rnd=2, start=0)
    assert mod.detect_oscillation(entries) == (frozenset(_A), frozenset(_B))


def test_detect_oscillation_requires_consecutive_attempts() -> None:
    mod = _import_v2(GOOD_DIR)
    entries = [
        {"attempt": 1, "round": 1, "failset": sorted(_A)},
        {"attempt": 2, "round": 1, "failset": sorted(_B)},
        {"attempt": 4, "round": 1, "failset": sorted(_A)},
    ]
    assert mod.detect_oscillation(entries) is None


def _ledger_harness(mod, root: Path, git_root: Path, max_retries: int, run_failsets: list):
    """Build a real loop: mock compress, real retry ledger, mock fix, mock run.

    ``run_failsets[i]`` is the set of test names attempt i+1's run reports as
    FAILED; ``None`` makes that run pass.  Returns (pipeline, ledger, fix_calls,
    run_calls).
    """
    from norn.dsl import OnFailure, Pipeline, Stage, stage_failed
    from norn.models import StageResult
    from norn.stages.base import BaseStage

    step = _LEDGER_STEP
    run_name = f"run validation {step}"
    ledger_name = f"retry ledger {step}"

    class _Compress(BaseStage):
        async def run(self, ctx, **kwargs):
            prev = ctx.results.get(run_name)
            if prev is None or prev.success:
                return StageResult(name="", success=True, output="")
            return StageResult(name="", success=True, output=prev.output["stdout"])

    class _Fix(BaseStage):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(self, ctx, **kwargs):
            self.calls.append(ctx.results[ledger_name].output)
            return StageResult(name="", success=True, output=f"fix response {len(self.calls)}")

    class _Run(BaseStage):
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, ctx, **kwargs):
            failset = run_failsets[self.calls]
            self.calls += 1
            if failset is None:
                return StageResult(name="", success=True,
                                   output={"stdout": "1 passed", "stderr": "", "returncode": 0})
            filler = "".join(f"progress line {i}\n" for i in range(80))
            failed = "".join(f"FAILED {t} - AssertionError\n" for t in sorted(failset))
            return StageResult(
                name="", success=False, error="Command exited with code 1",
                output={"stdout": f"run {self.calls}\n{filler}{failed}", "stderr": "",
                        "returncode": 1},
            )

    class _SpyLedger(mod._RetryLedger):
        def __init__(self, **kw) -> None:
            super().__init__(**kw)
            self.calls: list[tuple[int, bool, bool]] = []

        async def run(self, ctx, *, node_id, attempt, **kw):
            r = await super().run(ctx, node_id=node_id, attempt=attempt, **kw)
            self.calls.append((attempt, r.success, os.path.exists(self.paths["needs_human"])))
            return r

    ledger = _SpyLedger(step=step, root=str(root), git_root=str(git_root),
                        max_retries=max_retries)
    fix, run = _Fix(), _Run()
    pipeline = Pipeline("ledger-harness").loop(
        f"validate {step}",
        max_retries=max_retries,
        on_exhaust=OnFailure.FAIL,
        stages=[
            Stage(f"compress {step}", _Compress()),
            Stage(ledger_name, ledger, when=stage_failed(run_name)),
            Stage(f"fix {step}", fix, when=stage_failed(run_name)),
            Stage(run_name, run),
        ],
    )
    return pipeline, ledger, fix, run


@pytest.mark.asyncio
async def test_retry_ledger_through_a_real_loop(tmp_path) -> None:
    """Three failed runs then a pass, max_retries=4, via run_pipeline."""
    import json

    from norn.runner import run_pipeline

    mod = _import_v2(GOOD_DIR)
    root = tmp_path / "snapshots"
    root.mkdir()
    _init_temp_repo(tmp_path / "repo")
    pipeline, ledger, fix, run = _ledger_harness(
        mod, root, tmp_path / "repo", 4, [_A, _B, _C, None]
    )

    ctx = await run_pipeline(pipeline)

    step = _LEDGER_STEP
    assert [c[0] for c in ledger.calls] == [2, 3, 4], "ledger must run at attempts 2, 3 and 4"
    assert all(ok for _, ok, _ in ledger.calls)
    assert run.calls == 4
    assert len(fix.calls) == 3
    assert ctx.results[f"run validation {step}"].success

    def p(name: str) -> Path:
        return root / f"{step}.{name}"

    for n in (1, 2, 3):
        assert p(f"attempt-{n}.fail.txt").exists(), f"attempt-{n}.fail.txt missing"
    assert not p("attempt-4.fail.txt").exists()
    # The fix does not run in attempt 1.
    assert not p("attempt-1.fix.txt").exists()
    assert p("attempt-2.fix.txt").read_text() == "fix response 1"
    assert p("attempt-3.fix.txt").read_text() == "fix response 2"
    for n in (2, 3, 4):
        snap = p(f"attempt-{n}.pre-fix.json")
        assert snap.exists(), f"attempt-{n}.pre-fix.json missing"
        assert isinstance(json.loads(snap.read_text()), dict)
    assert not p("attempt-1.pre-fix.json").exists()

    # The fail tail is the last 60 lines of the compressed log.
    tail = p("attempt-1.fail.txt").read_text().splitlines()
    assert len(tail) == mod.LEDGER_FAIL_TAIL_LINES == 60
    assert tail[-1] == "FAILED tests/test_x.py::test_a - AssertionError"
    assert "run 1" not in tail

    entries = json.loads(p("ledger.json").read_text())
    assert [e["attempt"] for e in entries] == [1, 2, 3]
    assert [e["round"] for e in entries] == [1, 1, 1]
    assert [e["failset"] for e in entries] == [sorted(_A), sorted(_B), sorted(_C)]

    # Output reaches the fix: attempt 2 has no "two fixes" paragraph, attempt 3 does.
    at2, at3, at4 = fix.calls
    assert at2.startswith("Validation attempt 2 of 4.")
    assert "Two fixes have failed" not in at2
    assert "Validation attempt 3 of 4" in at3
    assert mod.LEDGER_TWO_FIXES_FAILED in at3
    assert "Two fixes have failed. Before editing, name the assumption" in at3
    assert "tests/test_x.py::test_a" in at3 and "tests/test_x.py::test_b" in at3
    assert "Validation attempt 4 of 4" in at4
    assert not p("needs-human").exists()
    assert not p("oscillation.md").exists()


def test_retry_ledger_sets_emits_events_for_the_attempt_kwarg(tmp_path) -> None:
    mod = _import_v2(GOOD_DIR)
    assert mod._RetryLedger.emits_events is True
    assert mod._RetryLedger.needs_agent is False


@pytest.mark.asyncio
async def test_retry_ledger_run_without_attempt_raises_type_error(tmp_path) -> None:
    from norn.models import PipelineContext

    mod = _import_v2(GOOD_DIR)
    stage = mod._RetryLedger(step=_LEDGER_STEP, root=str(tmp_path), git_root=str(tmp_path),
                             max_retries=3)
    with pytest.raises(TypeError):
        await stage.run(PipelineContext())
    with pytest.raises(TypeError):
        await stage.run(PipelineContext(), node_id="loop:x/stage:y")


@pytest.mark.asyncio
async def test_retry_ledger_stops_on_oscillation_through_a_real_loop(tmp_path) -> None:
    """A,B,A with max_retries=5: needs-human at attempt 4, attempt 5 fails at the ledger."""
    import json

    from norn.runner import RetriesExhaustedError, run_pipeline

    mod = _import_v2(GOOD_DIR)
    root = tmp_path / "snapshots"
    root.mkdir()
    _init_temp_repo(tmp_path / "repo")
    pipeline, ledger, fix, run = _ledger_harness(
        mod, root, tmp_path / "repo", 5, [_A, _B, _A, None, None]
    )

    with pytest.raises(RetriesExhaustedError) as exc_info:
        await run_pipeline(pipeline)

    assert ledger.calls == [
        (2, True, False),
        (3, True, False),
        (4, False, True),   # oscillation detected: needs-human written
        (5, False, True),   # fails at once, no fix
    ]
    assert len(fix.calls) == 2, "the fix runs only at attempts 2 and 3"
    assert run.calls == 3

    step = _LEDGER_STEP
    last = exc_info.value.result
    assert last.name == f"retry ledger {step}"
    assert "stopped before another fix; needs a human" in last.error

    entries = json.loads((root / f"{step}.ledger.json").read_text())
    assert [e["attempt"] for e in entries] == [1, 2, 3]

    osc = (root / f"{step}.oscillation.md").read_text()
    assert "tests/test_x.py::test_a" in osc and "tests/test_x.py::test_b" in osc
    assert "FAILED tests/test_x.py::test_b - AssertionError" in osc
    assert sum(1 for line in osc.splitlines() if line.startswith("### ")) == 1


@pytest.mark.asyncio
async def test_retry_ledger_detection_error_names_both_sets(tmp_path) -> None:
    """The stop's error names both fail sets; attempt 1 (a new round) clears it."""
    from norn.models import PipelineContext, StageResult

    mod = _import_v2(GOOD_DIR)
    _init_temp_repo(tmp_path / "repo")
    root = tmp_path / "snapshots"
    root.mkdir()
    step = _LEDGER_STEP
    stage = mod._RetryLedger(step=step, root=str(root), git_root=str(tmp_path / "repo"),
                             max_retries=6)
    ctx = PipelineContext()

    async def call(attempt: int, failset: set) -> StageResult:
        text = "".join(f"FAILED {t}\n" for t in sorted(failset))
        ctx.results[f"compress {step}"] = StageResult(name="c", success=True, output=text)
        ctx.results[f"run validation {step}"] = StageResult(
            name="r", success=False, output={"stdout": text, "stderr": "", "returncode": 1}
        )
        ctx.results[f"fix {step}"] = StageResult(name="f", success=True, output="a fix")
        result = await stage.run(ctx, node_id="n", attempt=attempt)
        ctx.results[f"retry ledger {step}"] = result
        return result

    assert (await call(2, _A)).success
    assert (await call(3, _B)).success
    stopped = await call(4, _A)
    assert not stopped.success
    assert "tests/test_x.py::test_a" in stopped.error
    assert "tests/test_x.py::test_b" in stopped.error
    assert "stopped before another fix; needs a human" in stopped.error
    assert (root / f"{step}.needs-human").exists()

    # An operator [r]etry restarts the loop at attempt 1: a new round.
    resumed = await call(1, _C)
    assert resumed.success, resumed.error
    assert not (root / f"{step}.needs-human").exists()
    assert "round 2" in resumed.output
    import json
    entries = json.loads((root / f"{step}.ledger.json").read_text())
    assert entries[-1] == {"attempt": 0, "round": 2, "failset": sorted(_C)}


def test_fix_prompt_has_retry_discipline() -> None:
    mod = _import_v2(GOOD_DIR)
    for stem in _GOOD_STEMS:
        prompt = _get_fix_prompt(mod, stem)
        assert "## Retry discipline" in prompt
        assert f"{{retry ledger {stem}.output}}" in prompt
        for label in ("PREVIOUS HYPOTHESIS:", "EVIDENCE:", "NEW HYPOTHESIS:"):
            assert label in prompt, f"{stem}: fix prompt missing {label!r}"
        assert "invalidated" in prompt
        assert "End your response with the same three lines" in prompt


def test_fix_prompt_template_has_no_stray_braces() -> None:
    """Outside the placeholders it means, the fix prompt template has no braces."""
    import re

    mod = _import_v2(GOOD_DIR)
    prompt = mod._fix_prompt("step-x", "/abs/step-x.md", "BODY", "CMD", 3)
    stripped = re.sub(r"\{(compress|retry ledger) step-x\.output\}", "", prompt)
    assert "{" not in stripped and "}" not in stripped


def test_max_retries_still_defaults_to_three() -> None:
    mod = _import_v2(GOOD_DIR)
    assert mod.MAX_RETRIES == 3
    for stem in _GOOD_STEMS:
        ledger = _stage(mod, f"retry ledger {stem}")
        assert ledger.impl.max_retries == 3
        assert ledger.impl.root == mod.snapshot_root
        assert ledger.impl.git_root == mod.GIT_TOPLEVEL


def test_retry_ledger_stage_runs_only_after_a_failed_run() -> None:
    from norn.models import PipelineContext, StageResult

    mod = _import_v2(GOOD_DIR)
    stem = _LEDGER_STEP
    ledger = _stage(mod, f"retry ledger {stem}")
    ctx = PipelineContext()
    assert ledger.when(ctx) is False
    ctx.results[f"run validation {stem}"] = StageResult(name="r", success=True)
    assert ledger.when(ctx) is False
    ctx.results[f"run validation {stem}"] = StageResult(name="r", success=False)
    assert ledger.when(ctx) is True


def test_handoff_manifest_cmd_has_oscillations_section() -> None:
    mod = _import_v2(GOOD_DIR)
    assert "## Oscillations" in mod._handoff_manifest_cmd()


@pytest.mark.asyncio
async def test_handoff_manifest_lists_oscillations_or_none(tmp_path, monkeypatch) -> None:
    repo, repo_path, feat_dir, mod, ctx = _setup_reviewed_repo(tmp_path, monkeypatch)
    assert (await _stage(mod, "record start").impl.run(ctx)).success
    manifest = Path(mod.snapshot_root) / "handoff-manifest.md"
    for stale in Path(mod.snapshot_root).glob("*.oscillation.md"):
        stale.unlink()

    r = await _stage(mod, "handoff manifest").impl.run(ctx)
    assert r.success, r.output
    text = manifest.read_text()
    section = text[text.index("## Oscillations"):]
    assert section.split("\n", 2)[2].strip() == "none"

    osc = Path(mod.snapshot_root) / f"{_LEDGER_STEP}.oscillation.md"
    osc.write_text(f"### {_LEDGER_STEP}: round 1, attempts 1-3\n\nfail set A\n")
    try:
        r = await _stage(mod, "handoff manifest").impl.run(ctx)
        assert r.success, r.output
        text = manifest.read_text()
        section = text[text.index("## Oscillations"):]
        assert f"### {_LEDGER_STEP}: round 1, attempts 1-3" in section
        assert "none" not in section
    finally:
        osc.unlink()


@pytest.mark.asyncio
async def test_record_baseline_resets_the_step_ledger(tmp_path, monkeypatch) -> None:
    """A stale ledger from an earlier run is cleared; oscillation.md is kept."""
    from norn.models import PipelineContext

    repo_path = tmp_path / "repo"
    _init_temp_repo(repo_path)
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)
    monkeypatch.chdir(repo_path)
    mod = _import_v2_in_repo(feat_dir, monkeypatch)

    root = Path(mod.snapshot_root)
    stale = [
        root / f"{_LEDGER_STEP}.ledger.json",
        root / f"{_LEDGER_STEP}.needs-human",
        root / f"{_LEDGER_STEP}.attempt-1.fail.txt",
        root / f"{_LEDGER_STEP}.attempt-2.fix.txt",
        root / f"{_LEDGER_STEP}.attempt-2.pre-fix.json",
    ]
    for f in stale:
        f.write_text("stale\n")
    other_step = root / "step-02-v2-fixture-beta.ledger.json"
    other_step.write_text("[]\n")
    osc = root / f"{_LEDGER_STEP}.oscillation.md"
    osc.write_text("history\n")

    r = await _stage(mod, f"record baseline {_LEDGER_STEP}").impl.run(PipelineContext())
    assert r.success, r.output
    for f in stale:
        assert not f.exists(), f"{f.name} must be cleared by record baseline"
    assert other_step.exists(), "another step's ledger must be left alone"
    assert osc.read_text() == "history\n", "oscillation.md is append-only"


# ---------------------------------------------------------------------------
# Learn stages, proposal only (step 11)
# ---------------------------------------------------------------------------

_LEARN_STAGES = [
    "collect learning evidence",
    "learn",
    "learn postcondition",
    "decide learning",
    "publish learning proposal",
    "record learning outcome",
]


def _import_v3_in(feature_dir: Path, *extra_argv: str):
    """Reload implement_features_v3 for *feature_dir* (cwd must be its repo)."""
    saved = list(sys.argv)
    sys.argv = [saved[0], str(feature_dir), *extra_argv]
    try:
        from norn.pipelines import implement_features_v3
        return importlib.reload(implement_features_v3)
    finally:
        sys.argv = saved


def _learn_items(mod) -> list:
    """The pipeline items between `review passed` and `handoff manifest`."""
    items = mod.config.items
    names = [getattr(i, "name", None) for i in items]
    return items[names.index("review passed") + 1 : names.index("handoff manifest")]


def test_learn_stages_sit_between_review_passed_and_handoff_manifest() -> None:
    from norn.dsl import ClearContext, Stage

    mod = _import_v2(GOOD_DIR)
    between = _learn_items(mod)
    assert isinstance(between[0], ClearContext), "the learn phase starts in a fresh session"
    assert isinstance(between[-1], ClearContext), "the handoff phase starts in a fresh session"
    stages = [i for i in between if isinstance(i, Stage)]
    assert [s.name for s in stages] == _LEARN_STAGES
    assert mod.LEARN_STAGE_NAMES == tuple(_LEARN_STAGES)


def test_learn_off_removes_every_learn_stage() -> None:
    from norn.dsl import Stage

    mod = _import_v2(GOOD_DIR, "--arg", "learn=off")
    assert mod.LEARN_ON is False
    names = {i.name for i in mod.config.items if isinstance(i, Stage)}
    assert names.isdisjoint(_LEARN_STAGES)
    assert all(not isinstance(i, Stage) for i in _learn_items(mod))


def test_learn_knob_defaults() -> None:
    mod = _import_v2(GOOD_DIR)
    assert mod.LEARN == "on" and mod.LEARN_ON is True
    assert mod.LEARN_MODEL == "light"
    assert _stage(mod, "learn").impl.model == "light"


def test_learn_model_knob_lands_on_the_learn_stage() -> None:
    mod = _import_v2(GOOD_DIR, "--arg", "learn_model=standard")
    assert _stage(mod, "learn").impl.model == "standard"


def test_bad_learn_knob_raises() -> None:
    with pytest.raises(ValueError, match="--arg learn: must be 'on' or 'off'"):
        _import_v2(GOOD_DIR, "--arg", "learn=bogus")


def test_bad_learn_model_raises() -> None:
    with pytest.raises(ValueError, match="--arg learn_model"):
        _import_v2(GOOD_DIR, "--arg", "learn_model=bogus")


def test_learn_knobs_are_documented_but_not_required_args() -> None:
    mod = _import_v2(GOOD_DIR)
    assert "--arg learn=on|off" in mod.__doc__
    assert "--arg learn_model=<label>" in mod.__doc__
    assert list(mod.metadata["args"]) == ["args"]


def test_learn_agent_stage_config() -> None:
    from norn.dsl import OnFailure
    from norn.pipelines._learnings import LearnGenerate

    mod = _import_v2(GOOD_DIR)
    s = _stage(mod, "learn")
    assert isinstance(s.impl, LearnGenerate)
    assert s.timeout is None, "a runner timeout would fail a run that passed review"
    assert s.impl.timeout == mod.TIMEOUT_LEARN == 300
    assert s.on_failure == OnFailure.FAIL
    assert s.impl.allowed_tools == ["Write"]
    assert s.impl.permission_mode == "acceptEdits"
    assert s.impl.add_dirs == [str(mod.LEARN_PATHS.scratch)]
    assert s.impl.max_turns == 4
    assert s.impl.paths.root == Path(mod.snapshot_root)
    assert s.impl.setting_sources is None, "the learn agent cannot read the repository"


@pytest.mark.asyncio
async def test_learn_write_guard_allows_only_the_three_scratch_files() -> None:
    mod = _import_v2(GOOD_DIR)
    hook = _stage(mod, "learn").impl.hooks["PreToolUse"][0]["hooks"][0]
    for path in mod.LEARN_PATHS.writable:
        assert await hook({"tool_input": {"file_path": str(path)}}, "t", None) == {}
    for path in (mod.LEARN_PATHS.store_hint, mod.LEARN_PATHS.agent_error,
                 Path(mod.PROJECT_DIR) / "README.md", mod.LEARN_PATHS.scratch / "slug.md"):
        denied = await hook({"tool_input": {"file_path": str(path)}}, "t", None)
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny", path


def test_learn_write_guard_is_claude_code_only() -> None:
    mod = _import_v2(GOOD_DIR, "--agent-provider", "opencode")
    assert _stage(mod, "learn").impl.hooks is None


def test_learn_stage_whens_match_the_table() -> None:
    """collect and record always run; the agent, postcondition and decision need
    learn.pre.json; publish needs learn.ready.  Every learn stage is on_failure=fail."""
    from norn.dsl import OnFailure
    from norn.models import PipelineContext

    mod = _import_v2(GOOD_DIR)
    paths = mod.LEARN_PATHS
    gates = {
        "collect learning evidence": None,
        "learn": paths.pre_json,
        "learn postcondition": paths.pre_json,
        "decide learning": paths.pre_json,
        "publish learning proposal": paths.marker_ready,
        "record learning outcome": None,
    }
    ctx = PipelineContext()
    try:
        for p in (paths.pre_json, paths.marker_ready):
            p.unlink(missing_ok=True)
        for name, gate in gates.items():
            s = _stage(mod, name)
            assert s.on_failure == OnFailure.FAIL, name
            if gate is None:
                assert s.when is None, name
            else:
                assert s.when(ctx) is False, name
        paths.pre_json.write_text("{}")
        for name, gate in gates.items():
            if gate is not None:
                assert _stage(mod, name).when(ctx) is (gate == paths.pre_json), name
        paths.pre_json.unlink()
        paths.marker_ready.write_text("t\n")
        for name, gate in gates.items():
            if gate is not None:
                assert _stage(mod, name).when(ctx) is (gate == paths.marker_ready), name
    finally:
        for p in (paths.pre_json, paths.marker_ready):
            p.unlink(missing_ok=True)


def test_no_learn_stage_commits_stages_or_pushes() -> None:
    import inspect
    import re

    mod = _import_v2(GOOD_DIR)
    forbidden = re.compile(r"git\s+(add|commit|push)\b")
    for name in _LEARN_STAGES:
        cmd = getattr(_stage(mod, name).impl, "cmd", "") or ""
        assert not forbidden.search(cmd), f"{name}: {cmd}"
    source = inspect.getsource(mod)
    learn_block = source[source.index("# Learn phase:"):source.index("# Handoff phase:")]
    assert not forbidden.search(learn_block)
    assert not re.search(r"""_learn_git\(\s*["'](add|commit|push)["']""", learn_block)
    from norn.pipelines import _learnings
    tail = inspect.getsource(_learnings)
    tail = tail[tail.index("# Learn building blocks"):]
    assert not forbidden.search(tail)
    assert "subprocess" not in tail, "the learn building blocks run no commands"


def test_learn_postcondition_checks_head_index_and_tree() -> None:
    mod = _import_v2(GOOD_DIR)
    cmd = _stage(mod, "learn postcondition").impl.cmd
    assert mod._assert_head_unchanged(mod.REVIEWED_HEAD, "the learn stages") in cmd
    assert mod._assert_index_clean("the learn stages") in cmd
    assert str(mod.LEARN_PATHS.pre_json) in cmd


def test_handoff_and_audit_keep_the_reviewed_head_rule() -> None:
    """Q1: no learn stage moves HEAD, so the v2 HEAD rule stays exactly as it was."""
    for extra in ((), ("--arg", "learn=off")):
        mod = _import_v2(GOOD_DIR, *extra)
        assert (
            mod._assert_head_unchanged(mod.REVIEWED_HEAD, "handoff")
            in mod._handoff_postcondition_cmd
        )
        assert (
            mod._assert_head_unchanged(mod.REVIEWED_HEAD, "the post-review phase")
            in mod._final_audit_cmd
        )
        assert "learn" not in " ".join(mod._all_changed_for_audit)


def test_handoff_prompt_quotes_the_learning_outcome() -> None:
    import re

    mod = _import_v2(GOOD_DIR)
    prompt = _stage(mod, "handoff").impl.prompt
    assert "{record learning outcome.output}" in prompt
    assert "**Learning**" in prompt and "**For the human**" in prompt
    assert "`Copy to commit:`" in prompt and "`Instruction-file note:`" in prompt
    assert "Learning: off" not in prompt

    off = _stage(_import_v2(GOOD_DIR, "--arg", "learn=off"), "handoff").impl.prompt
    assert "Learning: off" in off
    assert "{record learning outcome" not in off
    assert not re.search(r"\{[^{}]*learn[^{}]*\.output\}", off)


def test_learn_prompt_preview_is_the_shared_prompt() -> None:
    from norn.pipelines._learnings import DURABLE_BAR

    mod = _import_v2(GOOD_DIR)
    preview = _stage(mod, "learn").impl.prompt
    assert DURABLE_BAR in preview
    assert "{collect learning evidence.output}" in preview
    assert "<store>/<category>/<slug>.md" in preview


# -- learn stages against a temp repo ------------------------------------------


_LEARN_STEP = "step-01-v2-fixture-alpha"
_LEARN_LEARNING = Path(__file__).parent / "fixtures" / "learnings" / "corpus" / "docs" \
    / "solutions" / "integration-issues" / "portable-structured-output-schemas.md"
_LEARN_TARGET = "docs/solutions/integration-issues/portable-schemas.md"


def _learn_repo(tmp_path: Path, monkeypatch, *extra_argv: str, docs_root: str | None = None,
                agents_md: str | None = None):
    """A temp repo whose run passed review: base, one step commit, review.md, reviewed.head.

    Returns (repo, repo_path, mod).
    """
    repo_path = tmp_path / "repo"
    repo = _init_temp_repo(repo_path)
    (repo_path / "norn").mkdir()
    (repo_path / "norn" / "real.py").write_text("".join(f"x{i} = {i}\n" for i in range(1, 6)))
    note = repo_path / "docs" / "solutions" / "best-practices" / "fixture-notes.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntitle: Fixture notes\ndate: 2026-01-01\ncategory: best-practices\n"
        "module: tests\nproblem_type: best_practice\ncomponent: testing_framework\n"
        "severity: low\ntags:\n  - fixture\n---\n\n# Fixture notes\n\nKeep fixtures small.\n"
    )
    added = ["norn/real.py", "docs/solutions/best-practices/fixture-notes.md"]
    if docs_root is not None:
        cfg = repo_path / ".compound-engineering" / "config.yaml"
        cfg.parent.mkdir()
        cfg.write_text(f"docs_root: {docs_root}\n")
        added.append(".compound-engineering/config.yaml")
    if agents_md is not None:
        (repo_path / "AGENTS.md").write_text(agents_md)
        added.append("AGENTS.md")
    repo.index.add(added)
    repo.index.commit("base")
    feat_dir = repo_path / "feat"
    shutil.copytree(GOOD_DIR, feat_dir)
    monkeypatch.chdir(repo_path)
    mod = _import_v3_in(feat_dir, *extra_argv)

    root = Path(mod.snapshot_root)
    for stale in list(root.glob("learn*")) + list(root.glob("*.oscillation.md")):
        shutil.rmtree(stale) if stale.is_dir() else stale.unlink()
    Path(mod.BASE_SHA).write_text(repo.head.commit.hexsha + "\n")
    (repo_path / "feature.py").write_text("def feature():\n    return 1\n")
    repo.index.add(["feature.py"])
    repo.index.commit(f"refactor: {_LEARN_STEP} — Alpha")
    Path(mod.REVIEWED_HEAD).write_text(repo.head.commit.hexsha + "\n")
    Path(mod.REVIEW_MD).write_text("VERDICT: PASS\nBase: x\nHead: y\n\n## Blocking findings\n\nnone\n")
    return repo, repo_path, mod


def _journal(tmp_path: Path):
    from norn.journal import RunJournal

    run_dir = tmp_path / "state.runs" / "1"
    (run_dir / "stages").mkdir(parents=True)
    return RunJournal(run_dir)


def _learn_ctx(journal=None):
    from norn.models import PipelineContext

    ctx = PipelineContext()
    ctx.journal = journal
    return ctx


def _write_snapshot(path: Path, data: dict) -> None:
    import json

    path.write_text(json.dumps(data))


@pytest.mark.asyncio
async def test_collect_learning_evidence_sections_caps_and_baseline(tmp_path, monkeypatch) -> None:
    import json

    from norn.models import StageResult

    repo, repo_path, mod = _learn_repo(tmp_path, monkeypatch)
    root = Path(mod.snapshot_root)
    paths = mod.LEARN_PATHS
    step = _LEARN_STEP

    # The final review is longer than the cap.
    Path(mod.REVIEW_MD).write_text("VERDICT: PASS\n" + "r" * 25_000 + "\n")

    # Step 01: runs 1 and 2 failed, the fix before run 2 failed, the fix before run 3 passed.
    (root / f"{step}.ledger.json").write_text(json.dumps([
        {"attempt": 1, "round": 1, "failset": ["tests/t.py::test_a"]},
        {"attempt": 2, "round": 1, "failset": ["tests/t.py::test_b"]},
    ]))
    for n in (1, 2):
        (root / f"{step}.attempt-{n}.fail.txt").write_text(
            "".join(f"fail{n} line {i:02d}\n" for i in range(60))
        )
    (root / f"{step}.attempt-2.fix.txt").write_text("".join(f"fix2 line {i:02d}\n" for i in range(50)))
    _write_snapshot(root / f"{step}.attempt-2.pre-fix.json", {})
    _write_snapshot(root / f"{step}.attempt-3.pre-fix.json", {"a.py": ["??", "1"]})
    _write_snapshot(Path(mod._LEARN_STEP_PROOFS[step]),
                    {"a.py": ["??", "1"], "feature.py": ["??", "2"]})
    (root / f"{step}.head").write_text(Path(mod.BASE_SHA).read_text())
    short = repo.head.commit.hexsha[:7]
    (root / f"{step}.facts").write_text(
        f"Commit: {short} refactor: {step} — Alpha\nChanged: feature.py\nValidation: PASS - x\n"
    )
    # Step 02 failed nothing; its ledger must not linger from another test.
    (root / "step-02-v2-fixture-beta.ledger.json").unlink(missing_ok=True)

    ctx = _learn_ctx(_journal(tmp_path))
    ctx.results[f"fix {step}"] = StageResult(
        name="", success=True, output="the passing fix\nNEW HYPOTHESIS: the import order"
    )
    ctx.results[f"closeout {step}"] = StageResult(
        name="", success=True, output="EVIDENCE: test_a failed first"
    )

    seen_at_snapshot: list[bool] = []
    real_snapshot = mod._snapshot_repo

    def spy(out: str) -> None:
        seen_at_snapshot.append(Path(mod.LEARN_EVIDENCE).exists() and paths.scratch.is_dir())
        real_snapshot(out)

    monkeypatch.setattr(mod, "_snapshot_repo", spy)
    result = await _stage(mod, "collect learning evidence").impl.run(ctx)
    assert result.success, result.output
    text = result.output
    assert Path(mod.LEARN_EVIDENCE).read_text() == text
    assert not any(p.exists() for p in paths.markers), "a finished collection writes no marker"
    assert paths.pre_json.exists()
    assert seen_at_snapshot == [True], "learn.pre.json is collection's last action"

    sections = ["## Review", "## Validation attempts", "## Oscillations", "## Step closeouts",
                "## Commits", "## Related learnings"]
    positions = [text.index(s) for s in sections]
    assert positions == sorted(positions)

    review = text[text.index("## Review"):text.index("## Validation attempts")]
    kept = 20_000 - len("VERDICT: PASS\n")
    assert "r" * kept in review and "r" * (kept + 1) not in review
    assert "cut at 20000 characters" in review

    attempts = text[text.index(f"### {step}"):text.index("### step-02-v2-fixture-beta")]
    assert "fail1 line 59" in attempts and "fail1 line 20" in attempts
    assert "fail1 line 19" not in attempts, "only the last 40 lines of a failed run"
    assert "fix2 line 49" in attempts and "fix2 line 20" in attempts
    assert "fix2 line 19" not in attempts, "only the last 30 lines of a fix response"
    assert "Paths it changed: `a.py`" in attempts
    assert "Paths it changed: `feature.py`" in attempts
    assert "NEW HYPOTHESIS: the import order" in attempts
    assert "+def feature():" in attempts, "the passing fix's diff in the step commit"
    assert attempts.index("Fix before run 2") < attempts.index("Failed run 2")
    validation = text[text.index("## Validation attempts"):text.index("## Oscillations")]
    assert "### step-02-v2-fixture-beta\n\nNo failed attempts." in validation

    assert "## Oscillations\n\nnone" in text
    assert "EVIDENCE: test_a failed first" in text
    commits = text[text.index("## Commits"):text.index("## Related learnings")]
    assert f"refactor: {step}" in commits and "feature.py" in commits
    related = text[text.index("## Related learnings"):]
    assert "`docs/solutions/best-practices/fixture-notes.md` — full text" in related
    assert "Keep fixtures small." in related
    assert "Existing category directories: `best-practices`" in related


@pytest.mark.asyncio
async def test_collect_learning_evidence_caps_the_commit_log(tmp_path, monkeypatch) -> None:
    repo, repo_path, mod = _learn_repo(tmp_path, monkeypatch)
    for i in range(80):
        (repo_path / f"f{i}.py").write_text(f"x = {i}\n")
        repo.index.add([f"f{i}.py"])
        repo.index.commit(f"commit {i}")
    Path(mod.REVIEWED_HEAD).write_text(repo.head.commit.hexsha + "\n")
    result = await _stage(mod, "collect learning evidence").impl.run(_learn_ctx(_journal(tmp_path)))
    text = result.output
    commits = text[text.index("## Commits"):text.index("## Related learnings")]
    body = commits.split(mod._EVIDENCE_FENCE)[1].strip("\n").splitlines()
    assert len(body) == mod.LEARN_COMMITS_LINES + 1
    assert body[-1].endswith("more lines cut)")


@pytest.mark.asyncio
async def test_collect_without_a_journal_skips(tmp_path, monkeypatch) -> None:
    _, _, mod = _learn_repo(tmp_path, monkeypatch)
    paths = mod.LEARN_PATHS
    paths.pre_json.write_text("{}")  # stale from an earlier run
    result = await _stage(mod, "collect learning evidence").impl.run(_learn_ctx(None))
    assert result.success
    assert result.output == "skipped: no journal to hold the proposal"
    assert paths.marker_skipped.read_text() == "no journal to hold the proposal\n"
    assert not paths.pre_json.exists()


@pytest.mark.asyncio
async def test_collect_with_an_unusable_docs_root_fails_the_outcome(tmp_path, monkeypatch) -> None:
    _, _, mod = _learn_repo(tmp_path, monkeypatch, docs_root="../outside")
    paths = mod.LEARN_PATHS
    result = await _stage(mod, "collect learning evidence").impl.run(_learn_ctx(_journal(tmp_path)))
    assert result.success
    assert result.output.startswith("failed: the learning store is unusable: docs_root=")
    assert "../outside" in paths.marker_failed.read_text()
    assert not paths.pre_json.exists()


@pytest.mark.asyncio
async def test_learn_postcondition_fails_when_a_repo_file_changed(tmp_path, monkeypatch) -> None:
    repo, repo_path, mod = _learn_repo(tmp_path, monkeypatch)
    ctx = _learn_ctx(_journal(tmp_path))
    assert (await _stage(mod, "collect learning evidence").impl.run(ctx)).success
    post = _stage(mod, "learn postcondition").impl

    ok = await post.run(ctx)
    assert ok.success, ok.output

    (repo_path / "README.md").write_text("# changed by the learn agent\n")
    bad = await post.run(ctx)
    assert not bad.success
    assert "README.md" in bad.output["stderr"]
    assert "changed during the learn stages" in bad.output["stderr"]


@pytest.mark.asyncio
async def test_learn_postcondition_fails_when_head_moved(tmp_path, monkeypatch) -> None:
    repo, repo_path, mod = _learn_repo(tmp_path, monkeypatch)
    ctx = _learn_ctx(_journal(tmp_path))
    assert (await _stage(mod, "collect learning evidence").impl.run(ctx)).success
    (repo_path / "x.md").write_text("x\n")
    repo.index.add(["x.md"])
    repo.index.commit("docs(solutions): sneaked in")
    bad = await _stage(mod, "learn postcondition").impl.run(ctx)
    assert not bad.success
    assert "HEAD moved during the learn stages" in bad.output["stderr"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_name, target", [
    ("collect learning evidence", "_learning_evidence"),
    ("decide learning", "decide_learning"),
    ("publish learning proposal", "publish_proposal"),
    ("record learning outcome", "outcome_text"),
])
async def test_learn_outcome_stages_turn_errors_into_learn_failed(
    tmp_path, monkeypatch, stage_name, target
) -> None:
    import asyncio

    _, _, mod = _learn_repo(tmp_path, monkeypatch)
    paths = mod.LEARN_PATHS
    paths.scratch.mkdir(parents=True, exist_ok=True)
    ctx = _learn_ctx(_journal(tmp_path))

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mod, target, boom)
    result = await _stage(mod, stage_name).impl.run(ctx)
    assert result.success, f"{stage_name} must never fail the run"
    assert result.output == f"failed: {stage_name}: disk full"
    assert paths.marker_failed.read_text() == f"{stage_name}: disk full\n"
    assert not paths.pre_json.exists()

    def cancel(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mod, target, cancel)
    with pytest.raises(asyncio.CancelledError):
        await _stage(mod, stage_name).impl.run(ctx)


def _learn_pipeline(mod):
    from norn.dsl import Pipeline

    pipeline = Pipeline("learn-harness")
    pipeline.items.extend(_learn_items(mod))
    return pipeline


def _statuses(ctx) -> dict[str, str]:
    return {e.name: e.status for e in ctx.stage_log}


async def _no_agent(self, ctx, **kwargs):
    raise AssertionError("the learn agent must not run")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no journal", "invalid docs_root"])
async def test_early_learn_markers_through_run_pipeline(tmp_path, monkeypatch, case) -> None:
    from norn.runner import run_pipeline
    from norn.stages.generate import Generate

    docs_root = "../outside" if case == "invalid docs_root" else None
    _, _, mod = _learn_repo(tmp_path, monkeypatch, docs_root=docs_root)
    paths = mod.LEARN_PATHS
    journal = _journal(tmp_path) if case == "invalid docs_root" else None

    with patch.object(Generate, "run", _no_agent):
        ctx = await run_pipeline(_learn_pipeline(mod), journal=journal)

    assert not paths.pre_json.exists()
    status = _statuses(ctx)
    for name in ("learn", "learn postcondition", "decide learning", "publish learning proposal"):
        assert status[name] == "skipped_condition", f"{name}: {status[name]}"
    for name in ("collect learning evidence", "record learning outcome"):
        assert status[name] == "passed"
    outcome = ctx.results["record learning outcome"].output
    if case == "no journal":
        assert paths.marker_skipped.exists()
        assert outcome == "skipped: no journal to hold the proposal"
    else:
        assert paths.marker_failed.exists()
        assert outcome.startswith("failed: the learning store is unusable: ")
    assert paths.outcome.read_text() == outcome + "\n"


@pytest.mark.asyncio
async def test_learn_proposal_end_to_end_through_run_pipeline(tmp_path, monkeypatch) -> None:
    from norn.models import StageResult
    from norn.runner import run_pipeline
    from norn.stages.generate import Generate

    repo, repo_path, mod = _learn_repo(tmp_path, monkeypatch, agents_md="# Rules\n")
    paths = mod.LEARN_PATHS
    journal = _journal(tmp_path)
    head_before = repo.head.commit.hexsha
    prompts: list[str] = []

    async def agent(self, ctx, **kwargs):
        prompts.append(self._resolve_prompt(ctx))
        paths.learning.write_text(_LEARN_LEARNING.read_text())
        paths.target.write_text(_LEARN_TARGET + "\n")
        return StageResult(name="", success=True, output=f"learned: {_LEARN_TARGET}")

    with patch.object(Generate, "run", agent):
        ctx = await run_pipeline(_learn_pipeline(mod), journal=journal)

    assert len(prompts) == 1
    prompt = prompts[0]
    assert "# Learning evidence" in prompt, "the evidence is resolved into the prompt"
    assert "`docs/solutions/<category>/<slug>.md`" in prompt
    assert "`best-practices`" in prompt

    status = _statuses(ctx)
    assert all(status[n] == "passed" for n in _LEARN_STAGES), status
    proposal = journal.dir / "learning.md"
    assert proposal.read_text() == _LEARN_LEARNING.read_text()
    assert (journal.dir / "learning-target").read_text() == _LEARN_TARGET + "\n"
    outcome = ctx.results["record learning outcome"].output.splitlines()
    assert outcome[0] == f"proposed at {proposal}"
    assert outcome[1] == (
        f"Copy to commit: mkdir -p docs/solutions/integration-issues && "
        f"cp {proposal} {_LEARN_TARGET}"
    )
    assert outcome[2].startswith("Instruction-file note: `docs/solutions/` — documented solutions")

    # Q1: nothing was committed, staged or written into the repository.
    assert repo.head.commit.hexsha == head_before
    assert not (repo_path / _LEARN_TARGET).exists()
    assert repo.git.diff("--cached", "--name-only") == ""


@pytest.mark.asyncio
async def test_learn_agent_writing_into_the_repo_fails_the_run(tmp_path, monkeypatch) -> None:
    """The one fatal learn stage: an unreviewed change in a reviewed tree."""
    from norn.models import StageResult
    from norn.runner import PipelineError, run_pipeline
    from norn.stages.generate import Generate

    repo, repo_path, mod = _learn_repo(tmp_path, monkeypatch)

    async def rogue(self, ctx, **kwargs):
        (repo_path / _LEARN_TARGET).parent.mkdir(parents=True, exist_ok=True)
        (repo_path / _LEARN_TARGET).write_text(_LEARN_LEARNING.read_text())
        return StageResult(name="", success=True, output="learned")

    with patch.object(Generate, "run", rogue), pytest.raises(PipelineError) as exc_info:
        await run_pipeline(_learn_pipeline(mod), journal=_journal(tmp_path))
    assert exc_info.value.stage_name == "learn postcondition"


@pytest.mark.asyncio
async def test_learn_agent_timeout_is_a_failed_outcome_not_a_failed_run(tmp_path, monkeypatch) -> None:
    import asyncio

    from norn.runner import run_pipeline
    from norn.stages.generate import Generate

    _, _, mod = _learn_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(_stage(mod, "learn").impl, "timeout", 0.05)

    async def slow(self, ctx, **kwargs):
        await asyncio.sleep(5)

    with patch.object(Generate, "run", slow):
        ctx = await run_pipeline(_learn_pipeline(mod), journal=_journal(tmp_path))

    status = _statuses(ctx)
    assert status["publish learning proposal"] == "skipped_condition"
    outcome = ctx.results["record learning outcome"].output
    assert outcome == "failed: the learn agent timed out after 0.05s"
