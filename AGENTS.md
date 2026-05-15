# AGENTS.md

This is the canonical repo instruction file for agents. Keep user-facing setup and CLI docs in `README.md`; `CLAUDE.md` is only a Claude Code compatibility pointer.

## Commands
- Install exactly like CI: `uv sync --group dev`.
- Run the CI-equivalent unit suite: `uv run python -m pytest tests/ -v`.
- Run a focused test: `uv run python -m pytest tests/test_runner.py::test_loop_retries -v`.
- There is no configured lint, formatter, or typecheck step in `pyproject.toml` or CI; do not invent one as required verification.
- Run a bundled pipeline: `uv run python -m norn run hello`; list and inspect them with `uv run python -m norn list` and `uv run python -m norn describe hello`.
- Run an external config: `uv run python -m norn run examples/hello.py`; `bin/norn examples/hello.py` only auto-prepends `run` when the first arg is a file.
- Run BATS only when needed: `bats -r bats/ -v`. CI does not run BATS, BATS expects bats-support/assert/file libraries, and several E2E tests call Claude.

## CLI State And Args
- `--resume` is checkpoint-based: it loads `<config>.checkpoint`, restores outputs, and skips completed stages as cached.
- `--continue` only resumes the Claude session from the checkpoint; it reruns stages.
- Runtime state is `<config>.checkpoint` and `<config>.history`, not `.session`; these files are gitignored.
- External pipeline files outside the workspace write new state beside a same-named file in the current working directory, with the external path only used as a read fallback.
- Positional args after the config are joined into `{param.args}`; `--arg KEY=VALUE` becomes `{param.KEY}`; `--skip` must match the exact human stage name, including spaces.

## Architecture
- `norn/` is the generic core and must not import from `norn.contrib`; contrib/domain code can import core.
- Real entrypoints are `norn/cli.py` for argument resolution, `norn/runner.py` for execution, `norn/dsl.py` for pipeline definitions, and `norn/stages/base.py` for stage contracts.
- External pipeline files are loaded with `importlib` and must define `config = Pipeline(...)`.
- Bundled pipelines live in `norn/pipelines/`; `norn/catalog.py` discovers their docstring and `metadata` via AST before importing.
- Stage plugins are discovered from the `norn.stages` entry point group declared in `pyproject.toml`.

## Stage Semantics
- All stage `run()` methods are async and return `StageResult`; leave `name=""` in custom stages because the runner sets the real stage name.
- Set `needs_agent = True` only for stages that need Claude SDK kwargs; non-agent stages must not import `claude-agent-sdk`.
- A `Loop` retries the whole body from the top after any failing stage, not just the failed stage.
- `clear_context()` drops only the current Claude session; prior `StageResult` outputs remain available through `ctx.get("stage name")`.
- `Parallel` runs stages concurrently and each agent-backed stage starts with a fresh session.

## Generate Stage Quirks
- If `permission_mode` or `allowed_tools` is set, Claude writes files through tools and `Generate.output_file` is ignored.
- Use absolute paths in prompts for agent-backed file edits; bundled/dogfooding pipelines do this because the bundled Claude CLI may resolve its project root differently.
- Use `setting_sources=["project"]` when a Generate stage should load repo guidance such as `CLAUDE.md`.
- Model shorthands are repo-defined in `norn/stages/generate.py`: `opus`, `sonnet`, and `haiku` map to concrete Claude model IDs.

## Config And Secrets
- The CLI loads env files automatically in this order: `~/.norn/env`, then `.norn.env`; explicit process env vars win.
- Real Generate stages need Claude auth via `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`.
- Org configs are Python pipeline files under `~/.norn/orgs/` or `NORN_CONFIG_DIR/orgs/`; issue-key runs choose an org by matching `Pipeline.projects(...)` keys.
- Treat local agent settings such as `.claude/settings.local.json` as user-local and potentially secret; do not copy their values into docs, tests, or commits.

## Tests
- Unit tests mock SDK calls, subprocesses, GitHub/Jira/search, and other external systems; keep new tests offline unless explicitly writing an E2E/BATS test.
- Use `pytest.mark.asyncio` for async tests even though `asyncio_mode = "auto"` is configured.
- BATS test fixtures live under `bats/testfiles/`; generated runtime files should go under `tmp/` or `target/`, both ignored.
