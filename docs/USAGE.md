# norn Usage Guide

Generic pipeline framework for orchestrating multi-step AI agent workflows.
Pipelines are defined as sequences of stages and loops using a Python DSL.

## Installation

Requires Python >= 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Quick Start

```bash
# Run a pipeline
bin/norn examples/hello.py

# Same thing, explicit subcommand
uv run python -m norn run examples/hello.py

# Preview what will run without executing
bin/norn examples/hello.py --dry-run

# Pass arguments
bin/norn dogfooding/vanilla_change.py "add logging to the runner"

# Resume from checkpoint after failure
bin/norn examples/hello.py --resume
```

The `bin/norn` wrapper auto-prepends `run` when the first argument is a file.

---

## CLI Reference

### `norn run`

```
norn run <config.py> [options] [positional args...]
```

| Flag | Description |
|------|-------------|
| `--resume` | Resume from the checkpoint saved by the previous run |
| `--dry-run` | Show pipeline structure without executing |
| `--step` | Interactive stepping mode — prompt before each stage |
| `--arg KEY=VALUE` | Pass a named parameter (accessible as `{param.KEY}`) |
| `--skip STAGE_NAME` | Skip a stage by name (repeatable) |
| `-v` / `--verbose` | Enable debug logging |

Positional arguments after the config file are joined and exposed as `{param.args}`.

File references with `@path` syntax are expanded to the file's contents:
```bash
bin/norn pipeline.py "implement changes from @spec.txt"
```

### `norn history`

```
norn history <config.py> [--compare RUN_A RUN_B] [--run RUN_ID]
```

Show run history for a pipeline config, compare two runs side-by-side, or inspect
the detailed step log for a single run.

```bash
# List all runs
bin/norn run examples/hello.py   # (use 'run' because 'history' is not a file)
uv run python -m norn history examples/hello.py

# Compare runs #1 and #3
uv run python -m norn history examples/hello.py --compare 1 3

# Inspect the full step-by-step log for run #3
uv run python -m norn history examples/hello.py --run 3
```

Output:
```
  Run   Timestamp          Status       Cost    Time   Info
   #1   2026-03-24 14:30   ✓ Complete   $0.24   12.3s  3 stages
   #2   2026-03-24 15:10   ✗ Failed     $0.18    8.1s  stage: test
   #3   2026-03-25 09:00   ✓ Complete   $0.19   10.5s  3 stages
```

History is stored as JSONL using the pipeline state key.
For bundled pipelines, that means the current working directory.
For pipeline files inside your repo, it stays beside the config file.
For external shared pipeline files run from another repo, it is written in the
current working directory using the pipeline filename, e.g. `implement_features.history`.
Each run now includes a detailed stage log with per-step status, cost, tokens,
timing, model, and running totals.
History is appended incrementally during execution, so interrupted runs still
leave behind an in-progress record you can inspect later.

---

## Writing Pipelines

Pipeline configs are Python files with a `config` variable holding a `Pipeline` instance.
They are loaded dynamically via `importlib` — they are not part of the project source.

### Minimal Example

```python
from norn.dsl import Pipeline, Stage, fail
from norn.stages.run_command import RunCommand

config = (
    Pipeline("greet")
    .stage("hello", RunCommand(cmd="echo 'Hello, world!'"), on_failure=fail)
)
```

### Complete Example

```python
from norn.alerts import MacOSChannel
from norn.dsl import Pipeline, Stage, fail, ask_user
from norn.stages.generate import Generate
from norn.stages.read_file import ReadFile
from norn.stages.run_command import RunCommand

config = (
    Pipeline("hello")
    .alert(MacOSChannel())
    .stage("read_spec", ReadFile(path="examples/spec.txt"), on_failure=fail)
    .clear_context()
    .loop(
        "generate_and_build",
        max_retries=3,
        on_exhaust=fail,
        stages=[
            Stage("generate", Generate(
                prompt="Create a Python class based on this spec: {read_spec.output}",
                permission_mode="acceptEdits",
            )),
            Stage("test", RunCommand(cmd="python -m pytest tests/ -v")),
        ],
    )
)
```

---

## DSL Reference

### Pipeline

The root container. All methods return `self` for chaining.

```python
Pipeline(name, default_model=None)
```

