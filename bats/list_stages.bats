#!/usr/bin/env bats

# CLI test for list-stages and orgs commands.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"

setup_file() {
    cd "$PROJECT_DIR" || fail "Could not cd to project dir"

    # Check uv is available
    if ! command -v uv &> /dev/null; then
        skip "uv is not installed"
    fi

    # Check dependencies are installed
    run uv sync
    assert_success
}

setup() {
    cd "$PROJECT_DIR" || fail "Could not cd to project dir"
}

@test "list-stages shows built-in stages" {
    run uv run python -m norn list-stages
    assert_success
    assert_output --partial "read_file"
    assert_output --partial "run_command"
    assert_output --partial "generate"
    assert_output --partial "validate"
}

@test "list-stages shows module paths" {
    run uv run python -m norn list-stages
    assert_success
    assert_output --partial "norn.stages.read_file.ReadFile"
    assert_output --partial "norn.stages.run_command.RunCommand"
}

@test "orgs with no config dir shows message" {
    NORN_CONFIG_DIR="$PROJECT_DIR/tmp/nonexistent_orgs_dir" \
        run uv run python -m norn orgs
    assert_success
    assert_output --partial "No org configs found"
}

@test "norn --help shows available commands" {
    run uv run python -m norn --help
    assert_success
    assert_output --partial "list-stages"
    assert_output --partial "orgs"
}
