from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from git import Repo

from norn.worktree import MergeResult, WorktreeError, WorktreeSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(path: str | Path) -> Repo:
    """Create a git repo with local identity and an initial commit."""
    repo = Repo.init(path)
    repo.config_writer().set_value("user", "name", "Test User").release()
    repo.config_writer().set_value("user", "email", "test@example.com").release()
    # Initial commit
    readme = Path(path) / "README.md"
    readme.write_text("# test\n")
    repo.index.add(["README.md"])
    repo.index.commit("initial commit")
    return repo


# ---------------------------------------------------------------------------
# create() tests
# ---------------------------------------------------------------------------


class TestWorktreeCreate:
    def test_create_success(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        session = WorktreeSession.create("abc123", base_dir=str(repo.working_tree_dir))

        assert session.repo_root == str(repo.working_tree_dir)
        assert session.base_ref == "master" or session.base_ref == "main"
        assert session.work_branch == "norn/run-abc123"
        assert Path(session.worktree_dir).exists()
        # Worktree dir should contain a checkout
        assert (Path(session.worktree_dir) / "README.md").exists()
        # Clean up
        session.cleanup()

    def test_not_a_repo(self, tmp_path: Path) -> None:
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        with pytest.raises(WorktreeError, match="Not a git repository"):
            WorktreeSession.create("x", base_dir=str(non_repo))

    def test_detached_head(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        sha = repo.head.commit.hexsha
        repo.head.reference = repo.commit(sha)
        repo.head.reset(index=True, working_tree=True)
        with pytest.raises(WorktreeError, match="Detached HEAD"):
            WorktreeSession.create("x", base_dir=str(repo.working_tree_dir))

    def test_dirty_tree_refuses(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        (Path(repo.working_tree_dir) / "dirty.txt").write_text("uncommitted\n")
        with pytest.raises(WorktreeError, match="dirty"):
            WorktreeSession.create("x", base_dir=str(repo.working_tree_dir))

    def test_branch_collision(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        repo.create_head("norn/run-dup")
        with pytest.raises(WorktreeError, match="Branch already exists"):
            WorktreeSession.create("dup", base_dir=str(repo.working_tree_dir))

    def test_dir_collision(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        # Pre-create the worktree directory
        wt_dir = tmp_path / ".norn-worktrees" / f"repo-collide"
        wt_dir.mkdir(parents=True)
        with pytest.raises(WorktreeError, match="Worktree directory already exists"):
            WorktreeSession.create("collide", base_dir=str(repo.working_tree_dir))


# ---------------------------------------------------------------------------
# merge_back() tests
# ---------------------------------------------------------------------------


class TestWorktreeMergeBack:
    def test_fast_forward_merge(self, tmp_path: Path) -> None:
        """Work branch ahead, launch unchanged → ff merge."""
        repo = _init_repo(tmp_path / "repo")
        session = WorktreeSession.create("ff", base_dir=str(repo.working_tree_dir))

        # Make a change in the worktree
        wt_file = Path(session.worktree_dir) / "new_file.txt"
        wt_file.write_text("hello\n")

        result = session.merge_back(message="norn run test (ff)")
        assert result.changed is True
        assert result.merged is True
        assert result.conflict is False
        assert result.refused is None
        assert "new_file.txt" in result.files
        assert result.commit_sha is not None
        assert result.work_branch == "norn/run-ff"
        assert result.base_ref == session.base_ref

        # File should exist in launch repo now
        assert (Path(repo.working_tree_dir) / "new_file.txt").exists()

        session.cleanup()

    def test_no_change_run(self, tmp_path: Path) -> None:
        """No changes in worktree → changed=False."""
        repo = _init_repo(tmp_path / "repo")
        session = WorktreeSession.create("noop", base_dir=str(repo.working_tree_dir))

        result = session.merge_back(message="norn run test (noop)")
        assert result.changed is False
        assert result.merged is False

        session.cleanup()
        # Branch should be deleted
        assert "norn/run-noop" not in [b.name for b in repo.branches]

    def test_non_ff_merge(self, tmp_path: Path) -> None:
        """Launch branch advanced during the run → non-ff merge."""
        repo = _init_repo(tmp_path / "repo")
        session = WorktreeSession.create("nff", base_dir=str(repo.working_tree_dir))

        # Advance the launch branch (different file to avoid conflict)
        (Path(repo.working_tree_dir) / "launch_change.txt").write_text("from launch\n")
        repo.index.add(["launch_change.txt"])
        repo.index.commit("advance launch branch")

        # Make a change in the worktree (different file)
        wt_file = Path(session.worktree_dir) / "wt_change.txt"
        wt_file.write_text("from worktree\n")

        result = session.merge_back(message="norn run test (nff)")
        assert result.changed is True
        assert result.merged is True
        assert result.conflict is False
        assert "wt_change.txt" in result.files

        # Both files should exist in launch repo
        assert (Path(repo.working_tree_dir) / "launch_change.txt").exists()
        assert (Path(repo.working_tree_dir) / "wt_change.txt").exists()

        session.cleanup()

    def test_conflict(self, tmp_path: Path) -> None:
        """Same file changed in both → conflict, merge aborted, branch kept."""
        repo = _init_repo(tmp_path / "repo")
        session = WorktreeSession.create("conf", base_dir=str(repo.working_tree_dir))

        # Change README.md in launch tree
        (Path(repo.working_tree_dir) / "README.md").write_text("launch version\n")
        repo.index.add(["README.md"])
        repo.index.commit("launch edits README")

        # Change README.md differently in worktree
        (Path(session.worktree_dir) / "README.md").write_text("worktree version\n")

        result = session.merge_back(message="norn run test (conf)")
        assert result.changed is True
        assert result.merged is False
        assert result.conflict is True
        assert "README.md" in result.files
        assert result.work_branch == "norn/run-conf"

        # Launch repo should be clean (merge aborted)
        assert not repo.is_dirty()

        # Cleanup with keep=True since conflict
        session.cleanup(keep=True)
        assert "norn/run-conf" in [b.name for b in repo.branches]

    def test_dirty_launch_at_merge(self, tmp_path: Path) -> None:
        """Launch tree dirtied after create → refused."""
        repo = _init_repo(tmp_path / "repo")
        session = WorktreeSession.create("dirty", base_dir=str(repo.working_tree_dir))

        # Make a change in worktree
        (Path(session.worktree_dir) / "wt.txt").write_text("change\n")

        # Dirty the launch tree (don't commit)
        (Path(repo.working_tree_dir) / "uncommitted.txt").write_text("oops\n")

        result = session.merge_back(message="norn run test (dirty)")
        assert result.changed is True
        assert result.merged is False
        assert result.refused == "dirty-launch-tree"

        session.cleanup(keep=True)

    def test_no_identity_commit_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Commit fails when user identity is not configured → refused='no-identity'."""
        repo = _init_repo(tmp_path / "repo")
        session = WorktreeSession.create("noid", base_dir=str(repo.working_tree_dir))

        # Remove user identity from the worktree config
        wt_repo = Repo(session.worktree_dir)
        cw = wt_repo.config_writer()
        cw.remove_option("user", "name")
        cw.remove_option("user", "email")
        cw.release()

        # Make a change
        (Path(session.worktree_dir) / "new.txt").write_text("data\n")

        # Clear any environment identity so git has no fallback
        for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                    "GIT_COMMITTER_EMAIL", "EMAIL"):
            monkeypatch.delenv(var, raising=False)

        # Override HOME to a temp dir with no gitconfig
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))
        # Prevent git from reading system config
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

        result = session.merge_back(message="norn run test (noid)")

        # Git may or may not reject empty identity depending on version.
        # If it rejected: refused="no-identity"; if it accepted: it's a valid merge.
        # We test the code path, not the git version behavior.
        if result.refused == "no-identity":
            assert result.changed is True
            assert result.merged is False
            session.cleanup(keep=True)
        else:
            # Some git versions accept empty identity — still a valid outcome
            assert result.changed is True
            assert result.merged is True
            session.cleanup(keep=False)


# ---------------------------------------------------------------------------
# cleanup() tests
# ---------------------------------------------------------------------------


class TestWorktreeCleanup:
    def test_cleanup_keep_false_removes_worktree_and_branch(self, tmp_path: Path) -> None:
        """cleanup(keep=False) removes the directory and deletes the branch."""
        repo = _init_repo(tmp_path / "repo")
        session = WorktreeSession.create("rm", base_dir=str(repo.working_tree_dir))

        assert Path(session.worktree_dir).exists()
        assert "norn/run-rm" in [b.name for b in repo.branches]

        session.cleanup(keep=False)

        assert not Path(session.worktree_dir).exists()
        # Refresh repo object to see updated branches
        repo = Repo(repo.working_tree_dir)
        assert "norn/run-rm" not in [b.name for b in repo.branches]

    def test_cleanup_keep_true_preserves_directory_and_branch(self, tmp_path: Path) -> None:
        """cleanup(keep=True) leaves the worktree directory AND the branch intact.

        This is the regression test for the data-loss bug: a caller that says
        keep=True must find the directory — and any uncommitted file inside it —
        exactly as it was before the call.
        """
        repo = _init_repo(tmp_path / "repo")
        session = WorktreeSession.create("keep", base_dir=str(repo.working_tree_dir))

        # Write an uncommitted file — this is the data that must survive
        secret_file = Path(session.worktree_dir) / "precious.txt"
        secret_file.write_text("do not lose me\n")
        assert secret_file.exists()

        session.cleanup(keep=True)

        # Directory must still be there
        assert Path(session.worktree_dir).exists()
        # File contents must be intact — not just directory existence
        assert secret_file.read_text() == "do not lose me\n"
        # Branch must still exist in the repo
        repo2 = Repo(repo.working_tree_dir)
        assert "norn/run-keep" in [b.name for b in repo2.branches]

    def test_cleanup_keep_true_noop_when_already_absent(self, tmp_path: Path) -> None:
        """cleanup(keep=True) is silent even if the worktree dir is somehow gone."""
        repo = _init_repo(tmp_path / "repo")
        session = WorktreeSession.create("gone", base_dir=str(repo.working_tree_dir))

        # Manually blow away the worktree (simulate prior partial cleanup)
        import shutil
        shutil.rmtree(session.worktree_dir)

        # Must not raise
        session.cleanup(keep=True)

    def test_cleanup_after_successful_merge(self, tmp_path: Path) -> None:
        """After a successful merge, cleanup(keep=False) removes everything."""
        repo = _init_repo(tmp_path / "repo")
        session = WorktreeSession.create("ok", base_dir=str(repo.working_tree_dir))

        (Path(session.worktree_dir) / "f.txt").write_text("x\n")
        result = session.merge_back(message="test")
        assert result.merged is True

        session.cleanup(keep=False)
        assert not Path(session.worktree_dir).exists()
        repo = Repo(repo.working_tree_dir)
        assert "norn/run-ok" not in [b.name for b in repo.branches]

    def test_cleanup_after_conflict_keep_true_preserves_branch(self, tmp_path: Path) -> None:
        """After a conflict, cleanup(keep=True) preserves worktree and branch."""
        repo = _init_repo(tmp_path / "repo")
        session = WorktreeSession.create("cx", base_dir=str(repo.working_tree_dir))

        # Create conflict scenario
        (Path(repo.working_tree_dir) / "README.md").write_text("launch\n")
        repo.index.add(["README.md"])
        repo.index.commit("launch change")

        (Path(session.worktree_dir) / "README.md").write_text("worktree\n")
        result = session.merge_back(message="test")
        assert result.conflict is True

        session.cleanup(keep=True)
        # Both worktree directory and branch must survive
        assert Path(session.worktree_dir).exists()
        repo2 = Repo(repo.working_tree_dir)
        assert "norn/run-cx" in [b.name for b in repo2.branches]

    def test_cleanup_after_no_change(self, tmp_path: Path) -> None:
        """No-change run: cleanup(keep=False) removes worktree and branch."""
        repo = _init_repo(tmp_path / "repo")
        session = WorktreeSession.create("nc", base_dir=str(repo.working_tree_dir))

        result = session.merge_back(message="test")
        assert result.changed is False

        session.cleanup(keep=False)
        assert not Path(session.worktree_dir).exists()
        repo = Repo(repo.working_tree_dir)
        assert "norn/run-nc" not in [b.name for b in repo.branches]

    def test_cleanup_keep_false_git_error_recovery(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When git worktree remove fails, shutil.rmtree + git worktree prune still removes the dir.

        This exercises the except-GitCommandError recovery branch in cleanup(): when
        ``git worktree remove --force`` raises, the code falls back to shutil.rmtree so
        the directory disappears even if git cannot be told cleanly.
        """
        from unittest.mock import MagicMock

        from git import GitCommandError as _GitCommandError

        repo = _init_repo(tmp_path / "repo")
        session = WorktreeSession.create("gcerr", base_dir=str(repo.working_tree_dir))

        # Put a sentinel file in the worktree to confirm the dir really disappears.
        (Path(session.worktree_dir) / "work.txt").write_text("work\n")

        # Patch norn.worktree.Repo so that cleanup()'s internal Repo(self.repo_root)
        # returns a mock whose git.worktree("remove", ...) raises GitCommandError while
        # git.worktree("prune") and git.branch("-D", ...) succeed silently.
        def _fake_repo(path, *args, **kwargs):
            mock = MagicMock()

            def _worktree_side_effect(*cmd_args, **_kw):
                if cmd_args and cmd_args[0] == "remove":
                    raise _GitCommandError("worktree", "forced failure")
                # prune succeeds — return nothing

            mock.git.worktree.side_effect = _worktree_side_effect
            mock.git.branch.return_value = ""
            return mock

        import norn.worktree as wt_mod
        monkeypatch.setattr(wt_mod, "Repo", _fake_repo)

        # Must not raise; shutil.rmtree handles the directory removal.
        session.cleanup(keep=False)

        # The worktree directory must be gone (shutil.rmtree ran as the fallback).
        assert not Path(session.worktree_dir).exists()
