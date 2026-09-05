"""Unit tests for norn/pipelines/_preplan.py.

All tests are offline: .md files are created under pytest's tmp_path and
repo_dir is set to str(tmp_path) so nothing depends on process cwd.
"""
from __future__ import annotations

import pytest

from norn.pipelines._preplan import (
    derive_paths,
    parse_arg_flags,
    resolve_preplan,
    slug_of,
    steps_dir_of,
)


# ---------------------------------------------------------------------------
# resolve_preplan
# ---------------------------------------------------------------------------


def test_resolve_preplan_cli_shape_absolute(tmp_path):
    """Standard norn-run CLI shape with an absolute pre-plan path resolves."""
    preplan = tmp_path / "x-preplan.md"
    preplan.write_text("# pre-plan")

    result = resolve_preplan(
        ["run", "plan_with_review", str(preplan), "--arg", "model=sonnet"],
        repo_dir=str(tmp_path),
    )
    assert result == str(preplan.resolve())
    assert result == str(preplan)  # already absolute


def test_resolve_preplan_cli_shape_relative(tmp_path):
    """CLI shape with a relative path resolves via repo_dir."""
    preplan = tmp_path / "x-preplan.md"
    preplan.write_text("# pre-plan")

    result = resolve_preplan(
        ["run", "plan_with_review", "x-preplan.md", "--arg", "model=sonnet"],
        repo_dir=str(tmp_path),
    )
    assert result == str(preplan.resolve())


def test_resolve_preplan_tui_shape(tmp_path):
    """TUI shape (bare absolute path only) resolves correctly."""
    preplan = tmp_path / "x-preplan.md"
    preplan.write_text("# pre-plan")

    result = resolve_preplan([str(preplan)], repo_dir=str(tmp_path))
    assert result == str(preplan.resolve())


def test_resolve_preplan_skip_value_not_a_candidate(tmp_path):
    """The value token after --skip is not treated as a file candidate."""
    preplan = tmp_path / "x-preplan.md"
    preplan.write_text("# pre-plan")

    result = resolve_preplan(
        ["run", "p", "--skip", "check open questions", str(preplan)],
        repo_dir=str(tmp_path),
    )
    assert result == str(preplan.resolve())


def test_resolve_preplan_arg_equals_form_dropped(tmp_path):
    """--arg=key=value (equals form) is a single token and does not break parsing."""
    preplan = tmp_path / "x-preplan.md"
    preplan.write_text("# pre-plan")

    result = resolve_preplan(
        ["run", "plan_with_review", "--arg=model=sonnet", str(preplan)],
        repo_dir=str(tmp_path),
    )
    assert result == str(preplan.resolve())


def test_resolve_preplan_zero_candidates_raises(tmp_path):
    """No .md file on argv raises ValueError with a message about positional arg."""
    with pytest.raises(ValueError, match="positional"):
        resolve_preplan(["run", "plan_with_review"], repo_dir=str(tmp_path))


def test_resolve_preplan_non_md_file_ignored(tmp_path):
    """An existing .txt file is not a candidate; raises the zero-candidate error."""
    txt = tmp_path / "brief.txt"
    txt.write_text("content")

    with pytest.raises(ValueError, match="positional"):
        resolve_preplan([str(txt)], repo_dir=str(tmp_path))


def test_resolve_preplan_nonexistent_md_ignored(tmp_path):
    """A .md path that does not exist is not a candidate."""
    with pytest.raises(ValueError, match="positional"):
        resolve_preplan([str(tmp_path / "ghost.md")], repo_dir=str(tmp_path))


def test_resolve_preplan_two_candidates_raises(tmp_path):
    """Two existing .md files raise ValueError naming both."""
    a = tmp_path / "a-preplan.md"
    b = tmp_path / "b-preplan.md"
    a.write_text("# a")
    b.write_text("# b")

    with pytest.raises(ValueError) as exc_info:
        resolve_preplan([str(a), str(b)], repo_dir=str(tmp_path))

    msg = str(exc_info.value)
    assert str(a) in msg
    assert str(b) in msg


