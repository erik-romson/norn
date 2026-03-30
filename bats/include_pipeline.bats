#!/usr/bin/env bats

# Tests that pipeline composition via .include() works for both inline and isolated modes.
# Does NOT require ANTHROPIC_API_KEY — uses only RunCommand stages.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
OUT_DIR="$PROJECT_DIR/target/batstest/include"
INLINE_PIPELINE="$PROJECT_DIR/bats/testfiles/include/inline_pipeline.py"
ISOLATED_PIPELINE="$PROJECT_DIR/bats/testfiles/include/isolated_pipeline.py"

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

@test "inline include: sub-pipeline stage runs" {
    run uv run python -m norn run "$INLINE_PIPELINE"
    assert_success
    assert_file_exist "$OUT_DIR/sub.txt"
}

@test "inline include: parent stage after include runs" {
    run uv run python -m norn run "$INLINE_PIPELINE"
    assert_success
    assert_file_exist "$OUT_DIR/after.txt"
}

@test "isolated include: sub-pipeline stage runs" {
    run uv run python -m norn run "$ISOLATED_PIPELINE"
    assert_success
    assert_file_exist "$OUT_DIR/sub.txt"
}

@test "isolated include: parent stage after include runs" {
    run uv run python -m norn run "$ISOLATED_PIPELINE"
    assert_success
    assert_file_exist "$OUT_DIR/after.txt"
}
