"""Tests for norn/pipelines/_jira_issue.py."""
import pytest

from norn.pipelines._jira_issue import (
    BRIEF_HEADINGS,
    ArtifactPaths,
    artifact_paths,
    brief_prompt,
    resolve_issue_args,
)


# ---------------------------------------------------------------------------
# resolve_issue_args
# ---------------------------------------------------------------------------


class TestResolveIssueArgs:
    def test_full_norn_run_shape(self):
        key, stop = resolve_issue_args(["run", "fix_jira_issue_v1", "CBS-2249"])
        assert key == "CBS-2249"
        assert stop is False

    def test_bare_key_shape(self):
        key, stop = resolve_issue_args(["CBS-2249"])
        assert key == "CBS-2249"
        assert stop is False

    def test_url_form(self):
        key, stop = resolve_issue_args(["https://jira.example.com/browse/CBS-2249"])
        assert key == "CBS-2249"
        assert stop is False

    def test_stop_flag(self):
        key, stop = resolve_issue_args(["CBS-2249", "stop"])
        assert key == "CBS-2249"
        assert stop is True

    def test_stop_flag_before_key(self):
        key, stop = resolve_issue_args(["stop", "CBS-2249"])
        assert key == "CBS-2249"
        assert stop is True

    def test_flags_and_values_skipped(self):
        key, stop = resolve_issue_args(
            ["run", "fix_jira_issue_v1", "CBS-2249", "--arg", "model=opus"]
        )
        assert key == "CBS-2249"
        assert stop is False

    def test_flag_with_equals_skipped(self):
        key, stop = resolve_issue_args(["CBS-2249", "--org=myorg"])
        assert key == "CBS-2249"
        assert stop is False

    def test_zero_keys_raises(self):
        with pytest.raises(ValueError, match="needs a Jira issue key"):
            resolve_issue_args(["run", "fix_jira_issue_v1"])

    def test_two_keys_raises(self):
        with pytest.raises(ValueError, match="multiple issue keys"):
            resolve_issue_args(["CBS-2249", "PROJ-1"])

    def test_lowercase_not_treated_as_key(self):
        # "cbs-2249" is lowercase — should not match
        with pytest.raises(ValueError):
            resolve_issue_args(["cbs-2249"])

    def test_subcommand_and_pipeline_name_ignored(self):
        # "run" and "fix_jira_issue" must not be mistaken for keys
        key, _ = resolve_issue_args(["run", "fix_jira_issue_v1", "CBS-2249"])
        assert key == "CBS-2249"

    def test_url_with_stop(self):
        key, stop = resolve_issue_args(
            ["https://jira.example.com/browse/CBS-2250", "stop"]
        )
        assert key == "CBS-2250"
        assert stop is True


# ---------------------------------------------------------------------------
# artifact_paths
# ---------------------------------------------------------------------------


class TestArtifactPaths:
    def test_returns_namedtuple(self, tmp_path):
        ap = artifact_paths(str(tmp_path), "CBS-2249")
        assert isinstance(ap, ArtifactPaths)

    def test_dir_under_tmp_jira_key(self, tmp_path):
        ap = artifact_paths(str(tmp_path), "CBS-2249")
        expected_dir = str(tmp_path / "tmp" / "jira" / "CBS-2249") + "/"
        assert ap.dir == expected_dir

    def test_issue_json(self, tmp_path):
        ap = artifact_paths(str(tmp_path), "CBS-2249")
        assert ap.issue_json == str(tmp_path / "tmp" / "jira" / "CBS-2249" / "issue.json")

    def test_issue_md(self, tmp_path):
        ap = artifact_paths(str(tmp_path), "CBS-2249")
        assert ap.issue_md == str(tmp_path / "tmp" / "jira" / "CBS-2249" / "issue.md")

    def test_attachments(self, tmp_path):
        ap = artifact_paths(str(tmp_path), "CBS-2249")
        assert ap.attachments == str(tmp_path / "tmp" / "jira" / "CBS-2249" / "attachments")

    def test_preplan(self, tmp_path):
        ap = artifact_paths(str(tmp_path), "CBS-2249")
        assert ap.preplan == str(tmp_path / "tmp" / "jira" / "CBS-2249" / "CBS-2249-preplan.md")

    def test_paths_are_absolute(self, tmp_path):
        ap = artifact_paths(str(tmp_path), "CBS-2249")
        for field in (ap.issue_json, ap.issue_md, ap.attachments, ap.preplan):
            assert field.startswith("/"), f"Expected absolute path, got: {field}"


# ---------------------------------------------------------------------------
# brief_prompt
# ---------------------------------------------------------------------------


class TestBriefPrompt:
    def _make_prompt(self, tmp_path):
        return brief_prompt(
            issue_md="/abs/issue.md",
            attachments="/abs/attachments",
            out="/abs/CBS-2249-preplan.md",
            project_dir=str(tmp_path),
        )

    def test_mentions_issue_md(self, tmp_path):
        prompt = self._make_prompt(tmp_path)
        assert "/abs/issue.md" in prompt

    def test_mentions_attachments(self, tmp_path):
        prompt = self._make_prompt(tmp_path)
        assert "/abs/attachments" in prompt

    def test_mentions_output_path(self, tmp_path):
        prompt = self._make_prompt(tmp_path)
        assert "/abs/CBS-2249-preplan.md" in prompt

    def test_contains_all_brief_headings(self, tmp_path):
        prompt = self._make_prompt(tmp_path)
        for heading in BRIEF_HEADINGS:
            assert heading in prompt, f"Missing heading: {heading}"

    def test_brief_headings_nonempty(self):
        assert len(BRIEF_HEADINGS) > 0

    def test_mentions_project_dir(self, tmp_path):
        prompt = self._make_prompt(tmp_path)
        assert str(tmp_path) in prompt


# ---------------------------------------------------------------------------
# resolve_issue_args: pipeline name in messages
# ---------------------------------------------------------------------------


class TestResolveIssueArgsPipelineName:
    def test_default_pipeline_name_is_v1(self):
        with pytest.raises(ValueError, match="fix_jira_issue_v1 needs"):
            resolve_issue_args([])

    def test_passed_pipeline_reaches_zero_key_message(self):
        with pytest.raises(ValueError) as exc:
            resolve_issue_args([], pipeline="fix_jira_issue_v2")
        assert "fix_jira_issue_v2 needs" in str(exc.value)
        assert "norn run fix_jira_issue_v2 CBS-2249" in str(exc.value)
        assert "fix_jira_issue_v1" not in str(exc.value)

    def test_passed_pipeline_reaches_multiple_keys_message(self):
        with pytest.raises(ValueError) as exc:
            resolve_issue_args(["CBS-1", "CBS-2"], pipeline="fix_jira_issue_v2")
        assert str(exc.value).startswith("fix_jira_issue_v2 found multiple")
