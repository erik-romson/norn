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

Model IDs and reasoning-effort levels are pinned in `norn/model_labels.toml`. If your opencode is authenticated against a different provider (e.g. Anthropic direct), edit the `[opencode]` section of that file to use the IDs `opencode models` lists.

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

Discover bundled pipelines and inspect available model labels:

```bash
uv run python -m norn list
uv run python -m norn describe hello
uv run python -m norn models
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

# Raise the run's ceilings before it starts (any pipeline, overrides its own)
uv run python -m norn run pipeline.py --budget 200 --token-budget 4000000
```

### Budgets

A pipeline's `.budget(...)` limit is checked after every stage. When the limit is
an `ask_user` one, crossing it stops the run and asks:

```
⚠ Budget exceeded: $31.2043 exceeds limit $30.00
  [c] +50%   [d] +100%   [u] No limit   [a] Abort
```

Continuing **raises the limit** rather than waving one stage through, measured
from what the run has already spent — so the prompt does not reappear after
every remaining stage. `u` drops the limit for the rest of the run; `a` aborts
with `BudgetExceededError`.

Set the ceiling before the run instead of negotiating with it mid-run: pass
`--budget`/`--token-budget` to `norn run`, fill in the **budget** and
**token_budget** fields on the TUI launcher's arguments screen (blank keeps the
pipeline's own limit), or — for pipelines that declare them — use their
`--arg budget=` knob.

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
| `tmp/<slug>-final-plan/` | Claude — `index.md` + `step-NN-*.md`, ready for `norn run implement_features_v2 tmp/<slug>-final-plan` |

`final` marks the deliverable so it is never mistaken for the brief it came
from: only a trailing `-preplan`/`_preplan` is stripped when deriving the slug,
so a brief named `x-plan.md` would otherwise produce `x-plan-plan.md`. The step
directory is the plan path minus `.md`. The final gate rejects a split
whose steps lack a real `test_cmd:` or carry front-matter keys
`implement_features_v2` refuses to load, so the run stops rather than handing
over steps that validate nothing or that the implementer cannot load.

