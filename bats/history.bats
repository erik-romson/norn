#!/usr/bin/env bats
# Tests for the `norn history` subcommand.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

setup() {
    TEST_CONFIG="$BATS_TMPDIR/history_test_pipeline.py"
    cat > "$TEST_CONFIG" << 'PYEOF'
from norn.dsl import Pipeline, Stage
from norn.stages.run_command import RunCommand

config = (
    Pipeline("history_test")
    .stage("greet", RunCommand(cmd="echo hello"))
)
PYEOF
    HISTORY_FILE="${TEST_CONFIG%.py}.history"
    CHECKPOINT_FILE="${TEST_CONFIG%.py}.checkpoint"
    rm -f "$HISTORY_FILE" "$CHECKPOINT_FILE"
}

teardown() {
    rm -f "$TEST_CONFIG" "$HISTORY_FILE" "$CHECKPOINT_FILE"
}

@test "history: no history file shows no-history message with exit 0" {
    run uv run python -m norn history "$TEST_CONFIG"
    assert_success
}

@test "history: running a pipeline creates a .history file" {
    run uv run python -m norn run "$TEST_CONFIG"
    assert_success
    assert_file_exist "$HISTORY_FILE"
}

@test "history: history file contains valid JSON after a run" {
    uv run python -m norn run "$TEST_CONFIG"
    run python3 -c "import json; [json.loads(l) for l in open('$HISTORY_FILE') if l.strip()]"
    assert_success
}

@test "history: shows history table after a run without error" {
    uv run python -m norn run "$TEST_CONFIG"
    run uv run python -m norn history "$TEST_CONFIG"
    assert_success
}

@test "history: --compare works after two runs" {
    uv run python -m norn run "$TEST_CONFIG"
    uv run python -m norn run "$TEST_CONFIG"
    run uv run python -m norn history "$TEST_CONFIG" --compare 1 2
    assert_success
}

@test "history: --compare with missing run ID exits successfully with error message" {
    uv run python -m norn run "$TEST_CONFIG"
    run uv run python -m norn history "$TEST_CONFIG" --compare 1 99
    assert_success
}
