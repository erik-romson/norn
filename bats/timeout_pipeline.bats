#!/usr/bin/env bats

# Tests that stage timeouts are enforced correctly.
# Does NOT require ANTHROPIC_API_KEY — uses only RunCommand stages.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
OUT_DIR="$PROJECT_DIR/target/batstest/timeout"
PIPELINE="$PROJECT_DIR/bats/testfiles/timeout_pipeline/pipeline.py"
SLOW_PIPELINE="$PROJECT_DIR/bats/testfiles/timeout_pipeline/timeout_pipeline_slow.py"

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
    rm -rf "$OUT_DIR"
}

teardown() {
    rm -rf "$OUT_DIR"
}

@test "stage with generous timeout completes successfully" {
    run uv run python -m norn run "$PIPELINE"
    assert_success
    assert_file_exist "$OUT_DIR/result.txt"
}

@test "stage that exceeds its timeout fails the pipeline" {
    run uv run python -m norn run "$SLOW_PIPELINE"
    assert_failure
    assert_output --partial "Timed out"
}
