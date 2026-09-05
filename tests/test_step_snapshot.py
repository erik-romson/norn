"""Tests for norn/pipelines/_step_snapshot.py.

Pure-function tests use handcrafted ``git status -z`` byte strings.
Subprocess tests build a throwaway git repo with gitpython.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from git import Repo

from norn.pipelines._step_snapshot import (
    build_snapshot,
    diff_snapshots,
    hook_fixes,
    main,
    parse_status_z,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> Repo:
    """Create a git repo with local identity and an initial commit."""
    repo = Repo.init(path)
    repo.config_writer().set_value("user", "name", "Test User").release()
    repo.config_writer().set_value("user", "email", "test@example.com").release()
    readme = path / "README.md"
    readme.write_text("# test\n")
    repo.index.add(["README.md"])
    repo.index.commit("initial commit")
    return repo


def _z(*entries: tuple[str, str], renames: dict[str, str] | None = None) -> bytes:
    """Build a ``git status -z`` byte payload from ``(status, path)`` pairs.

    *renames* maps new-path → orig-path for entries whose status contains
    R or C so the orig-path field is emitted after the new-path field.
    """
    renames = renames or {}
    out = bytearray()
    for status, path in entries:
        out += f"{status} {path}".encode("utf-8", errors="surrogateescape") + b"\0"
        if path in renames:
            out += renames[path].encode("utf-8", errors="surrogateescape") + b"\0"
    return bytes(out)


def _snapshot(tmp_path: Path, root: Path, name: str) -> str:
    """Run the snapshot subcommand and return the path to the JSON file."""
    out = str(tmp_path / name)
    rc = main(["_step_snapshot.py", "snapshot", "--root", str(root), out])
    assert rc == 0, f"snapshot returned {rc}"
    return out


def _diff_output(capsysbinary, pre: str, post: str, extra: list[str] | None = None) -> list[str]:
    """Run the diff subcommand and return the list of changed paths."""
    args = ["_step_snapshot.py", "diff", pre, post] + (extra or [])
    rc = main(args)
    assert rc == 0, f"diff returned {rc}"
    captured = capsysbinary.readouterr().out
    return [p.decode("utf-8", errors="surrogateescape") for p in captured.split(b"\0") if p]


# ---------------------------------------------------------------------------
# parse_status_z — pure unit tests
# ---------------------------------------------------------------------------


class TestParseStatusZ:
    def test_ordinary_paths(self) -> None:
        data = _z((" M", "foo.py"), ("M ", "bar.py"), ("??", "new.txt"))
        result = parse_status_z(data)
        assert result == [(" M", "foo.py"), ("M ", "bar.py"), ("??", "new.txt")]

    def test_rename_reports_new_name(self) -> None:
        data = _z(("R ", "new.py"), renames={"new.py": "old.py"})
        result = parse_status_z(data)
        assert result == [("R ", "new.py")]

    def test_rename_in_worktree_column_reports_new_name(self) -> None:
        # RM = renamed in index, modified in worktree
        data = _z(("RM", "renamed.py"), renames={"renamed.py": "orig.py"})
        result = parse_status_z(data)
        assert result == [("RM", "renamed.py")]

    def test_copy_reports_new_name(self) -> None:
        data = _z(("C ", "copy.py"), renames={"copy.py": "src.py"})
        result = parse_status_z(data)
        assert result == [("C ", "copy.py")]

    def test_path_with_space(self) -> None:
        data = _z((" M", "my file.py"))
        result = parse_status_z(data)
        assert result == [(" M", "my file.py")]

    def test_unicode_path(self) -> None:
        data = _z(("??", "café.py"))
        result = parse_status_z(data)
        assert result == [("??", "café.py")]

    def test_path_containing_newline(self) -> None:
        # git -z does not escape newlines; they appear literally in the path bytes
        raw = b" M foo\nbar.py\0"
        result = parse_status_z(raw)
        assert result == [(" M", "foo\nbar.py")]

    def test_empty_input(self) -> None:
        assert parse_status_z(b"") == []

    def test_multiple_renames_do_not_misalign(self) -> None:
        data = _z(
            ("R ", "b.py"),
            ("R ", "d.py"),
            renames={"b.py": "a.py", "d.py": "c.py"},
        )
        result = parse_status_z(data)
        assert result == [("R ", "b.py"), ("R ", "d.py")]


# ---------------------------------------------------------------------------
# build_snapshot — pure unit tests (with a real temp dir for file reads)
# ---------------------------------------------------------------------------


class TestBuildSnapshot:
    def test_existing_file_gets_sha1_digest(self, tmp_path: Path) -> None:
        (tmp_path / "foo.py").write_bytes(b"hello")
        entries = [(" M", "foo.py")]
        snap = build_snapshot(entries, str(tmp_path))
        assert snap["foo.py"][0] == " M"
        import hashlib
        assert snap["foo.py"][1] == hashlib.sha1(b"hello").hexdigest()

    def test_missing_file_records_sentinel(self, tmp_path: Path) -> None:
        entries = [("D ", "gone.py")]
        snap = build_snapshot(entries, str(tmp_path))
        assert snap["gone.py"] == ["D ", "-"]

    def test_same_status_different_content_gives_different_digests(self, tmp_path: Path) -> None:
        f = tmp_path / "file.py"
        f.write_bytes(b"version 1")
        snap1 = build_snapshot([(" M", "file.py")], str(tmp_path))
        f.write_bytes(b"version 2")
        snap2 = build_snapshot([(" M", "file.py")], str(tmp_path))
        assert snap1["file.py"][1] != snap2["file.py"][1]

    def test_empty_entries(self, tmp_path: Path) -> None:
        assert build_snapshot([], str(tmp_path)) == {}


# ---------------------------------------------------------------------------
# diff_snapshots — pure unit tests
# ---------------------------------------------------------------------------


class TestDiffSnapshots:
    def test_identical_snapshots_are_empty(self) -> None:
        snap = {"a.py": [" M", "abc123"]}
        assert diff_snapshots(snap, snap) == []

    def test_status_change_is_reported(self) -> None:
        pre = {"a.py": [" M", "abc123"]}
        post = {"a.py": ["M ", "abc123"]}
        assert diff_snapshots(pre, post) == ["a.py"]

    def test_content_change_same_status_is_reported(self) -> None:
        """P0-2b regression: same status but different digest must be reported."""
        pre = {"a.py": [" M", "aaa"]}
        post = {"a.py": [" M", "bbb"]}
        assert diff_snapshots(pre, post) == ["a.py"]

    def test_appearing_path_is_reported(self) -> None:
        pre: dict = {}
        post = {"new.py": ["??", "abc"]}
        assert diff_snapshots(pre, post) == ["new.py"]

    def test_disappearing_path_is_reported(self) -> None:
        pre = {"old.py": [" M", "abc"]}
        post: dict = {}
        assert diff_snapshots(pre, post) == ["old.py"]

    def test_result_is_sorted(self) -> None:
        pre: dict = {}
        post = {"z.py": ["??", "1"], "a.py": ["??", "2"], "m.py": ["??", "3"]}
        assert diff_snapshots(pre, post) == ["a.py", "m.py", "z.py"]


# ---------------------------------------------------------------------------
# hook_fixes — pure unit tests (mirrors v1 test_pipelines.py coverage)
# ---------------------------------------------------------------------------


class TestHookFixes:
    def _entries(self, *status_path_pairs: tuple[str, str]) -> list[tuple[str, str]]:
        return list(status_path_pairs)

    def test_mm_am_rm_cm_are_included(self) -> None:
        """The four hook-fix shapes from test_pipelines.py, expressed as -z entries."""
        entries = parse_status_z(
            _z(
                ("MM", "modified.py"),
                ("AM", "added.py"),
                ("RM", "renamed.py"),
                ("CM", "copied.py"),
                renames={"renamed.py": "old.py", "copied.py": "orig.py"},
            )
        )
        result = hook_fixes(entries)
        assert result == ["added.py", "copied.py", "modified.py", "renamed.py"]

    def test_untracked_is_excluded(self) -> None:
        entries = parse_status_z(_z(("??", "scratch.txt"), ("MM", "staged.py")))
        assert hook_fixes(entries) == ["staged.py"]

    def test_worktree_only_modification_is_excluded(self) -> None:
        """`space-M` means nothing staged — somebody else's dirty file."""
        entries = parse_status_z(_z((" M", "dirty.py"), ("MM", "staged.py")))
        assert hook_fixes(entries) == ["staged.py"]

    def test_cleanly_staged_paths_are_excluded(self) -> None:
        entries = parse_status_z(_z(("M ", "clean.py"), ("A ", "new.py"), ("D ", "gone.py")))
        assert hook_fixes(entries) == []

    def test_staged_then_deleted_is_excluded(self) -> None:
        """`MD` = staged then deleted from worktree, not a hook rewrite."""
        entries = parse_status_z(_z(("MD", "vanished.py")))
        assert hook_fixes(entries) == []

    def test_result_is_sorted(self) -> None:
        entries = parse_status_z(_z(("MM", "z.py"), ("AM", "a.py")))
        assert hook_fixes(entries) == ["a.py", "z.py"]


