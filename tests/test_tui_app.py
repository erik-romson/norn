"""Pilot integration tests for NornApp driving a pipeline run in-process.

Uses a mocked agent provider so no real Claude calls are made. The tests
prove that ``run_pipeline`` + ``EventSink`` + ``NornApp`` compose and
terminate without deadlock — exactly the risk the in-process model carries.

All tests are offline: no SDK calls, no subprocesses, no network.
"""
from __future__ import annotations

import asyncio

import pytest

from norn.dsl import Pipeline, Stage
from norn.graph import build_graph
from norn.models import PipelineContext, StageResult
from norn.stages.base import BaseStage
from norn.tui.app import NornApp, RunScreen
from norn.tui.viewmodel import RunViewModel
from norn.tui.widgets import NornGraph, NornHeader, STATUS_GLYPHS


# ---------------------------------------------------------------------------
# Stub stages
# ---------------------------------------------------------------------------


class _SuccessStage(BaseStage):
    """Non-agent stage that always succeeds."""

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        return StageResult(name="", success=True, output="ok")


class _FailStage(BaseStage):
    """Non-agent stage that always fails."""

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        return StageResult(name="", success=False, error="boom")


class _SlowStage(BaseStage):
    """Non-agent stage that takes a small delay, proving async works."""

    async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
        await asyncio.sleep(0.01)
        return StageResult(name="", success=True, output="slow-ok")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_for_run_done(app: NornApp, pilot, *, timeout: float = 5.0) -> None:
    """Poll until the app signals the run has finished, or timeout."""
    elapsed = 0.0
    step = 0.05
    while not app.run_finished and elapsed < timeout:
        await pilot.pause()
        await asyncio.sleep(step)
        elapsed += step
    # One last pause to drain any final messages
    await pilot.pause()


async def _wait_for_modal(app: NornApp, pilot, *, timeout: float = 5.0) -> bool:
    """Poll until the input-decision modal is the active screen, or timeout."""
    from norn.tui.modals import InputDecisionModal

    elapsed = 0.0
    step = 0.05
    while not isinstance(app.screen, InputDecisionModal) and elapsed < timeout:
        await pilot.pause()
        await asyncio.sleep(step)
        elapsed += step
    await pilot.pause()
    return isinstance(app.screen, InputDecisionModal)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_modal_retry_resolves_run():
    """When a stage fails with ASK_USER, the decision modal appears; pressing
    Retry re-runs the stage and the run completes successfully."""
    from norn.dsl import OnFailure

    calls: list[int] = []

    class _FailThenSucceed(BaseStage):
        async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
            calls.append(1)
            if len(calls) >= 2:
                return StageResult(name="", success=True, output="ok")
            return StageResult(name="", success=False, error="boom")

    pipeline = Pipeline("retry-modal").stage(
        "alpha", _FailThenSucceed(), on_failure=OnFailure.ASK_USER
    )
    app = NornApp(
        pipeline="retry-modal",
        graph=build_graph(pipeline),
        vm=RunViewModel(),
        pipeline_obj=pipeline,
    )

    async with app.run_test() as pilot:
        assert await _wait_for_modal(app, pilot), "decision modal did not appear"
        await pilot.press("r")  # Retry
        await _wait_for_run_done(app, pilot)

    assert app.run_success is True
    assert len(calls) == 2  # initial failure + retry


@pytest.mark.asyncio
async def test_failure_modal_abort_fails_run():
    """Pressing Abort in the modal aborts the run."""
    from norn.dsl import OnFailure

    pipeline = Pipeline("abort-modal").stage(
        "alpha", _FailStage(), on_failure=OnFailure.ASK_USER
    )
    app = NornApp(
        pipeline="abort-modal",
        graph=build_graph(pipeline),
        vm=RunViewModel(),
        pipeline_obj=pipeline,
    )

    async with app.run_test() as pilot:
        assert await _wait_for_modal(app, pilot), "decision modal did not appear"
        await pilot.press("a")  # Abort
        await _wait_for_run_done(app, pilot)

    assert app.run_success is False


