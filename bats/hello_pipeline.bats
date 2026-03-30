#!/usr/bin/env bats

# End-to-end test for the hello example pipeline.
# Requires ANTHROPIC_API_KEY to be set (calls Claude via claude-agent-sdk).

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
OUTPUT_DIR="$PROJECT_DIR/tmp/hello"

setup_file() {
    cd "$PROJECT_DIR" || fail "Could not cd to project dir"

    # Check uv is available
    if ! command -v uv &> /dev/null; then
        skip "uv is not installed"
    fi

    # Check dependencies are installed
    run uv sync
    assert_success

    # Auth is handled by Claude Code plan when ANTHROPIC_API_KEY is not set.
    # The SDK falls back to the logged-in Claude Code session automatically.
}

setup() {
    cd "$PROJECT_DIR" || fail "Could not cd to project dir"

    # Clean output directory before each test
    rm -rf "$OUTPUT_DIR"
}

teardown() {
    # Clean up generated files after each test
    rm -rf "$OUTPUT_DIR"
}

@test "hello pipeline generates greeter and passes tests" {
    run uv run python -m norn run examples/hello.py -v
    assert_success
    assert_output --partial "all stages passed"
    assert_file_exist "$OUTPUT_DIR/src/greeter.py"
    assert_file_exist "$OUTPUT_DIR/tests/test_greeter.py"

    # Verify generated code independently
    run uv run python -m py_compile "$OUTPUT_DIR/src/greeter.py"
    assert_success

    run uv run python -m pytest "$OUTPUT_DIR/tests/test_greeter.py" -v
    assert_success
}


@test "norn run with missing config fails" {
    run uv run python -m norn run nonexistent.py
    assert_failure
    assert_output --partial "not found"
}
