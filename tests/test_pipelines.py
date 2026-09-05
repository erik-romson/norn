"""Tests for bundled pipelines in norn/pipelines/."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from norn.dsl import Loop, Parallel, Stage
from norn.stages.generate import Generate

FIXTURE_FEATURE_DIR = Path(__file__).parent / "fixtures" / "implement_features_min"


def _collect_generates(items):
    """Walk pipeline items and yield all Generate impl instances."""
    for item in items:
        if isinstance(item, Stage) and isinstance(item.impl, Generate):
            yield item.impl
        elif isinstance(item, (Loop, Parallel)):
            yield from _collect_generates(item.stages)


def _import_implement_features():
    """Import implement_features against a fixture feature dir.

    The module reads ``sys.argv`` at import time to locate its step files, so a
    plain import under pytest resolves to whatever sits in the repo's ``tmp/``
    and raises ``ValueError`` — which used to skip these tests everywhere,
    including CI. Patching argv and reloading gives every test a stable
    pipeline to inspect instead.
    """
    saved = list(sys.argv)
    sys.argv = [saved[0], str(FIXTURE_FEATURE_DIR)]
    try:
        from norn.pipelines import implement_features

        return importlib.reload(implement_features)
    finally:
        sys.argv = saved


def test_pipelines_package_exists() -> None:
    """The norn.pipelines package is importable."""
    import norn.pipelines  # noqa: F401


def test_hello_has_metadata() -> None:
    """hello.py exposes a metadata dict."""
    from norn.pipelines import hello

    assert isinstance(hello.metadata, dict)
    assert "args" in hello.metadata


def test_vanilla_change_has_metadata() -> None:
    """vanilla_change.py exposes a metadata dict."""
    from norn.pipelines import vanilla_change

    assert isinstance(vanilla_change.metadata, dict)
    assert "env_vars" in vanilla_change.metadata
    assert "args" in vanilla_change.metadata


def test_implement_features_has_metadata() -> None:
    """implement_features.py exposes a metadata dict."""
    implement_features = _import_implement_features()

    assert isinstance(implement_features.metadata, dict)
    assert "env_vars" in implement_features.metadata
    assert "args" in implement_features.metadata


def test_hello_generate_stages_have_no_pinned_cwd() -> None:
    """hello.py Generate stages must not pin a cwd so they inherit ctx.working_dir."""
    from norn.pipelines import hello

    generates = list(_collect_generates(hello.config.items))
    assert generates, "expected at least one Generate stage in hello"
    for gen in generates:
        assert gen.cwd is None, (
            f"hello pipeline Generate stage has cwd={gen.cwd!r}; "
            "remove the import-time cwd pin so the stage inherits ctx.working_dir"
        )


def test_vanilla_change_generate_stages_have_no_pinned_cwd() -> None:
    """vanilla_change.py Generate stages must not pin a cwd so they inherit ctx.working_dir."""
    from norn.pipelines import vanilla_change

    generates = list(_collect_generates(vanilla_change.config.items))
    assert generates, "expected at least one Generate stage in vanilla_change"
    for gen in generates:
        assert gen.cwd is None, (
            f"vanilla_change pipeline Generate stage has cwd={gen.cwd!r}; "
            "remove the import-time cwd pin so the stage inherits ctx.working_dir"
        )


def test_implement_features_generate_stages_have_no_pinned_cwd() -> None:
    """implement_features.py Generate stages must not pin a cwd so they inherit ctx.working_dir."""
    implement_features = _import_implement_features()

    generates = list(_collect_generates(implement_features.config.items))
    assert generates, "expected at least one Generate stage in implement_features"
    for gen in generates:
        assert gen.cwd is None, (
            f"implement_features pipeline Generate stage has cwd={gen.cwd!r}; "
            "remove the import-time cwd pin so the stage inherits ctx.working_dir"
        )


def _commit_stage_cmd(implement_features) -> str:
    """Return the shell command of the first `commit <step>` stage."""
    for item in implement_features.config.items:
        if isinstance(item, Stage) and item.name.startswith("commit "):
            return item.impl.cmd
    raise AssertionError("no commit stage found in implement_features")


def test_commit_stage_aborts_when_git_add_fails() -> None:
    """A failing `git add` must not fall through to `git commit`.

    The old chain was `(A && B) || C`, so a non-zero `xargs -0 git add` made the
    left operand false and ran the commit anyway.
    """
    cmd = _commit_stage_cmd(_import_implement_features())

    assert "xargs -0 git add -A --" in cmd
    assert "ERROR: git add failed for the changed paths of this step" in cmd
    assert "(git diff --cached --quiet && echo \"nothing to commit\") || " not in cmd


def test_commit_stage_retries_once_after_hook_fixes() -> None:
    """Auto-fixing pre-commit hooks reject the commit; re-stage and retry once."""
    cmd = _commit_stage_cmd(_import_implement_features())

    assert cmd.count("git commit -F -") == 2, "expected exactly one retry"
    assert "--hook-fixes" in cmd
    assert "re-staging files rewritten by pre-commit hooks" in cmd
    assert "ERROR: commit failed twice." in cmd


def test_commit_stage_restages_from_git_state_not_the_changed_list() -> None:
    """The re-stage must read git state, not the step's snapshot changed list.

    Hooks run against the whole staged set, so the file a hook rewrites is often
    one an earlier step staged; a re-stage scoped to this step's paths would
    never pick it up.
    """
    cmd = _commit_stage_cmd(_import_implement_features())

    retry_block = cmd.split("commit rejected", 1)[1]
    assert ".changed" not in retry_block, "retry must not re-read the changed list"
    assert ".hookfix" in retry_block


def test_commit_stage_clears_partial_staging_before_first_attempt() -> None:
    """Partial staging left by an earlier rejected commit is cleared up front.

    Otherwise pre-commit stashes the unstaged delta, collides with its own
    fixes, rolls them back, and every retry reproduces the failure.
    """
    cmd = _commit_stage_cmd(_import_implement_features())

    first_commit = cmd.index("git commit -F -")
    assert cmd.index("--hook-fixes") < first_commit
    assert cmd.count("--hook-fixes") == 2, "expected a pre-attempt and a retry re-stage"


def _write_status(tmp_path, *lines: str) -> str:
    """Write a `git status --porcelain -uall` snapshot and return its path."""
    path = tmp_path / "status.txt"
    path.write_text("".join(f"{line}\n" for line in lines))
    return str(path)


def test_hook_fixes_selects_partially_staged_paths(tmp_path) -> None:
    """A staged file an auto-fixing hook rewrote shows as `XM` and must be re-staged.

    This is the state ruff-format/black/prettier leave behind when they rewrite
    a staged file and reject the commit: staged in the index, modified again in
    the worktree.
    """
    from norn.pipelines._snapshot_diff import hook_fixes

    status = _write_status(
        tmp_path,
        "MM modified.py",
        "AM added.py",
        "RM old.py -> renamed.py",
        "CM copied.py",
    )

    assert hook_fixes(status) == ["added.py", "copied.py", "modified.py", "renamed.py"]


def test_hook_fixes_ignores_untracked_paths(tmp_path) -> None:
    """Untracked paths were never staged, so sweeping them in would commit
    files that were dirty before the step ran — the exact promise the snapshot
    diff exists to keep."""
    from norn.pipelines._snapshot_diff import hook_fixes

    status = _write_status(tmp_path, "?? scratch.txt", "MM staged.py")

    assert hook_fixes(status) == ["staged.py"]


def test_hook_fixes_ignores_paths_with_nothing_staged(tmp_path) -> None:
    """A worktree-only modification is somebody else's dirty file, not a hook fix."""
    from norn.pipelines._snapshot_diff import hook_fixes

    status = _write_status(tmp_path, " M dirty.py", "MM staged.py")

    assert hook_fixes(status) == ["staged.py"]


def test_hook_fixes_ignores_cleanly_staged_paths(tmp_path) -> None:
    """A fully staged path needs no re-add; only an unstaged delta means a rewrite."""
    from norn.pipelines._snapshot_diff import hook_fixes

    status = _write_status(tmp_path, "M  clean.py", "A  new.py", "D  gone.py")

    assert hook_fixes(status) == []


def test_hook_fixes_ignores_staged_then_deleted_paths(tmp_path) -> None:
    """`MD` is a staged file deleted from the worktree, not a hook rewrite."""
    from norn.pipelines._snapshot_diff import hook_fixes

    status = _write_status(tmp_path, "MD vanished.py")

    assert hook_fixes(status) == []
