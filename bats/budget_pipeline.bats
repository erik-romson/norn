#!/usr/bin/env bats

# Tests that a budget-configured pipeline runs correctly when usage stays under the limit.
# Does NOT require ANTHROPIC_API_KEY — uses only RunCommand stages (zero cost/tokens).

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
OUT_DIR="$PROJECT_DIR/target/batstest/budget"
PIPELINE="$PROJECT_DIR/bats/testfiles/budget_pipeline/pipeline.py"

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

@test "budget pipeline succeeds when usage is under limit" {
    run uv run python -m norn run "$PIPELINE"
    assert_success
}

@test "budget pipeline produces expected output" {
    run uv run python -m norn run "$PIPELINE"
    assert_success
    assert_file_exist "$OUT_DIR/result.txt"
}