**Human round-trip (open questions):** the run pauses and sends a notification →
open `<slug>-plan-questions.md` → fill in the `**Answer:**` slots (leave
`STATUS:` alone; that is the agent's line) → choose `[r]etry`. **`[c]ontinue`
does not skip the questions** — the run stops at the hard gate right after the
loop. To pick it up later, answer the file and re-run with `--continue`.

CLI-only knobs (not available under `norn ui`, because the TUI reconstructs only
positional args):

```bash
--arg model=standard          # intent label for draft/revise/apply (default: deep)
--arg codex_label=review      # label for the codex plan-review pass (default: review)
--arg budget=50               # cost ceiling in USD before the run pauses
```

(`--budget`/`--token-budget` and the TUI launcher's budget fields set the same
ceilings without the pipeline knob, and work under `norn ui`.)

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
| `step-NN-<name>.md` | One file per step, numbered from `01` and contiguous; optional front-matter: `test_cmd`, `bats_cmd`, `test_timeout`, `bats_timeout`, `model` (`light`\|`standard`\|`deep`), `allow_noop` (`true` for a verification-only step that is expected to change nothing) |

Optional `--arg` knobs (all have defaults; document them in plan files, not
in pipeline invocations):

| Knob | Default | Description |
| --- | --- | --- |
| `budget=<float>` | `$4 × steps + $25` | USD cost ceiling; `--budget` and the TUI's budget field override it |
| `token_budget=<int>` | `80k × steps + 200k` | Token ceiling; `--token-budget` and the TUI's token_budget field override it |
| `review_model=<light\|standard\|deep>` | `standard` | Label for review stages |
| `aggregate_model=<light\|standard\|deep>` | `standard` | Label for aggregate repair |
| `max_retries=<int>` | `3` | Per-step validation attempts (≥ 1) |
| `allow_dirty_index=1` | off | Skip the clean-index preflight check |
| `allow_dirty_worktree=1` | off | Skip the clean-worktree preflight check |

The pipeline writes two report files into the feature directory:

- `review.md` — structured review verdict written by the review agent; first
  three lines are `VERDICT: PASS`/`VERDICT: NEEDS_FIXES`, `Base: <sha>`,
  `Head: <sha>`. Findings sit under `## Blocking findings` (critical and major,
  which hold the verdict and are what the fix round applies), `## Non-blocking`
  (minor and nits, carried to the handoff) and `## For the human` (work outside
  the run's own commits, which the fix round has no git commands to do).
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

The run works on a branch named after the issue. When the branch you are
already on names the issue — `CBS-2249`, `feature/CBS-2249-sorting`,
`bugfix/cbs-2249`, matched case-insensitively — it stays there rather than
cutting a new branch and stranding the work in progress. Pass
`--arg branch=<name>` to name a branch yourself; an explicit name is always
honoured.

**Required environment variables:**

| Variable | Description |
| --- | --- |
| `JIRA_AUTH` | Jira Basic-auth credentials — plaintext `email:api-token` (`curl -u` encodes them) |
| `JIRA_BASE` | Jira instance URL with trailing slash — `https://your-org.atlassian.net/` |
| `ANTHROPIC_API_KEY` | Anthropic API key (or `CLAUDE_CODE_OAUTH_TOKEN`) |

**Artifacts** (written under `tmp/jira/<KEY>/`):

| File | Written by |
| --- | --- |
| `issue.json` | fetch — raw Jira API response |
| `issue.md` | fetch — Markdown rendition of the issue |
| `attachments/` | fetch — downloaded issue attachments |
| `<KEY>-preplan.md` | Claude (light) — structured brief |
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
--arg model=standard      # intent label for all Generate stages (default: deep)
--arg codex_label=review  # label for the codex plan-review pass (default: review)
--arg budget=20           # cost ceiling in USD before the run pauses (default: 50)
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

### merge_branch

`merge_branch` takes the current feature branch from "committed locally" to
"merged into its base branch": resolve the base and rebase, loop the local
test suite with an LLM either fixing a failure or saying STOP, push, open or
reuse the PR and request Copilot as reviewer, wait out the PR's checks
(handing red Actions checks to a `check_and_fix_ci` child run) and Copilot's
review, classify and post review findings, fast-forward merge, and watch the
post-merge build.

```bash
uv run python -m norn run merge_branch --non-interactive
```

`--non-interactive` is the fire-and-forget form: the run never pauses for a
human, so if it halts it simply ends (a halted guard still exits 0, but
`--non-interactive` always answers the closing prompt `[a]bort`).

**Prerequisites:**

- `gh auth login` with a push-capable token — `preflight` checks push
  permission on the repository before spending anything.
- macOS, for the `osascript`-based lifecycle notifications fired on pause and
  at the end of the run (`norn run` only; `norn ui` passes no `AlertManager`,
  so a run launched from the TUI gets none).
- The base branch's protection rules must accept a direct fast-forward push
  of a PR head that already satisfies them — the merge step is
  `git push origin <sha>:refs/heads/<base>`, never `gh pr merge`. A squash or
  GitHub-rebase merge would re-create the commit SHA the checks ran against;
  `--auto` would end the run before the merge happens, with the outcome
  unknown.

**CLI-only knobs** (not available under `norn ui`, same as `fix_jira_issue`):

| Knob | Default | Description |
| --- | --- | --- |
| `base=<branch>` | resolved | Base branch to rebase onto and merge into — checked against an open PR's base, then the nearest fork point among `main`/`master`/`develop`/`development`, then the repo's default branch |
| `test_cmd="..."` | auto-detected | Local test command |
| `fix_model=<light\|standard\|deep>` | `standard` | Label for the local-fix Generate stage |
| `classify_model=<light\|standard\|deep>` | `standard` | Label for the review-findings classifier |
| `budget=<float>` | `15` | USD cost ceiling before the run pauses |
| `guidance="..."` | (none) | Extra text passed verbatim to the local fixer |
| `overwrite_remote=<sha>` | (none) | One-shot authorisation to force-push over commits on the remote branch with no patch-equivalent on HEAD — refused even when the same human wrote them, unless this is passed and matches the current `origin/<branch>` sha exactly. Never repeated by the rerun command the run's own summary prints. |

**Interactive runs** (no `--non-interactive`) pause only at the very end, and
only when the run halted — `escalate` is the pipeline's one `ask_user` stage.
The prompt offers:

- `[a]bort` — ends the run. Recommended; this is what `--non-interactive`
  always answers.
- `[c]ontinue` — ends the run recorded as a success, even though nothing
  merged.
- `[r]etry` — asks again (nothing about the halt changes by retrying the
  prompt itself; fix the underlying cause and rerun the pipeline instead).

**Red PR checks** start a `check_and_fix_ci` child run (at most one per
round) with its own journal under `<state-key>.runs/<n>/` and its own
lifecycle notifications, separate from `merge_branch`'s. A merge that lands
with no push-triggered workflow on the base within 120 seconds still ends
`MERGED`, with a warning in the summary and a notification — not a halt; a
base branch with no CI at all is not this run's problem. CI that runs longer
than an hour always halts on the timeout ceiling rather than waiting
indefinitely.

**Where the summary lives:** `<tempfile.gettempdir()>/norn-merge/v1/<hash of
the repo path>/<branch-slug>-merge-state.md` — never inside the repository.
Every halt path writes it, along with a narrated `## What happened` section.

**Rerun, don't resume:** `--resume` is refused (`AssertFreshRun`) — a halted
guard exits 0 and is checkpointed as a success, so resuming would just skip
straight past it. Every stage is idempotent, so recovery is always "fix the
cause, then rerun the exact command the summary prints."

A few halts are structural, not bugs to route around:

- A repository that requires a human approval on the PR always stops the run
  at the merge gate (`review_required`) — by design, not a gap to fill in.
- A check that must gate the merge belongs in branch protection. A
  non-required app check that only reports after the merge-time re-read
  cannot be seen by this pipeline and is not part of its contract.

**Halt catalogue** — what each named stop means and what to do next (answer
`[a]bort` at the `escalate` prompt first, if the run is interactive):

| Kind | What to do next |
| --- | --- |
| `rebase_conflict` | Rebase by hand onto `origin/<base>`, resolve, then rerun. |
| `local_stop`, `local_cap`, `local_stuck` | Fix it yourself and commit, or rerun with `--arg guidance="..."`, then rerun. |
| `local_verdict_missing`, `local_verdict_malformed` | Rerun. If it repeats, fix by hand. |
| `local_head_moved`, `local_no_diff`, `untracked_files` | Inspect `git status`/`git log`, clean up, rerun. |
| `local_gate_red`, `test_timeout` | Fix, commit, rerun. |
| `commit_rejected` | Read the round's commit log (usually a pre-commit hook), fix, rerun. |
| `push_rejected` | The remote branch moved or refused the push. Reconcile, rerun. |
| `pr_base_mismatch`, `pr_draft` | Retarget or undraft the PR yourself, rerun. |
| `checks_red` | Fix CI by hand, push, rerun. |
| `checks_timeout`, `checks_none`, `pr_head_moved` | `checks_none` means the workflows did not trigger. Check the Actions tab, push, rerun. |
| `checks_changed` | A check appeared or changed after settling. Look at it, rerun. A gating check belongs in branch protection. |
| `ci_fix_failed`, `ci_fix_timeout` | Read the child's journal (path is in the summary). Fix CI by hand, push, rerun. |
| `ci_fix_unpushed` | The child left commits unpushed or edits uncommitted. Inspect, clean up, rerun. |
| `attribution_found` | Remove the assistant attribution line, rerun. |
| `copilot_timeout` | Wait for or re-request the Copilot review of the current head, rerun. |
| `copilot_rerequest_failed` | Re-request Copilot on the PR by hand, rerun. |
| `findings_malformed`, `classifier_touched_repo` | Rerun. Discard any change the classifier made. |
| `findings_blocking`, `changes_requested` | Address the findings (by hand or with `implement_features_v2`), push, rerun. |
| `review_required`, `merge_state` | Get the approval, or fix the named merge state, then rerun. |
| `head_moved_since_checks` | A commit landed after the checks ran. Rerun. |
| `merge_rejected` | Branch protection refused the fast-forward. Merge by hand. |
| `merged_pr_not_closed`, `branch_delete_failed` | The base is already fast-forwarded. Check the PR and branch by hand. |
| `no_merge_after_rounds` | The base moves too fast. Merge by hand or rerun later. |
| `base_build_red`, `base_build_timeout` | The merge landed and the base build is red. Fix the base. |
| `unexpected_failure` | Read `## Why it stopped` in the summary. Transient (gh 5xx, network)? Rerun. Else fix the cause. `Status` in the summary says whether the merge landed. |

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

**Which `claude` binary runs**

The Claude Agent SDK ships its own copy of the Claude Code CLI and prefers it
over the one on your `PATH`. That copy is frozen at the SDK release, so it
falls behind the Claude Code you installed, and the gap shows up as a flag the
CLI rejects mid-run — `--effort xhigh`, which the `deep` label sends, needs CLI
2.1.9x or newer. norn compares the two versions and spawns the newer one,
logging its choice once per run:

```
[claude-code] CLI: /Users/you/.local/bin/claude (PATH, 2.1.263)
```

Set `NORN_CLAUDE_CLI` to name a binary explicitly; it wins over both and is not
version-checked.

```bash
export NORN_CLAUDE_CLI=/opt/claude/bin/claude
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

## Evaluate, rate and compare runs

After a run, norn can compute deterministic metrics, run an LLM judge over the stage records, and record a human rating — all from the same journal written during the run.

### Run directory layout

```
<config>.runs/<run_id>/
  run.json          # run metadata (pipeline, provider, start/end, success)
  events.jsonl      # full redacted event stream
  metrics.json      # deterministic cost/error/tool metrics (zero tokens)
  findings.json     # rule-based findings (threshold violations)
  stages/
    <stage>.json    # per-stage record (prompt, output, usage)
    <stage>.eval.json  # per-stage judge verdict (after norn evaluate)
  evaluation.json   # aggregate evaluation result
  feedback.md       # narrative feedback written by norn evaluate
  rating.json       # human rating (after norn rate)
```

### Commands

```bash
# Run a pipeline and evaluate it afterwards (tokens billed separately)
uv run python -m norn run pipeline.py --evaluate

# Evaluate a completed run (by run id)
uv run python -m norn evaluate pipeline.py --run 3

# Record a human rating (1–5) for a completed run
uv run python -m norn rate pipeline.py --run 3 4

# Inspect metrics and findings for a single run
uv run python -m norn history pipeline.py --run 3

# Compare two runs side by side
uv run python -m norn history pipeline.py --compare 2 3
```

Default labels: **light** for the stage judge, **standard** for the narrative feedback pass. Use `--model deep` to upgrade both. `--max-cost` defaults to `5.0` USD; the command exits with an error if the running evaluation spend would exceed it.

Evaluation cost is reported separately and never added to the pipeline's history totals.

### Optional: CodexBar

[CodexBar](https://codexbar.app/) is an optional macOS menu-bar tool that exposes your Claude subscription quota over a local CLI. Norn can sample it to record how much of your monthly budget a run consumed.

```bash
# Install CodexBar CLI (macOS only)
brew install steipete/tap/codexbar
# or use the in-app "Install CLI" option

# Sample quota during a run (opt-in; norn never calls CodexBar unless asked)
uv run python -m norn run pipeline.py --quota codexbar

# Abort the run when quota cannot be read, instead of continuing
uv run python -m norn run pipeline.py --quota codexbar --require-quota

# Refuse to start unless 20% of the session window is left
uv run python -m norn run pipeline.py --require-quota 20

# Same, against the weekly window
uv run python -m norn run pipeline.py --require-quota 20:weekly

# Start anyway when CodexBar cannot tell us how much is left
uv run python -m norn run pipeline.py --require-quota 20 --quota-fail-open
```

Given a percentage, `--require-quota` runs `codexbar guard` *before* the pipeline starts and stops the run when headroom is short. `--quota-fail-open` overrides it, but only for the "quota unavailable" case (CodexBar missing, timing out, or reporting exit 69) — a real "not enough quota" answer still blocks the run.

norn never auto-detects or calls CodexBar. Identity fields (account, org) are stripped before anything is written to the journal.

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
