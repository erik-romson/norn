"""The learning-store contract: schema, store resolution and the validator.

This module is the ONE home of norn's copy of the Compound Engineering (CE)
learning schema. A learning is a markdown document with YAML frontmatter that
lives under ``<docs_root>/solutions/<category>/<slug>.md``. norn only
*proposes* learnings (a human copies and commits them), so everything here is
about checking a proposal before a human commits it.

The validator fails closed: unattended norn cannot adjudicate a flag the way an
interactive CE session does, so every rule is a hard problem line. The one
allowance is a cited path that is missing but marked ``removed``/``deleted``/
``renamed`` on the same line.

Values are copied from CE 3.30.5 (``skills/ce-compound/references/schema.yaml``,
``yaml-schema.md`` and ``assets/resolution-template.md``); those files are never
read at run time.
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Schema constants (CE schema.yaml / yaml-schema.md)
# ---------------------------------------------------------------------------

BUG_PROBLEM_TYPES: tuple[str, ...] = (
    "build_error",
    "test_failure",
    "runtime_error",
    "performance_issue",
    "database_issue",
    "security_issue",
    "ui_bug",
    "integration_issue",
    "logic_error",
)
KNOWLEDGE_PROBLEM_TYPES: tuple[str, ...] = (
    "best_practice",
    "documentation_gap",
    "workflow_issue",
    "developer_experience",
    "architecture_pattern",
    "design_pattern",
    "tooling_decision",
    "convention",
)
SEVERITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
RESOLUTION_TYPES: tuple[str, ...] = (
    "code_fix",
    "migration",
    "config_change",
    "test_fix",
    "dependency_update",
    "environment_setup",
    "workflow_improvement",
    "documentation_update",
    "tooling_addition",
    "seed_data_update",
)
COMPONENT_SUGGESTED: tuple[str, ...] = (
    "data_model",
    "api_layer",
    "service_layer",
    "background_job",
    "database",
    "frontend",
    "messaging",
    "infrastructure",
    "observability",
    "authentication",
    "payments",
    "development_workflow",
    "testing_framework",
    "documentation",
    "tooling",
)
ROOT_CAUSE_SUGGESTED: tuple[str, ...] = (
    "wrong_api",
    "data_integrity",
    "concurrency",
    "async_timing",
    "memory_leak",
    "config_error",
    "logic_error",
    "test_isolation",
    "missing_validation",
    "missing_permission",
    "missing_workflow_step",
    "inadequate_documentation",
    "missing_tooling",
    "incomplete_setup",
)
CATEGORY_DEFAULTS: dict[str, str] = {
    "build_error": "build-errors",
    "test_failure": "test-failures",
    "runtime_error": "runtime-errors",
    "performance_issue": "performance-issues",
    "database_issue": "database-issues",
    "security_issue": "security-issues",
    "ui_bug": "ui-bugs",
    "integration_issue": "integration-issues",
    "logic_error": "logic-errors",
    "developer_experience": "developer-experience",
    "workflow_issue": "workflow-issues",
    "best_practice": "best-practices",
    "documentation_gap": "documentation-gaps",
    "architecture_pattern": "architecture-patterns",
    "design_pattern": "design-patterns",
    "tooling_decision": "tooling-decisions",
    "convention": "conventions",
}
MAX_SYMPTOMS = 5
MAX_APPLIES_WHEN = 5
MAX_TAGS = 8
KNOWN_FIELDS: tuple[str, ...] = (
    "title",
    "date",
    "last_updated",
    "category",
    "module",
    "problem_type",
    "component",
    "severity",
    "symptoms",
    "root_cause",
    "resolution_type",
    "applies_when",
    "related_components",
    "tags",
    "retire_when",
    "framework_version",
)
BUG_SECTIONS: tuple[str, ...] = (
    "Problem",
    "Symptoms",
    "What Didn't Work",
    "Solution",
    "Why This Works",
    "Prevention",
)
KNOWLEDGE_SECTIONS: tuple[str, ...] = (
    "Context",
    "Guidance",
    "Why This Matters",
    "When to Apply",
    "Examples",
)
DURABLE_BAR = (
    "A learning earns its place only when it holds durable project reasoning that is not "
    "readily recoverable from the final code, tests, types, comments, or existing "
    "documentation, and losing it would plausibly cause recurrence, material risk, or "
    "substantial rediscovery. Apply this counterfactual: if the learning document "
    "disappeared, would a future engineer reading the final implementation still be likely "
    "to repeat the mistake or redo substantial investigation? Completion, effort, and diff "
    "size do not establish eligibility."
)

_REQUIRED_FIELDS: tuple[str, ...] = (
    "title",
    "date",
    "module",
    "problem_type",
    "component",
    "severity",
)
_ARRAY_FIELDS: tuple[str, ...] = ("symptoms", "applies_when", "related_components", "tags")
_COMMIT_WORDS = (
    "commits",
    "commit",
    "committed",
    "revision",
    "rev",
    "revert",
    "reverted",
    "cherry-pick",
    "rebase",
    "landed in",
    "fixed in",
    "introduced in",
)
_STOPWORDS = frozenset(
    {
        "step",
        "test",
        "tests",
        "norn",
        "pipelines",
        "with",
        "from",
        "into",
        "that",
        "this",
        "when",
        "then",
    }
)
_PLACEHOLDERS = (
    "[Clear ",
    "[Observable ",
    "[Attempted ",
    "[Concrete ",
    "[Related ",
    "[Module",
    "[YYYY",
)
# Extensions that make a slash-less backticked token path-shaped. A slash-less
# token such as ``os.path`` must not be mistaken for a file.
_KNOWN_EXTENSIONS = frozenset(
    {
        "py", "md", "toml", "yaml", "yml", "json", "sh", "js", "ts", "tsx", "jsx",
        "txt", "cfg", "ini", "lock", "rs", "go", "java", "rb", "c", "h", "cpp",
        "html", "css", "sql", "bats", "mjs", "cjs", "xml", "csv",
    }
)  # fmt: skip

_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_DATED_SLUG_RE = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8})$")
_TAG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class StoreConfigError(Exception):
    """The ``docs_root`` setting is unusable."""


# ---------------------------------------------------------------------------
# Store resolution
# ---------------------------------------------------------------------------


def _containment_problem(repo: Path, path: Path) -> str | None:
    """Return why *path*'s real path is not a usable place inside *repo*, or None."""
    repo_real = Path(os.path.realpath(repo))
    real = Path(os.path.realpath(path))
    try:
        rel = real.relative_to(repo_real)
    except ValueError:
        return "resolves outside the repository"
    if rel == Path("."):
        return "is the repository root"
    if rel.parts[0] == ".git":
        return "is under .git/"
    return None


