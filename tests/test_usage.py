from norn.models import UsageRecord, UsageTracker


def test_usage_record_total_tokens():
    r = UsageRecord(stage_name="gen", input_tokens=100, output_tokens=50)
    assert r.total_tokens == 150


def test_usage_record_defaults():
    r = UsageRecord(stage_name="gen")
    assert r.total_tokens == 0
    assert r.total_cost_usd == 0.0
    assert r.session_id is None
    assert r.attempt == 1


def test_usage_tracker_empty():
    t = UsageTracker()
    assert t.total_tokens == 0
    assert t.total_cost_usd == 0.0
    assert t.unique_sessions == 0
    assert t.total_turns == 0


def test_usage_tracker_aggregation():
    t = UsageTracker()
    t.add(UsageRecord(
        stage_name="gen",
        session_id="s1",
        input_tokens=1000,
        output_tokens=200,
        cache_read_input_tokens=500,
        cache_creation_input_tokens=100,
        total_cost_usd=0.01,
        duration_ms=3000,
        duration_api_ms=2000,
        num_turns=2,
    ))
    t.add(UsageRecord(
        stage_name="gen",
        session_id="s1",
        input_tokens=800,
        output_tokens=150,
        cache_read_input_tokens=600,
        cache_creation_input_tokens=0,
        total_cost_usd=0.008,
        duration_ms=2500,
        duration_api_ms=1500,
        num_turns=1,
        attempt=2,
    ))

    assert t.total_input_tokens == 1800
    assert t.total_output_tokens == 350
    assert t.total_tokens == 2150
    assert t.total_cache_read_tokens == 1100
    assert t.total_cache_creation_tokens == 100
    assert t.total_cost_usd == pytest.approx(0.018)
    assert t.total_duration_ms == 5500
    assert t.total_api_duration_ms == 3500
    assert t.unique_sessions == 1
    assert t.total_turns == 3


def test_usage_tracker_multiple_sessions():
    t = UsageTracker()
    t.add(UsageRecord(stage_name="a", session_id="s1", input_tokens=100))
    t.add(UsageRecord(stage_name="b", session_id="s2", input_tokens=200))
    t.add(UsageRecord(stage_name="c", session_id=None, input_tokens=50))

    assert t.unique_sessions == 2
    assert t.total_input_tokens == 350


def test_usage_tracker_totals_are_cumulative_across_stages():
    """UsageUpdated is emitted with tracker.total_* — the cumulative run sum,
    not the per-stage slice.  Pin this so the viewmodel's 'treat UsageUpdated
    as authoritative run total' contract cannot be silently invalidated by a
    change to the emitter.

    After two stages (100 + 200 input tokens), tracker.total_input_tokens must
    equal 300, not 200.  The viewmodel stores the last UsageUpdated value as the
    run total; if the emitter ever changed to per-stage values the display would
    only show the most recent stage.
    """
    t = UsageTracker()
    t.add(UsageRecord(stage_name="stage-a", input_tokens=100, output_tokens=20, total_cost_usd=0.01))
    # After stage-a the tracker carries cumulative = 100 input
    assert t.total_input_tokens == 100

    t.add(UsageRecord(stage_name="stage-b", input_tokens=200, output_tokens=40, total_cost_usd=0.02))
    # After stage-b the tracker carries cumulative = 300 input (NOT just 200)
    assert t.total_input_tokens == 300
    assert t.total_output_tokens == 60
    import pytest
    assert pytest.approx(t.total_cost_usd, rel=1e-6) == 0.03


import pytest
