# norn

Generic pipeline framework for orchestrating multi-step AI agent workflows.

Pipelines are Python configs built from stages, loops, parallel blocks, and includes. Norn handles execution, retries, context passing, checkpoints, run history, and agent calls for agent-backed stages.

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

Agent-backed `Generate` stages need credentials for the configured provider.

For the default `claude-code` provider (Claude Agent SDK):

```bash
# API key
export ANTHROPIC_API_KEY=sk-ant-...

# Claude Code plan token
claude setup-token
export CLAUDE_CODE_OAUTH_TOKEN=<token from setup-token>
```

For the `opencode` provider, OpenCode must be installed and authenticated separately:

```bash
# 1. Install (Homebrew shown; see https://opencode.ai/docs for other platforms)
brew install sst/tap/opencode

# 2. Log in to a provider — opens an OAuth flow (GitHub Copilot, Anthropic, etc.)
opencode providers login

# 3. Verify credentials are stored and the expected models are visible
opencode providers list
opencode models | grep -E 'claude|gpt'
```

Credentials are stored in `~/.local/share/opencode/auth.json` and refreshed automatically. Norn does not pass tokens to the subprocess; the `opencode run` child process reads `auth.json` directly.

The bundled opencode model aliases (`opus`, `sonnet`, `haiku`) map to `github-copilot/claude-*` IDs in `norn/agents/models.py`. If your opencode is authenticated against a different provider (e.g. Anthropic direct), edit `MODEL_ALIASES["opencode"]` to point at the IDs `opencode models` lists.

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
uv run python -m norn run examples/derived.py

# Shorthand wrapper: prepends "run" only when the first arg is a file
bin/norn examples/derived.py
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

### plan_with_review

Takes a pre-plan (a brief describing what the real plan must cover) and produces
a finished, reviewed plan, split into step files. It pauses for you when the
draft has open questions, it has codex read the final result before Claude
applies the review, and it ends by splitting the plan with the project's
`split-plan` skill.

```bash
uv run python -m norn run plan_with_review tmp/my-feature-preplan.md
```

For a pre-plan at `tmp/<slug>-preplan.md`, the pipeline writes four files and
one directory:

| File | Written by |
| --- | --- |
| `tmp/<slug>-final-plan.md` | Claude — the deliverable |
| `tmp/<slug>-plan-questions.md` | Claude writes; **you answer** |
| `tmp/<slug>-plan-review.md` | codex |
| `tmp/<slug>-plan-review-response.md` | Claude |
| `tmp/<slug>-final-plan/` | Claude — `index.md` + `step-NN-*.md`, ready for `norn run implement_features tmp/<slug>-final-plan` |

`final` marks the deliverable so it is never mistaken for the brief it came
from: only a trailing `-preplan`/`_preplan` is stripped when deriving the slug,
so a brief named `x-plan.md` would otherwise produce `x-plan-plan.md`. The step
directory is the plan path minus `.md`. The final gate rejects a split
whose steps lack a real `test_cmd:`, so the run stops rather than handing
`implement_features` steps that validate nothing.

