---
title: Keep skill bodies short and move detail into references
date: 2026-01-12
last_updated: 2026-02-01
category: skill-design
module: skills
problem_type: design_pattern
component: documentation
severity: low
applies_when:
  - "A skill body grows past a screenful"
tags:
  - skills
  - progressive-disclosure
---

# Keep skill bodies short and move detail into references

## Context

Long skill bodies are read in full on every invocation.

## Guidance

Put detail in reference files the skill loads on demand.

## Why This Matters

Context is the scarce resource.

## When to Apply

When a skill exceeds a screenful.

## Examples

See `README.md` for the shape of a short entry point.