# ---------------------------------------------------------------------------
# Subprocess / CLI tests using a real temp git repo
# ---------------------------------------------------------------------------


class TestSnapshotSubcommand:
    """Tests for the `snapshot` subcommand against a real repo."""

    def test_clean_repo_produces_empty_snapshot(self, tmp_path: Path) -> None:
        _init_repo(tmp_path / "repo")
        out = str(tmp_path / "snap.json")
        rc = main(["_step_snapshot.py", "snapshot", "--root", str(tmp_path / "repo"), out])
        assert rc == 0
        assert json.loads(Path(out).read_text()) == {}

    def test_untracked_file_appears_in_snapshot(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        root = Path(repo.working_tree_dir)
        (root / "new.py").write_bytes(b"new")
        out = str(tmp_path / "snap.json")
        rc = main(["_step_snapshot.py", "snapshot", "--root", str(root), out])
        assert rc == 0
        snap = json.loads(Path(out).read_text())
        assert "new.py" in snap
        assert snap["new.py"][0] == "??"

    def test_modified_tracked_file_appears_in_snapshot(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        root = Path(repo.working_tree_dir)
        (root / "README.md").write_bytes(b"modified")
        out = str(tmp_path / "snap.json")
        rc = main(["_step_snapshot.py", "snapshot", "--root", str(root), out])
        assert rc == 0
        snap = json.loads(Path(out).read_text())
        assert "README.md" in snap
        assert snap["README.md"][0] == " M"

    def test_missing_root_arg_exits_2(self, tmp_path: Path) -> None:
        rc = main(["_step_snapshot.py", "snapshot", str(tmp_path / "out.json")])
        assert rc == 2

    def test_missing_out_arg_exits_2(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        rc = main(["_step_snapshot.py", "snapshot", "--root", str(repo.working_tree_dir)])
        assert rc == 2


class TestDiffSubcommand:
    """Tests for the `diff` subcommand."""

    def test_same_status_changed_content_is_reported(
        self, tmp_path: Path, capsysbinary
    ) -> None:
        """P0-2b regression: ` M` → ` M` with different bytes must appear in diff."""
        repo = _init_repo(tmp_path / "repo")
        root = Path(repo.working_tree_dir)

        # First modification — worktree-modified, status = " M"
        (root / "README.md").write_bytes(b"version 1")
        pre = _snapshot(tmp_path, root, "pre.json")

        # Second modification — status still " M", but content changed
        (root / "README.md").write_bytes(b"version 2")
        post = _snapshot(tmp_path, root, "post.json")

        changed = _diff_output(capsysbinary, pre, post)
        assert "README.md" in changed

    def test_unchanged_dirty_file_is_not_reported(
        self, tmp_path: Path, capsysbinary
    ) -> None:
        """Snapshot twice with no edit between — diff must be empty."""
        repo = _init_repo(tmp_path / "repo")
        root = Path(repo.working_tree_dir)

        (root / "README.md").write_bytes(b"dirty")
        pre = _snapshot(tmp_path, root, "pre.json")
        post = _snapshot(tmp_path, root, "post.json")

        changed = _diff_output(capsysbinary, pre, post)
        assert changed == []

    def test_appearing_untracked_file_is_reported(
        self, tmp_path: Path, capsysbinary
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        root = Path(repo.working_tree_dir)

        pre = _snapshot(tmp_path, root, "pre.json")
        (root / "new.py").write_bytes(b"new")
        post = _snapshot(tmp_path, root, "post.json")

        changed = _diff_output(capsysbinary, pre, post)
        assert "new.py" in changed

    def test_reverted_file_is_reported(self, tmp_path: Path, capsysbinary) -> None:
        """A file that disappears from status (reverted) between snapshots is reported."""
        repo = _init_repo(tmp_path / "repo")
        root = Path(repo.working_tree_dir)

        # Dirty the file so it shows in status
        (root / "README.md").write_bytes(b"dirty")
        pre = _snapshot(tmp_path, root, "pre.json")

        # Revert — file no longer appears in status (clean)
        (root / "README.md").write_bytes(b"# test\n")
        post = _snapshot(tmp_path, root, "post.json")

        changed = _diff_output(capsysbinary, pre, post)
        assert "README.md" in changed

    def test_deleted_file_records_sentinel_and_is_reported(
        self, tmp_path: Path, capsysbinary
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        root = Path(repo.working_tree_dir)

        # Stage a new file, snapshot, then delete it
        new_file = root / "soon_gone.py"
        new_file.write_bytes(b"temporary")
        repo.index.add(["soon_gone.py"])

        pre = _snapshot(tmp_path, root, "pre.json")
        pre_data = json.loads(Path(pre).read_text())
        # File exists — digest is not the sentinel
        assert pre_data["soon_gone.py"][1] != "-"

        # Delete the file from the worktree
        new_file.unlink()
        post = _snapshot(tmp_path, root, "post.json")
        post_data = json.loads(Path(post).read_text())
        # File was deleted from worktree — sentinel recorded
        assert post_data["soon_gone.py"][1] == "-"

        changed = _diff_output(capsysbinary, pre, post)
        assert "soon_gone.py" in changed

    def test_ignore_drops_exact_path(self, tmp_path: Path, capsysbinary) -> None:
        repo = _init_repo(tmp_path / "repo")
        root = Path(repo.working_tree_dir)

        pre = _snapshot(tmp_path, root, "pre.json")
        (root / "a.py").write_bytes(b"a")
        (root / "b.py").write_bytes(b"b")
        post = _snapshot(tmp_path, root, "post.json")

        changed = _diff_output(capsysbinary, pre, post, ["--ignore", "a.py"])
        assert "a.py" not in changed
        assert "b.py" in changed

    def test_ignore_file_drops_all_listed_paths(
        self, tmp_path: Path, capsysbinary
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        root = Path(repo.working_tree_dir)

        pre = _snapshot(tmp_path, root, "pre.json")
        (root / "a.py").write_bytes(b"a")
        (root / "b.py").write_bytes(b"b")
        (root / "c.py").write_bytes(b"c")
        post = _snapshot(tmp_path, root, "post.json")

        # Write a NUL-separated ignore list
        ignore_file = str(tmp_path / "ignore.nul")
        Path(ignore_file).write_bytes(b"a.py\0b.py\0")

        changed = _diff_output(capsysbinary, pre, post, ["--ignore-file", ignore_file])
        assert "a.py" not in changed
        assert "b.py" not in changed
        assert "c.py" in changed

    def test_ignore_file_missing_exits_2(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        root = Path(repo.working_tree_dir)
        pre = _snapshot(tmp_path, root, "pre.json")
        post = _snapshot(tmp_path, root, "post.json")
        rc = main([
            "_step_snapshot.py", "diff", pre, post,
            "--ignore-file", str(tmp_path / "no_such_file.nul"),
        ])
        assert rc == 2

    def test_missing_pre_exits_2(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        root = Path(repo.working_tree_dir)
        post = _snapshot(tmp_path, root, "post.json")
        rc = main([
            "_step_snapshot.py", "diff",
            str(tmp_path / "nonexistent_pre.json"),
            post,
        ])
        assert rc == 2

    def test_missing_post_exits_2(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        root = Path(repo.working_tree_dir)
        pre = _snapshot(tmp_path, root, "pre.json")
        rc = main([
            "_step_snapshot.py", "diff",
            pre,
            str(tmp_path / "nonexistent_post.json"),
        ])
        assert rc == 2

    def test_missing_positional_args_exits_2(self, tmp_path: Path) -> None:
        rc = main(["_step_snapshot.py", "diff"])
        assert rc == 2


class TestHookFixesSubcommand:
    """Tests for the `hook-fixes` subcommand against a real repo."""

    def test_partially_staged_file_is_reported(
        self, tmp_path: Path, capsysbinary
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        root = Path(repo.working_tree_dir)

        # Stage a modification
        (root / "README.md").write_bytes(b"staged content")
        repo.index.add(["README.md"])
        # Then modify again in the worktree (simulating a hook rewrite)
        (root / "README.md").write_bytes(b"hook rewrote this")

        rc = main(["_step_snapshot.py", "hook-fixes", "--root", str(root)])
        assert rc == 0
        out = capsysbinary.readouterr().out
        paths = [p.decode() for p in out.split(b"\0") if p]
        assert "README.md" in paths

    def test_clean_worktree_gives_empty_output(
        self, tmp_path: Path, capsysbinary
    ) -> None:
        _init_repo(tmp_path / "repo")
        rc = main(["_step_snapshot.py", "hook-fixes", "--root", str(tmp_path / "repo")])
        assert rc == 0
        assert capsysbinary.readouterr().out == b""

    def test_untracked_file_is_not_reported(
        self, tmp_path: Path, capsysbinary
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        root = Path(repo.working_tree_dir)
        (root / "scratch.txt").write_bytes(b"untracked")

        rc = main(["_step_snapshot.py", "hook-fixes", "--root", str(root)])
        assert rc == 0
        out = capsysbinary.readouterr().out
        paths = [p.decode() for p in out.split(b"\0") if p]
        assert "scratch.txt" not in paths

    def test_missing_root_arg_exits_2(self) -> None:
        rc = main(["_step_snapshot.py", "hook-fixes"])
        assert rc == 2


# ---------------------------------------------------------------------------
# Top-level CLI usage errors
# ---------------------------------------------------------------------------


class TestCLIUsageErrors:
    def test_no_args_exits_2(self) -> None:
        rc = main(["_step_snapshot.py"])
        assert rc == 2

    def test_unknown_subcommand_exits_2(self) -> None:
        rc = main(["_step_snapshot.py", "frobnicate"])
        assert rc == 2
