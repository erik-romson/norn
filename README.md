# norn

Generic pipeline framework for orchestrating multi-step AI agent workflows.

Pipelines are Python configs built from stages, loops, parallel blocks, and includes. Norn handles execution, retries, context passing, checkpoints, run history, and Claude Agent SDK calls for agent-backed stages.

## Install

```bash
pip install norn          # core only
pip install norn[jira]    # with Jira support
pip install norn[all]     # everything
```

## Development Setup

Requires Python >= 3.13 and `uv`.

```bash
uv sync --group dev
```

## Authentication

Agent-backed `Generate` stages use the Claude Agent SDK and need one of these credentials:

```bash
# API key
export ANTHROPIC_API_KEY=sk-ant-...

# Claude Code plan token
claude setup-token
export CLAUDE_CODE_OAUTH_TOKEN=<token from setup-token>
```

## Configuration

Environment variables are loaded automatically at startup:

1. `~/.norn/env` for global defaults
2. `.norn.env` in the current working directory for project overrides

Explicit process environment variables take precedence over both files. Env files use `KEY=VALUE`, allow optional quotes, and ignore `#` comments and blank lines.

Org configs live under `~/.norn/orgs/` by default. Set `NORN_CONFIG_DIR` to override. Legacy `ISSUEPROC_CONFIG_DIR` and `~/.issueprocessing/` are still supported as fallbacks.

## Run Pipelines

```bash
# Run a bundled pipeline by name
uv run python -m norn run hello

# Run an external pipeline config file
uv run python -m norn run examples/hello.py

# Shorthand wrapper: prepends "run" only when the first arg is a file
bin/norn examples/hello.py
```

Discover bundled pipelines:

```bash
uv run python -m norn list
uv run python -m norn describe hello
```

Useful run flags:

```bash
# Pass positional text as {param.args}
uv run python -m norn run pipeline.py "implement this change"

# Pass named parameters as {param.KEY}
uv run python -m norn run pipeline.py --arg key=value

# Skip exact stage names, including spaces
uv run python -m norn run pipeline.py --skip "test python"

# Show pipeline structure without executing
uv run python -m norn run pipeline.py --dry-run
```

## Resume And History

Norn writes runtime state beside the config state key:

- `<config>.checkpoint` stores completed stages and the latest Claude session ID.
- `<config>.history` stores JSONL run history and stage logs.

Use `--resume` to load the checkpoint, restore prior stage outputs, and skip completed stages as cached:

```bash
uv run python -m norn run examples/hello.py --resume
```

Use `--continue` to resume the Claude session from the checkpoint while rerunning stages:

```bash
uv run python -m norn run examples/hello.py --continue
```

External pipeline files outside the current workspace write new `.checkpoint` and `.history` files beside a same-named file in the current working directory; the external path is only used as a read fallback for old state.

Inspect history:

```bash
uv run python -m norn history examples/hello.py
uv run python -m norn history examples/hello.py --run 1
uv run python -m norn history examples/hello.py --compare 1 2
```

## Pipeline Configs

External pipeline files are loaded with `importlib` and must define `config = Pipeline(...)`.

```python
from norn.dsl import Pipeline, Stage, fail
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

config = (
    Pipeline("hello")
    .stage("generate", Generate(prompt="Write hello.py", output_file="hello.py"))
    .loop(
        "check",
        max_retries=3,
        on_exhaust=fail,
        stages=[
            Stage("compile", RunCommand(cmd="python -m py_compile hello.py")),
        ],
    )
)
```

Bundled pipelines live in `norn/pipelines/`. Stage plugins are discovered from the `norn.stages` entry point group.

## Run Tests

```bash
# CI-equivalent unit suite
uv run python -m pytest tests/ -v

# Focused test
uv run python -m pytest tests/test_runner.py::test_loop_retries -v
```

BATS tests are optional CLI/E2E tests and are not run by CI:

```bash
bats -r bats/ -v
```

Several BATS tests require bats-support/assert/file libraries and some call Claude.

## Project Layout

- `norn/cli.py` resolves CLI args, config files, bundled pipelines, org configs, checkpoints, and history.
- `norn/runner.py` executes stages, loops, parallel blocks, includes, hooks, budgets, checkpoints, and history snapshots.
- `norn/dsl.py` defines `Pipeline`, `Stage`, `Loop`, `Parallel`, `Include`, and helper predicates.
- `norn/stages/` contains generic core stages.
- `norn/contrib/` contains optional domain integrations such as Jira, GitHub, search, build, and notifications.
