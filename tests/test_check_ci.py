from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from norn.models import PipelineContext
from norn.stages.check_ci import CheckCI, _summarize_log


def _make_run(
    *,
    run_id: int = 123,
    name: str = "CI",
    status: str = "completed",
    conclusion: str | None = "success",
    html_url: str = "https://github.com/org/repo/actions/runs/123",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id, name=name, status=status, conclusion=conclusion, html_url=html_url,
    )


def _make_job(
    *,
    job_id: int = 1,
    name: str = "build",
    conclusion: str = "success",
    steps: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(id=job_id, name=name, conclusion=conclusion, steps=steps or [])


def _make_step(*, name: str = "Run tests", conclusion: str = "failure") -> SimpleNamespace:
    return SimpleNamespace(name=name, conclusion=conclusion)


def _mock_gh(runs: list, jobs: list | None = None, job_log: str = ""):
    """Build a mock GitHub client with async methods."""
    gh = MagicMock()

    # list_workflow_runs_for_repo
    runs_resp = SimpleNamespace(parsed_data=SimpleNamespace(workflow_runs=runs))
    gh.rest.actions.async_list_workflow_runs_for_repo = AsyncMock(return_value=runs_resp)
    gh.rest.actions.async_list_workflow_runs = AsyncMock(return_value=runs_resp)

    # list_jobs_for_workflow_run
    if jobs is not None:
        jobs_resp = SimpleNamespace(parsed_data=SimpleNamespace(jobs=jobs))
        gh.rest.actions.async_list_jobs_for_workflow_run = AsyncMock(return_value=jobs_resp)

    # download_job_logs_for_workflow_run
    log_resp = SimpleNamespace(text=job_log)
    gh.rest.actions.async_download_job_logs_for_workflow_run = AsyncMock(return_value=log_resp)

    return gh


@pytest.fixture
def _patch_env(monkeypatch):
    """Patch token resolution and repo/branch detection."""
    def _apply(token="ghp_test123", repo="org/repo", branch="main"):
        monkeypatch.setattr(
            "norn.stages.check_ci._githubkit_available", lambda: True,
        )
        monkeypatch.setattr(
            "norn.stages.check_ci._resolve_token", AsyncMock(return_value=token),
        )
        monkeypatch.setattr(
            "norn.stages.check_ci._detect_repo", AsyncMock(return_value=repo),
        )
        monkeypatch.setattr(
            "norn.stages.check_ci._current_branch", AsyncMock(return_value=branch),
        )
    return _apply


@pytest.mark.asyncio
async def test_success_run(_patch_env):
    _patch_env()
    gh = _mock_gh([_make_run()])

    with patch("norn.stages.check_ci._create_client", return_value=gh):
        stage = CheckCI()
        result = await stage.run(PipelineContext())

    assert result.success
    assert result.output["conclusion"] == "success"
    assert result.output["run_id"] == 123


@pytest.mark.asyncio
async def test_failed_run_fetches_logs(_patch_env):
    _patch_env()
    failed_step = _make_step(name="Run tests", conclusion="failure")
    failed_job = _make_job(job_id=10, name="build", conclusion="failure", steps=[failed_step])
    gh = _mock_gh(
        [_make_run(run_id=456, name="Build", conclusion="failure")],
        jobs=[failed_job],
        job_log="Error: test_foo FAILED\nassert 1 == 2",
    )

    with patch("norn.stages.check_ci._create_client", return_value=gh):
        stage = CheckCI()
        result = await stage.run(PipelineContext())

    assert not result.success
    assert result.output["conclusion"] == "failure"
    assert "Failed job: build" in result.output["logs"]
    assert "test_foo FAILED" in result.output["logs"]


@pytest.mark.asyncio
async def test_no_runs_found(_patch_env):
    _patch_env(branch="feature-x")
    gh = _mock_gh([])

    with patch("norn.stages.check_ci._create_client", return_value=gh):
        stage = CheckCI(branch="feature-x")
        result = await stage.run(PipelineContext())

    assert not result.success
    assert "No workflow runs found" in result.error


@pytest.mark.asyncio
async def test_in_progress_without_poll(_patch_env):
    _patch_env()
    gh = _mock_gh([_make_run(run_id=789, name="CI", status="in_progress", conclusion=None)])

    with patch("norn.stages.check_ci._create_client", return_value=gh):
        stage = CheckCI(poll=False)
        result = await stage.run(PipelineContext())

    assert not result.success
    assert "still in_progress" in result.error


@pytest.mark.asyncio
async def test_explicit_repo_and_branch(_patch_env):
    _patch_env()
    gh = _mock_gh([_make_run(run_id=100, name="Tests")])

    with patch("norn.stages.check_ci._create_client", return_value=gh):
        stage = CheckCI(repo="myorg/myrepo", branch="dev")
        result = await stage.run(PipelineContext())

    assert result.success
    gh.rest.actions.async_list_workflow_runs_for_repo.assert_called_once_with(
        owner="myorg", repo="myrepo", branch="dev", per_page=1,
    )


@pytest.mark.asyncio
async def test_workflow_filter(_patch_env):
    _patch_env()
    gh = _mock_gh([_make_run(run_id=200, name="E2E")])

    with patch("norn.stages.check_ci._create_client", return_value=gh):
        stage = CheckCI(workflow="e2e-tests.yml")
        result = await stage.run(PipelineContext())

    assert result.success
    gh.rest.actions.async_list_workflow_runs.assert_called_once_with(
        owner="org", repo="repo", workflow_id="e2e-tests.yml", branch="main", per_page=1,
    )


@pytest.mark.asyncio
async def test_no_token(_patch_env):
    _patch_env(token=None)

    stage = CheckCI(repo="org/repo")
    result = await stage.run(PipelineContext())

    assert not result.success
    assert "No GitHub token found" in result.error


@pytest.mark.asyncio
async def test_no_repo(_patch_env):
    _patch_env(repo=None)

    stage = CheckCI()
    result = await stage.run(PipelineContext())

    assert not result.success
    assert "Could not detect GitHub repo" in result.error


@pytest.mark.asyncio
async def test_missing_githubkit(monkeypatch):
    monkeypatch.setattr(
        "norn.stages.check_ci._githubkit_available", lambda: False,
    )

    stage = CheckCI(repo="org/repo")
    result = await stage.run(PipelineContext())

    assert not result.success
    assert "githubkit is not installed" in result.error


# ---------- _summarize_log ----------

def test_summarize_strips_timestamps_and_groups():
    raw = "\n".join([
        "2026-04-13T15:38:30.2534247Z ##[group]Setup Java",
        "2026-04-13T15:38:30.2534247Z Downloading JDK...",
        "2026-04-13T15:38:30.2534247Z ##[endgroup]",
        "2026-04-13T15:38:31.0000000Z ##[error]Process completed with exit code 1.",
    ])
    out = _summarize_log(raw, context_lines=5, max_lines=100)
    assert "2026-04-13" not in out
    assert "##[group]" not in out
    assert "##[endgroup]" not in out
    assert "Downloading JDK..." in out
    assert "##[error]Process completed with exit code 1." in out


def test_summarize_extracts_window_around_error_marker():
    lines = [f"2026-04-13T00:00:00.0000000Z line {i}" for i in range(100)]
    lines[50] = "2026-04-13T00:00:00.0000000Z ##[error]boom"
    raw = "\n".join(lines)

    out = _summarize_log(raw, context_lines=10, max_lines=100)
    assert "##[error]boom" in out
    assert "line 45" in out  # within context window
    assert "line 55" in out  # within after-window (10 // 2 = 5)
    assert "line 0" not in out
    assert "line 99" not in out


def test_summarize_falls_back_to_keywords():
    lines = [f"line {i}" for i in range(50)]
    lines[25] = "FATAL: Build failed with exit code 2"
    raw = "\n".join(lines)

    out = _summarize_log(raw, context_lines=5, max_lines=100)
    assert "FATAL: Build failed" in out
    assert "line 22" in out
    assert "line 0" not in out


def test_summarize_falls_back_to_tail_when_no_errors():
    raw = "\n".join(f"routine line {i}" for i in range(500))
    out = _summarize_log(raw, context_lines=10, max_lines=50)
    assert "no error markers found" in out
    assert "routine line 499" in out
    assert "routine line 0" not in out


def test_summarize_merges_overlapping_windows():
    lines = [f"line {i}" for i in range(100)]
    lines[10] = "##[error]first"
    lines[15] = "##[error]second"
    raw = "\n".join(lines)

    out = _summarize_log(raw, context_lines=10, max_lines=200)
    assert "first" in out
    assert "second" in out
    # Since windows overlap, should not have a "..." separator between them
    assert out.count("\n...") == 0


def test_summarize_adds_separator_between_distant_windows():
    lines = [f"line {i}" for i in range(200)]
    lines[10] = "##[error]first"
    lines[180] = "##[error]second"
    raw = "\n".join(lines)

    out = _summarize_log(raw, context_lines=5, max_lines=500)
    assert "first" in out
    assert "second" in out
    assert "\n...\n" in out


def test_summarize_respects_max_lines():
    lines = ["##[error]fail"] * 200
    raw = "\n".join(lines)
    out = _summarize_log(raw, context_lines=20, max_lines=30)
    assert len(out.splitlines()) <= 30 + 1  # +1 for the truncation header


def test_summarize_truncates_at_post_job_cleanup():
    raw = "\n".join([
        "[INFO] Building cbslink-common",
        "[ERROR] Compilation failure",
        "##[error]Process completed with exit code 1.",
        "Post job cleanup.",
        "[command]/usr/bin/git version",
        "git version 2.53.0",
        "Cleaning up orphan processes",
        "##[warning]Node.js 20 actions are deprecated.",
    ])
    out = _summarize_log(raw, context_lines=20, max_lines=100)
    assert "Compilation failure" in out
    assert "##[error]Process completed" in out
    assert "Post job cleanup" not in out
    assert "git version" not in out
    assert "orphan processes" not in out
    assert "Node.js 20 actions are deprecated" not in out


def test_summarize_drops_git_housekeeping_noise():
    raw = "\n".join([
        "[ERROR] Compilation failure in X.java",
        "##[error]Build failed",
        "[command]/usr/bin/git config --local --name-only --get-regexp core",
        "Temporarily overriding HOME='/tmp/xyz'",
        "Adding repository directory to the temporary git global config",
    ])
    out = _summarize_log(raw, context_lines=10, max_lines=100)
    assert "Compilation failure" in out
    # Cleanup truncation isn't triggered here (no "Post job cleanup."),
    # but the noise-prefix filter should still strip these lines.
    assert "[command]/usr/bin/git" not in out
    assert "Temporarily overriding HOME" not in out
    assert "Adding repository directory" not in out


def test_summarize_warnings_are_not_error_markers():
    """Deprecation warnings should not trigger the error-window extractor."""
    lines = [f"routine {i}" for i in range(40)]
    lines[20] = "##[warning]Node.js 20 deprecated"
    raw = "\n".join(lines)
    out = _summarize_log(raw, context_lines=5, max_lines=100)
    # With no real errors, we should fall back to tail-of-log mode.
    assert "no error markers found" in out


# ---------- Anchor-based extraction for real-world tool logs ----------

def test_summarize_maven_build_failure():
    """Real Maven compilation failure: full BUILD FAILURE block is captured."""
    raw = "\n".join([
        "2026-04-13T15:37:58.0000000Z ##[group]Run mvn clean package",
        "2026-04-13T15:38:00.0000000Z [INFO] Scanning for projects...",
        "2026-04-13T15:38:25.0000000Z [INFO] Building cbslink-common 1.0",
        "2026-04-13T15:38:27.0000000Z [INFO] " + "-" * 72,
        "2026-04-13T15:38:27.0000000Z [INFO] BUILD FAILURE",
        "2026-04-13T15:38:27.0000000Z [INFO] " + "-" * 72,
        "2026-04-13T15:38:27.0000000Z [INFO] Total time:  29.644 s",
        "2026-04-13T15:38:27.0000000Z Error:  Failed to execute goal ...compile",
        "2026-04-13T15:38:27.0000000Z Error:  /path/Foo.java:[4,26] cannot find symbol",
        "2026-04-13T15:38:27.0000000Z Error:    symbol: class DatabasePlatform",
        "2026-04-13T15:38:29.0000000Z ##[error]Process completed with exit code 1.",
        "2026-04-13T15:38:30.0000000Z Post job cleanup.",
        "2026-04-13T15:38:30.0000000Z [command]/usr/bin/git version",
    ])
    out = _summarize_log(raw)
    assert "detected: maven" in out
    assert "[INFO] BUILD FAILURE" in out
    assert "cannot find symbol" in out
    assert "DatabasePlatform" in out
    assert "Process completed with exit code 1." in out
    # The [INFO] --- separator just above BUILD FAILURE should be included
    # as lead context.
    assert out.count("-" * 72) >= 1
    # Cleanup noise must not leak in.
    assert "Post job cleanup" not in out
    assert "git version" not in out
    # Pre-failure "Scanning for projects..." should NOT be included —
    # only 2 lines of lead context above the anchor.
    assert "Scanning for projects" not in out


def test_summarize_gradle_build_failure():
    raw = "\n".join([
        "> Task :compileJava FAILED",
        "/src/main/java/Foo.java:10: error: cannot find symbol",
        "        Bar b = new Bar();",
        "        ^",
        "",
        "FAILURE: Build failed with an exception.",
        "",
        "* What went wrong:",
        "Execution failed for task ':compileJava'.",
        "> Compilation failed; see the compiler error output for details.",
        "",
        "* Try:",
        "> Run with --stacktrace option to get the stack trace.",
        "",
        "BUILD FAILED in 12s",
        "##[error]Process completed with exit code 1.",
    ])
    out = _summarize_log(raw)
    assert "detected: gradle" in out
    assert "FAILURE: Build failed with an exception" in out
    assert "What went wrong" in out
    assert "Compilation failed" in out
    assert "BUILD FAILED" in out


def test_summarize_cargo_compile_error():
    raw = "\n".join([
        "   Compiling myapp v0.1.0",
        "error[E0425]: cannot find value `foo` in this scope",
        "  --> src/main.rs:4:13",
        "   |",
        "4  |     let x = foo;",
        "   |             ^^^ not found in this scope",
        "",
        "error: could not compile `myapp` due to 1 previous error",
        "##[error]Process completed with exit code 101.",
    ])
    out = _summarize_log(raw)
    assert "detected: cargo" in out
    assert "E0425" in out
    assert "cannot find value" in out
    assert "could not compile" in out


def test_summarize_pytest_failures():
    raw = "\n".join([
        "collecting ...",
        "collected 10 items",
        "test_foo.py::test_bar PASSED",
        "test_foo.py::test_baz FAILED",
        "",
        "=================================== FAILURES ===================================",
        "_____________________________ test_baz _______________________________",
        "",
        "    def test_baz():",
        ">       assert 1 == 2",
        "E       AssertionError",
        "",
        "test_foo.py:12: AssertionError",
        "=========================== short test summary info ============================",
        "FAILED test_foo.py::test_baz - AssertionError",
        "========================= 1 failed, 9 passed in 0.12s =========================",
    ])
    out = _summarize_log(raw)
    assert "detected: pytest" in out
    assert "FAILURES" in out
    assert "AssertionError" in out
    assert "short test summary info" in out
    assert "1 failed, 9 passed" in out


def test_summarize_go_test_failure():
    raw = "\n".join([
        "=== RUN   TestFoo",
        "--- PASS: TestFoo (0.00s)",
        "=== RUN   TestBar",
        "    bar_test.go:42: expected 5, got 3",
        "--- FAIL: TestBar (0.00s)",
        "FAIL",
        "FAIL\tgithub.com/acme/pkg\t0.123s",
        "##[error]Process completed with exit code 1.",
    ])
    out = _summarize_log(raw)
    assert "detected: go" in out
    assert "--- FAIL: TestBar" in out
    assert "expected 5, got 3" in out


def test_summarize_npm_error():
    raw = "\n".join([
        "> my-app@1.0.0 build",
        "> webpack --mode production",
        "",
        "npm ERR! code ELIFECYCLE",
        "npm ERR! errno 2",
        "npm ERR! my-app@1.0.0 build: `webpack --mode production`",
        "npm ERR! Exit status 2",
        "##[error]Process completed with exit code 2.",
    ])
    out = _summarize_log(raw)
    assert "detected: npm" in out
    assert "ELIFECYCLE" in out
    assert "webpack --mode production" in out


def test_summarize_tsc_error():
    raw = "\n".join([
        "tsc --noEmit",
        "src/foo.ts(10,5): error TS2322: Type 'string' is not assignable to type 'number'.",
        "src/foo.ts(12,5): error TS2339: Property 'bar' does not exist on type 'Foo'.",
        "",
        "Found 2 errors in 1 file.",
        "##[error]Process completed with exit code 1.",
    ])
    out = _summarize_log(raw)
    assert "detected: tsc" in out
    assert "TS2322" in out
    assert "TS2339" in out
    assert "Found 2 errors" in out


def test_summarize_gcc_compiler_error():
    raw = "\n".join([
        "cc -c foo.c -o foo.o",
        "foo.c:10:5: error: 'bar' undeclared (first use in this function)",
        "   10 |     bar = 5;",
        "      |     ^~~",
        "make: *** [foo.o] Error 1",
        "##[error]Process completed with exit code 2.",
    ])
    out = _summarize_log(raw)
    assert "detected: gcc" in out
    assert "'bar' undeclared" in out
    assert "foo.c:10:5" in out


def test_summarize_msbuild_error():
    raw = "\n".join([
        "Microsoft (R) Build Engine version 17.0",
        "Build started 2026-04-13",
        "src/App.cs(10,5): error CS0103: The name 'foo' does not exist",
        "Build FAILED.",
        "    0 Warning(s)",
        "    1 Error(s)",
        "##[error]Process completed with exit code 1.",
    ])
    out = _summarize_log(raw)
    assert "detected: msbuild" in out
    assert "Build FAILED" in out or "CS0103" in out


def test_summarize_flutter_test_failure():
    """Flutter test reporter emits ##[group]❌ prefixed lines; the ##[group]
    prefix must be stripped, not the whole line."""
    raw = "\n".join([
        "2026-03-29T15:38:27.3750894Z ✅ test/a_test.dart: Verify empty label and tip",
        "2026-03-29T15:38:30.2102439Z ##[group]❌ test/super_user_screen_test.dart: Verify tab names (failed)",
        "2026-03-29T15:38:30.2108562Z ══╡ EXCEPTION CAUGHT BY FLUTTER TEST FRAMEWORK ╞════",
        "2026-03-29T15:38:30.2111099Z The following TestFailure was thrown running a test:",
        "2026-03-29T15:38:30.2112747Z Expected: <3>",
        "2026-03-29T15:38:30.2113090Z   Actual: <4>",
        "2026-03-29T15:38:30.2186256Z ##[endgroup]",
        "2026-03-29T15:40:44.6194464Z ##[error]539 tests passed, 2 failed.",
        "2026-03-29T15:40:44.7299559Z ##[error]Process completed with exit code 1.",
    ])
    out = _summarize_log(raw)
    assert "detected: flutter" in out
    # The ❌ line must appear (the ##[group] prefix was stripped, not the whole line)
    assert "❌ test/super_user_screen_test.dart" in out
    assert "Verify tab names" in out
    assert "EXCEPTION CAUGHT BY FLUTTER" in out
    assert "Expected: <3>" in out
    assert "Actual: <4>" in out
    assert "539 tests passed, 2 failed" in out
    assert "Process completed with exit code 1." in out


def test_summarize_group_prefix_stripped_not_dropped():
    """##[group]X lines should become 'X' (content preserved), not disappear."""
    raw = "\n".join([
        "##[group]some useful content here",
        "##[endgroup]",
        "##[group]more content",
        "inner line",
        "##[endgroup]",
    ])
    out = _summarize_log(raw)
    assert "some useful content here" in out
    assert "more content" in out
    assert "inner line" in out
    # The pure marker lines should be gone
    assert "##[group]" not in out
    assert "##[endgroup]" not in out


def test_summarize_anchor_beats_marker_fallback():
    """When both an anchor and ##[error] exist, the anchor wins and gives a wider block."""
    raw = "\n".join([
        "some prelude line 1",
        "some prelude line 2",
        "[INFO] BUILD FAILURE",
        "[ERROR] stuff 1",
        "[ERROR] stuff 2",
        "[ERROR] stuff 3",
        "##[error]Process completed with exit code 1.",
    ])
    out = _summarize_log(raw, context_lines=1)
    assert "detected: maven" in out
    # All three [ERROR] lines must be in the block, not just ±1 around ##[error].
    assert "stuff 1" in out
    assert "stuff 2" in out
    assert "stuff 3" in out
