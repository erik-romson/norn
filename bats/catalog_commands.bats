#!/usr/bin/env bats

# CLI tests for list, describe, and running bundled pipelines by name.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"

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

# --- norn list ---

@test "list shows bundled pipelines" {
    run uv run python -m norn list
    assert_success
    assert_output --partial "hello"
    assert_output --partial "vanilla_change"
    assert_output --partial "implement_features"
}

# --- norn describe ---

@test "describe hello shows pipeline details" {
    run uv run python -m norn describe hello
    assert_success
    assert_output --partial "hello"
    assert_output --partial "Usage"
}

@test "describe vanilla_change shows env vars" {
    run uv run python -m norn describe vanilla_change
    assert_success
    assert_output --partial "ANTHROPIC_API_KEY"
}

@test "describe nonexistent pipeline exits with error" {
    run uv run python -m norn describe nonexistent_pipeline
    assert_failure
    assert_output --partial "unknown pipeline"
}

# --- norn run <bundled-name> ---

@test "run bundled pipeline by name with --dry-run" {
    run uv run python -m norn run hello --dry-run
    assert_success
}

# --- help includes new commands ---

@test "norn --help shows list and describe commands" {
    run uv run python -m norn --help
    assert_success
    assert_output --partial "list"
    assert_output --partial "describe"
}
