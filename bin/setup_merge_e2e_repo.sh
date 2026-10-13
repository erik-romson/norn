#!/usr/bin/env bash
# Set up (or tear down) the throwaway GitHub repository used for the
# merge_branch manual E2E checks in
# tmp/implement-fire-forget-merge-pipeline-plan-final-plan.md ("Manual E2E verification").
#
# What it creates:
#   - a tiny uv/pytest project, so detect_test_cmd picks
#     `uv run python -m pytest tests/ -v`;
#   - two workflows: `tests` (ruff + pytest, fast) and `slow` (sleep + pytest);
#     ruff runs only in CI, which gives scenario 5 its CI-only failure;
#   - `main`, and `develop` one commit ahead of it (scenario 1 expects
#     `base: develop (fork point)`);
#   - one ruleset on main and develop requiring both checks (strict, so a
#     moved base reads BEHIND) and a linear history, with no pull-request rule
#     so merge_branch's fast-forward push is allowed.
#
# Safe to rerun: each step reuses what already exists (repo, clone, branches,
# ruleset) and converges it, e.g. rerun with --public after a failed private run.
#
# Copilot review is not configured here: merge_branch requests it itself with
# `gh pr edit --add-reviewer @copilot`, which needs a Copilot plan that
# includes code review on the account running it.
set -euo pipefail

NORN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="norn-merge-e2e"
OWNER=""
DIR=""
VISIBILITY="--private"
SLOW_SECONDS=240
ACTION="setup"

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Create a throwaway GitHub repo for the merge_branch manual E2E checks.

Options:
  --name NAME          Repository name (default: $NAME)
  --owner OWNER        User or org that owns it (default: the gh user)
  --dir DIR            Local clone (default: $NORN_ROOT/tmp/merge-e2e/<name>)
  --public             Create a public repo (default: private; rulesets on a
                       private repo need GitHub Pro, Team or Enterprise)
  --slow-seconds N     How long the slow workflow sleeps (default: $SLOW_SECONDS)
  --delete             Delete the GitHub repo and the local clone instead
                       (needs: gh auth refresh -s delete_repo)
  -h, --help           Show this help
EOF
}

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --name) NAME="${2:?--name needs a value}"; shift 2 ;;
        --owner) OWNER="${2:?--owner needs a value}"; shift 2 ;;
        --dir) DIR="${2:?--dir needs a value}"; shift 2 ;;
        --public) VISIBILITY="--public"; shift ;;
        --slow-seconds) SLOW_SECONDS="${2:?--slow-seconds needs a value}"; shift 2 ;;
        --delete) ACTION="delete"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

case "$SLOW_SECONDS" in ''|*[!0-9]*) die "--slow-seconds must be a whole number" ;; esac
case "$NAME" in ''|*[!A-Za-z0-9._-]*) die "--name may hold only letters, digits, '.', '_' and '-'" ;; esac

command -v gh >/dev/null || die "gh is not installed"
command -v git >/dev/null || die "git is not installed"
gh auth status >/dev/null 2>&1 || die "gh is not logged in (run: gh auth login)"

[ -n "$OWNER" ] || OWNER="$(gh api user -q .login)"
[ -n "$DIR" ] || DIR="$NORN_ROOT/tmp/merge-e2e/$NAME"
FULL="$OWNER/$NAME"

repo_exists() { gh repo view "$FULL" --json name >/dev/null 2>&1; }

delete_repo() {
    if repo_exists; then
        log "Deleting $FULL"
        gh repo delete "$FULL" --yes
    else
        log "$FULL does not exist on GitHub"
    fi
    if [ -d "$DIR" ]; then
        log "Removing $DIR"
        rm -rf "$DIR"
    fi
}

