# Phase 2 — Agent Sessions & Usage Tracking

Extends the Phase 1 pipeline framework with persistent agent sessions within loops and comprehensive token/cost tracking across all stages and sub-agents.

## Goals

1. **Agent sessions per loop** — stages inside a loop share a Claude session so the agent remembers prior errors when retrying.
2. **Usage tracking** — every `Generate` call captures tokens, cost, and timing. The pipeline aggregates these into a final report.
3. **Context clearing** — `clear_context()` terminates the current session and starts a fresh one.
4. **End-of-pipeline report** — print a summary of total tokens, cost, duration, and per-stage breakdown.

## What the SDK Gives Us

The `claude-agent-sdk` `query()` yields a stream of typed messages. The key ones for tracking:

| Message type | Fields we use |
|---|---|
| `AssistantMessage` | `message.usage.input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` |
| `ResultMessage` | `total_cost_usd`, `usage` (dict with token counts), `duration_ms`, `duration_api_ms`, `num_turns`, `session_id`, `is_error` |

`ResultMessage` is emitted once per `query()` call and contains **aggregated** totals for that call — this is the primary data source.

## Data Model

### `UsageRecord`

One record per `Generate` call (one `query()` invocation).

```python
@dataclass
class UsageRecord:
    stage_name: str
    session_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    total_cost_usd: float = 0.0
    duration_ms: int = 0
    duration_api_ms: int = 0
    num_turns: int = 0
    is_error: bool = False
    attempt: int = 1            # which loop attempt (1 for non-loop stages)
```

### `UsageTracker`

Accumulates `UsageRecord`s across the entire pipeline run.

```python
@dataclass
class UsageTracker:
    records: list[UsageRecord] = field(default_factory=list)

    @property
    def total_input_tokens(self) -> int: ...

    @property
    def total_output_tokens(self) -> int: ...

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_cost_usd(self) -> float: ...

    @property
    def total_duration_ms(self) -> int: ...

    @property
    def total_api_duration_ms(self) -> int: ...

    def add(self, record: UsageRecord) -> None:
        self.records.append(record)

    def summary(self) -> str:
        """Human-readable summary for end-of-pipeline report."""
        ...

    def per_stage_breakdown(self) -> dict[str, dict]: ...
```

### Extend `StageResult`

```python
@dataclass
class StageResult:
    name: str
    success: bool
    output: Any = None
    error: str | None = None
    usage: UsageRecord | None = None    # NEW — populated by Generate
```

### Extend `PipelineContext`

```python
@dataclass
class PipelineContext:
    results: dict[str, StageResult] = field(default_factory=dict)
    usage_tracker: UsageTracker = field(default_factory=UsageTracker)  # NEW
```

## Agent Session Management

### Session Lifecycle

```
Pipeline start
│
├─ stage("read_spec", ReadFile(...))          ← no session needed
│
├─ clear_context()                            ← destroy current session (if any)
│
├─ loop("generate_and_build", max_retries=3)
│   │
│   │  ── [session created on first Generate in loop] ──
│   │
│   ├─ attempt 1
│   │   ├─ stage("generate", Generate(...))   ← uses session A
│   │   ├─ stage("check", RunCommand(...))    ← no session needed
│   │   └─ stage("test", RunCommand(...))     ← FAIL → retry
│   │
│   ├─ attempt 2
│   │   ├─ stage("generate", Generate(...))   ← reuses session A (sees prior error)
│   │   ├─ stage("check", RunCommand(...))
│   │   └─ stage("test", RunCommand(...))     ← PASS
│   │
│   │  ── [session A ends when loop completes or pipeline moves on] ──
│
├─ clear_context()                            ← explicit session teardown
│
└─ Pipeline done → print usage report
```

### Implementation

In Phase 1, `Generate` calls `query()` directly — each call is stateless. In Phase 2 we introduce an optional session that `Generate` uses when available.

