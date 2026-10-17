#!/usr/bin/env bats

# Dry-run test for fix_issue_v1: imports the pipeline from a scratch git
# repository (with an origin) using real argv. Needs no credentials and no
# network. The norn checkout's own origin is never consulted.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
SCRATCH="$PROJECT_DIR/target/batstest/fix_issue_v1"

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
    git init -q
    git remote add origin https://github.com/acme/app.git
    unset GH_HOST
}

teardown() {
    cd "$PROJECT_DIR" || true
    rm -rf "$SCRATCH"
}

@test "dry run of a Jira key lists the four gates and no auto stage" {
    run uv run --project "$PROJECT_DIR" python -m norn run fix_issue_v1 CBS-1234 --dry-run
    assert_success
    assert_output --partial "fetch issue"
    assert_output --partial "ship decided"
    assert_output --partial "wait for checks final"
    refute_output --partial "auto decide ship"
}

@test "dry run of a bare issue number succeeds" {
    run uv run --project "$PROJECT_DIR" python -m norn run fix_issue_v1 1 --dry-run
    assert_success
    assert_output --partial "fetch issue"
}

@test "dry run of a quoted #1 in auto mode lists the auto decision stages" {
    run uv run --project "$PROJECT_DIR" python -m norn run fix_issue_v1 '#1' auto --dry-run
    assert_success
    assert_output --partial "auto decide ship"
}

@test "dry run of owner/repo#N for the launch repo succeeds" {
    run uv run --project "$PROJECT_DIR" python -m norn run fix_issue_v1 acme/app#1 --dry-run
    assert_success
}

@test "dry run of an issue in another repo succeeds" {
    run uv run --project "$PROJECT_DIR" python -m norn run fix_issue_v1 other/lib#45 --dry-run
    assert_success
}

@test "dry run of an issue URL in another repo succeeds" {
    run uv run --project "$PROJECT_DIR" python -m norn run fix_issue_v1 https://github.com/other/lib/issues/45 --dry-run
    assert_success
}

@test "a URL on another host is refused and names GH_HOST" {
    run uv run --project "$PROJECT_DIR" python -m norn run fix_issue_v1 https://ghe.corp/acme/app/issues/1 --dry-run
    assert_failure
    assert_output --partial "GH_HOST"
}

@test "a pull URL is refused" {
    run uv run --project "$PROJECT_DIR" python -m norn run fix_issue_v1 https://github.com/acme/app/pull/3 --dry-run
    assert_failure
    assert_output --partial "pull"
}

@test "an unquoted #1 is a shell comment and leaves no reference" {
    run bash -c 'uv run --project "$1" python -m norn run fix_issue_v1 #1 --dry-run' _ "$PROJECT_DIR"
    assert_failure
    assert_output --partial "needs one issue reference"
}

@test "an unrecognised positional is refused by name" {
    run uv run --project "$PROJECT_DIR" python -m norn run fix_issue_v1 CBS-1234 Auto --dry-run
    assert_failure
    assert_output --partial "Auto"
}

@test "stop is refused" {
    run uv run --project "$PROJECT_DIR" python -m norn run fix_issue_v1 CBS-1234 stop --dry-run
    assert_failure
    assert_output --partial "stop"
}

@test "a bare number without an origin is refused" {
    git remote remove origin
    run uv run --project "$PROJECT_DIR" python -m norn run fix_issue_v1 1 --dry-run
    assert_failure
    assert_output --partial "cannot read owner/repo from origin"
}

@test "describe shows the pipeline" {
    run uv run --project "$PROJECT_DIR" python -m norn describe fix_issue_v1
    assert_success
    assert_output --partial "Fix a Jira or GitHub issue"
    assert_output --partial "Required environment variables"
}