| Method | Description |
|--------|-------------|
| `.stage(name, impl, ...)` | Add a sequential stage |
| `.loop(name, *, stages, max_retries, ...)` | Add a retry loop |
| `.parallel(name, *, stages, fail_fast)` | Add concurrent stages |
| `.include(path, *, isolated, outputs, args)` | Include a sub-pipeline |
| `.clear_context()` | Discard the current agent session |
| `.alert(channel)` | Add an alert channel |
| `.alerts(channels)` | Add multiple alert channels |
| `.budget(max_cost_usd, max_tokens, on_exceed)` | Add a cost/token budget |
| `.hook(event, impl)` | Register a lifecycle hook |
| `.context(path, label)` | Inject a file into Generate prompts |
| `.context_cmd(cmd, label)` | Inject command output into prompts |
| `.env(name, value)` | Set an environment variable |
| `.secret(name, source)` | Declare a secret |
| `.skills(skill_list)` | Apply skills to all Generate stages |

### Stage

```python
Stage(name, impl, on_failure=fail, when=None, timeout=None)
```

- `name` — human-readable name (lowercase, may contain spaces)
- `impl` — a `BaseStage` implementation (Generate, RunCommand, ReadFile, etc.)
- `on_failure` — `fail` (raise error) or `ask_user` (interactive prompt)
- `when` — optional predicate: `lambda ctx: bool`. Stage is skipped if False.
- `timeout` — seconds before the stage is cancelled (raises PipelineError)

### Loop

Retry loop: if any stage fails, the loop restarts from the top. Agent stages
within the same loop share a session so Claude remembers prior errors.

```python
.loop(
    "name",
    max_retries=5,
    on_exhaust=fail,     # or ask_user
    timeout=600,         # optional: seconds for the entire loop
    stages=[
        Stage("generate", Generate(...)),
        Stage("test", RunCommand(cmd="pytest")),
    ],
)
```

### Parallel

Run independent stages concurrently. Each stage gets its own agent session.

```python
.parallel(
    "name",
    fail_fast=True,      # raise on first failure (default)
    stages=[
        Stage("frontend", Generate(prompt="Build React...")),
        Stage("backend", Generate(prompt="Build API...")),
    ],
)
```

### Include

Include stages from another pipeline file.

```python
# Inline: stages are flattened into the parent, sharing context and session
.include("pipelines/test_suite.py")

# Isolated: runs in a fresh context with a forked agent session
.include("pipelines/test_suite.py", isolated=True, outputs=["test"])

# Parameterized
.include("pipelines/test_suite.py", args={"target": "backend"})
```

### clear_context()

Discards the current agent conversation session. `StageResult` outputs survive —
only the agent memory is shed. Place it between unrelated pipeline sections.

```python
.stage("read_spec", ReadFile(path="spec.txt"))
.clear_context()
.stage("generate", Generate(prompt="..."))
```

---

## Stage Types

### Generate

Sends a prompt to Claude via the claude-agent-sdk.

```python
Generate(
    prompt="...",                    # The prompt (supports {stage.output} and {param.key} placeholders)
    template="name",                 # OR use a named template (see Templates section)
    input="{prior_stage.output}",    # Input for template placeholders
    model="sonnet",                  # Model: "opus", "sonnet", "haiku", or full model ID
    thinking={"type": "enabled", "budget_tokens": 10000},  # Thinking budget
    permission_mode="acceptEdits",   # "default", "acceptEdits", "plan", "bypassPermissions"
    allowed_tools=["Read", "Edit", "Bash"],  # Pre-approved tools
    max_turns=20,                    # Max conversation turns
    cwd="/path/to/project",          # Working directory for the agent
    setting_sources=["project"],     # Load Claude project settings from cwd
    add_dirs=["/extra/dir"],         # Additional allowed directories
    output_file="out.py",            # Write extracted code to file (only when no tools)
    env={"KEY": "value"},            # Environment variables for the agent
    hooks={...},                     # SDK-level hooks (PreToolUse, PostToolUse)
    skills=["review-pr"],            # Skills to inject into system prompt
)
```

**Placeholders** in prompts:
- `{stage_name.output}` — output from a prior stage
- `{param.key}` — pipeline parameter from `--arg` or positional args
- Unresolved placeholders are left as-is (not an error)

**Model shortcuts:**
| Short name | Model ID |
|------------|----------|
| `opus` | `claude-opus-4-6` |
| `sonnet` | `claude-sonnet-4-6` |
| `haiku` | `claude-haiku-4-5-20251001` |

**Artifact tracking:** When the agent uses Write/Edit tools, the file paths are
captured automatically via a PostToolUse hook and displayed after the stage.

### ReadFile

Read a file from disk.

```python
ReadFile(path="spec.txt")
```

Returns the file contents as `StageResult.output`.