@pytest.mark.asyncio
async def test_failure_modal_continue_proceeds_past_failure():
    """Pressing Continue proceeds past the failed stage without aborting."""
    from norn.dsl import OnFailure

    pipeline = Pipeline("continue-modal").stage(
        "alpha", _FailStage(), on_failure=OnFailure.ASK_USER
    )
    app = NornApp(
        pipeline="continue-modal",
        graph=build_graph(pipeline),
        vm=RunViewModel(),
        pipeline_obj=pipeline,
    )

    async with app.run_test() as pilot:
        assert await _wait_for_modal(app, pilot), "decision modal did not appear"
        await pilot.press("c")  # Continue past the failure
        await _wait_for_run_done(app, pilot)

    assert app.run_success is True


@pytest.mark.asyncio
async def test_two_stage_pipeline_reaches_finished():
    """A 2-stage pipeline runs end-to-end inside NornApp and both stages pass."""
    pipeline = (
        Pipeline("two-stage")
        .stage("alpha", _SuccessStage())
        .stage("beta", _SuccessStage())
    )
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(
        pipeline="two-stage",
        graph=graph,
        vm=vm,
        pipeline_obj=pipeline,
    )

    async with app.run_test() as pilot:
        await _wait_for_run_done(app, pilot)

        assert app.run_finished is True
        assert app.run_success is True

        # ViewModel header reflects finished state
        assert vm.header.status == "passed"
        assert vm.header.pipeline_name == "two-stage"
        assert vm.header.stages_done == 2


