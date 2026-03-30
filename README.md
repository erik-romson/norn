# norn

Generic pipeline framework for orchestrating multi-step AI agent workflows.

## Install

```bash
pip install norn          # core only
pip install norn[jira]    # with Jira support
pip install norn[all]     # everything
```

## Setup (development)

```bash
uv sync
```

### Authentication

The pipeline framework uses the Claude Agent SDK which needs authentication. Two options:

**Option A: API key** (requires API credits)
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

**Option B: Claude Code plan** (uses your Max/Pro subscription)
```bash
claude setup-token
export CLAUDE_CODE_OAUTH_TOKEN=<token from setup-token>
```

## Configuration

### Env files

Environment variables are loaded automatically at startup:

1. `~/.norn/env` — global defaults (API keys, default org)
2. `.norn.env` in CWD — project-specific overrides

Explicit environment variables always take precedence. Format is `KEY=VALUE` with optional quotes, `#` comments, and blank lines.

### Config directory

Org configs live in `~/.norn/` (previously `~/.issueprocessing/`). Override with `NORN_CONFIG_DIR`. The legacy `ISSUEPROC_CONFIG_DIR` and `~/.issueprocessing/` are still supported as fallbacks.

## Run a pipeline

```bash
# Run a bundled pipeline by name
uv run python -m norn run hello

# Run an external pipeline config
uv run python -m norn run examples/hello.py

# Or use the shorthand wrapper
bin/norn examples/hello.py
```

### Discover bundled pipelines

```bash
# List all bundled pipelines
uv run python -m norn list

# Show details for a specific pipeline
uv run python -m norn describe hello
```

## Run tests

```bash
uv run python -m pytest
```
