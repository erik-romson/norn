#!/usr/bin/env bats

# Tests that pipeline-level hooks (pre_stage, post_stage, on_failure, on_retry)
# execute at the correct lifecycle points.
# Does NOT require ANTHROPIC_API_KEY — uses only RunCommand stages.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
EVENTS_FILE="$PROJECT_DIR/target/batstest/hooks/events.txt"
PIPELINE="$PROJECT_DIR/bats/testfiles/hooks/pipeline.py"

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
    rm -rf "$PROJECT_DIR/target/batstest/hooks"
}

teardown() {
    rm -rf "$PROJECT_DIR/target/batstest/hooks"
}

@test "pre_stage and post_stage hooks fire around each stage" {
    run uv run python -m norn run "$PIPELINE"
    assert_success

    assert_file_exist "$EVENTS_FILE"

    # Two stages → pre fires twice, post fires twice
    run grep -c "^pre$" "$EVENTS_FILE"
    assert_output "2"

    run grep -c "^post$" "$EVENTS_FILE"
    assert_output "2"
}

@test "hooks fire in correct order: pre before stage, post after" {
    run uv run python -m norn run "$PIPELINE"
    assert_success

    assert_file_exist "$EVENTS_FILE"

    # Events must appear in order: pre, post, pre, post (no on_failure since all succeed)
    run uv run python -c "
events = open('$EVENTS_FILE').read().splitlines()
assert events == ['pre', 'post', 'pre', 'post'], f'unexpected order: {events}'
print('OK')
"
    assert_success
    assert_output "OK"
}
