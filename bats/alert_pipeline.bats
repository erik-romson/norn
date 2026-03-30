#!/usr/bin/env bats

# Tests that FileChannel alerts are written to disk when a pipeline runs.
# Does NOT require ANTHROPIC_API_KEY — uses only RunCommand stages.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
ALERT_FILE="$PROJECT_DIR/tmp/alert_demo/alerts.jsonl"

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
    rm -f "$ALERT_FILE"
}

teardown() {
    rm -rf "$PROJECT_DIR/tmp/alert_demo"
}

@test "alert_demo pipeline writes a COMPLETE alert to file" {
    run uv run python -m norn run examples/alert_demo.py
    assert_success

    assert_file_exist "$ALERT_FILE"

    run grep "COMPLETE" "$ALERT_FILE"
    assert_success
}

@test "alert_demo pipeline alert file contains valid JSON" {
    run uv run python -m norn run examples/alert_demo.py
    assert_success

    assert_file_exist "$ALERT_FILE"

    # Each line must be parseable JSON with an "event" key
    run uv run python -c "
import json, sys
lines = open('$ALERT_FILE').read().splitlines()
assert lines, 'alert file is empty'
for line in lines:
    obj = json.loads(line)
    assert 'event' in obj, f'missing event key in: {line}'
    assert 'title' in obj, f'missing title key in: {line}'
print(f'OK: {len(lines)} alert(s) found')
"
    assert_success
    assert_output --partial "alert(s) found"
}
