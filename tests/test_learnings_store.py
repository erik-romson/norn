"""Tests for the learning-store contract: store rules, validator, finder and CLI."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from git import Repo

from norn.pipelines import _find_related, _learnings
from norn.pipelines._learnings import (
    CATEGORY_DEFAULTS,
    StoreConfigError,
    existing_categories,
    keywords_from,
    resolve_store,
    store_relpath,
    validate_learning,
)

FIXTURES = Path(__file__).parent / "fixtures" / "learnings"
STORE_REL = "docs/solutions"
BUG_TARGET = "docs/solutions/integration-issues/another-slug.md"
KNOWLEDGE_TARGET = "docs/solutions/skill-design/another-slug.md"
BUG_FIXTURE = FIXTURES / "corpus/docs/solutions/integration-issues/portable-structured-output-schemas.md"
KNOWLEDGE_FIXTURE = FIXTURES / "corpus/docs/solutions/skill-design/progressive-disclosure.md"
LEGACY_FIXTURE = FIXTURES / "corpus/docs/solutions/best-practices/legacy.md"
ROOT_NOTE_FIXTURE = FIXTURES / "corpus/docs/solutions/root-note.md"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path, fixture: str = "corpus") -> Path:
    """Copy a fixture into a temp git repo that also holds files the docs cite."""
    root = tmp_path / "repo"
    shutil.copytree(FIXTURES / fixture, root)
    (root / "README.md").write_text("".join(f"line {i}\n" for i in range(1, 11)))
    (root / "norn").mkdir(exist_ok=True)
    (root / "norn" / "real.py").write_text("".join(f"x{i} = {i}\n" for i in range(1, 6)))
    repo = Repo.init(root)
    repo.config_writer().set_value("user", "name", "Test User").release()
    repo.config_writer().set_value("user", "email", "test@example.com").release()
    repo.git.add("-A")
    repo.index.commit("initial commit")
    return root


def _check(repo: Path, text: str, target: str, **kwargs) -> list[str]:
    kwargs.setdefault("base_sha", None)
    kwargs.setdefault("existing_text", None)
    return validate_learning(text, target=target, repo=repo, store_rel=STORE_REL, **kwargs)


def _sub(text: str, old: str, new: str) -> str:
    assert old in text, f"{old!r} not in fixture text"
    return text.replace(old, new, 1)


def _drop(text: str, key: str) -> str:
    out = re.sub(rf"^{key}:.*\n", "", text, count=1, flags=re.MULTILINE)
    assert out != text
    return out


def _set_list(text: str, key: str, items: list[str]) -> str:
    block = f"{key}:\n" + "".join(f'  - "{i}"\n' for i in items) if items else f"{key}: []\n"
    out = re.sub(rf"^{key}:\n(?:  - .*\n)+", block, text, count=1, flags=re.MULTILINE)
    assert out != text
    return out


def _bug() -> str:
    return BUG_FIXTURE.read_text()


def _knowledge() -> str:
    return KNOWLEDGE_FIXTURE.read_text()


def _only(problems: list[str], needle: str) -> None:
    assert len(problems) == 1, problems
    assert needle in problems[0], problems


# ---------------------------------------------------------------------------
# schema constants
# ---------------------------------------------------------------------------


def test_schema_constants_shape():
    assert len(CATEGORY_DEFAULTS) == 17
    assert set(CATEGORY_DEFAULTS) == set(_learnings.BUG_PROBLEM_TYPES) | set(
        _learnings.KNOWLEDGE_PROBLEM_TYPES
    )
    assert len(_learnings.KNOWN_FIELDS) == 16
    assert (_learnings.MAX_SYMPTOMS, _learnings.MAX_APPLIES_WHEN, _learnings.MAX_TAGS) == (5, 5, 8)
    assert _learnings.DURABLE_BAR.startswith("A learning earns its place only when")
    assert _learnings.DURABLE_BAR.endswith("do not establish eligibility.")


# ---------------------------------------------------------------------------
# store resolution
# ---------------------------------------------------------------------------


def test_store_defaults_without_config(tmp_path):
    assert resolve_store(tmp_path) == tmp_path / "docs" / "solutions"


def test_store_follows_docs_root():
    repo = FIXTURES / "configured"
    store = resolve_store(repo)
    assert store == repo / "kb" / "solutions"
    assert store_relpath(repo, store) == "kb/solutions"


def test_store_ignores_local_config(tmp_path):
    cfg = tmp_path / ".compound-engineering"
    cfg.mkdir()
    (cfg / "config.local.yaml").write_text("docs_root: elsewhere\n")
    assert resolve_store(tmp_path) == tmp_path / "docs" / "solutions"


def test_store_bad_root_fixture_raises_naming_value():
    with pytest.raises(StoreConfigError, match=r"docs_root.*\.\./outside"):
        resolve_store(FIXTURES / "bad-root")


@pytest.mark.parametrize("value", [".", ".git/x", "../outside", "/abs/path"])
def test_store_rejects_unusable_docs_root(tmp_path, value):
    cfg = tmp_path / ".compound-engineering"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(f"docs_root: {value}\n")
    (tmp_path / ".git").mkdir()
    with pytest.raises(StoreConfigError) as exc:
        resolve_store(tmp_path)
    assert "docs_root" in str(exc.value)
    assert value in str(exc.value)


def test_store_rejects_symlink_escaping_the_repo(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    repo.mkdir()
    (repo / "kb").symlink_to(outside)
    (repo / ".compound-engineering").mkdir()
    (repo / ".compound-engineering" / "config.yaml").write_text("docs_root: kb\n")
    with pytest.raises(StoreConfigError, match="kb"):
        resolve_store(repo)


def test_existing_categories(tmp_path):
    assert existing_categories(tmp_path / "missing") == []
    assert existing_categories(FIXTURES / "corpus/docs/solutions") == [
        "best-practices",
        "integration-issues",
        "skill-design",
    ]


def test_keywords_from_filters_and_caps():
    kws = keywords_from(
        ["step-08-learning-store-contract", "ab"],
        ["norn/pipelines/_learnings.py", "tests/test_learnings_store.py", "12345"],
    )
    assert "learning" in kws and "store" in kws and "contract" in kws
    assert "step" not in kws and "norn" not in kws and "tests" not in kws
    assert "08" not in kws and "12345" not in kws and "ab" not in kws
    assert len(kws) == len(set(kws))
    many = keywords_from([], [f"alpha{i}x/beta{i}y" for i in range(20)])
    assert len(many) == 12


# ---------------------------------------------------------------------------
# validator: valid docs
# ---------------------------------------------------------------------------


def test_valid_bug_doc_passes(tmp_path):
    repo = _make_repo(tmp_path)
    assert _check(repo, _bug(), BUG_TARGET) == []


def test_valid_knowledge_doc_passes(tmp_path):
    repo = _make_repo(tmp_path)
    assert _check(repo, _knowledge(), KNOWLEDGE_TARGET) == []


# ---------------------------------------------------------------------------
# validator: required fields and enums
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["title", "date", "module", "problem_type", "component", "severity"])
def test_missing_required_field(tmp_path, field):
    repo = _make_repo(tmp_path)
    _only(_check(repo, _drop(_bug(), field), BUG_TARGET), f"`{field}`")


@pytest.mark.parametrize("field", ["root_cause", "resolution_type"])
def test_missing_bug_track_field(tmp_path, field):
    repo = _make_repo(tmp_path)
    _only(_check(repo, _drop(_bug(), field), BUG_TARGET), f"`{field}`")


def test_missing_bug_symptoms(tmp_path):
    repo = _make_repo(tmp_path)
    text = re.sub(r"^symptoms:\n(?:  - .*\n)+", "", _bug(), flags=re.MULTILINE)
    _only(_check(repo, text, BUG_TARGET), "`symptoms`")


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ("problem_type: integration_issue", "problem_type: bogus", "`problem_type`"),
        ("severity: medium", "severity: urgent", "`severity`"),
        ("resolution_type: code_fix", "resolution_type: magic", "`resolution_type`"),
    ],
)
def test_enum_violation(tmp_path, old, new, needle):
    repo = _make_repo(tmp_path)
    _only(_check(repo, _sub(_bug(), old, new), BUG_TARGET), needle)


def test_six_symptoms(tmp_path):
    repo = _make_repo(tmp_path)
    text = _set_list(_bug(), "symptoms", [f"symptom {i}" for i in range(6)])
    _only(_check(repo, text, BUG_TARGET), "`symptoms`")


def test_zero_bug_symptoms(tmp_path):
    repo = _make_repo(tmp_path)
    text = _set_list(_bug(), "symptoms", [])
    _only(_check(repo, text, BUG_TARGET), "`symptoms`")


def test_nine_tags(tmp_path):
    repo = _make_repo(tmp_path)
    text = _set_list(_bug(), "tags", [f"tag-{i}" for i in range(9)])
    _only(_check(repo, text, BUG_TARGET), "`tags`")


def test_tag_must_be_lowercase_hyphenated(tmp_path):
    repo = _make_repo(tmp_path)
    text = _set_list(_bug(), "tags", ["Not_Ok"])
    _only(_check(repo, text, BUG_TARGET), "Not_Ok")


def test_framework_version_only_on_bug_track(tmp_path):
    repo = _make_repo(tmp_path)
    text = _sub(_knowledge(), "severity: low", "severity: low\nframework_version: 3.1")
    _only(_check(repo, text, KNOWLEDGE_TARGET), "framework_version")
    assert _check(repo, _sub(_bug(), "severity: medium", "severity: medium\nframework_version: 3.1"), BUG_TARGET) == []


def test_unquoted_backtick_array_item(tmp_path):
    repo = _make_repo(tmp_path)
    text = _sub(
        _bug(), '  - "One model CLI rejected the schema that another accepted"', "  - `cmd` failed"
    )
    _only(_check(repo, text, BUG_TARGET), "YAML")


def test_non_string_array_item(tmp_path):
    repo = _make_repo(tmp_path)
    text = _set_list(_bug(), "tags", ["ok"]).replace('  - "ok"\n', "  - 5\n")
    _only(_check(repo, text, BUG_TARGET), "`tags`")


def test_unquoted_hash_in_title(tmp_path):
    repo = _make_repo(tmp_path)
    text = _sub(_bug(), "title: Structured-output schemas must be portable across model CLIs",
                "title: Structured-output schemas must be portable # across model CLIs")
    _only(_check(repo, text, BUG_TARGET), "`title`")


def test_frontmatter_delimiters(tmp_path):
    repo = _make_repo(tmp_path)
    _only(_check(repo, "# no frontmatter\n", BUG_TARGET), "first line")
    unclosed = _bug().replace("\n---\n", "\n", 1)
    _only(_check(repo, unclosed, BUG_TARGET), "closing")


# ---------------------------------------------------------------------------
# validator: new docs versus updates
# ---------------------------------------------------------------------------


def test_unknown_key_in_new_doc(tmp_path):
    repo = _make_repo(tmp_path)
    text = _sub(_bug(), "severity: medium", "severity: medium\nrails_version: 7")
    _only(_check(repo, text, BUG_TARGET), "rails_version")


def test_update_may_keep_a_legacy_key_and_category(tmp_path):
    repo = _make_repo(tmp_path)
    legacy = LEGACY_FIXTURE.read_text()
    target = "docs/solutions/best-practices/legacy.md"
    assert _check(repo, legacy, target, existing_text=legacy) == []


def test_update_must_keep_its_category(tmp_path):
    repo = _make_repo(tmp_path)
    legacy = LEGACY_FIXTURE.read_text()
    changed = _sub(legacy, "category: docs/solutions/best-practices/", "category: best-practices")
    problems = _check(repo, changed, "docs/solutions/best-practices/legacy.md", existing_text=legacy)
    _only(problems, "category")


def test_update_needs_last_updated(tmp_path):
    repo = _make_repo(tmp_path)
    legacy = LEGACY_FIXTURE.read_text()
    without = _drop(legacy, "last_updated")
    problems = _check(repo, without, "docs/solutions/best-practices/legacy.md", existing_text=legacy)
    _only(problems, "last_updated")


def test_category_mismatch_in_new_doc(tmp_path):
    repo = _make_repo(tmp_path)
    text = _sub(_bug(), "category: integration-issues", "category: skill-design")
    _only(_check(repo, text, BUG_TARGET), "`category`")


def test_missing_section_in_new_doc(tmp_path):
    repo = _make_repo(tmp_path)
    text = _sub(_bug(), "## Prevention\n", "")
    _only(_check(repo, text, BUG_TARGET), "## Prevention")


def test_update_needs_only_a_body(tmp_path):
    repo = _make_repo(tmp_path)
    existing = ROOT_NOTE_FIXTURE.read_text()
    head, _, _ = existing.partition("\n# ")
    sparse = head + "\n# A note kept at the store root\n\nJust a line.\n"
    assert _check(repo, sparse, "docs/solutions/root-note.md", existing_text=existing) == []


def test_update_of_store_root_doc_keeps_its_path(tmp_path):
    repo = _make_repo(tmp_path)
    existing = ROOT_NOTE_FIXTURE.read_text()
    assert _check(repo, existing, "docs/solutions/root-note.md", existing_text=existing) == []


def test_update_of_legacy_named_doc(tmp_path):
    repo = _make_repo(tmp_path)
    old = repo / "docs/solutions/integration-issues/Old_Name.md"
    old.write_text(_bug())
    assert _check(repo, _bug(), "docs/solutions/integration-issues/Old_Name.md", existing_text=_bug()) == []


def test_new_doc_at_store_root_fails_with_category_rule(tmp_path):
    repo = _make_repo(tmp_path)
    problems = _check(repo, _bug(), "docs/solutions/x.md")
    _only(problems, "category rule")


def test_coined_category_directory(tmp_path):
    repo = _make_repo(tmp_path)
    text = _sub(_bug(), "category: integration-issues", "category: misc-stuff")
    _only(_check(repo, text, "docs/solutions/misc-stuff/x.md"), "misc-stuff")


def test_default_category_directory_is_allowed_before_it_exists(tmp_path):
    repo = _make_repo(tmp_path)
    text = _sub(_bug(), "category: integration-issues", "category: test-failures")
    assert _check(repo, text, "docs/solutions/test-failures/x.md") == []


def test_dated_slug(tmp_path):
    repo = _make_repo(tmp_path)
    _only(_check(repo, _bug(), "docs/solutions/integration-issues/slug-2026-01-02.md"), "date")


def test_bad_slug(tmp_path):
    repo = _make_repo(tmp_path)
    _only(_check(repo, _bug(), "docs/solutions/integration-issues/Bad_Slug.md"), "slug")


def test_target_leaving_the_store_through_dotdot(tmp_path):
    repo = _make_repo(tmp_path)
    problems = _check(repo, _bug(), "docs/solutions/../../README.md", existing_text="x")
    _only(problems, "not contained")


def test_update_target_symlink_pointing_outside_the_store(tmp_path):
    repo = _make_repo(tmp_path)
    link = repo / "docs/solutions/integration-issues/link.md"
    link.symlink_to(repo / "README.md")
    problems = _check(repo, _bug(), "docs/solutions/integration-issues/link.md", existing_text="x")
    _only(problems, "not contained")


def test_target_must_be_a_single_md_line(tmp_path):
    repo = _make_repo(tmp_path)
    _only(_check(repo, _bug(), "docs/solutions/integration-issues/a.md\nb.md"), "single")
    _only(_check(repo, _bug(), "docs/solutions/integration-issues/a.txt"), ".md")


def test_gitignored_target(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / ".gitignore").write_text("docs/solutions/integration-issues/ignored.md\n")
    _only(_check(repo, _bug(), "docs/solutions/integration-issues/ignored.md"), "gitignored")


def test_existing_text_must_agree_with_the_target(tmp_path):
    repo = _make_repo(tmp_path)
    # exists, but no existing text given
    problems = _check(repo, _bug(), "docs/solutions/best-practices/legacy.md")
    assert any("exists but" in p for p in problems)
    # existing text given, target missing
    problems = _check(repo, _bug(), BUG_TARGET, existing_text=_bug())
    assert any("does not exist" in p for p in problems)


# ---------------------------------------------------------------------------
# validator: grounding
# ---------------------------------------------------------------------------


def _with_solution(text: str, extra: str) -> str:
    return _sub(text, "## Why This Works", f"{extra}\n\n## Why This Works")


def test_cited_missing_path(tmp_path):
    repo = _make_repo(tmp_path)
    text = _with_solution(_bug(), "See `norn/nope.py:3` for it.")
    _only(_check(repo, text, BUG_TARGET), "norn/nope.py:3")


def test_cited_line_out_of_range(tmp_path):
    repo = _make_repo(tmp_path)
    text = _with_solution(_bug(), "See `README.md:99999` for it.")
    _only(_check(repo, text, BUG_TARGET), "README.md:99999")
    ok = _with_solution(_bug(), "See `README.md:2-9` and `norn/real.py:5`.")
    assert _check(repo, ok, BUG_TARGET) == []


def test_removed_line_may_cite_a_missing_path(tmp_path):
    repo = _make_repo(tmp_path)
    text = _with_solution(_bug(), "The helper `norn/gone.py` was removed.")
    assert _check(repo, text, BUG_TARGET) == []


def test_non_path_tokens_are_not_checked(tmp_path):
    repo = _make_repo(tmp_path)
    text = _with_solution(
        _bug(), "Use `os.path`, `<dir>/x.py`, `path/to/file.py`, `https://x.io/a.md` and `a b/c.py`."
    )
    assert _check(repo, text, BUG_TARGET) == []


def test_unresolvable_sha(tmp_path):
    repo = _make_repo(tmp_path)
    text = _with_solution(_bug(), "This was fixed in deadbee1 upstream.")
    _only(_check(repo, text, BUG_TARGET), "deadbee1")


def test_sha_must_descend_from_base(tmp_path):
    root = _make_repo(tmp_path)
    repo = Repo(root)
    base = repo.head.commit.hexsha
    (root / "extra.txt").write_text("x\n")
    repo.git.add("-A")
    head = repo.index.commit("second").hexsha
    text = _with_solution(_bug(), f"The change landed in {head[:10]}.")
    assert _check(root, text, BUG_TARGET) == []
    assert _check(root, text, BUG_TARGET, base_sha=head) == []
    _only(_check(root, text, BUG_TARGET, base_sha=base), head[:10])


def test_hex_word_without_a_commit_word_is_ignored(tmp_path):
    repo = _make_repo(tmp_path)
    text = _with_solution(_bug(), "The id deadbee1 is only an id, and the commit deadbeef is letters.")
    assert _check(repo, text, BUG_TARGET) == []


def test_relative_markdown_link(tmp_path):
    repo = _make_repo(tmp_path)
    good = _with_solution(_bug(), "See [the other](../skill-design/progressive-disclosure.md).")
    assert _check(repo, good, BUG_TARGET) == []
    bad = _with_solution(_bug(), "See [nothing](./missing.md).")
    _only(_check(repo, bad, BUG_TARGET), "missing.md")
    web = _with_solution(_bug(), "See [web](https://example.com/x.md) and [here](#top).")
    assert _check(repo, web, BUG_TARGET) == []


# ---------------------------------------------------------------------------
# validator: scaffold
# ---------------------------------------------------------------------------


def test_learning_n_scaffold(tmp_path):
    repo = _make_repo(tmp_path)
    text = _with_solution(_bug(), "As in Learning 2 above.")
    _only(_check(repo, text, BUG_TARGET), "Learning")


def test_template_braces_in_prose_but_not_in_a_fence(tmp_path):
    repo = _make_repo(tmp_path)
    prose = _with_solution(_bug(), "The title is {{title}} here.")
    _only(_check(repo, prose, BUG_TARGET), "{{")
    fenced = _with_solution(_bug(), "```\n{{title}} and TODO\n```")
    assert _check(repo, fenced, BUG_TARGET) == []
    span = _with_solution(_bug(), "Write `{{title}}` and `TODO`.")
    assert _check(repo, span, BUG_TARGET) == []


def test_todo_and_placeholders(tmp_path):
    repo = _make_repo(tmp_path)
    _only(_check(repo, _with_solution(_bug(), "TODO write this"), BUG_TARGET), "TODO")
    _only(_check(repo, _with_solution(_bug(), "[Clear description here]"), BUG_TARGET), "[Clear")


# ---------------------------------------------------------------------------
# finder
# ---------------------------------------------------------------------------


def _doc(path: Path, *, title: str, tags: list[str] | None = None, body: str = "Body.\n", extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = f"---\ntitle: {title}\nmodule: core\n"
    if tags:
        fm += "tags:\n" + "".join(f"  - {t}\n" for t in tags)
    fm += extra + "---\n"
    path.write_text(fm + body)


def test_finder_ranks_by_matched_keyword_count(tmp_path):
    store = tmp_path / "solutions"
    _doc(store / "a" / "one.md", title="Alpha only")
    _doc(store / "a" / "two.md", title="Alpha beta together")
    _doc(store / "b" / "three.md", title="Gamma", tags=["alpha"])
    _doc(store / "b" / "four.md", title="Unrelated")
    found = _find_related.find_related(store, ["Alpha", "beta", "alpha"])
    assert [c.path.name for c in found] == ["two.md", "one.md", "three.md"]
    assert [c.score for c in found] == [2, 1, 1]
    assert found[0].matched == ["alpha", "beta"]


def test_finder_matches_block_list_tags_and_store_root(tmp_path):
    store = tmp_path / "solutions"
    _doc(store / "cat" / "t.md", title="Plain", tags=["structured-output", "schema"])
    _doc(store / "root.md", title="Root doc", tags=["structured-output"])
    found = _find_related.find_related(store, ["structured"])
    assert {c.path.name for c in found} == {"t.md", "root.md"}


def test_finder_narrows_thirty_hits_to_twenty_five(tmp_path):
    store = tmp_path / "solutions"
    for i in range(30):
        _doc(store / "c" / f"d{i:02d}.md", title=f"Alpha note {i}")
    found = _find_related.find_related(store, ["alpha"])
    assert len(found) == 25
    assert [c.path.name for c in found] == [f"d{i:02d}.md" for i in range(25)]


def test_finder_broadens_to_body_text_below_three_hits(tmp_path):
    store = tmp_path / "solutions"
    _doc(store / "c" / "front.md", title="Needle in the title")
    _doc(store / "c" / "body.md", title="Other", body="Talks about the needle here.\n")
    _doc(store / "c" / "none.md", title="Nothing")
    found = _find_related.find_related(store, ["needle"])
    assert [c.path.name for c in found] == ["front.md", "body.md"]

    for i in range(3):
        _doc(store / "c" / f"more{i}.md", title=f"needle {i}")
    found = _find_related.find_related(store, ["needle"])
    assert "body.md" not in {c.path.name for c in found}
    assert len(found) == 4


def test_finder_prints_thirty_line_heads(tmp_path, capsys):
    store = tmp_path / "solutions"
    _doc(store / "c" / "long.md", title="Needle", body="".join(f"row {i}\n" for i in range(60)))
    assert _find_related.main([str(store), "needle"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"=== {store / 'c' / 'long.md'} (matched: needle)"
    assert len(out) == 1 + 30
    assert out[1] == "---"


def test_finder_runs_as_a_script_and_handles_a_missing_store(tmp_path):
    script = Path(_find_related.__file__)
    proc = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "missing"), "x"], capture_output=True, text=True
    )
    assert proc.returncode == 0 and proc.stdout == ""
    assert _find_related.find_related(tmp_path / "missing", ["x"]) == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

CLI = Path(_learnings.__file__).with_name("_validate_learning.py")


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True)


def test_cli_exit_codes(tmp_path):
    repo = _make_repo(tmp_path)
    doc = tmp_path / "proposal.md"
    doc.write_text(_bug())

    ok = _cli(str(doc), "--repo", str(repo), "--target", BUG_TARGET)
    assert ok.returncode == 0, ok.stdout + ok.stderr

    doc.write_text(_with_solution(_bug(), "See `norn/nope.py:3`."))
    bad = _cli(str(doc), "--repo", str(repo), "--target", BUG_TARGET)
    assert bad.returncode == 1
    assert "norn/nope.py:3" in bad.stdout

    assert _cli(str(tmp_path / "absent.md"), "--repo", str(repo), "--target", BUG_TARGET).returncode == 2
    assert _cli(str(doc)).returncode == 2


def test_cli_store_config_error_is_a_usage_error(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES / "bad-root", repo)
    doc = tmp_path / "proposal.md"
    doc.write_text(_bug())
    proc = _cli(str(doc), "--repo", str(repo), "--target", "outside/solutions/x/y.md")
    assert proc.returncode == 2
    assert "docs_root" in proc.stderr


def test_cli_validates_an_update_of_an_existing_target(tmp_path):
    repo = _make_repo(tmp_path)
    existing = ROOT_NOTE_FIXTURE.read_text()
    doc = tmp_path / "proposal.md"
    doc.write_text(existing)
    proc = _cli(str(doc), "--repo", str(repo), "--target", "docs/solutions/root-note.md")
    assert proc.returncode == 0, proc.stdout + proc.stderr
