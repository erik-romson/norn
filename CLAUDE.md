# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Generic pipeline framework for orchestrating multi-step AI agent workflows. Pipelines are defined as sequences of stages and loops using a Python DSL. The framework handles execution, context management, retries, and structured data passing between stages.

The Jira-to-PR automated issue processing use case is a future plugin (see `main-plan.md`), not part of the core framework.

## Status

Early implementation — Phase 1. See `plan.md` for current scope and `main-plan.md` for the full vision.

## Build & Run

This project uses **uv** for dependency and virtual environment management.

```bash
# Install dependencies
uv sync

# Run a bundled pipeline by name
uv run python -m norn run hello

# Run an external pipeline config file
uv run python -m norn run examples/hello.py

# Resume from the session saved by the previous run
uv run python -m norn run examples/hello.py --resume

# Pass named parameters
uv run python -m norn run pipeline.py --arg key=value

# Skip specific stages
uv run python -m norn run pipeline.py --skip "stage name"

# List bundled pipelines
uv run python -m norn list

# Run tests
uv run python -m pytest

# Single test
uv run python -m pytest tests/test_runner.py::test_loop_retries -v
```

Requires Python >= 3.13.

## Architecture

```
norn/
├── dsl.py          # DSL: Pipeline, Stage, Loop, clear_context, fail, ask_user
├── models.py       # StageResult, PipelineContext, UsageTracker, UsageRecord
├── runner.py       # Executes pipeline: sequential stages, do-while loops, context clearing
├── ui.py           # Rich terminal output: spinners, colors, usage reports, interactive prompts
├── cli.py          # Entry point, arg parsing, pipeline loading
├── catalog.py      # AST-based bundled pipeline discovery (list, describe, load)
├── envfile.py      # Env file loader (~/.norn/env, .norn.env)
├── alerts.py       # AlertManager, MacOSChannel, SlackChannel, FileChannel
├── checkpoint.py   # Save/restore pipeline progress for resumption on failure
├── history.py      # Append run records to <config>.history
├── loader.py       # Dynamic pipeline config loading via importlib
├── profiles.py     # SessionProfile: reusable permission/tool/env presets for stages
├── registry.py     # Stage class registry for declarative configs
├── secrets.py      # {secret.NAME} placeholder resolution
├── secrets_managers/ # Pluggable secret backends (AWS SSM, Vault)
├── skills.py       # Skill injection into agent system prompts
├── templates.py    # PromptTemplate loading and resolution
├── testing.py      # Test helpers (SuccessStage, FailStage, etc.)
├── pipelines/      # Bundled pipeline configs (hello, vanilla_change, implement_features)
├── stages/         # Core stages
│   ├── base.py     # Abstract BaseStage class (async def run(ctx) -> StageResult)
│   ├── read_file.py
│   ├── run_command.py
│   ├── generate.py # Calls claude-agent-sdk
│   └── validate.py
├── contrib/        # Domain extensions
│   ├── models/     # IssueContext, FixPlan, CodeResult, PipelineResult
│   ├── stages/     # ReadIssue, Clone, Fix, Plan, Ship, Analyze, WriteTest, etc.
│   ├── matchers/   # Repo matching (keyword, component, label, stacktrace, LLM)
│   ├── extractors/ # Stacktrace, class name extraction
│   ├── sources/    # Issue sources (Jira, future: GitHub Issues)
│   ├── search/     # Elasticsearch/OpenSearch
│   ├── build/      # Build system detection and configs
│   ├── notifications/ # Slack, email
│   ├── tools/      # MCP tool definitions (Jira, GitHub, testing, shipping, logs)
│   ├── dsl/        # Domain DSL helpers (Jira builder, matchers)
│   ├── parsers/    # Structured output parsers (fix plans)
│   ├── github/     # GitHub integration utilities
│   └── utils/      # Shared helpers (slugify)
```

**Package conventions:**
- Core (`norn/`) never imports from `contrib/`
- `contrib/` freely imports from core
- New domain extensions go under `contrib/<name>/`

### Key Concepts

- **Stages** are named, generic units of work. They receive a `PipelineContext` and return a `StageResult`. Stages are not numbered — they can be reordered freely.
- **Loops** wrap stages in do-while retry logic. If any stage in a loop fails, the whole loop restarts from the top. `max_retries` prevents infinite cycling. `on_exhaust` controls what happens when retries run out (`fail`, `ask_user`).
- **`clear_context()`** is an explicit DSL directive that discards the current agent conversation session. Structured outputs (`StageResult`) survive — only the agent memory is shed. Place it wherever accumulated context is no longer useful.
- **`PipelineContext`** carries all `StageResult` outputs. Stages access prior outputs via `ctx.get("stage_name")`.
- **Session resumption** — after every successful run the final Claude session ID is saved to `<config>.session` (e.g. `examples/hello.session`). Pass `--resume` on the next run to continue from that session, giving Claude memory of the prior conversation.

### DSL Example