def resolve_store(repo: Path | str) -> Path:
    """Return the learning store directory for *repo*.

    Reads ``docs_root`` from ``<repo>/.compound-engineering/config.yaml`` only
    (never ``config.local.yaml``). Unset or no file gives ``<repo>/docs/solutions``;
    a set value ``X`` gives ``<repo>/X/solutions``. Never falls back to ``docs``.
    """
    repo = Path(repo)
    cfg = repo / ".compound-engineering" / "config.yaml"
    value: Any = None
    if cfg.is_file():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise StoreConfigError(f"{cfg}: not valid YAML: {exc}") from exc
        if data is not None and not isinstance(data, dict):
            raise StoreConfigError(f"{cfg}: expected a mapping, got {type(data).__name__}")
        if data is not None:
            value = data.get("docs_root")
    if value is None:
        docs_root = "docs"
    else:
        if not isinstance(value, str) or not value.strip():
            raise StoreConfigError(f"docs_root={value!r} in {cfg}: must be a non-empty string")
        docs_root = value
        if PurePosixPath(docs_root).is_absolute() or Path(docs_root).is_absolute():
            raise StoreConfigError(f"docs_root={value!r} in {cfg}: must be repo-relative")
    problem = _containment_problem(repo, repo / docs_root)
    if problem:
        raise StoreConfigError(f"docs_root={docs_root!r}: {problem}")
    store = repo / docs_root / "solutions"
    problem = _containment_problem(repo, store)
    if problem:
        raise StoreConfigError(f"docs_root={docs_root!r}: the store {problem}")
    return store


def store_relpath(repo: Path | str, store: Path | str) -> str:
    """The store path relative to the repo, as a POSIX string."""
    return Path(store).relative_to(Path(repo)).as_posix()


