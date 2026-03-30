#!/usr/bin/env bats

# Tests for prompt template loading and Generate(template=...) integration.
# Does NOT require ANTHROPIC_API_KEY — uses --dry-run and direct Python checks.

export BATS_LIB_PATH="/usr/local/lib/bats/:$BATS_LIB_PATH"

bats_load_library bats-support
bats_load_library bats-assert
bats_load_library bats-file

PROJECT_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
PIPELINE="$PROJECT_DIR/bats/testfiles/template_pipeline/pipeline.py"
TEMPLATES_DIR="$PROJECT_DIR/bats/testfiles/template_pipeline/templates"

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

@test "pipeline with Generate(template=) passes dry-run" {
    run uv run python -m norn run --dry-run "$PIPELINE"
    assert_success
}

@test "load_template loads a PromptTemplate from a file" {
    run uv run python -c "
import os, sys
os.chdir('$PROJECT_DIR/bats/testfiles/template_pipeline')
from norn.templates import load_template
t = load_template('greeting')
assert t.name == 'greeting', f'expected greeting, got {t.name}'
assert '{input}' in t.template, 'template should contain {input}'
assert t.system_prompt == 'You are a friendly greeter.'
print('ok')
"
    assert_success
    assert_output --partial "ok"
}

@test "load_template raises FileNotFoundError for missing template" {
    run uv run python -c "
import os
os.chdir('$PROJECT_DIR/bats/testfiles/template_pipeline')
from norn.templates import load_template
try:
    load_template('nonexistent')
    print('FAIL: expected FileNotFoundError')
    exit(1)
except FileNotFoundError as e:
    print('ok:', e)
"
    assert_success
    assert_output --partial "ok"
}

@test "PromptTemplate is importable from norn.dsl" {
    run uv run python -c "from norn.dsl import PromptTemplate; print(PromptTemplate.__name__)"
    assert_success
    assert_output --partial "PromptTemplate"
}
