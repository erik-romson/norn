# AGENTS.md

This is the canonical repo instruction file for agents. Keep user-facing setup and CLI docs in `README.md`; `CLAUDE.md` is only a Claude Code compatibility pointer.

## Principles
- **No fallbacks. Fail fast and hard.** When something required is missing or fails — a dependency, an external tool, a file, an env var, a capability, an unexpected value — surface it immediately with a clear error or let the exception propagate. Never swallow it with a `try/except`-to-default, never silently degrade to an alternate path, never guess a default to keep going. This applies to everything, with no exceptions for any one dependency. (A multi-location lookup that a feature documents as part of its contract — e.g. resolving state files from a primary then a legacy location — is designed behavior, not an error-masking fallback.)

## Commands
- Install exactly like CI: `uv sync --group dev`.
- Run the CI-equivalent unit suite: `uv run python -m pytest tests/ -v`.
- Run a focused test: `uv run python -m pytest tests/test_runner.py::test_loop_retries -v`.
- There is no configured lint, formatter, or typecheck step in `pyproject.toml` or CI; do not invent one as required verification.
- Run a bundled pipeline: `uv run python -m norn run hello`; list and inspect them with `uv run python -m norn list` and `uv run python -m norn describe hello`.
- Run an external config: `uv run python -m norn run examples/derived.py`; `bin/norn examples/derived.py` only auto-prepends `run` when the first arg is a file.
- The package is installed editable, so edits to `norn/pipelines/*.py` take effect on the next run; never reinstall or `uv sync` just to pick up a pipeline change.
- To drive a pipeline against another repo, `cd` there first: pipelines that pin `PROJECT_DIR = os.getcwd()` read it at import time, and `--project <norn checkout>` selects the environment without changing cwd.
- `bin/show_ci_data.py` dumps exactly what `check_and_fix_ci` would feed Claude for a GitHub Actions run URL; it is a debug tool, not a pipeline.
- Run BATS only when needed: `bats -r bats/ -v`. CI does not run BATS, BATS expects bats-support/assert/file libraries, and several E2E tests call Claude.

## CLI State And Args
- `--resume` is checkpoint-based: it loads `<config>.checkpoint`, restores outputs, and skips completed stages as cached.
- `--continue` only resumes the agent session from the checkpoint; it reruns stages.
- Runtime state is `<config>.checkpoint` and `<config>.history`, not `.session`; these files are gitignored.
- External pipeline files outside the workspace write new state beside a same-named file in the current working directory, with the external path only used as a read fallback.
- Positional args after the config are joined into `{param.args}`; `--arg KEY=VALUE` becomes `{param.KEY}`; `--skip` must match the exact human stage name, including spaces.
- Every key in `metadata["args"]` is rendered as a required positional by `norn describe` (`norn/cli.py:293-302`) and prompted for by the TUI launcher (`norn/tui/launch.py:33-46`); optional `--arg` knobs belong in the module docstring instead.