write_project() {
    mkdir -p "$DIR/tests" "$DIR/.github/workflows"

    cat >"$DIR/pyproject.toml" <<'EOF'
[project]
name = "merge-e2e"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[dependency-groups]
dev = ["pytest>=8"]

# Makes this directory pytest's rootdir, so an enclosing project's config is not picked up.
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
EOF

    cat >"$DIR/calc.py" <<'EOF'
"""Tiny module for the merge_branch E2E repository."""


def add(a: int, b: int) -> int:
    return a + b


def multiply(a: int, b: int) -> int:
    return a * b
EOF

    cat >"$DIR/tests/test_calc.py" <<'EOF'
from calc import add, multiply


def test_add() -> None:
    assert add(2, 3) == 5


def test_multiply() -> None:
    assert multiply(4, 5) == 20
EOF

    cat >"$DIR/.gitignore" <<'EOF'
.venv/
__pycache__/
.pytest_cache/
.ruff_cache/
EOF

    cat >"$DIR/README.md" <<EOF
# $NAME

Throwaway repository for norn's \`merge_branch\` manual E2E checks.
Created by \`bin/setup_merge_e2e_repo.sh\` in the norn repo; safe to delete.
EOF

    cat >"$DIR/.github/workflows/tests.yml" <<'EOF'
name: tests
on:
  push:
    branches: [main, develop]
  pull_request:
jobs:
  tests:
    name: tests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - name: Lint (CI only)
        run: uvx ruff check .
      - name: Tests
        run: uv run python -m pytest tests/ -v
EOF

    cat >"$DIR/.github/workflows/slow.yml" <<EOF
name: slow
on:
  push:
    branches: [main, develop]
  pull_request:
jobs:
  slow:
    name: slow
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - name: Simulate a slow build
        run: sleep $SLOW_SECONDS
      - name: Tests
        run: uv run python -m pytest tests/ -v
EOF
}

ensure_local() {
    if [ ! -d "$DIR/.git" ]; then
        if [ -d "$DIR" ] && [ -n "$(ls -A "$DIR")" ]; then
            die "$DIR exists but is not a git repository; move it away and rerun"
        fi
        if repo_exists; then
            log "Cloning $FULL into $DIR"
            gh repo clone "$FULL" "$DIR" -- -q
        else
            mkdir -p "$DIR"
            git -C "$DIR" init -q -b main
        fi
    fi
    cd "$DIR" || die "cannot cd to $DIR"

    if git rev-parse -q --verify HEAD >/dev/null; then
        log "Reusing the local clone at $DIR"
        return
    fi

    log "Writing the project to $DIR"
    git symbolic-ref HEAD refs/heads/main
    write_project
    if command -v uv >/dev/null; then
        log "Locking dependencies and checking the local suite"
        uv lock -q
        uv run -q python -m pytest -q
    else
        echo "warning: uv not found; skipping uv.lock and the local test check" >&2
    fi
    git add -A
    git commit -q -m "chore: initial project"
}

ensure_remote() {
    local wanted="${VISIBILITY#--}" current
    if repo_exists; then
        current="$(gh repo view "$FULL" --json visibility -q .visibility | tr '[:upper:]' '[:lower:]')"
        if [ "$current" = "$wanted" ]; then
            log "Reusing $FULL ($current)"
        else
            log "Changing $FULL from $current to $wanted"
            gh repo edit "$FULL" --visibility "$wanted" --accept-visibility-change-consequences
        fi
    else
        log "Creating $FULL ($wanted)"
        gh repo create "$FULL" "$VISIBILITY" \
            --description "Throwaway repo for norn merge_branch E2E checks" >/dev/null
    fi

    if git remote get-url origin >/dev/null 2>&1; then
        case "$(git remote get-url origin)" in
            *"/$FULL"|*"/$FULL.git"|*":$FULL"|*":$FULL.git") ;;
            *) die "origin in $DIR is $(git remote get-url origin), not $FULL" ;;
        esac
    else
        git remote add origin "$(gh repo view "$FULL" --json url -q .url).git"
    fi
    git fetch -q --prune origin
}

# Pushes a branch only when the remote lacks it, so a rerun never rewrites
# a base branch a scenario has since moved.
push_if_missing() {
    local branch="$1"
    if git show-ref -q --verify "refs/remotes/origin/$branch"; then
        log "origin already has $branch"
    else
        log "Pushing $branch"
        git push -q -u origin "$branch"
    fi
}

ensure_branches() {
    push_if_missing main
    if ! git show-ref -q --verify refs/heads/develop; then
        if git show-ref -q --verify refs/remotes/origin/develop; then
            git branch -q --track develop origin/develop
        else
            log "Creating develop one commit ahead of main"
            git switch -q -c develop main
            cat >CHANGELOG.md <<'EOF'
# Changelog

- develop starts one commit ahead of main.
EOF
            git add CHANGELOG.md
            git commit -q -m "docs: add changelog"
        fi
    fi
    push_if_missing develop
}

ruleset_id() {
    gh api "repos/$FULL/rulesets" -q '.[] | select(.name == "merge-e2e") | .id' 2>/dev/null || true
}

# A ruleset left by an earlier run rejects pushing a branch that has no
# checks yet; ensure_ruleset turns it back on afterwards.
suspend_ruleset() {
    local id
    id="$(ruleset_id)"
    [ -n "$id" ] || return 0
    log "Suspending the ruleset while pushing branches"
    gh api --method PUT "repos/$FULL/rulesets/$id" -f enforcement=disabled >/dev/null
}

ensure_ruleset() {
    local body id method endpoint
    body="$(cat <<'EOF'
{
  "name": "merge-e2e",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {"include": ["refs/heads/main", "refs/heads/develop"], "exclude": []}
  },
  "rules": [
    {"type": "required_linear_history"},
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [{"context": "tests"}, {"context": "slow"}]
      }
    }
  ]
}
EOF
)"
    id="$(ruleset_id)"
    if [ -n "$id" ]; then
        log "Updating the ruleset on main and develop"
        method=PUT endpoint="repos/$FULL/rulesets/$id"
    else
        log "Creating the ruleset on main and develop"
        method=POST endpoint="repos/$FULL/rulesets"
    fi
    if ! echo "$body" | gh api --method "$method" "$endpoint" --input - >/dev/null; then
        die "writing the ruleset failed; a private repo needs GitHub Pro/Team (rerun with --public)"
    fi
}

setup_repo() {
    ensure_local
    ensure_remote
    suspend_ruleset
    ensure_branches
    ensure_ruleset

    local url
    url="$(gh repo view "$FULL" --json url -q .url)"
    cat <<EOF

Done: $url
Local clone: $DIR (on $(git branch --show-current))

Workflow runs for main and develop start on their first push; they seed the
CI duration estimates. Watch them with:
  gh run list -R $FULL

Scenario 1 (branch cut from develop):
  cd $DIR
  git switch develop && git switch -c feature/one
  echo '- feature one' >> CHANGELOG.md && git commit -am "docs: note feature one"
  uv run --project $NORN_ROOT norn run merge_branch --non-interactive

Scenario 2 needs develop == main: fast-forward main to develop first, e.g.
  git push origin develop:main   (after develop's checks are green)

Before scenario 1, check that Copilot can be requested on this repo
(merge_branch records 'unavailable' otherwise).

Tear down with:
  $0 --name $NAME --owner $OWNER --dir $DIR --delete
EOF
}

if [ "$ACTION" = "delete" ]; then
    delete_repo
else
    setup_repo
fi