```python
from norn.dsl import *

config = (
    Pipeline("hello")
    .stage("read_spec", ReadFile(path="spec.txt"), on_failure=fail)
    .clear_context()
    .loop("generate_and_build", max_retries=3, on_exhaust=fail,
        stages=[
            Stage("generate", Generate(prompt="...", output_file="src/greeter.py")),
            Stage("check", RunCommand(cmd="python -m py_compile src/greeter.py")),
            Stage("test", RunCommand(cmd="python -m pytest tests/ -v")),
        ]
    )
)
```

## CLI Reference

### Subcommands

| Command | Description |
|---------|-------------|
| `norn run <config>` | Run a pipeline (file path, org issue key, or bundled pipeline name) |
| `norn list` | List all bundled pipelines |
| `norn describe <name>` | Show details for a bundled pipeline |
| `norn list-stages` | List all registered stage plugins |
| `norn orgs` | List configured orgs |
| `norn history` | Show run history |

### Run flags

| Flag | Description |
|------|-------------|
| `--resume` | Reload the session saved by the previous run of this config |
| `--arg KEY=VALUE` | Pass a named parameter to the pipeline (accessible as `{param.KEY}`) |
| `--skip STAGE_NAME` | Skip a stage by name (repeatable) |
| `--dry-run` | Show pipeline structure without executing |
| `-v` / `--verbose` | Enable debug logging |

Shorthand wrapper: `bin/norn` auto-prepends `run` if the first arg is a file.

Positional arguments after the config file are joined and exposed as `{param.args}`.

### Configuration

Env files are loaded automatically at startup (later overrides earlier):
1. `~/.norn/env` — global defaults
2. `.norn.env` in CWD — project overrides

Org configs live in `~/.norn/` (override with `NORN_CONFIG_DIR`). Legacy `~/.issueprocessing/` and `ISSUEPROC_CONFIG_DIR` are still supported.

## Code Style & Conventions

### Python

- `from __future__ import annotations` at the top of every module
- Dataclasses for all data containers (`@dataclass`), not dicts or namedtuples
- Type hints on all function signatures and return types
- Use `str | None` union syntax, not `Optional[str]`
- `Any` only where truly unavoidable (e.g. `PipelineContext.params`)
- Imports: `from __future__` first, stdlib, third-party, then project (`norn.*`)
- Use `TYPE_CHECKING` guard for imports only needed by type checkers
- Logging via `log = logging.getLogger(__name__)` — one per module
- All UI output goes through `norn/ui.py` (rich console), never bare `print()` in runner/cli
- Enums for fixed option sets (`OnFailure`, `AlertEvent`)
- Fluent/chained DSL methods return `self` (→ `Pipeline`)

### Async

- All stage `run()` methods are `async`
- Runner uses `asyncio.run()` as the single entry point (in `cli.py`)
- Subprocess calls via `asyncio.create_subprocess_shell()`
- Blocking I/O (file writes, HTTP) wrapped in `loop.run_in_executor()`

### Stage Implementations

- Inherit from `BaseStage` (abstract base in `stages/base.py`)
- Set `needs_agent = True` for stages that call the Claude SDK
- `run(ctx: PipelineContext, **kwargs) -> StageResult` is the only interface
- Stages are stateless between pipeline runs — no mutable instance state that persists
- Non-agent stages (ReadFile, RunCommand) must not import claude-agent-sdk

### Testing

- pytest + pytest-asyncio, `asyncio_mode = "auto"` in pyproject.toml
- Test files: `tests/test_<module>.py` — one per source module
- Test helpers (SuccessStage, FailStage, etc.) defined in the test file that needs them
- Use `@pytest.mark.asyncio` on async tests
- Mock external dependencies (SDK, subprocess) — never make real API calls in tests
- BATS tests in `bats/` for CLI integration testing

### Naming

- Stage names are lowercase, human-readable, may contain spaces: `"test python"`, `"commit 01-hooks"`
- Pipeline names are lowercase with underscores: `"vanilla_change"`, `"implement_features"`
- Module-level constants are UPPER_SNAKE: `PROJECT_DIR`
- Private helpers prefixed with underscore: `_run_stage`, `_handle_failure`

### File Organization

- Core framework: `norn/` — generic, no domain logic
- Stage implementations: `norn/stages/` — one file per stage type
- Pipeline configs: `examples/` (samples) and `dogfooding/` (self-use)
- Tests: `tests/` (pytest) and `bats/` (CLI integration)
- Feature plans / specs: `tmp/` (not committed)

## Design Principles

- The framework is generic — no Jira, GitHub, or domain-specific logic in the core.
- Async throughout (`asyncio`).
- Stages that don't need an LLM (ReadFile, RunCommand) are pure Python — no agent session overhead.
- Pipeline configs are external Python files loaded dynamically via `importlib`, not part of the project source.
- Session IDs are persisted beside the config file (`<config>.session`) so resumption requires no extra configuration.
- Fail fast — no fallbacks or silent error swallowing. Errors propagate up to the runner.
- UI output is separated from logic — `ui.py` handles all terminal display via `rich`.
