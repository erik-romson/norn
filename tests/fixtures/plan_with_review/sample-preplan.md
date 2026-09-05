# Pre-plan: sample feature

## Goal

Add a `--dry-run` flag to the CLI that prints the pipeline structure
without executing any stages.

## Scope

- Parse `--dry-run` from argv
- Print stage names, loop boundaries, and parallel blocks
- Exit 0 after printing

## Constraints

- No agent calls
- No file writes
- Output goes to stdout