```python
# runner.py changes

async def _run_loop(loop: Loop, ctx: PipelineContext) -> None:
    session = None  # lazy — created on first Generate

    for attempt in range(1, loop.max_retries + 1):
        for stage in loop.stages:
            if isinstance(stage.impl, Generate):
                result = await stage.impl.run(ctx, session=session, attempt=attempt)
                if session is None and result.usage:
                    session = result.usage.session_id
            else:
                result = await stage.impl.run(ctx)
            ...

    # Cleanup session when loop ends
    session = None
```

The `Generate.run()` signature gains optional kwargs:

```python
async def run(
    self,
    ctx: PipelineContext,
    *,
    session: str | None = None,
    attempt: int = 1,
) -> StageResult:
```

When `session` is provided, `query()` is called with `session_id=session` to continue the existing conversation. When `None`, a new session starts and the returned `session_id` from `ResultMessage` is captured for subsequent calls.

### `clear_context()` Implementation

```python
# In run_pipeline():
if isinstance(item, ClearContext):
    # Structured results survive — only the session is shed
    current_session = None
    log.info("[clear_context] Session discarded")
    continue
```

## Capturing Usage in `Generate`

```python
async def run(self, ctx: PipelineContext, *, session=None, attempt=1) -> StageResult:
    from claude_agent_sdk import query, ResultMessage, AssistantMessage

    resolved_prompt = self._resolve_prompt(ctx)
    chunks: list[str] = []
    usage_record = UsageRecord(stage_name="", attempt=attempt)

    async for message in query(prompt=resolved_prompt, session_id=session):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "text"):
                    chunks.append(block.text)

        elif isinstance(message, ResultMessage):
            usage_record.session_id = message.session_id
            usage_record.total_cost_usd = message.total_cost_usd or 0.0
            usage_record.duration_ms = message.duration_ms
            usage_record.duration_api_ms = message.duration_api_ms
            usage_record.num_turns = message.num_turns
            usage_record.is_error = message.is_error
            if message.usage:
                usage_record.input_tokens = message.usage.get("input_tokens", 0)
                usage_record.output_tokens = message.usage.get("output_tokens", 0)
                usage_record.cache_read_input_tokens = message.usage.get("cache_read_input_tokens", 0)
                usage_record.cache_creation_input_tokens = message.usage.get("cache_creation_input_tokens", 0)

    # ... write file, build StageResult ...
    result = StageResult(name="", success=True, output=code, usage=usage_record)
    ctx.usage_tracker.add(usage_record)
    return result
```

## End-of-Pipeline Report

After `run_pipeline()` completes, the runner prints (or returns) a usage summary:

```
═══════════════════════════════════════════════════════
 Pipeline "hello" — Usage Report
═══════════════════════════════════════════════════════

 Stage Breakdown
 ───────────────────────────────────────────────────────
 generate (attempt 1)     12,450 in  │  3,200 out  │ $0.0234  │  4.2s
 generate (attempt 2)      8,100 in  │  2,800 out  │ $0.0187  │  3.1s
 ───────────────────────────────────────────────────────

 Totals
 ───────────────────────────────────────────────────────
 Input tokens:            20,550
   Cache read:             8,100  (39.4%)
   Cache creation:         2,200
 Output tokens:            6,000
 Total tokens:            26,550
 Total cost:              $0.0421
 API time:                 7.3s
 Wall time:               12.8s
 Sessions:                 1
 Turns:                    4
 ───────────────────────────────────────────────────────
```

### Implementation