def existing_categories(store: Path | str) -> list[str]:
    """The directories directly under the store (sorted; empty when it does not exist)."""
    store = Path(store)
    if not store.is_dir():
        return []
    return sorted(p.name for p in store.iterdir() if p.is_dir())


def keywords_from(names: list[str] | tuple[str, ...], paths: list[str] | tuple[str, ...]) -> list[str]:
    """Search keywords from step names and changed paths (at most 12)."""
    out: list[str] = []
    for text in [*names, *paths]:
        for tok in re.split(r"[-_/. ]", text):
            tok = tok.lower()
            if len(tok) < 4 or tok.isdigit() or tok in _STOPWORDS or tok in out:
                continue
            out.append(tok)
            if len(out) == 12:
                return out
    return out


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _is_date(value: Any) -> bool:
    if isinstance(value, datetime.datetime):
        return False
    if isinstance(value, datetime.date):
        return True
    if isinstance(value, str) and _DATE_RE.match(value):
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            return False
        return True
    return False


def _split_frontmatter(doc_text: str) -> tuple[list[str], str] | str:
    """Return (frontmatter lines, body) or an error string."""
    lines = doc_text.splitlines()
    if not lines or lines[0] != "---":
        return "the first line must be `---`"
    for i in range(1, len(lines)):
        if lines[i] == "---":
            return lines[1:i], "\n".join(lines[i + 1 :])
    return "no closing `---` line for the frontmatter"


def _frontmatter_keys(text: str) -> list[str]:
    parts = _split_frontmatter(text)
    if isinstance(parts, str):
        return []
    keys = []
    for line in parts[0]:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):", line)
        if m:
            keys.append(m.group(1))
    return keys


def _frontmatter_value(text: str, key: str) -> str | None:
    parts = _split_frontmatter(text)
    if isinstance(parts, str):
        return None
    for line in parts[0]:
        m = re.match(rf"^{re.escape(key)}:[ \t]*(.*)$", line)
        if m:
            v = m.group(1).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            return v
    return None


def _strip_fences(text: str) -> str:
    """Blank out fenced code blocks (the fence lines included)."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


def _mask_spans(text: str) -> str:
    return re.sub(r"`[^`\n]*`", lambda m: " " * len(m.group(0)), text)


def _target_problems(
    target: str, repo: Path, store_rel: str, existing_text: str | None
) -> tuple[list[str], bool]:
    """Checks that apply to every target. Returns (problems, usable)."""
    problems: list[str] = []
    if "\n" in target or "\r" in target or not target.strip():
        return ["target must be a single non-empty line"], False
    posix = PurePosixPath(target)
    if posix.is_absolute() or Path(target).is_absolute():
        return [f"target {target!r} must be repo-relative"], False
    if not target.endswith(".md"):
        return [f"target {target!r} must end in .md"], False

    store_real = Path(os.path.realpath(repo / store_rel))
    store_problem = _containment_problem(repo, repo / store_rel)
    target_real = Path(os.path.realpath(repo / target))
    contained = store_problem is None
    if contained:
        try:
            rel = target_real.relative_to(store_real)
            contained = rel != Path(".")
        except ValueError:
            contained = False
    if not contained:
        problems.append(
            f"target {target!r} is not contained in the store {store_rel!r} "
            "(a `..` or a symlink leads out of it)"
        )
        return problems, False

    ignored = _git(repo, "check-ignore", "-q", "--", target)
    if ignored.returncode == 0:
        problems.append(f"target {target!r} is gitignored, so a human cannot commit it")
    elif ignored.returncode != 1:
        problems.append(f"git check-ignore failed for {target!r}: {ignored.stderr.strip()}")

    exists = os.path.lexists(repo / target)
    if exists and existing_text is None:
        problems.append(f"target {target!r} exists but no existing text was given")
    if not exists and existing_text is not None:
        problems.append(f"existing text was given but target {target!r} does not exist")
    return problems, True


def _new_doc_target_problems(target: str, repo: Path, store_rel: str) -> tuple[list[str], str | None]:
    """Rules for a new doc's path. Returns (problems, category directory or None)."""
    parts = PurePosixPath(target).parts
    store_parts = PurePosixPath(store_rel).parts
    rest = parts[len(store_parts) :]
    if parts[: len(store_parts)] != store_parts or len(rest) != 2:
        return [
            f"category rule: a new doc must be {store_rel}/<category>/<slug>.md, "
            f"exactly one directory below the store, not {target!r}"
        ], None
    category, filename = rest
    problems: list[str] = []
    slug = filename[: -len(".md")] if filename.endswith(".md") else filename
    if not _SLUG_RE.match(slug):
        problems.append(f"slug {slug!r} must match ^[a-z0-9]+(-[a-z0-9]+)*$")
    elif _DATED_SLUG_RE.search(slug):
        problems.append(f"slug {slug!r} must not end in a date")
    allowed = set(existing_categories(repo / store_rel)) | set(CATEGORY_DEFAULTS.values())
    if category not in allowed:
        problems.append(
            f"category rule: {category!r} is neither an existing store directory nor one of "
            f"{sorted(set(CATEGORY_DEFAULTS.values()))}"
        )
    return problems, category


