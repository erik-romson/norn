---
title: Structured-output schemas must be portable across model CLIs
date: 2026-01-10
last_updated: 2026-02-01
category: integration-issues
module: agents
problem_type: integration_issue
component: tooling
severity: medium
symptoms:
  - "One model CLI rejected the schema that another accepted"
  - "Reply parsing failed only for the second provider"
root_cause: config_error
resolution_type: code_fix
tags:
  - structured-output
  - schema
  - providers
---

# Structured-output schemas must be portable across model CLIs

## Problem

A JSON schema written for one model CLI was rejected by another.

## Symptoms

- The second provider returned a schema error.

## What Didn't Work

- Loosening the schema only for one provider.

## Solution

Restrict the schema to the subset every provider accepts, as in `norn/real.py:2`.

## Why This Works

The shared subset has no provider-specific keywords.

## Prevention

Validate the schema against every provider in one test.
