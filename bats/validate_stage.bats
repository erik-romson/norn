#!/usr/bin/env bats

# Tests for the Validate stage (FileExists, Contains checks).
# Does NOT require ANTHROPIC_API_KEY — Validate is a pure Python stage.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
PASS_PIPELINE="$PROJECT_DIR/bats/testfiles/validate/pass_pipeline.py"
FAIL_PIPELINE="$PROJECT_DIR/bats/testfiles/validate/fail_pipeline.py"

setup_file() {
    cd "$PROJECT_DIR" || fail "Could not cd to project dir"

    if ! command -v uv &> /dev/null; then
        skip "uv is not installed"
    fi

    run uv sync
    assert_success
}

@test "pipeline with all checks passing exits 0" {
    cd "$PROJECT_DIR" || fail "Could not cd to project dir"
    run uv run python -m norn run "$PASS_PIPELINE"
    assert_success
}

@test "pipeline with failing checks exits non-zero" {
    cd "$PROJECT_DIR" || fail "Could not cd to project dir"
    run uv run python -m norn run "$FAIL_PIPELINE"
    assert_failure
}