@pytest.mark.asyncio
async def test_run_screen_shows_controls_footer():
    """The run screen renders a Footer advertising the available controls so
    the user can see how to pause/resume, cancel, go back and quit while a run
    is in flight."""
    from textual.widgets import Footer

    class _Blocking(BaseStage):
        async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
            await asyncio.sleep(5)  # keep the run in flight for the assertions
            return StageResult(name="", success=True, output="ok")

    pipeline = Pipeline("footer-test").stage("alpha", _Blocking())
    graph = build_graph(pipeline)
    app = NornApp(
        pipeline="footer-test",
        graph=graph,
        vm=RunViewModel(),
        pipeline_obj=pipeline,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.run_finished is False  # still running

        # A Footer widget is mounted on the run screen.
        assert app.query(Footer)

        # The run controls are advertised as active bindings while running —
        # Cancel is enabled only mid-run, which is exactly the desired UX.
        descriptions = {ab.binding.description for ab in app.screen.active_bindings.values()}
        assert {"Pause/Resume", "Cancel", "Back", "Quit"} <= descriptions


@pytest.mark.asyncio
async def test_graph_shows_both_stages_passed():
    """After a successful 2-stage run, both graph nodes show the passed glyph."""
    pipeline = (
        Pipeline("graph-test")
        .stage("first", _SuccessStage())
        .stage("second", _SuccessStage())
    )
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(
        pipeline="graph-test",
        graph=graph,
        vm=vm,
        pipeline_obj=pipeline,
    )

    async with app.run_test() as pilot:
        await _wait_for_run_done(app, pilot)

        # Check node_status in the ViewModel
        assert vm.node_status.get("stage:first") == "passed"
        assert vm.node_status.get("stage:second") == "passed"

        # Check widget labels
        graph_widget = app.query_one(NornGraph)
        from textual.widgets import Tree

        tree = app.query_one(Tree)
        labels = [str(c.label) for c in tree.root.children]
        assert all(STATUS_GLYPHS["passed"] in lbl for lbl in labels), labels


@pytest.mark.asyncio
async def test_failed_stage_sets_run_success_false():
    """When a stage fails the app reports run_success=False."""
    pipeline = Pipeline("fail-test").stage("bad", _FailStage())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(
        pipeline="fail-test",
        graph=graph,
        vm=vm,
        pipeline_obj=pipeline,
    )

    async with app.run_test() as pilot:
        await _wait_for_run_done(app, pilot)

        assert app.run_finished is True
        assert app.run_success is False
        assert vm.header.status == "failed"


@pytest.mark.asyncio
async def test_slow_stage_does_not_block_ui():
    """A stage with async sleep completes without deadlock."""
    pipeline = (
        Pipeline("slow-test")
        .stage("wait", _SlowStage())
        .stage("after", _SuccessStage())
    )
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(
        pipeline="slow-test",
        graph=graph,
        vm=vm,
        pipeline_obj=pipeline,
    )

    async with app.run_test() as pilot:
        await _wait_for_run_done(app, pilot)

        assert app.run_finished is True
        assert app.run_success is True
        assert vm.header.stages_done == 2


@pytest.mark.asyncio
async def test_header_updates_during_run():
    """The header widget reflects the pipeline name and provider after events arrive."""
    pipeline = Pipeline("hdr-test").stage("s", _SuccessStage())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(
        pipeline="hdr-test",
        graph=graph,
        vm=vm,
        pipeline_obj=pipeline,
    )

    async with app.run_test() as pilot:
        await _wait_for_run_done(app, pilot)

        header = app.query_one(NornHeader)
        content = header.get_content()
        assert "hdr-test" in content


@pytest.mark.asyncio
async def test_no_pipeline_obj_no_run_task():
    """When no pipeline_obj is given, no background task is started."""
    vm = RunViewModel()
    app = NornApp(vm=vm)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._run_task is None
        assert app.run_finished is False


@pytest.mark.asyncio
async def test_run_kwargs_forwarded():
    """Extra run_kwargs are forwarded to run_pipeline."""
    pipeline = Pipeline("kw-test").stage("s", _SuccessStage())
    graph = build_graph(pipeline)
    vm = RunViewModel()
    app = NornApp(
        pipeline="kw-test",
        graph=graph,
        vm=vm,
        pipeline_obj=pipeline,
        run_kwargs={"agent_provider": "claude-code"},
    )

    async with app.run_test() as pilot:
        await _wait_for_run_done(app, pilot)

        assert app.run_finished is True
        assert app.run_success is True
        # Provider came through in events
        assert vm.header.provider == "claude-code"


# ---------------------------------------------------------------------------
# Back vs Quit — returning to the launcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_back_binding_sets_go_back_and_exits():
    """Pressing 'b' returns to the launcher: go_back is set, app exits."""
    pipeline = Pipeline("back-test").stage("s", _SuccessStage())
    graph = build_graph(pipeline)
    app = NornApp(pipeline="back-test", graph=graph)  # no pipeline_obj → no run task
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
    assert app.go_back is True


@pytest.mark.asyncio
async def test_quit_binding_does_not_set_go_back():
    """Pressing 'q' quits without go_back (full exit, not back to launcher)."""
    pipeline = Pipeline("quit-test").stage("s", _SuccessStage())
    graph = build_graph(pipeline)
    app = NornApp(pipeline="quit-test", graph=graph)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
    assert app.go_back is False


@pytest.mark.asyncio
async def test_back_during_run_leaves_no_live_task():
    """Pressing Back while a run is in flight cancels the run and waits for
    teardown — no live run task should remain when the app exits."""

    class _BlockingStage(BaseStage):
        async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
            await asyncio.sleep(10)
            return StageResult(name="", success=True, output="blocked")

    pipeline = Pipeline("back-cancel").stage("blocker", _BlockingStage())
    graph = build_graph(pipeline)
    app = NornApp(
        pipeline="back-cancel",
        graph=graph,
        vm=RunViewModel(),
        pipeline_obj=pipeline,
    )

    async with app.run_test() as pilot:
        # Let the run start and the stage begin sleeping.
        await pilot.pause()
        await asyncio.sleep(0.05)
        await pilot.pause()

        assert app._run_task is not None
        assert not app._run_task.done()

        # Press Back — should cancel + await the run task.
        await pilot.press("b")
        await pilot.pause()

    # After the app has exited, the run task must be done.
    assert app._run_task is not None
    assert app._run_task.done()
    assert app.go_back is True


# ---------------------------------------------------------------------------
# Worktree lifecycle in _drive_run (mock WorktreeSession)
# ---------------------------------------------------------------------------


class _MockMergeResult:
    """Lightweight mock for MergeResult."""

    def __init__(self, *, changed=True, merged=True, conflict=False,
                 refused=None, files=None, commit_sha=None,
                 work_branch="norn/run-abc12345", worktree_dir="/tmp/wt",
                 base_ref="main"):
        self.changed = changed
        self.merged = merged
        self.conflict = conflict
        self.refused = refused
        self.files = files or []
        self.commit_sha = commit_sha
        self.work_branch = work_branch
        self.worktree_dir = worktree_dir
        self.base_ref = base_ref


class _MockSession:
    """Mock WorktreeSession that records calls."""

    def __init__(self, *, merge_result=None):
        self.worktree_dir = "/tmp/mock-worktree"
        self.work_branch = "norn/run-test1234"
        self.base_ref = "main"
        self._merge_result = merge_result or _MockMergeResult()
        self.merge_back_calls: list[str] = []
        self.cleanup_calls: list[dict] = []

    def merge_back(self, *, message: str):
        self.merge_back_calls.append(message)
        return self._merge_result

    def cleanup(self, *, keep: bool = False):
        self.cleanup_calls.append({"keep": keep})


def _patch_worktree_create(monkeypatch, session):
    """Patch WorktreeSession.create to return *session* (for all worktree tests)."""
    import norn.worktree as wt_mod
    monkeypatch.setattr(wt_mod.WorktreeSession, "create", classmethod(lambda cls, rid, **kw: session))


def _make_wt_app(pipeline, *, graph, use_worktree=True, run_id="test1234"):
    """Build a NornApp whose base screen is a worktree-aware RunScreen."""
    screen = RunScreen(
        pipeline=pipeline.name,
        graph=graph,
        vm=RunViewModel(),
        pipeline_obj=pipeline,
        use_worktree=use_worktree,
        run_id=run_id,
    )
    app = NornApp(
        pipeline=pipeline.name,
        graph=graph,
        vm=RunViewModel(),
        pipeline_obj=pipeline,
    )
    app._run_screen = screen
    return app


@pytest.mark.asyncio
async def test_worktree_successful_merge(monkeypatch):
    """A successful worktree run merges and cleans up (branch deleted)."""
    session = _MockSession(merge_result=_MockMergeResult(
        changed=True, merged=True, files=["a.py", "b.py"], base_ref="main"
    ))
    _patch_worktree_create(monkeypatch, session)

    pipeline = Pipeline("wt-merge").stage("s", _SuccessStage())
    graph = build_graph(pipeline)
    app = _make_wt_app(pipeline, graph=graph, run_id="abc12345")

    async with app.run_test() as pilot:
        await _wait_for_run_done(app, pilot)

    assert app.run_success is True
    assert len(session.merge_back_calls) == 1
    assert "wt-merge" in session.merge_back_calls[0]
    assert len(session.cleanup_calls) == 1
    assert session.cleanup_calls[0]["keep"] is False
    assert app._run_screen.worktree_message is not None
    assert "Merged into main" in app._run_screen.worktree_message


@pytest.mark.asyncio
async def test_worktree_no_changes(monkeypatch):
    """A worktree run with no changes cleans up (branch deleted)."""
    session = _MockSession(merge_result=_MockMergeResult(changed=False, merged=False))
    _patch_worktree_create(monkeypatch, session)

    pipeline = Pipeline("wt-noop").stage("s", _SuccessStage())
    app = _make_wt_app(pipeline, graph=build_graph(pipeline), run_id="noop1234")

    async with app.run_test() as pilot:
        await _wait_for_run_done(app, pilot)

    assert app.run_success is True
    assert session.cleanup_calls[0]["keep"] is False
    assert "no changes" in app._run_screen.worktree_message


@pytest.mark.asyncio
async def test_worktree_merge_conflict(monkeypatch):
    """A merge conflict keeps the branch for manual resolution."""
    session = _MockSession(merge_result=_MockMergeResult(
        changed=True, merged=False, conflict=True, files=["conflict.py"],
    ))
    _patch_worktree_create(monkeypatch, session)

    pipeline = Pipeline("wt-conflict").stage("s", _SuccessStage())
    app = _make_wt_app(pipeline, graph=build_graph(pipeline), run_id="conf1234")

    async with app.run_test() as pilot:
        await _wait_for_run_done(app, pilot)

    assert app.run_success is True  # pipeline itself succeeded
    assert session.cleanup_calls[0]["keep"] is True
    assert "conflict" in app._run_screen.worktree_message.lower()


@pytest.mark.asyncio
async def test_worktree_pipeline_failure_keeps_branch(monkeypatch):
    """When the pipeline fails, the worktree branch is kept."""
    session = _MockSession()
    _patch_worktree_create(monkeypatch, session)

    pipeline = Pipeline("wt-fail").stage("bad", _FailStage())
    app = _make_wt_app(pipeline, graph=build_graph(pipeline), run_id="fail1234")

    async with app.run_test() as pilot:
        await _wait_for_run_done(app, pilot)

    assert app.run_success is False
    # merge_back should NOT be called (pipeline failed)
    assert len(session.merge_back_calls) == 0
    assert session.cleanup_calls[0]["keep"] is True
    assert "failed" in app._run_screen.worktree_message


@pytest.mark.asyncio
async def test_worktree_run_cancelled_keeps_branch(monkeypatch):
    """Cancelling a run keeps the worktree — cancelled work is still work.

    Exercises the real cancel path rather than calling ``_worktree_finish``
    directly: Back cancels the active stage task, the runner raises
    ``run_control.CancelledError``, and ``_drive_run`` turns that into
    ``cancelled=True``. That flag is the only thing standing between a
    cancelled run and a deleted branch.
    """

    class _BlockingStage(BaseStage):
        async def run(self, ctx: PipelineContext, **kwargs) -> StageResult:
            await asyncio.sleep(10)
            return StageResult(name="", success=True, output="blocked")

    session = _MockSession()
    _patch_worktree_create(monkeypatch, session)

    pipeline = Pipeline("wt-cancel").stage("blocker", _BlockingStage())
    app = _make_wt_app(pipeline, graph=build_graph(pipeline), run_id="canc1234")

    async with app.run_test() as pilot:
        # Let the run start and the stage begin blocking.
        await pilot.pause()
        await asyncio.sleep(0.05)
        await pilot.pause()
        assert app._run_task is not None and not app._run_task.done()

        await pilot.press("b")
        await pilot.pause()

    assert app.run_success is False
    # A cancelled run never reaches merge-back.
    assert len(session.merge_back_calls) == 0
    assert session.cleanup_calls[0]["keep"] is True
    assert "cancelled" in app._run_screen.worktree_message


@pytest.mark.asyncio
async def test_worktree_merge_refused_keeps_branch(monkeypatch):
    """A refusal that is neither a conflict nor a no-op keeps the worktree.

    ``dirty-launch-tree``, ``no-identity`` and plain git errors all land in the
    same trailing branch of ``_worktree_finish``; nothing else covers it.
    """
    session = _MockSession(merge_result=_MockMergeResult(
        changed=True, merged=False, conflict=False, refused="dirty-launch-tree",
    ))
    _patch_worktree_create(monkeypatch, session)

    pipeline = Pipeline("wt-refused").stage("s", _SuccessStage())
    app = _make_wt_app(pipeline, graph=build_graph(pipeline), run_id="refu1234")

    async with app.run_test() as pilot:
        await _wait_for_run_done(app, pilot)

    assert app.run_success is True  # the pipeline itself succeeded
    assert len(session.merge_back_calls) == 1
    assert session.cleanup_calls[0]["keep"] is True
    assert "dirty-launch-tree" in app._run_screen.worktree_message


@pytest.mark.asyncio
async def test_worktree_create_failure_reports_error(monkeypatch):
    """WorktreeError during create surfaces as a run failure."""
    import norn.worktree as wt_mod
    from norn.worktree import WorktreeError

    def _fail_create(cls, rid, **kw):
        raise WorktreeError("Working tree is dirty")

    monkeypatch.setattr(wt_mod.WorktreeSession, "create", classmethod(_fail_create))

    pipeline = Pipeline("wt-dirty").stage("s", _SuccessStage())
    app = _make_wt_app(pipeline, graph=build_graph(pipeline), run_id="dirty123")

    async with app.run_test() as pilot:
        await _wait_for_run_done(app, pilot)

    assert app.run_finished is True
    assert app.run_success is False
    assert "dirty" in (app._run_screen.worktree_message or "").lower()


@pytest.mark.asyncio
async def test_worktree_resume_combination_rejected(monkeypatch):
    """Worktree mode combined with resume is rejected before any run starts:
    an error notification is shown and no RunScreen is pushed."""
    from norn.tui.app import NornUIApp

    app = NornUIApp()

    notifications: list[dict] = []
    pushed: list[object] = []

    async with app.run_test() as pilot:
        await pilot.pause()  # let on_mount push the launcher first
        monkeypatch.setattr(
            app, "notify",
            lambda message, **kw: notifications.append({"message": message, **kw}),
        )
        monkeypatch.setattr(
            app, "push_screen",
            lambda *a, **k: pushed.append(a[0] if a else None),
        )

        # The rejection short-circuits before the pipeline ref is ever loaded,
        # so a non-existent ref is fine here.
        app._start_run(
            "wt-resume", False, "wt-resume", True, {},
            resume=True, use_worktree=True,
        )
        await pilot.pause()

    assert any(n.get("severity") == "error" for n in notifications)
    assert any("resume" in n["message"].lower() for n in notifications)
    assert not any(isinstance(s, RunScreen) for s in pushed)
