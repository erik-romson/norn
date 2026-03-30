#!/usr/bin/env bats

# End-to-end tests for --resume checkpoint resumption.
# Verifies that:
#   1. A checkpoint file is created after a successful run.
#   2. A resumed run shows cached stages from the checkpoint.
#
# Requires a working Claude auth (API key or Claude Code plan).

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
PIPELINE_FILE="$BATS_TEST_DIRNAME/testfiles/resume_pipeline/pipeline.py"
CHECKPOINT_FILE="${PIPELINE_FILE%.py}.checkpoint"
OUTPUT_FILE="$PROJECT_DIR/tmp/resume_test/output.txt"

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
    rm -rf "$PROJECT_DIR/tmp/resume_test"
    rm -f "$CHECKPOINT_FILE"
}

teardown() {
    rm -rf "$PROJECT_DIR/tmp/resume_test"
    rm -f "$CHECKPOINT_FILE"
}

@test "first run creates checkpoint file" {
    run uv run python -m norn run "$PIPELINE_FILE" \
        "Respond with the single word: READY"
    assert_success
    assert_file_exist "$CHECKPOINT_FILE"
}

@test "resume run shows cached stages and resumption banner" {
    # Run 1: create checkpoint
    run uv run python -m norn run "$PIPELINE_FILE" \
        "Respond with the single word: READY"
    assert_success
    assert_file_exist "$CHECKPOINT_FILE"

    # Run 2: resume — the respond stage should be cached (skipped)
    run uv run python -m norn run "$PIPELINE_FILE" --resume \
        "This prompt is ignored because the stage is cached"
    assert_success

    # CLI must print the resumption banner
    assert_output --partial "Resuming from checkpoint"

    # The cached stage should show as cached in output
    assert_output --partial "cached"
}
