#!/usr/bin/env bats

# Tests for pipeline checkpoint save and resume behavior.
# Stage 1 always succeeds; stage 2 fails unless a trigger file is present.
# No ANTHROPIC_API_KEY required — uses only RunCommand stages.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
PIPELINE="$PROJECT_DIR/bats/testfiles/checkpoint_pipeline/pipeline.py"
CHECKPOINT_FILE="${PIPELINE%.py}.checkpoint"
TRIGGER_FILE="$PROJECT_DIR/target/batstest/checkpoint/trigger.txt"

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
    rm -f "$CHECKPOINT_FILE"
    rm -rf "$PROJECT_DIR/target/batstest/checkpoint"
}

teardown() {
    rm -f "$CHECKPOINT_FILE"
    rm -rf "$PROJECT_DIR/target/batstest/checkpoint"
}

@test "checkpoint is created after first successful stage even when pipeline fails" {
    # step2 fails (trigger file absent) — pipeline exits non-zero
    run uv run python -m norn run "$PIPELINE"
    assert_failure

    assert_file_exist "$CHECKPOINT_FILE"

    # step1 must be in completed_stages; step2 must not
    run uv run python -c "
import json, sys
data = json.load(open('$CHECKPOINT_FILE'))
assert 'step1' in data['completed_stages'], f\"step1 missing: {data['completed_stages']}\"
assert 'step2' not in data['completed_stages'], f\"step2 should not be cached: {data['completed_stages']}\"
print('OK')
"
    assert_success
    assert_output "OK"
}

@test "resume skips cached stages and re-runs the failed stage" {
    # Run 1: step1 succeeds, step2 fails
    run uv run python -m norn run "$PIPELINE"
    assert_failure
    assert_file_exist "$CHECKPOINT_FILE"

    # Create trigger so step2 succeeds on resume
    mkdir -p "$PROJECT_DIR/target/batstest/checkpoint"
    echo "trigger" > "$TRIGGER_FILE"

    # Run 2: step1 cached, step2 runs and passes
    run uv run python -m norn run "$PIPELINE" --resume
    assert_success
    assert_output --partial "(cached)"
}

@test "--resume with no checkpoint starts fresh" {
    # No checkpoint file exists
    run uv run python -m norn run "$PIPELINE" --resume
    # step2 still fails (no trigger) but the warning must appear
    assert_failure
    assert_output --partial "No saved checkpoint found"
}