### RunCommand

Run a shell command.

```python
RunCommand(
    cmd="python -m pytest tests/ -v",
    env={"NODE_ENV": "production"},    # Optional: extra env vars
)
```

Returns a dict as `StageResult.output`:
```python
{"stdout": "...", "stderr": "...", "returncode": 0}
```

Environment variables support `{secret.NAME}` and `{param.NAME}` placeholders.

---

## Conditional Stages

Stages can be conditionally executed based on prior results.

```python
from norn.dsl import stage_succeeded, stage_failed, output_contains, file_exists

Pipeline("build")
    .stage("check", RunCommand(cmd="git diff --name-only"))
    .stage("lint", RunCommand(cmd="ruff check ."),
           when=lambda ctx: ".py" in str(ctx.get("check")))
    .stage("deploy", RunCommand(cmd="./deploy.sh"),
           when=stage_succeeded("test"))
    .stage("fix", Generate(prompt="Fix: {test.output}"),
           when=stage_failed("test"))
    .stage("notify", RunCommand(cmd="..."),
           when=output_contains("check", ".tsx"))
    .stage("package", RunCommand(cmd="..."),
           when=file_exists("dist/bundle.js"))
```

Built-in predicates:
- `stage_succeeded(name)` — True if the named stage passed
- `stage_failed(name)` — True if the named stage failed
- `output_contains(name, text)` — True if stage output contains text
- `file_exists(path)` — True if file exists on disk

Skipped stages show as: `⊘ lint  skipped (condition not met)`

---

## Alerts

Get notified when pipelines complete, fail, or need attention.

### Channels

```python
from norn.alerts import MacOSChannel, SlackChannel, FileChannel, AlertEvent

# macOS system notification with sound
MacOSChannel()
MacOSChannel(app_name="My Pipeline", events={AlertEvent.FAILED})

# Slack incoming webhook
SlackChannel(webhook_url="https://hooks.slack.com/services/XXX/YYY/ZZZ")
SlackChannel(webhook_url="...", events={AlertEvent.FAILED, AlertEvent.ASK_USER})

# JSONL file (useful for CI)
FileChannel(path="tmp/alerts.jsonl")
```

### Events

| Event | Fires when |
|-------|-----------|
| `COMPLETE` | Pipeline finished successfully |
| `FAILED` | Pipeline terminated with an error |
| `ASK_USER` | Pipeline paused waiting for user input |
| `RETRIES_EXHAUSTED` | A loop ran out of retry attempts |

### Usage

```python
config = (
    Pipeline("build")
    .alert(MacOSChannel())
    .alert(SlackChannel(webhook_url="...", events={AlertEvent.FAILED}))
    .stage(...)
)
```

Channels with `events=None` (default) receive all events.
Channel failures are logged as warnings but never break the pipeline.

---

## Budgets

Limit cost or token usage. Checked after every stage.

```python
Pipeline("expensive")
    .budget(max_cost_usd=5.00)                           # fail on exceed
    .budget(max_cost_usd=10.00, on_exceed=ask_user)      # ask user on exceed
    .budget(max_tokens=500_000)                           # token limit
```

Multiple budgets can be stacked (all are checked independently).

The running total in terminal output shows budget progress:
```
  ─── Running total: $2.34 / $5.00 (47%) ───
```

On exceed:
- `fail` — raises `BudgetExceededError` immediately
- `ask_user` — prompts with `[c]ontinue / [a]bort`

---

## Lifecycle Hooks

Run stages before/after pipeline events.

```python
Pipeline("build")
    .hook("pre_stage", RunCommand(cmd="echo 'Starting stage'"))
    .hook("post_stage", RunCommand(cmd="git add -A && git commit -m 'auto'"))
    .hook("on_failure", RunCommand(cmd="./capture-diagnostics.sh"))
    .hook("on_retry", RunCommand(cmd="git checkout -- ."))
```

| Event | Fires |
|-------|-------|
| `pre_stage` | Before each stage runs |
| `post_stage` | After each stage succeeds (not on failure) |
| `on_failure` | When any stage fails |
| `on_retry` | Before a loop retry (not on first attempt) |

Hooks are `BaseStage` implementations — any stage type works.
A hook failure raises `PipelineError` and aborts the pipeline.

---

## Checkpoints and Resumption

The runner saves a checkpoint after each successful stage. On failure, resume
from where it left off:

```bash
# First run — fails at stage 3
bin/norn examples/hello.py
  ✓ read_spec         0.1s
  ✓ generate          $0.12  4.2s
  ✗ test              exit 1

# Resume — skips completed stages
bin/norn examples/hello.py --resume
  ⊘ read_spec         (cached)
  ⊘ generate          (cached)
  ✓ test              0.8s
```

Checkpoint file: `examples/hello.checkpoint` (JSON, using the same state-key
location as history files).

Contains: completed stage names, their outputs, session ID, timestamp.
The Claude session is also resumed so the agent has memory of prior conversation.

---

## Context Injection

Auto-load files or command output into Generate stage prompts.

```python
Pipeline("build")
    .context("ARCHITECTURE.md")                         # file
    .context("src/**/*.py", label="source_code")        # glob
    .context_cmd("git log --oneline -10", label="recent_commits")  # command
    .stage("generate", Generate(prompt="implement {param.args}"))
```

For stages with tools (`permission_mode` set), context is injected as `system_prompt`.
For pure-prompt stages, context is prepended to the prompt.

The total context size (approximate tokens) is logged on startup with `-v`.

---

## Secrets and Environment Variables

### Environment Variables

```python
Pipeline("deploy")
    .env("NODE_ENV", "production")
    .stage("build", RunCommand(cmd="npm run build"))
    .stage("generate", Generate(prompt="...", env={"DEBUG": "1"}))
```

Pipeline-level env vars are available to all stages.
Stage-level env vars merge with (and override) pipeline-level ones.

### Secrets

```python
Pipeline("deploy")
    .secret("DEPLOY_TOKEN", source="env")        # from $DEPLOY_TOKEN
    .secret("DB_PASSWORD", source="keychain")     # macOS Keychain
    .secret("API_KEY", source="file")             # from .env file
    .secret("PASSPHRASE", source="prompt")        # ask user at startup
    .stage("deploy", RunCommand(
        cmd="./deploy.sh",
        env={"TOKEN": "{secret.DEPLOY_TOKEN}"},
    ))
```

Secret sources:
| Source | Reads from |
|--------|-----------|
| `env` | Shell environment variable |
| `keychain` | macOS Keychain Access |
| `file` | `.env` file in working directory |
| `prompt` | Interactive prompt (masked input) |

Secrets are:
- Resolved at pipeline start (before any stages run)
- Available via `{secret.NAME}` in stage `env` dicts
- Automatically masked in all terminal output (replaced with `***`)

---

## Templates

Reusable prompt + system_prompt + output_format combinations.

### Define a Template

Create `templates/code_review.py`:

```python
from norn.templates import PromptTemplate

code_review = PromptTemplate(
    name="code_review",
    template="Review this code:\n{input}\n\nOutput JSON with: issues[], summary, score",
    system_prompt="You are a senior code reviewer. Be concise.",
    output_format={"type": "object", "required": ["issues", "summary", "score"]},
)
```

### Use in a Pipeline

```python
Stage("review", Generate(
    template="code_review",
    input="{generate.output}",
))
```

The SDK's `output_format` produces validated JSON via `ResultMessage.structured_output`.

---

## Skills

Inject specialized instructions into Generate stages.

### Named Skills (files)

Skill files are markdown files searched in order:
1. `skills/<name>.md` (pipeline-local)
2. `.claude/skills/<name>.md` (project)
3. `~/.claude/skills/<name>.md` (user)

Qualified names: `package:skill` searches `skills/package/skill.md` etc.

```python
Stage("review", Generate(
    prompt="Review src/",
    skills=["review-pr"],
))
```

### Inline Skills

```python
from norn.skills import Skill

strict = Skill(
    name="strict-review",
    content="Flag any function longer than 30 lines. Require type hints.",
)

Stage("review", Generate(prompt="Review src/", skills=[strict]))
```

### Pipeline-Level Skills

Apply to all Generate stages:

```python
Pipeline("build")
    .skills(["commit", "review-pr"])
    .stage("generate", Generate(prompt="...", skills=["extra"]))  # merged
```

Skills are injected into the agent's `system_prompt`.

---

## Model Selection

Choose different models per stage for cost optimization.

```python
Pipeline("smart-build", default_model="sonnet")
    .stage("plan", Generate(prompt="Plan: {param.args}", model="opus"))
    .stage("implement", Generate(prompt="Implement: {plan.output}"))  # uses sonnet
    .stage("review", Generate(prompt="Quick check", model="haiku"))
```

The usage report shows which model was used per stage.

Thinking budget control:
```python
Generate(
    prompt="Complex reasoning task...",
    model="opus",
    thinking={"type": "enabled", "budget_tokens": 10000},
)
```