**Human round-trip (open questions):** the run pauses and sends a notification →
open `<slug>-plan-questions.md` → fill in the `**Answer:**` slots (leave
`STATUS:` alone; that is the agent's line) → choose `[r]etry`. **`[c]ontinue`
does not skip the questions** — the run stops at the hard gate right after the
loop. To pick it up later, answer the file and re-run with `--continue`.

CLI-only knobs (not available under `norn ui`, because the TUI reconstructs only
positional args):

```bash
--arg model=sonnet            # Claude model for draft/revise/apply (default: opus)
--arg codex_model=gpt-5-codex # model passed to `codex exec -m`
--arg budget=10               # cost ceiling in USD before the run pauses
```

`codex` must be on `PATH` (`brew install codex`) — the `preflight` stage checks
this before any tokens are spent. The split stage uses the `split-plan` skill:
the launch repo's copy when it has one (`.claude/skills/split-plan/SKILL.md`),
otherwise norn's own. Codex spend is invisible to norn's budget
meter because `RunCommand` reports no usage.

### implement_features_v2

`implement_features_v2` implements a feature described by a directory of step
files, committing each step's changes, running an aggregate validation pass,
gating on a structured review, and writing a handoff document. It is a deliberate
parallel implementation of `implement_features` that closes v1 correctness gaps;
both ship side by side until v2 has enough completed runs to replace v1, and v1
is frozen.

```bash
uv run python -m norn run implement_features_v2 <feature-dir>
```

The feature directory must contain:

| File | Role |
| --- | --- |
| `index.md` | Shared context for every step; optional front-matter: `test_cmd`, `bats_cmd`, `test_timeout`, `bats_timeout`, `final_test_cmd` |
| `step-NN-<name>.md` | One file per step, numbered from `01` and contiguous; optional front-matter: `test_cmd`, `bats_cmd`, `test_timeout`, `bats_timeout`, `model` (`sonnet`\|`opus`) |

Optional `--arg` knobs (all have defaults; document them in plan files, not
in pipeline invocations):

| Knob | Default | Description |
| --- | --- | --- |
| `budget=<float>` | `30` | USD cost ceiling (unmeasured guess) |
| `token_budget=<int>` | `500000` | Token ceiling (unmeasured guess) |
| `review_model=<sonnet\|opus>` | `sonnet` | Model for review stages |
| `aggregate_model=<sonnet\|opus>` | `sonnet` | Model for aggregate repair |
| `max_retries=<int>` | `3` | Per-step validation attempts (≥ 1) |
| `allow_dirty_index=1` | off | Skip the clean-index preflight check |
| `allow_dirty_worktree=1` | off | Skip the clean-worktree preflight check |

The pipeline writes two report files into the feature directory:

- `review.md` — structured review verdict written by the review agent; first
  three lines are `VERDICT: PASS`/`VERDICT: NEEDS_FIXES`, `Base: <sha>`,
  `Head: <sha>`.
- `handoff.md` — narrative handoff document written from a pipeline-assembled
  manifest (commits, diff, stat, dependency changes) and the completed review.

**When a gate fails:** fix the underlying issue, then rerun. Steps whose commit
subjects match `refactor: <step-stem>` are detected as already committed and
skipped automatically on the next run. Use `--resume` for stage-level resume
(loads the checkpoint and skips completed stages as cached).

### fix_jira_issue

`fix_jira_issue` takes a Jira issue key and carries it end-to-end: fetches the
issue, writes a structured brief, plans, reviews the plan, and implements it on
a branch — all in one command.

```bash
uv run python -m norn run fix_jira_issue CBS-2249
```

**Required environment variables:**

| Variable | Description |
| --- | --- |
| `JIRA_AUTH` | Jira Basic-auth credentials — `email:api-token`, base64-encoded |
| `JIRA_BASE` | Jira instance URL with trailing slash — `https://your-org.atlassian.net/` |
| `ANTHROPIC_API_KEY` | Anthropic API key (or `CLAUDE_CODE_OAUTH_TOKEN`) |

**Artifacts** (written under `tmp/jira/<KEY>/`):

| File | Written by |
| --- | --- |
| `issue.json` | fetch — raw Jira API response |
| `issue.md` | fetch — Markdown rendition of the issue |
| `attachments/` | fetch — downloaded issue attachments |
| `<KEY>-preplan.md` | Claude (haiku) — structured brief |
| `<KEY>-plan-questions.md` | Claude — open questions for you to answer |
| `<KEY>-final-plan.md` | Claude — deliverable plan |
| `<KEY>-plan-review.md` | codex — review of the plan |
| `<KEY>-plan-review-response.md` | Claude — review response |
| `<KEY>-final-plan/` | Claude — `index.md` + `step-NN-*.md`, run by `implement_features_v2` |
| `<KEY>-final-plan/review.md` | Claude — implementation review (`VERDICT: PASS`/`NEEDS_FIXES`) |
| `<KEY>-final-plan/handoff.md` | Claude — narrative handoff document |

**`stop` round-trip:** add `stop` as a positional to pause after plan review and
before implementation starts:

```bash
uv run python -m norn run fix_jira_issue CBS-2249 stop
```

A macOS toast fires when the plan is ready; open `<KEY>-final-plan.md`, read
and edit it freely, then press `[c]ontinue` to proceed with implementation — or
`[a]bort` to stop here and resume later with `--resume`.

**CLI-only knobs** (not available under `norn ui`):

```bash
--arg model=sonnet        # Claude model for all Generate stages (default: opus)
--arg codex_model=...     # model passed to `codex exec -m` for plan review
--arg budget=20           # cost ceiling in USD before the run pauses (default: 10)
--arg branch=CBS-2249     # git branch name (default: the issue key)
```

**Ignore-path requirement:** the preflight stage checks that three paths are
gitignored before spending any tokens. Add them to `.git/info/exclude` (or
`.gitignore`):

```
tmp/jira/CBS-2249
fix_jira_issue.checkpoint
implement_features_v2.checkpoint
```

Replace `CBS-2249` with the actual key. A glob like `tmp/jira/` also works for
the first line if you want to cover all issues in one entry.

**Recovery:** if the `implement` stage fails or you abort there, press `[r]etry`
to rerun it — `implement_features_v2` skips already-committed steps automatically
on re-run. To rerun manually:

```bash
uv run python -m norn run implement_features_v2 tmp/jira/CBS-2249/CBS-2249-final-plan
```

## Launch the UI

`norn ui` opens a terminal UI (Textual) with a launcher, an arguments prompt, a
live pipeline graph, a transcript, stage detail, and a budget meter — all in one
app.

```bash
# Open the launcher to browse bundled + discovered pipelines
uv run python -m norn ui

# Run a bundled name or a .py path directly (skips the launcher)
uv run python -m norn ui hello
uv run python -m norn ui examples/derived.py

# Shorthand wrapper
bin/norn ui
```

Textual is a required core dependency, so `norn ui` works in any proper install.
See `docs/norn-ui.md` for the full key bindings and screen reference.

## Agent Provider

`Generate` stages delegate to a pluggable agent provider. The default is `claude-code` (Claude Agent SDK). `opencode` is also supported via the OpenCode CLI.

**Provider selection priority** (highest wins):

1. `--agent-provider` CLI flag
2. `NORN_AGENT_PROVIDER` environment variable
3. `Pipeline.agent_provider(...)` in the pipeline definition
4. Default: `claude-code`

Select the provider at the CLI:

```bash
uv run python -m norn run pipeline.py --agent-provider opencode
```

Or in the pipeline definition:

```python
config = Pipeline("hello").agent_provider("opencode")
```

Or via environment variable:

```bash
export NORN_AGENT_PROVIDER=opencode
```

**OpenCode limitations (first-pass)**

The following features are only supported with `claude-code` and will fail fast when used with `opencode`:

- SDK hooks (including `blocked_patterns`-derived hooks)
- MCP tools (`mcp_tools` stage parameter)
- Non-`project` `setting_sources` values

Checkpoint and history records include `agent_provider`. Attempting `--resume` or `--continue` with a different provider than the one that created the checkpoint will exit with an error.

## Resume And History

Norn writes runtime state beside the config state key:

- `<config>.checkpoint` stores completed stages and the latest agent session ID.
- `<config>.history` stores JSONL run history and stage logs.

Use `--resume` to load the checkpoint, restore prior stage outputs, and skip completed stages as cached:

```bash
uv run python -m norn run examples/derived.py --resume
```

Use `--continue` to resume the agent session from the checkpoint while rerunning stages:

```bash
uv run python -m norn run examples/derived.py --continue
```

External pipeline files outside the current workspace write new `.checkpoint` and `.history` files beside a same-named file in the current working directory; the external path is only used as a read fallback for old state.

Inspect history:

```bash
uv run python -m norn history examples/derived.py
uv run python -m norn history examples/derived.py --run 1
uv run python -m norn history examples/derived.py --compare 1 2
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

A pipeline reaches norn one of two ways, and it is worth keeping them straight:

| | `norn/pipelines/` | any other `.py` path |
| --- | --- | --- |
| Referenced as | a **name** — `norn run implement_features` | a **path** — `norn run examples/derived.py` |
| Discovered by | `norn/catalog.py` (AST, no import) | nothing — you supply the path |
| Shows up in | `norn list`, `norn describe`, TUI launcher | TUI launcher only via a configured pipeline dir |
| Loaded via | `importlib` as `norn.pipelines.<name>` | `importlib` from the file path |
| Ships with the package | yes | no |

Bundled pipelines are the shipped ones; put a pipeline there when you want to
run it by name from any directory. Modules starting with `_` are treated as
private helpers and are not listed. `examples/` holds sample configs that
demonstrate the run-by-path form.

The install is editable (`uv run --project <norn checkout>`), so editing a
bundled pipeline takes effect on the next run — no reinstall, no `uv sync`.

Stage plugins are discovered from the `norn.stages` entry point group.

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
- `norn/pipelines/` contains bundled pipelines, run by name; `norn/catalog.py` discovers them.
- `norn/stages/` contains generic core stages.
- `norn/contrib/` contains optional domain integrations such as Jira, GitHub, search, build, and notifications.
