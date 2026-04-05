---
name: split-plan
description: "Split a plan/spec file into step files for the implement_features pipeline. Creates an index.md with shared context and numbered step-*.md files. Use when you have a plan and want to break it into executable steps. Triggers on: split plan, divide plan, break into steps, create steps from plan."
argument-hint: "[plan-file] [output-directory]"
disable-model-invocation: true
---

# Split Plan into Pipeline Steps

Take a plan file and split it into `index.md` + `step-NN-name.md` files that can be executed by `bin/norn dogfooding/implement_features.py <output-directory>`.

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

Each step file must contain:

1. **What** — What to do in this step (concrete, not vague)
2. **Actions** — Specific file operations, commands, or changes
3. **Test command** — How to verify this step worked

### `README.md` — Optional overview for humans

A table showing the step order, what each does, and why tests pass between steps. This file is NOT picked up by the pipeline.

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
- **If it is not natural for a step to have a test.** Create some kind of placeholder as the all tests will be tested for each step 
- **Each step should be independently understandable.** Combined with `index.md`, a step file must give the implementing agent everything it needs. No implicit knowledge from prior steps.
- **Steps should be as small as possible but no smaller.** Group tightly coupled changes that can't be separated without breaking tests. Split everything else.
- **Dependency order matters.** If module A depends on module B, move B first.
- **Name steps descriptively.** `step-03-move-matchers-sources` not `step-03-part3`.
