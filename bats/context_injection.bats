#!/usr/bin/env bats

# Tests for pipeline context injection (.context() and .context_cmd()).
# Does NOT require ANTHROPIC_API_KEY — uses only RunCommand stages.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
PIPELINE="$PROJECT_DIR/bats/testfiles/context_injection/pipeline.py"
MISSING_PIPELINE="$PROJECT_DIR/bats/testfiles/context_injection/missing_context_pipeline.py"

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
}

@test "pipeline with context file succeeds" {
    run uv run python -m norn run "$PIPELINE"
    assert_success
}

@test "pipeline with missing context file fails fast" {
    run uv run python -m norn run "$MISSING_PIPELINE"
    assert_failure
    assert_output --partial "nonexistent_file_that_does_not_exist.txt"
}
