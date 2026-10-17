#!/usr/bin/env bats

# End-to-end test for norn/pipelines/_commit_rewrite_v3.py, run as its own
# python3 process the way fix_jira_issue_v3's `rewrite commits` stage runs it.
# It builds a throwaway repo under target/batstest/commit_rewrite_v3/.
# No network and no Claude.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
SCRIPT="$PROJECT_DIR/norn/pipelines/_commit_rewrite_v3.py"
TEMPLATE="$PROJECT_DIR/bats/testfiles/commit_rewrite_v3/plan.md.tmpl"
WORK="$PROJECT_DIR/target/batstest/commit_rewrite_v3"
REPO="$WORK/repo"
PLAN="$WORK/plan.md"

g() {
    git -C "$REPO" "$@"
}

setup() {
    rm -rf "$WORK"
    mkdir -p "$REPO"

    g init -q -b main
    g config user.email bats@example.com
    g config user.name "Bats Test"
    g config commit.gpgsign false
    g config core.hooksPath "$REPO/.git/hooks"

    printf 'hello\n' > "$REPO/greeting.txt"
    printf 'old\n' > "$REPO/old.txt"
    g add -A
    g commit -q -m "initial"
    BASE="$(g rev-parse HEAD)"

    g switch -q -c CBS-1
    printf 'hello, world\n' > "$REPO/greeting.txt"
    printf 'new\n' > "$REPO/new.txt"
    g add -A
    g commit -q -m "refactor: step-01-a — A"
    g rm -q old.txt
    mkdir -p "$REPO/dir"
    printf 'export default 1\n' > "$REPO/dir/[id].tsx"
    g add -A
    g commit -q -m "refactor: step-02-b — B"

    START="$(g rev-list --reverse "$BASE..HEAD" | head -n 1)"
    HEAD_SHA="$(g rev-parse HEAD)"
    TREE="$(g rev-parse 'HEAD^{tree}')"

    # Render with bash substitution: no sed, so no locale or delimiter issues.
    local plan
    plan="$(cat "$TEMPLATE")"
    plan="${plan//@START_SHORT@/${START:0:7}}"
    plan="${plan//@HEAD_SHORT@/${HEAD_SHA:0:7}}"
    plan="${plan//@START@/$START}"
    plan="${plan//@HEAD@/$HEAD_SHA}"
    plan="${plan//@BASE@/$BASE}"
    printf '%s\n' "$plan" > "$PLAN"
}

teardown() {
    rm -rf "$WORK"
}

@test "apply regroups the range into the plan's two commits and keeps the tree" {
    run python3 "$SCRIPT" apply --plan "$PLAN" --repo "$REPO" --base "$BASE" \
        --key CBS-1 --expect-head "$HEAD_SHA"
    assert_success
    assert_output --partial "backup branch: backup/CBS-1-"
    assert_output --partial "backup ref: refs/backup/split-${START:0:7}-${HEAD_SHA:0:7}-"

    run git -C "$REPO" log --reverse --format=%s "$BASE..HEAD"
    assert_success
    assert_line --index 0 "CBS-1: update the greeting"
    assert_line --index 1 "CBS-1: retire the old file"
    assert_equal "${#lines[@]}" 2

    run git -C "$REPO" rev-parse 'HEAD^{tree}'
    assert_success
    assert_output "$TREE"

    run git -C "$REPO" status --porcelain --untracked-files=all
    assert_success
    assert_output ""
}

@test "a missing --plan is a usage error" {
    run python3 "$SCRIPT" apply --repo "$REPO" --base "$BASE" \
        --key CBS-1 --expect-head "$HEAD_SHA"
    assert_failure 2
}