def _scalar_problems(fm_lines: list[str]) -> list[str]:
    problems = []
    for line in fm_lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t]+(\S.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if value[0] in "\"'[{|>&*!#":
            continue
        if " #" in value or ": " in value:
            problems.append(f"frontmatter `{key}` is an unquoted scalar containing ' #' or ': '; quote it")
    return problems


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _frontmatter_problems(
    fm: dict[str, Any], *, is_update: bool, category_dir: str | None, existing_text: str | None
) -> tuple[list[str], str | None]:
    """Field checks. Returns (problems, track) with track in bug/knowledge/None."""
    problems: list[str] = []

    for field in _REQUIRED_FIELDS:
        if fm.get(field) is None:
            problems.append(f"missing required field `{field}`")
    for field in ("title", "module", "component"):
        v = fm.get(field)
        if v is not None and (not isinstance(v, str) or not v.strip()):
            problems.append(f"`{field}` must be a non-empty string")
    if fm.get("date") is not None and not _is_date(fm["date"]):
        problems.append("`date` must be YYYY-MM-DD")

    problem_type = fm.get("problem_type")
    track: str | None = None
    if problem_type is not None:
        if problem_type in BUG_PROBLEM_TYPES:
            track = "bug"
        elif problem_type in KNOWLEDGE_PROBLEM_TYPES:
            track = "knowledge"
        else:
            problems.append(
                f"`problem_type` {problem_type!r} must be one of "
                f"{list(BUG_PROBLEM_TYPES + KNOWLEDGE_PROBLEM_TYPES)}"
            )
    if fm.get("severity") is not None and fm["severity"] not in SEVERITIES:
        problems.append(f"`severity` {fm['severity']!r} must be one of {list(SEVERITIES)}")

    for field in _ARRAY_FIELDS:
        if field in fm and fm[field] is not None and not _is_str_list(fm[field]):
            problems.append(f"`{field}` must be a list of strings")
    if _is_str_list(fm.get("applies_when")) and len(fm["applies_when"]) > MAX_APPLIES_WHEN:
        problems.append(f"`applies_when` has more than {MAX_APPLIES_WHEN} items")
    if _is_str_list(fm.get("tags")):
        if len(fm["tags"]) > MAX_TAGS:
            problems.append(f"`tags` has more than {MAX_TAGS} items")
        for tag in fm["tags"]:
            if not _TAG_RE.match(tag):
                problems.append(f"tag {tag!r} must be lowercase and hyphen-separated")
    symptoms = fm.get("symptoms")

    if track == "bug":
        if symptoms is None:
            problems.append("missing required field `symptoms` (bug track)")
        elif _is_str_list(symptoms) and not 1 <= len(symptoms) <= MAX_SYMPTOMS:
            problems.append(f"`symptoms` needs 1 to {MAX_SYMPTOMS} items on the bug track")
        if fm.get("root_cause") is None:
            problems.append("missing required field `root_cause` (bug track)")
        elif not isinstance(fm["root_cause"], str) or not fm["root_cause"].strip():
            problems.append("`root_cause` must be a string")
        if fm.get("resolution_type") is None:
            problems.append("missing required field `resolution_type` (bug track)")
        elif fm["resolution_type"] not in RESOLUTION_TYPES:
            problems.append(
                f"`resolution_type` {fm['resolution_type']!r} must be one of {list(RESOLUTION_TYPES)}"
            )
    else:
        if _is_str_list(symptoms) and len(symptoms) > MAX_SYMPTOMS:
            problems.append(f"`symptoms` has more than {MAX_SYMPTOMS} items")
        if fm.get("resolution_type") is not None and fm["resolution_type"] not in RESOLUTION_TYPES:
            problems.append(
                f"`resolution_type` {fm['resolution_type']!r} must be one of {list(RESOLUTION_TYPES)}"
            )
        if track == "knowledge" and "framework_version" in fm:
            problems.append("`framework_version` appears only on the bug track")
    if fm.get("retire_when") is not None and not isinstance(fm["retire_when"], str):
        problems.append("`retire_when` must be a string")

    if is_update:
        assert existing_text is not None
        allowed = set(KNOWN_FIELDS) | set(_frontmatter_keys(existing_text))
        for key in fm:
            if str(key) not in allowed:
                problems.append(f"unknown frontmatter key `{key}` (the existing doc does not have it)")
        old_category = _frontmatter_value(existing_text, "category")
        if old_category is not None and str(fm.get("category")) != old_category:
            problems.append(f"an update must keep `category: {old_category}`")
        if not _is_date(fm.get("last_updated")):
            problems.append("an update must carry `last_updated` (YYYY-MM-DD)")
    else:
        for key in fm:
            if str(key) not in KNOWN_FIELDS:
                problems.append(f"unknown frontmatter key `{key}`")
        if category_dir is not None and fm.get("category") != category_dir:
            problems.append(
                f"`category` {fm.get('category')!r} must equal the target directory {category_dir!r}"
            )
        if fm.get("last_updated") is not None and not _is_date(fm["last_updated"]):
            problems.append("`last_updated` must be YYYY-MM-DD")
    return problems, track


def _grounding_problems(doc_text: str, repo: Path, target: str, base_sha: str | None) -> list[str]:
    problems: list[str] = []
    unfenced = _strip_fences(doc_text)

    # -- backticked repo paths -------------------------------------------------
    seen: set[str] = set()
    for line in unfenced.splitlines():
        for m in re.finditer(r"`([^`\n]+)`", line):
            token = m.group(1).strip()
            if token in seen:
                continue
            shaped = _path_token(token)
            if shaped is None:
                continue
            seen.add(token)
            path, first, last = shaped
            full = repo / path
            if _containment_problem(repo, full) in ("resolves outside the repository",):
                problems.append(f"cited path `{token}` resolves outside the repository")
                continue
            if not full.exists():
                if not re.search(r"\b(removed|deleted|renamed)\b", line, re.IGNORECASE):
                    problems.append(f"cited path `{token}` does not exist in the working tree")
                continue
            if first is not None:
                if not full.is_file():
                    problems.append(f"cited `{token}` has a line number but is not a file")
                    continue
                count = len((full).read_text(encoding="utf-8", errors="replace").splitlines())
                end = last if last is not None else first
                if first < 1 or end < first or end > count:
                    problems.append(f"cited line range in `{token}` is outside the file ({count} lines)")

    # -- commit SHAs -----------------------------------------------------------
    words = "|".join(re.escape(w) for w in _COMMIT_WORDS)
    sha_re = re.compile(rf"\b(?:{words})\b[\s:`(#]*([0-9a-fA-F]{{7,40}})\b", re.IGNORECASE)
    seen_sha: set[str] = set()
    for m in sha_re.finditer(unfenced):
        sha = m.group(1)
        if sha in seen_sha or not (re.search(r"\d", sha) and re.search(r"[a-fA-F]", sha)):
            continue
        seen_sha.add(sha)
        resolved = _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
        if resolved.returncode != 0:
            problems.append(f"commit {sha} does not resolve in this repository")
            continue
        if base_sha:
            anc = _git(repo, "merge-base", "--is-ancestor", sha, base_sha)
            if anc.returncode == 1:
                problems.append(
                    f"commit {sha} is not an ancestor of the base {base_sha}; the run's own "
                    "commits are rewritten by the rebase, so cite the path instead"
                )
            elif anc.returncode != 0:
                problems.append(f"git merge-base failed for {sha}: {anc.stderr.strip()}")

    # -- relative markdown links ----------------------------------------------
    masked = _mask_spans(unfenced)
    base_dir = (repo / target).parent
    seen_links: set[str] = set()
    for m in re.finditer(r"\[[^\]\n]*\]\(([^)\s]+)\)", masked):
        link = m.group(1)
        if link in seen_links or link.startswith("#") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", link):
            continue
        seen_links.add(link)
        rel = link.split("#", 1)[0].split("?", 1)[0]
        if not rel:
            continue
        if not os.path.exists(os.path.normpath(base_dir / rel)):
            problems.append(f"markdown link `{link}` does not resolve from the target's directory")
    return problems


def _path_token(token: str) -> tuple[str, int | None, int | None] | None:
    """Return (path, first line, last line) when *token* is shaped like a repo path."""
    if re.search(r"\s", token) or any(c in token for c in "<>{}*$") or "path/to" in token or "://" in token:
        return None
    first = last = None
    path = token
    m = re.match(r"^(?P<path>.+?):(?P<a>\d+)(?:-(?P<b>\d+))?$", token)
    if m:
        path = m.group("path")
        first = int(m.group("a"))
        last = int(m.group("b")) if m.group("b") else None
    if not re.fullmatch(r"[\w@+~./-]+", path) or path.startswith(("/", "~")):
        return None
    if path.strip("./") == "":
        return None
    if "/" not in path:
        ext = re.search(r"\.([A-Za-z0-9]+)$", path)
        if ext is None or ext.group(1).lower() not in _KNOWN_EXTENSIONS:
            return None
    return path, first, last


def _scaffold_problems(doc_text: str) -> list[str]:
    masked = _mask_spans(_strip_fences(doc_text))
    problems = []
    if re.search(r"\bLearnings? \d+", masked):
        problems.append("scaffold text: `Learning <n>` placeholder")
    if re.search(r"\{\{.*?\}\}", masked):
        problems.append("scaffold text: `{{…}}` template placeholder")
    if re.search(r"\b(TODO|TBD)\b", masked):
        problems.append("scaffold text: TODO/TBD")
    for ph in _PLACEHOLDERS:
        if ph in masked:
            problems.append(f"scaffold text: template placeholder {ph.strip()!r}")
    return problems


def validate_learning(
    doc_text: str,
    *,
    target: str,
    repo: Path | str,
    store_rel: str,
    base_sha: str | None = None,
    existing_text: str | None = None,
) -> list[str]:
    """Validate a learning proposal. Returns one line per problem, empty when clean.

    *target* is the repo-relative path where a human will copy the proposal;
    *existing_text* is the text already at *target* (an update) or ``None`` (a new doc).
    """
    repo = Path(repo)
    problems, usable = _target_problems(target, repo, store_rel, existing_text)
    if not usable:
        return problems
    is_update = existing_text is not None
    category_dir: str | None = None
    if not is_update:
        target_problems, category_dir = _new_doc_target_problems(target, repo, store_rel)
        problems += target_problems

    split = _split_frontmatter(doc_text)
    if isinstance(split, str):
        return problems + [f"frontmatter: {split}"]
    fm_lines, body = split

    scalar = _scalar_problems(fm_lines)
    try:
        loaded = yaml.safe_load("\n".join(fm_lines))
    except yaml.YAMLError as exc:
        detail = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        return problems + (scalar or [f"frontmatter is not valid YAML: {detail}"])
    if not isinstance(loaded, dict):
        return problems + scalar + ["frontmatter must be a YAML mapping"]
    problems += scalar

    fm_problems, track = _frontmatter_problems(
        loaded, is_update=is_update, category_dir=category_dir, existing_text=existing_text
    )
    problems += fm_problems

    unfenced_body = _strip_fences(body)
    if not re.search(r"^# \S", unfenced_body, re.MULTILINE):
        problems.append("the body needs an H1 (`# Title`)")
    if is_update:
        if not body.strip():
            problems.append("an update needs a non-empty body")
    elif track is not None:
        sections = BUG_SECTIONS if track == "bug" else KNOWLEDGE_SECTIONS
        for section in sections:
            if not re.search(rf"^##[ \t]+{re.escape(section)}[ \t]*$", unfenced_body, re.MULTILINE):
                problems.append(f"missing section `## {section}`")

    problems += _grounding_problems(doc_text, repo, target, base_sha)
    problems += _scaffold_problems(doc_text)
    return problems
