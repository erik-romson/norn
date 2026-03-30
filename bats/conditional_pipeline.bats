#!/usr/bin/env bats

# Tests that conditional stages run or are skipped based on their when predicate.
# Does NOT require ANTHROPIC_API_KEY — uses only RunCommand stages.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
OUT_DIR="$PROJECT_DIR/target/batstest/conditional"
PIPELINE="$PROJECT_DIR/bats/testfiles/conditional/pipeline.py"

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

@test "conditional pipeline succeeds" {
    run uv run python -m norn run "$PIPELINE"
    assert_success
}

@test "stage with met condition runs" {
    run uv run python -m norn run "$PIPELINE"
    assert_success

    assert_file_exist "$OUT_DIR/ran.txt"
}

@test "stage with unmet condition is skipped" {
    run uv run python -m norn run "$PIPELINE"
    assert_success

    assert_file_not_exist "$OUT_DIR/skipped.txt"
}