---

## Interactive Stepping

Debug pipelines by stepping through stages one at a time.

```bash
bin/norn examples/hello.py --step
```

```
  Next: read_spec [ReadFile: examples/spec.txt]
  [r]un  [s]kip  [a]bort  [i]nspect? i
  > Command: cat examples/spec.txt
  [r]un  [s]kip  [a]bort? r
  ✓ read_spec                        0.1s

  Next: generate [Generate]
  [r]un  [s]kip  [a]bort  [i]nspect? r
  ✓ generate          $0.12          4.2s
```

Commands:
- `r` or Enter — run the stage
- `s` — skip (mark as success with no output)
- `a` — abort the pipeline
- `i` — inspect (show resolved prompt, command, session info)

---

## Terminal Output

norn uses [rich](https://github.com/Textualize/rich) for terminal output.

```
Pipeline hello starting

  ↻ generate_and_build (attempt 1/3)
  ✓ read_spec                         0.1s
  ✓ generate          $0.12  (12.4k in / 2.1k out)  4.2s
    + tmp/hello/src/greeter.py
    + tmp/hello/tests/test_greeter.py
  ─── Running total: $0.12 ───
  ✗ test              exit 1           1.3s
      AssertionError: expected 'hello' got None

  ↻ generate_and_build (attempt 2/3)
  ✓ generate          $0.08  (8.1k in / 1.4k out)   3.1s
  ✓ test                               0.8s
  ✓ generate_and_build — all stages passed

  Pipeline "hello" — Usage Report
   Stage         Input    Output      Cost    Time
   generate     12,400     2,100    $0.1200    4.2s
   generate      8,100     1,400    $0.0800    3.1s
   ─────────────────────────────────────────────────
   Totals       20,500     3,500    $0.2000    7.3s

  Session: abc123
  To resume: norn run examples/hello.py --resume
```

Stage indicators:
- `✓` green — succeeded
- `✗` red — failed
- `↻` yellow — loop retry
- `⊘` dim — skipped (by `--skip`, condition, or cached)
- `⇶` blue — parallel block
- `⤵` magenta — include

---

## Dynamic Pipeline Generation

Pipeline configs are Python files — use any Python to generate stages dynamically.

```python
from glob import glob
from pathlib import Path

from norn.dsl import Pipeline, Stage, fail
from norn.stages.generate import Generate
from norn.stages.run_command import RunCommand

pipeline = Pipeline("batch")

for feature_file in sorted(glob("tmp/*.md")):
    name = Path(feature_file).stem
    content = Path(feature_file).read_text()

    pipeline.loop(
        f"implement {name}",
        max_retries=5,
        on_exhaust=fail,
        stages=[
            Stage(f"implement {name}", Generate(
                prompt=f"Implement this feature:\n{content}",
                permission_mode="acceptEdits",
            )),
            Stage(f"test {name}", RunCommand(cmd="pytest tests/ -v")),
        ],
    )
    pipeline.stage(f"commit {name}", RunCommand(
        cmd=f'git add -A && git commit -m "feat: {name}"',
    ))
    pipeline.clear_context()

config = pipeline
```

---

## Project Layout

```
norn/
├── dsl.py          # Pipeline, Stage, Loop, Parallel, Include, conditions
├── models.py       # StageResult, PipelineContext, UsageTracker, UsageRecord
├── runner.py       # Executes pipelines: stages, loops, parallel, includes
├── ui.py           # Rich terminal output: colors, tables, interactive prompts
├── alerts.py       # AlertManager, MacOSChannel, SlackChannel, FileChannel
├── checkpoint.py   # Save/restore pipeline state for --resume
├── history.py      # Run history (JSONL), comparison
├── secrets.py      # Secret resolution (env, keychain, file, prompt)
├── templates.py    # PromptTemplate loading from templates/ directory
├── skills.py       # Skill resolution (local, project, user directories)
├── loader.py       # Dynamic pipeline loading via importlib
├── cli.py          # Entry point, arg parsing
├── stages/
│   ├── base.py     # Abstract BaseStage (async def run(ctx) -> StageResult)
│   ├── read_file.py
│   ├── run_command.py
│   └── generate.py # Claude agent SDK integration
bin/
└── norn      # Wrapper script (auto-prepends 'run')
examples/           # Sample pipeline configs
dogfooding/         # Self-use pipeline configs
templates/          # Prompt template files
skills/             # Skill markdown files
tests/              # pytest tests
bats/               # BATS CLI integration tests
```
