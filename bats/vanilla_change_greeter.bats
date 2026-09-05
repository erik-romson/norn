#!/usr/bin/env bats

# End-to-end test for the bundled vanilla_change pipeline.
# Uses the pipeline to generate a Greeter class from the spec, same goal as hello.py
# but driven via the vanilla_change pipeline with a positional argument.
#
# Requires ANTHROPIC_API_KEY to be set (calls Claude via claude-agent-sdk).

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
OUTPUT_DIR="$PROJECT_DIR/tmp/hello"

setup_file() {
    cd "$PROJECT_DIR" || fail "Could not cd to project dir"

    if ! command -v uv &> /dev/null; then
        skip "uv is not installed"
    fi

    run uv sync
    assert_success

    # Auth is handled by Claude Code plan when ANTHROPIC_API_KEY is not set.
    # The SDK falls back to the logged-in Claude Code session automatically.
}

setup() {
    cd "$PROJECT_DIR" || fail "Could not cd to project dir"
    rm -rf "$OUTPUT_DIR"
}

teardown() {
    rm -rf "$OUTPUT_DIR"
}

@test "vanilla_change pipeline generates greeter from spec" {
    run uv run python -m norn run vanilla_change -v \
        --skip "test bats" \
        "Create a Python class based on this spec: @examples/spec.txt Place it in tmp/hello/src/greeter.py, then create a pytest test for it in tmp/hello/tests/test_greeter.py. The test should verify the greet() method returns the expected string."
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