## Architecture
- `norn/` is the generic core and must not import from `norn.contrib`; contrib/domain code can import core.
- Real entrypoints are `norn/cli.py` for argument resolution, `norn/runner.py` for execution, `norn/dsl.py` for pipeline definitions, and `norn/stages/base.py` for stage contracts.
- External pipeline files are loaded with `importlib` and must define `config = Pipeline(...)`.
- Bundled pipelines live in `norn/pipelines/`; `norn/catalog.py` discovers their docstring and `metadata` via AST before importing, and they are run by name (`norn run implement_features`).
- Every pipeline has exactly ONE home. Do not copy a bundled pipeline to another directory to run it by path — that duplication drifted for months before it was consolidated. Add new pipelines to `norn/pipelines/`; `examples/` holds only sample configs that demonstrate the run-by-path form.
- `implement_features`'s commit stage tolerates auto-fixing pre-commit hooks: it clears any partial staging before committing, and if the commit is still rejected it re-stages what the hooks rewrote and retries **once** before failing. The re-stage list comes from git state via `_snapshot_diff.py --hook-fixes`, not from the step's `.changed` list, because hooks act on the whole staged set — the rewritten file is often one an earlier step staged.
- `implement_features_v2` is a deliberate parallel implementation of `implement_features`, built from `tmp/implement-features-pipeline-improvements.md`. Intent: replace v1 once v2 has enough completed runs in its own history file to compare; until then both ship and v1 is frozen. Two things v2 changes that a reader will trip over: (1) every loop gate is repeated as a hard top-level stage with `on_failure=fail` (the in-loop check is a prompt, not a contract), and (2) after all steps and aggregate validation, the review phase is a gate that writes `<feature_dir>/review.md` with a three-line header (`VERDICT: PASS`/`NEEDS_FIXES`, `Base: <sha>`, `Head: <sha>`) that the pipeline checks in shell; a stale or malformed header fails the gate.
- v2's residual limits (deliberate deferrals to norn core, not oversights): commits are not transactional (a concurrent external write during a step is not detected); review is constrained and postcondition-checked, not sandboxed; hook-rewrite revalidation runs after the commit so a failure leaves already-committed rewritten code; resume matches commit subjects `refactor: <step stem>` and two plans sharing a step stem collide; turn caps (`max_turns`) are Claude-Code-only and v2 is not OpenCode-verified.
- Modules under `norn/pipelines/` whose name starts with `_` are private helpers (e.g. `_snapshot_diff.py`), not pipelines; `norn/catalog.py` skips them so they never appear in `norn list` or the TUI launcher.
- `norn/pipelines/_step_snapshot.py` is a private helper for `implement_features_v2`: content-aware worktree snapshots (SHA-1 digest per file to catch dirty-byte regressions), snapshot diff, and hook-fix detection; it produces NUL-separated path lists. `_snapshot_diff.py` is the v1 equivalent and stays untouched.
- `norn/pipelines/_plan_gates.py` and `norn/pipelines/_preplan.py` are private helpers for `plan_with_review`, not pipelines; the catalog skips them by the same `_`-prefix rule. `parse_arg_flags` from `_preplan.py` is also reused by `implement_features_v2` to parse optional `--arg` knobs at import time.
- `plan_with_review` drives codex through `RunCommand`, not an `AgentProvider`, because `Pipeline.agent_provider()` sets one provider for the whole run and `Generate` has no per-stage override — a codex provider would force codex on the drafting stages too.
- `plan_with_review`'s last stage splits the finished plan into `implement_features` step files using the `split-plan` skill, resolved through `norn/skills.py` once at import so a worktree run still uses the launch repo's copy. Resolution never raises: it falls back to norn's own `.claude/skills/split-plan/SKILL.md` and then to the bare skill name, because a pipeline that raises at import cannot be loaded at all — the TUI launcher reports `Could not load pipeline` instead of failing the stage. The deliverable is `<slug>-final-plan.md` and the step directory is that path minus `.md` (`tmp/x-final-plan.md` → `tmp/x-final-plan/`); `slug_of` strips only `-preplan`/`_preplan`, so without the `final` marker a brief named `x-plan.md` produced a deliverable called `x-plan-plan.md`.
- A `Loop(on_exhaust=ask_user)` whose body ends in a gate is a **prompt, not a contract**: the runner treats `[c]ontinue` as "proceed" (`norn/runner.py:718-740`), so any mandatory gate must be repeated after the loop as a top-level stage with `on_failure=fail`. `plan_with_review` does this three times (`open questions resolved`, `dispositions recorded`, `step files written`); `implement_features_v2` applies the rule to every loop it has. The post-loop stage must use a **different name** from the in-loop one because `ctx.results` is keyed by stage name. A top-level check that must not abort the run but cannot retry inside a loop exits 0 and writes a marker file; later stages use `when=file_exists(marker)` to gate on the marker's presence.
- `norn/pipelines/_plan_review.py` is the ONE home of the plan-review flow builder (`add_plan_review_stages`). Both `plan_with_review` and `fix_jira_issue` call it. Do not inline or duplicate the builder stages in the pipeline module.
- `norn/pipelines/_jira_fetch.sh`, `norn/pipelines/_jira_issue.py`, and `norn/pipelines/_launch_tree.py` are private helpers for `fix_jira_issue`; the catalog skips them (`_`-prefix rule for `.py`; `.sh` is never scanned by the catalog).
- `fix_jira_issue` runs `implement_features_v2` as a child subprocess (`sys.executable -m norn run implement_features_v2 <steps_dir> --non-interactive`) rather than importing it directly, because `Pipeline.agent_provider()` sets one provider for the whole parent run and there is no per-stage override; the child process gets its own provider, budget, and checkpoint.
- `--non-interactive` sets `NonInteractiveResponder` as the active responder, which returns abort (`"a"`) on every budget/failure prompt and raises `RuntimeError` on step-mode prompts. Use it only for child processes that must not pause waiting for a human at the terminal.
- The "stage that fails on purpose" pause pattern (`WaitForApproval` in `_plan_review.py`) is a deliberate `StageResult(success=False)` returned by a stage with `on_failure=ask_user`; the runner fires `ASK_USER` and pauses. This is legitimate when the intent is to interrupt the run for a human review step, not to signal an error.
- Switching `implement_features_v2` from its own inline `AssertLaunchTree` to the shared copy in `_launch_tree.py` is deferred until the v1-vs-v2 comparison period ends; do not make that change while v2 is frozen.
- Stage plugins are discovered from the `norn.stages` entry point group declared in `pyproject.toml`.

