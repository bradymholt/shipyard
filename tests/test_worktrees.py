import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from shipyard import worktrees


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class GitCase(unittest.TestCase):
    """A bare "origin" on github.com/o/r's URL shape, and a clone under a scan root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(os.path.realpath(self.tmp.name))
        self.root = base / "dev"
        self.root.mkdir()
        origin = base / "github.com" / "o" / "r.git"
        origin.mkdir(parents=True)
        git(origin, "init", "--bare", "-q", "-b", "main")
        seed = base / "seed"
        git(base, "init", "-q", "-b", "main", str(seed))
        for args in (("config", "user.email", "t@example.com"), ("config", "user.name", "T")):
            git(seed, *args)
        (seed / "app.py").write_text("print(1)\n")
        git(seed, "add", ".")
        git(seed, "commit", "-qm", "init")
        git(seed, "remote", "add", "origin", str(origin))
        git(seed, "push", "-q", "origin", "main")
        # someone else's PR #5, published the way GitHub publishes PR heads
        git(seed, "switch", "-qc", "theirs")
        (seed / "app.py").write_text("print(2)\n")
        git(seed, "commit", "-qam", "their change")
        self.pr_head = git(seed, "rev-parse", "HEAD")
        git(seed, "push", "-q", "origin", "HEAD:refs/pull/5/head")
        self.clone = self.root / "r"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.clone)], check=True)
        for args in (("config", "user.email", "t@example.com"), ("config", "user.name", "T")):
            git(self.clone, *args)
        self.repo = worktrees.parse_origin(git(self.clone, "config", "--get", "remote.origin.url"))
        self.roots = [str(self.root)]


class ReviewWorktree(GitCase):
    def test_creates_then_moves_to_new_head(self):
        out = worktrees.review_worktree(self.repo, 5, self.pr_head, self.roots)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["path"], str(self.clone / ".claude" / "worktrees" / "review-5"))
        self.assertEqual(git(out["path"], "rev-parse", "HEAD"), self.pr_head)
        again = worktrees.review_worktree(self.repo, 5, None, self.roots)
        self.assertTrue(again["ok"], again)

    def test_refuses_to_move_a_review_worktree_with_edits(self):
        out = worktrees.review_worktree(self.repo, 5, self.pr_head, self.roots)
        Path(out["path"], "app.py").write_text("edited\n")
        again = worktrees.review_worktree(self.repo, 5, self.pr_head, self.roots)
        self.assertFalse(again["ok"])


    def test_refuses_to_move_or_remove_a_review_worktree_with_its_own_commits(self):
        out = worktrees.review_worktree(self.repo, 5, self.pr_head, self.roots)
        Path(out["path"], "app.py").write_text("tried something\n")
        git(out["path"], "commit", "-qam", "experiment")
        again = worktrees.review_worktree(self.repo, 5, self.pr_head, self.roots)
        self.assertFalse(again["ok"])
        self.assertIn("commits of its own", again["error"])
        gone = worktrees.cleanup_worktree(self.repo, None, self.roots, path=out["path"])
        self.assertFalse(gone["ok"])


class Cleanup(GitCase):
    def make_worktree(self, branch):
        out = worktrees.new_task_worktree(self.repo, branch, self.roots)
        self.assertTrue(out["ok"], out)
        return out["path"]

    def test_removes_clean_worktree_and_squash_merged_branch(self):
        path = self.make_worktree("me/feature")
        Path(path, "app.py").write_text("print(3)\n")
        git(path, "commit", "-qam", "feature")
        head = git(path, "rev-parse", "HEAD")
        # unmerged by Git's reckoning (a squash merge), but its tip is what GitHub merged
        out = worktrees.cleanup_worktree(self.repo, "me/feature", self.roots, merged_head=head)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["branchDeleted"])
        self.assertFalse(os.path.exists(path))

    def test_keeps_branch_with_commits_github_never_saw(self):
        path = self.make_worktree("me/other")
        Path(path, "app.py").write_text("print(4)\n")
        git(path, "commit", "-qam", "local only")
        out = worktrees.cleanup_worktree(self.repo, "me/other", self.roots, merged_head="0" * 40)
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["branchDeleted"])
        self.assertIn("me/other", git(self.clone, "branch", "--list", "me/other"))

    def test_refuses_dirty_or_untracked_worktree(self):
        path = self.make_worktree("me/dirty")
        Path(path, "notes.txt").write_text("unsaved idea\n")
        out = worktrees.cleanup_worktree(self.repo, "me/dirty", self.roots)
        self.assertFalse(out["ok"])
        self.assertTrue(os.path.exists(path))

    def test_refuses_main_clone_and_paths_outside_claude_worktrees(self):
        out = worktrees.cleanup_worktree(self.repo, "main", self.roots)
        self.assertFalse(out["ok"])
        out = worktrees.cleanup_worktree(self.repo, None, self.roots, path=str(self.clone))
        self.assertFalse(out["ok"])

    def test_review_worktree_cleanup_by_path(self):
        made = worktrees.review_worktree(self.repo, 5, self.pr_head, self.roots)
        out = worktrees.cleanup_worktree(self.repo, None, self.roots, path=made["path"])
        self.assertTrue(out["ok"], out)
        self.assertFalse(os.path.exists(made["path"]))


if __name__ == "__main__":
    unittest.main()


class ApplyFix(GitCase):
    def test_applies_new_and_changed_files_commits_and_pushes(self):
        from shipyard import actions
        path = worktrees.new_task_worktree(self.repo, "me/fix", self.roots)["path"]
        git(path, "push", "-q", "-u", "origin", "me/fix")
        head = git(path, "rev-parse", "HEAD")
        diff = ("--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-print(1)\n+print(10)\n"
                "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hello\n")
        snap = {"head": head, "branch": "me/fix"}
        out = actions.apply_fix(None, snap, {"diff": diff}, {"worktree": {"path": path}})
        self.assertEqual(out, "applied the fix and pushed")
        self.assertEqual(git(path, "status", "--porcelain"), "")
        self.assertIn("new.txt", git(path, "show", "--stat", "HEAD"))
        self.assertEqual(git(self.clone, "ls-remote", "origin", "me/fix").split()[0], git(path, "rev-parse", "HEAD"))

    def test_refuses_a_checkout_on_another_branch(self):
        from shipyard import actions
        path = worktrees.new_task_worktree(self.repo, "me/fix3", self.roots)["path"]
        head = git(path, "rev-parse", "HEAD")
        git(path, "switch", "-qc", "other")
        with self.assertRaisesRegex(actions.ActionError, "isn't on me/fix3"):
            actions.apply_fix(None, {"head": head, "branch": "me/fix3"}, {"diff": "x"}, {"worktree": {"path": path}})

    def test_refuses_when_checkout_moved_or_diff_does_not_apply(self):
        from shipyard import actions
        path = worktrees.new_task_worktree(self.repo, "me/fix2", self.roots)["path"]
        with self.assertRaises(actions.ActionError):
            actions.apply_fix(None, {"head": "0" * 40, "branch": "me/fix2"}, {"diff": "x"}, {"worktree": {"path": path}})
        head = git(path, "rev-parse", "HEAD")
        with self.assertRaisesRegex(actions.ActionError, "doesn't apply"):
            actions.apply_fix(None, {"head": head, "branch": "me/fix2"},
                              {"diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-nope\n+x\n"}, {"worktree": {"path": path}})
