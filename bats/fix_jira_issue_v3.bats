#!/usr/bin/env bats

# Dry-run test for fix_jira_issue_v3: imports the pipeline from a non-git
# scratch directory with real argv. Needs no credentials and no network.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
SCRATCH="$PROJECT_DIR/target/batstest/fix_jira_issue_v3"

setup_file() {
    cd "$PROJECT_DIR" || fail "Could not cd to project dir"

    if ! command -v uv &> /dev/null; then
        skip "uv is not installed"
    fi
}

setup() {
    rm -rf "$SCRATCH"
    mkdir -p "$SCRATCH"
    cd "$SCRATCH" || fail "Could not cd to scratch dir"
}

teardown() {
    cd "$PROJECT_DIR" || true
    rm -rf "$SCRATCH"
}

@test "dry run of an interactive run lists the four gates and no auto stage" {
    run uv run --project "$PROJECT_DIR" python -m norn run fix_jira_issue_v3 CBS-1234 --dry-run
    assert_success
    assert_output --partial "plan approved"
    assert_output --partial "code review decided"
    assert_output --partial "ship decided"
    assert_output --partial "wait for checks final"
    refute_output --partial "auto decide ship"
}

@test "dry run of an auto run lists the auto decision stages" {
    run uv run --project "$PROJECT_DIR" python -m norn run fix_jira_issue_v3 CBS-1234 auto --dry-run
    assert_success
    assert_output --partial "auto decide ship"
}

@test "stop is refused" {
    run uv run --project "$PROJECT_DIR" python -m norn run fix_jira_issue_v3 CBS-1234 stop --dry-run
    assert_failure
    assert_output --partial "stop"
}

@test "an unrecognised positional is refused by name" {
    run uv run --project "$PROJECT_DIR" python -m norn run fix_jira_issue_v3 CBS-1234 Auto --dry-run
    assert_failure
    assert_output --partial "Auto"
}