## UI And Event Seam
- The terminal UI (`norn ui`), the typed run-event seam (`norn/events.py`, `norn/event_sink.py`), and the Textual code under `norn/tui/` are documented in `docs/norn-ui.md`; read it before touching any of them.
- `docs/norn-ui.md` is canonical and MUST be updated in the same change whenever you alter `norn/tui/`, the event seam, the runner's event emission, the launcher/args/run flow, or the `norn ui` CLI wiring.
- Textual is a REQUIRED dependency, declared in `[project.dependencies]`; there is no optional `ui` extra. Per Principles, `norn ui` imports `norn.tui` directly and a missing Textual fails hard — no `ImportError` guard, no install hint, no degraded path. The same applies to `fzf` (the file picker raises loudly when it is not installed).
- Keep all `textual` imports inside `norn/tui/` and import them lazily from the `ui` command so `norn run`/`norn history`/etc. stay light — this is import locality (don't load Textual unless `norn ui` runs), NOT optionality. Textual is always installed.

## Stage Semantics
- All stage `run()` methods are async and return `StageResult`; leave `name=""` in custom stages because the runner sets the real stage name.
- Set `needs_agent = True` only for stages that need agent provider kwargs; non-agent stages must not import `claude-agent-sdk`.
- A `Loop` retries the whole body from the top after any failing stage, not just the failed stage.
- `clear_context()` drops only the current agent session; prior `StageResult` outputs remain available through `ctx.get("stage name")`.
- `Parallel` runs stages concurrently and each agent-backed stage starts with a fresh session.
- Relative paths in stage `run()` methods resolve against `ctx.working_dir` via `resolve_run_path` (in `norn/runner.py`) when set; absolute paths pass through unchanged. This is the durable worktree-isolation contract — stages must use `resolve_run_path` rather than bare `pathlib.Path` for any filesystem access.
- Contrib stages are not worktree-aware in v1: `norn/contrib/` stages (`ci.py`, `ship.py`, `push.py`) shell out relative to process cwd or `IssueContext.local_path` (the `Clone` stage owns its own checkout), and the `implement_features` and `check_and_fix_ci` pipelines pin `PROJECT_DIR = os.getcwd()` at import time for their `RunCommand` `cd` and git calls. These run against the launch repo even under the worktree toggle. Their `Generate` stages deliberately do NOT set `cwd=`, so agent work follows `ctx.working_dir`.

## Agent Provider
- Default provider is `claude-code` (Claude Agent SDK). `opencode` is also supported via the OpenCode CLI.
- Select provider via `--agent-provider` CLI flag, `NORN_AGENT_PROVIDER` env var, or `Pipeline.agent_provider(...)` (priority in that order).
- Checkpoint and history records store `agent_provider`; `--resume`/`--continue` with a mismatched provider exits with an error.
- Hooks, MCP tools, and non-`project` `setting_sources` are `claude-code`-only and fail fast with other providers.

## Generate Stage Quirks
- If `permission_mode` or `allowed_tools` is set, the agent writes files through tools and `Generate.output_file` is ignored.
- Use absolute paths in prompts for agent-backed file edits; bundled pipelines do this because the bundled CLI may resolve its project root differently.
- Use `setting_sources=["project"]` when a Generate stage should load repo guidance such as `AGENTS.md` or `CLAUDE.md`.
- Model shorthands are repo-defined in `norn/stages/generate.py`: `opus`, `sonnet`, and `haiku` map to provider-specific model IDs.

## Config And Secrets
- The CLI loads env files automatically in this order: `~/.norn/env`, then `.norn.env`; explicit process env vars win.
- Real Generate stages need Claude auth via `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`.
- Org configs are Python pipeline files under `~/.norn/orgs/` or `NORN_CONFIG_DIR/orgs/`; issue-key runs choose an org by matching `Pipeline.projects(...)` keys.
- Treat local agent settings such as `.claude/settings.local.json` as user-local and potentially secret; do not copy their values into docs, tests, or commits.

## Tests
- Unit tests mock SDK calls, subprocesses, GitHub/Jira/search, and other external systems; keep new tests offline unless explicitly writing an E2E/BATS test.
- Use `pytest.mark.asyncio` for async tests even though `asyncio_mode = "auto"` is configured.
- BATS test fixtures live under `bats/testfiles/`; generated runtime files should go under `tmp/` or `target/`, both ignored.