def test_resolve_preplan_result_is_absolute(tmp_path):
    """resolve_preplan always returns an absolute path."""
    preplan = tmp_path / "x-preplan.md"
    preplan.write_text("# pre-plan")

    result = resolve_preplan(
        ["run", "plan_with_review", "x-preplan.md"],
        repo_dir=str(tmp_path),
    )
    assert result.startswith("/")


# ---------------------------------------------------------------------------
# parse_arg_flags
# ---------------------------------------------------------------------------


def test_parse_arg_flags_multiple():
    result = parse_arg_flags(["--arg", "model=sonnet", "--arg", "codex_model=gpt-5-codex"])
    assert result == {"model": "sonnet", "codex_model": "gpt-5-codex"}


def test_parse_arg_flags_value_with_equals():
    """A value containing = keeps everything after the first =."""
    result = parse_arg_flags(["--arg", "cmd=a=b"])
    assert result == {"cmd": "a=b"}


def test_parse_arg_flags_empty():
    assert parse_arg_flags([]) == {}


# ---------------------------------------------------------------------------
# slug_of
# ---------------------------------------------------------------------------


def test_slug_of_dash_preplan():
    assert slug_of("tmp/norn-fleet-preplan.md") == "norn-fleet"


def test_slug_of_underscore_preplan():
    assert slug_of("tmp/norn_fleet_preplan.md") == "norn_fleet"


def test_slug_of_no_suffix():
    assert slug_of("tmp/brief.md") == "brief"


# ---------------------------------------------------------------------------
# derive_paths
# ---------------------------------------------------------------------------


def test_derive_paths_suffixes(tmp_path):
    preplan = tmp_path / "norn-fleet-preplan.md"
    preplan.write_text("# pre-plan")

    plan, questions, review, response = derive_paths(str(preplan))

    assert plan == str(tmp_path / "norn-fleet-final-plan.md")
    assert questions == str(tmp_path / "norn-fleet-plan-questions.md")
    assert review == str(tmp_path / "norn-fleet-plan-review.md")
    assert response == str(tmp_path / "norn-fleet-plan-review-response.md")


def test_derive_paths_plan_never_doubles_the_plan_suffix(tmp_path):
    """A pre-plan named `<x>-plan.md` must not yield `<x>-plan-plan.md`.

    Only `-preplan`/`_preplan` is stripped from the stem, so the deliverable
    used to read like a second copy of the brief it came from.
    """
    preplan = tmp_path / "github-builds-plan.md"
    preplan.write_text("# pre-plan")

    plan, *_ = derive_paths(str(preplan))

    assert plan == str(tmp_path / "github-builds-plan-final-plan.md")
    assert plan != str(preplan)


def test_derive_paths_review_and_response_distinct(tmp_path):
    preplan = tmp_path / "x-preplan.md"
    preplan.write_text("# pre-plan")

    _, _, review, response = derive_paths(str(preplan))

    assert review != response
    assert review.endswith("-plan-review.md")
    assert response.endswith("-plan-review-response.md")


def test_derive_paths_are_absolute(tmp_path):
    preplan = tmp_path / "x-preplan.md"
    preplan.write_text("# pre-plan")

    paths = derive_paths(str(preplan))
    for p in paths:
        assert p.startswith("/"), f"Expected absolute path, got: {p}"


def test_derive_paths_sibling_of_preplan(tmp_path):
    sub = tmp_path / "plans"
    sub.mkdir()
    preplan = sub / "my-preplan.md"
    preplan.write_text("# pre-plan")

    plan, questions, review, response = derive_paths(str(preplan))
    for p in (plan, questions, review, response):
        assert p.startswith(str(sub)), f"Expected sibling under {sub}, got: {p}"


# ---------------------------------------------------------------------------
# steps_dir_of
# ---------------------------------------------------------------------------


def test_steps_dir_of_drops_the_md_suffix(tmp_path):
    plan = tmp_path / "norn-fleet-final-plan.md"

    assert steps_dir_of(str(plan)) == str(tmp_path / "norn-fleet-final-plan")


def test_steps_dir_of_is_sibling_of_plan():
    assert steps_dir_of("tmp/x-plan.md") == "tmp/x-plan"
