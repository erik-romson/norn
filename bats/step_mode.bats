#!/usr/bin/env bats

# Tests for interactive stepping mode (--step flag).
# Does NOT require ANTHROPIC_API_KEY — uses only RunCommand stages.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
PIPELINE="$PROJECT_DIR/bats/testfiles/step/pipeline.py"

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

@test "--step with 'r' runs all stages successfully" {
    run bash -c "printf 'r\nr\n' | uv run python -m norn run --step '$PIPELINE'"
    assert_success
}

@test "--step with 's' skips a stage and continues" {
    run bash -c "printf 's\nr\n' | uv run python -m norn run --step '$PIPELINE'"
    assert_success
}

@test "--step with 'a' aborts the pipeline" {
    run bash -c 'printf "a\n" | uv run python -m norn run --step "$PIPELINE"'
    assert_failure
}