```python
# runner.py

async def run_pipeline(pipeline: Pipeline) -> PipelineContext:
    ...
    log.info("Pipeline '%s' completed successfully", pipeline.name)
    _print_usage_report(pipeline.name, ctx.usage_tracker)
    return ctx


def _print_usage_report(name: str, tracker: UsageTracker) -> None:
    if not tracker.records:
        return

    print(f"\n{'═' * 55}")
    print(f" Pipeline \"{name}\" — Usage Report")
    print(f"{'═' * 55}\n")

    print(" Stage Breakdown")
    print(f" {'─' * 53}")
    for rec in tracker.records:
        label = f" {rec.stage_name} (attempt {rec.attempt})"
        print(f"{label:<30} {rec.input_tokens:>8,} in │ {rec.output_tokens:>6,} out │ "
              f"${rec.total_cost_usd:.4f} │ {rec.duration_api_ms / 1000:.1f}s")
    print(f" {'─' * 53}\n")

    cache_pct = (
        tracker.total_cache_read_tokens / tracker.total_input_tokens * 100
        if tracker.total_input_tokens > 0 else 0
    )
    unique_sessions = len({r.session_id for r in tracker.records if r.session_id})
    total_turns = sum(r.num_turns for r in tracker.records)

    print(" Totals")
    print(f" {'─' * 53}")
    print(f" Input tokens:       {tracker.total_input_tokens:>10,}")
    print(f"   Cache read:       {tracker.total_cache_read_tokens:>10,}  ({cache_pct:.1f}%)")
    print(f"   Cache creation:   {tracker.total_cache_creation_tokens:>10,}")
    print(f" Output tokens:      {tracker.total_output_tokens:>10,}")
    print(f" Total tokens:       {tracker.total_tokens:>10,}")
    print(f" Total cost:           ${tracker.total_cost_usd:>9.4f}")
    print(f" API time:           {tracker.total_api_duration_ms / 1000:>10.1f}s")
    print(f" Wall time:          {tracker.total_duration_ms / 1000:>10.1f}s")
    print(f" Sessions:           {unique_sessions:>10}")
    print(f" Turns:              {total_turns:>10}")
    print(f" {'─' * 53}")
```

## Sub-Agent Tracking

When `Generate` calls `query()` and Claude spawns sub-agents (tool calls that themselves use Claude), the SDK's `ResultMessage.usage` already includes all token consumption from the entire agent tree — sub-agent tokens are rolled up into the parent's totals. No extra work needed to capture sub-agent costs.

If we later need per-sub-agent granularity, we can:
1. Listen for `TaskProgressMessage` / `TaskNotificationMessage` events in the stream — these carry `TaskUsage` with `total_tokens`, `tool_uses`, and `duration_ms` for individual sub-tasks.
2. Store these as child records under the parent `UsageRecord`.

This is deferred unless we find the aggregated totals insufficient.

## Programmatic Access

The `UsageTracker` is available on the returned `PipelineContext`, so callers can inspect usage programmatically:

```python
ctx = await run_pipeline(pipeline)

print(ctx.usage_tracker.total_cost_usd)
print(ctx.usage_tracker.total_tokens)

for record in ctx.usage_tracker.records:
    print(f"{record.stage_name}: {record.input_tokens + record.output_tokens} tokens")
```

## File Changes

| File | Change |
|---|---|
| `norn/models.py` | Add `UsageRecord`, `UsageTracker`. Add `usage` field to `StageResult`. Add `usage_tracker` to `PipelineContext`. |
| `norn/stages/generate.py` | Capture `ResultMessage` usage data. Accept optional `session`/`attempt` kwargs. Register usage with `ctx.usage_tracker`. |
| `norn/stages/base.py` | Update `run()` signature to accept optional `**kwargs` for session passthrough. |
| `norn/runner.py` | Manage session lifecycle in loops. Pass session to `Generate`. Call `_print_usage_report()` on completion. |
| `norn/dsl.py` | No changes needed. |
| `tests/test_usage.py` | Unit tests for `UsageRecord`, `UsageTracker` aggregation, report formatting. |
| `tests/test_runner_sessions.py` | Tests for session reuse within loops, session teardown on `clear_context()`. |

## Implementation Order

1. `UsageRecord` and `UsageTracker` in `models.py` — pure data, easy to test
2. Extend `StageResult` and `PipelineContext` with usage fields
3. Update `Generate.run()` to capture `ResultMessage` and populate `UsageRecord`
4. Add session passthrough (`session_id` kwarg) to `Generate.run()`
5. Update `runner.py` loop logic to manage sessions and pass them to `Generate`
6. Implement `clear_context()` session teardown in runner
7. Add `_print_usage_report()` to runner
8. Tests

## What We Defer to Phase 3

- Per-sub-agent token breakdown (listen for `TaskProgressMessage`)
- Usage persistence (writing usage data to a file/database)
- Usage budgets / cost limits (abort pipeline if cost exceeds threshold)
- Token-aware context clearing (auto-clear when approaching context window limit)
