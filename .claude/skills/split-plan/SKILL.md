---
name: split-plan
description: "Split a plan/spec file into step files for the implement_features_v2 pipeline. Creates an index.md with shared context and numbered step-*.md files. Use when you have a plan and want to break it into executable steps. Triggers on: split plan, divide plan, break into steps, create steps from plan."
argument-hint: "[plan-file] [output-directory]"
disable-model-invocation: true
---

# Split Plan into Pipeline Steps

Take a plan file and split it into `index.md` + `step-NN-name.md` files that can be executed by `norn run implement_features_v2 <output-directory>`.

## The pipeline contract (read first)

`implement_features_v2` (`norn/pipelines/implement_features_v2.py`) runs the
steps, and it enforces rules that a step file cannot opt out of. A step written
against a different model of the pipeline fails a gate at run time, hours and
dollars in. Every step you write must fit these rules:

- **The pipeline owns git. The agent never touches it.** After each step the
  pipeline stages exactly the paths the step changed and commits them with the
  subject `refactor: <step stem>` (`refactor: step-03-move-matchers`). The
  agent is forbidden to run `git add`, `git commit`, `git amend`, `git reset`,
  `git checkout`, `git restore`, `git rebase`, `git stash`, `git clean` or
  `git push`, and a HEAD that moves during a step fails the run. So a step's
  **Actions** must never say "commit", "stage", "amend", "stash", "reset",
  "check out a branch" or "push". Never write a "land the pending work" or
  "commit the working tree" step.
- **The run starts from a clean worktree and a clean index.** Uncommitted work
  is refused at preflight. If the tree carries finished but uncommitted work
  the plan depends on, do not turn it into a step: say in `README.md`, under
  "Before running", that the human must commit it first, and name the paths.
- **Every step must change at least one file.** A step that changes nothing
  stops the run to ask, and aborts a `--non-interactive` run. A step that is
  verification-only declares `allow_noop: true` (rules below). Never write a
  step whose only output is git state.
- **`test_cmd` runs after the agent, in the working tree, before the commit.**
  It must not assert git state (commit subjects, a clean tree, HEAD position),
  and it must leave tracked files as it found them: a command that regenerates
  a fixture or rewrites a lock file dirties the tree and fails the final audit.
  Gitignored outputs are fine.
- **`test_cmd`s are rerun together at the end.** After the last commit the
  unique `test_cmd`s of all steps run again as the aggregate pass (or
  `final_test_cmd` from `index.md` when set), so each one must be idempotent
  and must still pass once later steps have landed.
- **Front matter is validated strictly at load.** A step accepts only
  `test_cmd`, `bats_cmd`, `test_timeout`, `bats_timeout`, `model` and
  `allow_noop`; `index.md` accepts only `test_cmd`, `bats_cmd`,
  `test_timeout`, `bats_timeout` and `final_test_cmd`. Any other key, including
  `effort`, makes the pipeline refuse to load. Step files are numbered from
  `01` and contiguous.
- **Step stems must be unique in the repo.** A rerun skips any step whose
  `refactor: <stem>` commit is already on HEAD, so two plans that share a stem
  collide. Include the feature in the stem when the name would otherwise be
  generic (`step-02-suggestions-plumbing`, not `step-02-plumbing`).

## Arguments

- `$0` — Path to the plan file (e.g. `tmp/refactor-plan.md`)
- `$1` — Output directory for the split files (e.g. `tmp/refactor`)

Both arguments are required. If missing, ask the user.

## Output Format

The output directory must contain:

### `index.md` — Shared context injected into every step

Contains information the agent needs for **every** step:
- The overall goal / what we're building toward
- Target structure or architecture
- Rules and conventions that apply across all steps
- Patterns to follow (e.g. naming, import style, shim strategy)
- What NOT to change

This is NOT a summary of the steps. It's the **context** that makes each step self-sufficient. Think: "what would someone need to know to implement step 7 without reading steps 1-6?"

### `step-NN-name.md` — One file per step

Naming: `step-01-short-name.md`, `step-02-short-name.md`, etc.

Each step file MUST start with YAML front-matter declaring a real
`test_cmd:` — the exact shell command the pipeline runs to validate the
step. The pipeline (`norn/pipelines/implement_features_v2.py`) does NOT guess
a default; a missing or empty `test_cmd` halts the run.

```markdown
---
test_cmd: uv run python -m pytest tests/test_widget.py -v
bats_cmd: bats bats/widget.bats          # optional, omit when not used
model: deep                              # optional, omit on routine steps
allow_noop: true                         # optional, verification-only steps only
---

# Step title

…
```

Rules for `allow_noop`:

- Set it **only** on a step whose whole job is to run a suite and confirm
  nothing broke — a regression gate that is expected to produce no commit.
- Leave it off everywhere else. The pipeline's default is to stop and ask
  when a step changes nothing, which is what catches an agent that silently
  did no work; `allow_noop: true` waives that check for one step.
- A verification-only step without it aborts any run started with
  `--non-interactive` (a child run under `fix_jira_issue`), because there is
  no operator to accept the no-op.

Rules for `test_cmd`:

- **Must be a real, runnable command** — never `true`, never a
  placeholder. If a step is purely documentation/refactor with no
  behaviour change, point `test_cmd` at the existing test suite (or the
  most relevant subset) so a regression in this step is still caught.
- **Must not depend on git state or dirty the tree** — see the pipeline
  contract above. It runs on the uncommitted working tree before the
  step's commit exists, and again in the aggregate pass at the end.
- **Must run in plain bash without a Claude session** — no Claude Code
  skills, no MCP tools. The pipeline executes `test_cmd` via
  `RunCommand`, which shells out with no agent context, so anything
  that depends on a skill is unreachable. If the validation needs a
  skill, wrap it in a CLI script (Snowflake Python connector, `gh`
  CLI, etc.) and call that instead.
- **As narrow as possible** — prefer the file or module whose behaviour
  this step actually changes. The narrower the command, the smaller the
  log handed to the fix loop on failure.
- **Use the project's own runner** — `uv run`, `mvn`, `npm test`,
  `cargo test`, etc. Match what already works in the repo.
- The same rules apply to `bats_cmd` when bats integration tests exist;
  omit the key entirely when there are none.
- **Multi-step `test_cmd` must be debuggable.** If the command chains
  more than one shell step (with `&&`, pipes, or a `timeout` wrapper),
  wrap the whole thing in `sh -ex -c '...'` and put an
  `echo "=== STEP N: <what> ==="` marker before each sub-step. This
  makes failures localizable: the shell `+ ` trace plus the last
  echoed marker pinpoint which sub-command died, instead of just
  showing `command exited with status 124` with no clue which of the
  five chained calls timed out. Single-call commands
  (`uv run python -m pytest …`) do NOT need this. Example:

  ```yaml
  test_cmd: |
    sh -ex -c '
      echo "=== STEP 1: validate config ==="
      docker compose -f compose.yml config -q
      echo "=== STEP 2: bring up service ==="
      docker compose -f compose.yml up -d svc
      echo "=== STEP 3: wait for ready ==="
      timeout 60 sh -c "until curl -sf localhost:8080/health; do sleep 2; done"
      echo "=== STEP 4: tear down ==="
      docker compose -f compose.yml down -v
    '
  ```

Optional `model:` override:

- Default: omit the field. The pipeline runs the step on its
  configured default (`standard`), which is fast and cheap.
- Set `model: deep` only when the step genuinely needs heavier
  reasoning — e.g. designing a non-trivial algorithm, untangling a
  subtle cross-module change, or producing code where prior runs of
  similar work on `standard` have failed. Routine refactors,
  rename/move/extract steps, and mechanical edits should NOT set
  this field.
- Do not default to `deep` across a whole plan. `deep` is several
  times the cost of `standard`; setting it everywhere defeats the
  per-step selection.
- Accepted values are the labels in `norn/model_labels.toml`:
  `light`, `standard`, `deep`.

If the plan does not give you enough information to pick a real
`test_cmd` for a step, ASK before generating the file rather than
inventing one. A wrong `test_cmd` masks regressions; a placeholder one
makes the step's outcome meaningless.

The body of each step file must contain:

1. **What** — What to do in this step (concrete, not vague)
2. **Actions** — Specific file operations, commands, or changes
3. **Why this `test_cmd` is the right contract** — one or two sentences
   explaining what the test actually exercises for this step

### `README.md` — Optional overview for humans

A table showing the step order, what each does, and why tests pass between steps. This file is NOT picked up by the pipeline.

Add a "Before running" section whenever the human has to do something the
pipeline forbids the agent: commit pending work so the tree is clean, land
a dependency in another repo, or provide a credential.

## Process

1. Read the plan file at `$0`
2. Read any existing files in `$1` (if the directory exists) to understand current state
3. Analyze the plan for:
   - Natural step boundaries (what can be done independently?)
   - Dependency order (what must come before what?)
   - Test-passing checkpoints (tests must pass after every step)
4. Identify shared context that applies to all steps
5. Create the output directory if needed
6. Write `index.md` with shared context
7. Write `step-NN-name.md` files in dependency order
8. Write `README.md` with the step overview table

## Key Principles

- **Tests must pass after every step.** If a step would break tests, include the fix in the same step or use a compatibility strategy (shims, re-exports, etc.) documented in `index.md`.
- **No step touches git.** The pipeline commits; a step that commits, stages, stashes or resets fails the run. Pending work is committed by the human before the run, not by a step.
- **Every step has a real `test_cmd`.** No placeholders, no `true`. If a step is pure refactor with no behaviour change, point `test_cmd` at the existing suite (or the narrowest relevant subset) so a regression still fails the step. Ask the user before falling back to the full suite for the whole plan.
- **Each step should be independently understandable.** Combined with `index.md`, a step file must give the implementing agent everything it needs. No implicit knowledge from prior steps.
- **Steps should be as small as possible but no smaller.** Group tightly coupled changes that can't be separated without breaking tests. Split everything else.
- **Dependency order matters.** If module A depends on module B, move B first.
- **Name steps descriptively.** `step-03-move-matchers-sources` not `step-03-part3`.
