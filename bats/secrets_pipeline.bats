#!/usr/bin/env bats

# Tests for secrets and environment variable management.
# Does NOT require ANTHROPIC_API_KEY — uses only RunCommand stages.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
ENV_PIPELINE="$PROJECT_DIR/bats/testfiles/secrets_pipeline/env_pipeline.py"
SECRET_PIPELINE="$PROJECT_DIR/bats/testfiles/secrets_pipeline/secret_env_pipeline.py"
MISSING_PIPELINE="$PROJECT_DIR/bats/testfiles/secrets_pipeline/missing_secret_pipeline.py"

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

@test "pipeline .env() injects static env var into RunCommand" {
    run uv run python -m norn run "$ENV_PIPELINE"
    assert_success
}

@test "pipeline .secret(source=env) resolves secret and injects into stage env" {
    TEST_PIPELINE_SECRET=test_value_123 run uv run python -m norn run "$SECRET_PIPELINE"
    assert_success
}

@test "missing env secret causes pipeline to fail with clear error" {
    run env -u TEST_MISSING_SECRET_XYZ uv run python -m norn run "$MISSING_PIPELINE"
    assert_failure
    assert_output --partial "TEST_MISSING_SECRET_XYZ"
}
