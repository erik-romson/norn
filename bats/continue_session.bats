#!/usr/bin/env bats

# Tests for --continue flag (session-only resume, no stage skipping).
# Does NOT require ANTHROPIC_API_KEY — uses only RunCommand stages.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
PIPELINE="$BATS_TEST_DIRNAME/testfiles/continue_session/pipeline.py"
CHECKPOINT_FILE="${PIPELINE%.py}.checkpoint"
COUNTER_FILE="$PROJECT_DIR/tmp/continue_test/counter.txt"

setup_file() {
    cd "$PROJECT_DIR" || fail "Could not cd to project dir"

    if ! command -v uv &> /dev/null; then
        skip "uv is not installed"
    fi

    run uv sync
    assert_success
}

setup() {
    cd "$PROJECT_DIR" || fail "Could not cd to project dir"
    rm -rf "$PROJECT_DIR/tmp/continue_test"
    rm -f "$CHECKPOINT_FILE"
}

teardown() {
    rm -rf "$PROJECT_DIR/tmp/continue_test"
    rm -f "$CHECKPOINT_FILE"
}

@test "--continue re-runs all stages (counter increments twice)" {
    # Run 1: counter goes to 1, checkpoint created
    run uv run python -m norn run "$PIPELINE"
    assert_success
    assert_file_exist "$CHECKPOINT_FILE"
    run cat "$COUNTER_FILE"
    assert_output --partial "1"

    # Run 2 with --continue: stage re-runs (not skipped), counter goes to 2
    # Note: RunCommand stages have no session_id, so --continue starts fresh
    # but still re-runs all stages (unlike --resume which skips cached stages)
    run uv run python -m norn run "$PIPELINE" --continue
    assert_success

    run cat "$COUNTER_FILE"
    assert_output --partial "2"
}

@test "--resume skips completed stages (counter stays at 1)" {
    # Run 1: counter goes to 1
    run uv run python -m norn run "$PIPELINE"
    assert_success
    run cat "$COUNTER_FILE"
    assert_output --partial "1"

    # Run 2 with --resume: stage is cached (skipped), counter stays at 1
    run uv run python -m norn run "$PIPELINE" --resume
    assert_success
    assert_output --partial "Resuming from checkpoint"
    assert_output --partial "cached"

    run cat "$COUNTER_FILE"
    assert_output --partial "1"
}

@test "--continue with no prior session starts fresh" {
    run uv run python -m norn run "$PIPELINE" --continue
    assert_success
    assert_output --partial "starting fresh"
}
