from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from git import GitCommandError, InvalidGitRepositoryError, Repo

log = logging.getLogger(__name__)


class WorktreeError(Exception):
    """Raised when worktree creation or validation fails."""


@dataclass(frozen=True)
class MergeResult:
    """Outcome of a worktree merge-back attempt."""

    changed: bool
    merged: bool
    conflict: bool = False
    refused: str | None = None  # "dirty-launch-tree" | "no-identity" | "git-error"
    files: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    work_branch: str = ""
    worktree_dir: str = ""
    base_ref: str = ""
    abort_failed: bool = False  # conflict abort itself failed; launch repo left mid-merge


@dataclass(frozen=True)
class WorktreeSession:
    """Manages a git worktree lifecycle: create, merge back, clean up."""

    repo_root: str
    base_ref: str
    base_sha: str
    work_branch: str
    worktree_dir: str

    @classmethod
    def create(cls, run_id: str, *, base_dir: str | None = None) -> WorktreeSession:
        """Create a new worktree for an isolated run.

        Args:
            run_id: Short identifier for the run (used in branch/dir names).
            base_dir: Directory inside the git repo to fork from.
                      Defaults to process cwd.

        Raises:
            WorktreeError: If the directory is not a git repo, HEAD is detached,
                           the working tree is dirty, or the branch/dir already exists.
        """
        resolve_dir = base_dir or os.getcwd()

        # Resolve repo toplevel
        try:
            repo = Repo(resolve_dir, search_parent_directories=True)
        except InvalidGitRepositoryError:
            raise WorktreeError(f"Not a git repository: {resolve_dir}")

        repo_root = repo.working_tree_dir
        if repo_root is None:
            raise WorktreeError(f"Bare repository not supported: {resolve_dir}")

        # Capture base ref (current branch name) — detached HEAD is an error
        if repo.head.is_detached:
            raise WorktreeError("Detached HEAD: cannot determine branch to merge into")

        base_ref = repo.active_branch.name
        base_sha = repo.head.commit.hexsha

        # Refuse a dirty launch tree
        if repo.is_dirty(untracked_files=True):
            raise WorktreeError(
                "Working tree is dirty; commit or stash changes before creating a worktree"
            )

        # Derive names
        work_branch = f"norn/run-{run_id}"
        repo_name = Path(repo_root).name
        worktree_parent = Path(repo_root).parent / ".norn-worktrees"
        worktree_dir = str(worktree_parent / f"{repo_name}-{run_id}")

        # Collision checks
        if work_branch in [ref.name for ref in repo.branches]:
            raise WorktreeError(f"Branch already exists: {work_branch}")
        if Path(worktree_dir).exists():
            raise WorktreeError(f"Worktree directory already exists: {worktree_dir}")

        # Create worktree with new branch
        worktree_parent.mkdir(parents=True, exist_ok=True)
        try:
            repo.git.worktree("add", "-b", work_branch, worktree_dir, base_sha)
        except GitCommandError as exc:
            raise WorktreeError(f"Failed to create worktree: {exc}") from exc

        log.info(
            "Created worktree %s on branch %s from %s (%s)",
            worktree_dir,
            work_branch,
            base_ref,
            base_sha[:8],
        )

        return cls(
            repo_root=repo_root,
            base_ref=base_ref,
            base_sha=base_sha,
            work_branch=work_branch,
            worktree_dir=worktree_dir,
        )

    def merge_back(self, *, message: str) -> MergeResult:
        """Commit worktree changes and merge back into the launch branch.

        Args:
            message: Commit message for both the work commit and any merge commit.

        Returns:
            MergeResult describing the outcome.
        """
        common = dict(
            work_branch=self.work_branch,
            worktree_dir=self.worktree_dir,
            base_ref=self.base_ref,
        )

        wt_repo = Repo(self.worktree_dir)

        # Check for changes in the worktree
        if wt_repo.is_dirty(untracked_files=True):
            # Stage everything and commit
            wt_repo.git.add("-A")
            try:
                wt_repo.git.commit("-m", message)
            except GitCommandError:
                # Commit failure (e.g. missing user identity)
                return MergeResult(
                    changed=True,
                    merged=False,
                    refused="no-identity",
                    **common,
                )

        # Check if the work branch has any commits beyond the base
        launch_repo = Repo(self.repo_root)
        work_ref = launch_repo.branches[self.work_branch]
        if work_ref.commit.hexsha == self.base_sha:
            # No new commits — nothing to merge
            return MergeResult(changed=False, merged=False, **common)

        # Collect the list of changed files from the work branch
        changed_files = launch_repo.git.diff(
            "--name-only", self.base_sha, work_ref.commit.hexsha
        ).splitlines()
        changed_files = [f for f in changed_files if f]  # drop empty strings

        # Verify launch tree is still on base_ref and clean
        if launch_repo.head.is_detached or launch_repo.active_branch.name != self.base_ref:
            return MergeResult(
                changed=True,
                merged=False,
                refused="dirty-launch-tree",
                files=changed_files,
                **common,
            )
        if launch_repo.is_dirty(untracked_files=True):
            return MergeResult(
                changed=True,
                merged=False,
                refused="dirty-launch-tree",
                files=changed_files,
                **common,
            )

        # Attempt fast-forward merge first
        try:
            launch_repo.git.merge("--ff-only", self.work_branch)
        except GitCommandError:
            # FF failed — try non-ff merge
            try:
                launch_repo.git.merge("--no-ff", "--no-edit", "-m", message, self.work_branch)
            except GitCommandError:
                # Check if it's a conflict
                try:
                    unmerged = launch_repo.git.diff(
                        "--name-only", "--diff-filter=U"
                    ).splitlines()
                    unmerged = [f for f in unmerged if f]
                except GitCommandError:
                    unmerged = []

                if unmerged:
                    # Conflict — abort the merge
                    abort_failed = False
                    try:
                        launch_repo.git.merge("--abort")
                    except GitCommandError as abort_exc:
                        abort_failed = True
                        log.warning(
                            "Failed to abort conflicted merge in %s; launch repo "
                            "may be left mid-merge: %s",
                            self.repo_root,
                            abort_exc,
                        )
                    return MergeResult(
                        changed=True,
                        merged=False,
                        conflict=True,
                        files=unmerged,
                        abort_failed=abort_failed,
                        **common,
                    )
                else:
                    # Some other git error
                    return MergeResult(
                        changed=True,
                        merged=False,
                        refused="git-error",
                        files=changed_files,
                        **common,
                    )

        # Success
        commit_sha = launch_repo.head.commit.hexsha
        return MergeResult(
            changed=True,
            merged=True,
            files=changed_files,
            commit_sha=commit_sha,
            **common,
        )

    def cleanup(self, *, keep: bool = False) -> None:
        """Remove the worktree and branch, or leave everything in place.

        Two outcomes only — there is no intermediate state:

        - ``keep=True``: do nothing.  The worktree directory **and** the work
          branch are left exactly as they are so the user can inspect or
          continue the work.  (A checked-out worktree branch cannot be deleted
          anyway, and no caller wants to delete the directory while keeping the
          branch attached to it.)
        - ``keep=False``: remove the worktree directory via ``git worktree
          remove --force`` (falling back to ``shutil.rmtree`` + ``git worktree
          prune`` if that fails), then delete the work branch with
          ``git branch -D``.

        Args:
            keep: If True, leave the worktree directory and branch untouched.
                  If False (the default), remove everything.
        """
        if keep:
            log.debug(
                "Keeping worktree %s on branch %s as requested",
                self.worktree_dir,
                self.work_branch,
            )
            return

        launch_repo = Repo(self.repo_root)

        # Remove worktree directory
        if Path(self.worktree_dir).exists():
            try:
                launch_repo.git.worktree("remove", "--force", self.worktree_dir)
            except GitCommandError:
                log.warning("Failed to remove worktree %s via git; removing manually", self.worktree_dir)
                import shutil
                shutil.rmtree(self.worktree_dir, ignore_errors=True)
                # Prune stale worktree entries
                try:
                    launch_repo.git.worktree("prune")
                except GitCommandError:
                    pass

        # Delete work branch
        try:
            launch_repo.git.branch("-D", self.work_branch)
        except GitCommandError:
            log.warning("Failed to delete branch %s", self.work_branch)
