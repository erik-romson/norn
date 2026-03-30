#!/usr/bin/env bats

# Tests that parallel stages run and produce results accessible to downstream stages.
# Does NOT require ANTHROPIC_API_KEY — uses only RunCommand stages.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
OUT_DIR="$PROJECT_DIR/target/batstest/parallel"
PIPELINE="$PROJECT_DIR/bats/testfiles/parallel/pipeline.py"

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

@test "parallel pipeline succeeds" {
    run uv run python -m norn run "$PIPELINE"
    assert_success
}

@test "all parallel stage output files are created" {
    run uv run python -m norn run "$PIPELINE"
    assert_success

    assert_file_exist "$OUT_DIR/a.txt"
    assert_file_exist "$OUT_DIR/b.txt"
}

@test "downstream stage after parallel block runs" {
    run uv run python -m norn run "$PIPELINE"
    assert_success

    assert_file_exist "$OUT_DIR/after.txt"
}
