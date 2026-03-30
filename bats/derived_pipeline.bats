#!/usr/bin/env bats

# Tests for config composition via Pipeline.derive().
# Uses --dry-run so no API key or execution is needed.

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

@test "derived pipeline dry-run succeeds" {
    run uv run python -m norn run examples/derived.py --dry-run
    assert_success
}

@test "derived pipeline has new name" {
    run uv run python -m norn run examples/derived.py --dry-run
    assert_success
    assert_output --partial "hello-fast"
}

@test "derived pipeline loop retains non-skipped stages" {
    run uv run python -m norn run examples/derived.py --dry-run
    assert_success
    assert_output --partial "generate"
    assert_output --partial "check"
    assert_output --partial "typecheck"
}

@test "derived pipeline loop has exactly four stages" {
    run uv run python -m norn run examples/derived.py --dry-run
    assert_success
    # generate, generate_test, check, typecheck — but not the original 'test' stage
    assert_output --regexp "4\. typecheck"
    refute_output --regexp "[0-9]+\. test "
}
