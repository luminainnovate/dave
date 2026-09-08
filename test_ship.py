"""
Behaviour tests for `!ship` - the offer, the gate and the squash-merge.

Runs against real throwaway git repositories, with a real bare remote. Only
`gh` is faked, by putting a script that records its argv first on PATH: it is
the one thing here that would otherwise reach GitHub. Everything else - the
staging pathspec, the branch checks, the marker file - is the real code.
"""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "ship", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ship.py"))
ship = importlib.util.module_from_spec(_spec)
sys.modules["ship"] = ship
_spec.loader.exec_module(ship)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args],
                          capture_output=True, text=True)


class Workspace:
    """A conversation workspace: a git repo with a `.cline_context`."""

    def __init__(self, with_remote=True, gitignore=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.ctx = os.path.join(self.root, ".cline_context")
        os.makedirs(self.ctx)

        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "test@local")
        git(self.root, "config", "user.name", "Test")
        self.write("README.md", "start\n")
        git(self.root, "add", "README.md")
        if gitignore:
            self.write(".gitignore", gitignore)
            git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-q", "-m", "initial")

        if with_remote:
            self.origin = tempfile.TemporaryDirectory()
            subprocess.run(["git", "init", "-q", "--bare", self.origin.name],
                           capture_output=True)
            git(self.root, "remote", "add", "origin", self.origin.name)
            git(self.root, "push", "-q", "-u", "origin", "main")

    def write(self, rel, content):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    def build(self, files=None, doc="# Add the widget cache\n\nbody"):
        """Reproduce what the container leaves behind: a branch and a dirty tree."""
        self.write(os.path.join(".cline_context", "distill_architect.md"), doc)
        self.write(os.path.join(".cline_context", ".design_pass"), "architect")
        self.write(os.path.join(".cline_context", "conversation.json"), '[{"secret": 1}]')
        git(self.root, "checkout", "-q", "-b", "agent/build-1700000000")
        default = {"widget.py": "cache = {}\n"}
        for rel, content in (default if files is None else files).items():
            self.write(rel, content)

    def branch(self):
        return git(self.root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


class FakeGh:
    """`gh` on PATH, recording every call. Restores PATH on exit."""

    def __init__(self, merge_fails=False):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "calls.log")
        script = os.path.join(self.tmp.name, "gh")
        with open(script, "w") as f:
            f.write(
                "#!/bin/sh\n"
                f'echo "$@" >> {self.log}\n'
                'case "$1 $2" in\n'
                '  "pr create") echo https://github.test/pr/1 ;;\n'
                '  "pr merge") '
                + ("echo 'protected branch' >&2; exit 1 ;;\n" if merge_fails else "echo merged ;;\n") +
                '  "pr view") echo https://github.test/pr/1 ;;\n'
                'esac\n'
            )
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        self._path = os.environ["PATH"]
        os.environ["PATH"] = self.tmp.name + os.pathsep + self._path

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [ln.strip() for ln in f if ln.strip()]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        os.environ["PATH"] = self._path


# =============================================================================


def test_the_base_branch_is_read_before_the_container_moves_off_it():
    w = Workspace()
    check("the user's branch is the base", ship.base_branch(w.root) == "main")
    w.build()
    check("and asking from the agent branch does not answer with itself",
          ship.base_branch(w.root) != w.branch(), ship.base_branch(w.root))


def test_a_finished_build_records_a_marker():
    w = Workspace()
    w.build()
    branch = ship.record_build_complete(w.root, container="c1", base="main")
    check("the branch is recorded", branch == w.branch(), branch)
    marker = ship.read_marker(w.root)
    check("with its base", marker["base"] == "main", marker)
    check("and it is not yet announced", marker["announced"] is False)


def test_a_build_that_never_left_the_users_branch_records_nothing():
    w = Workspace()
    w.write("widget.py", "x = 1\n")
    check("no marker", ship.record_build_complete(w.root, base="main") is None)
    check("and none on disk", ship.read_marker(w.root) is None)


def test_a_build_that_changed_nothing_records_nothing():
    w = Workspace()
    w.build(files={})
    check("no marker for an empty build",
          ship.record_build_complete(w.root, base="main") is None)


def test_the_offer_is_made_once_and_survives_a_restart():
    w = Workspace()
    w.build()
    ship.record_build_complete(w.root, base="main")

    first = ship.pending_offer(w.root)
    check("the offer names the branch", first and "agent/build-1700000000" in first, first)
    check("and names the file", "widget.py" in first, first)
    check("and does not claim to have merged anything", "!ship confirm" in first)
    check("a second turn does not repeat it", ship.pending_offer(w.root) is None)

    # The marker is a file, not memory: a fresh process still finds the build.
    check("the build is still shippable after a restart",
          ship.read_marker(w.root)["branch"] == "agent/build-1700000000")


def test_ship_refuses_off_a_build_branch_and_says_which_branch():
    w = Workspace()
    w.build()
    ship.record_build_complete(w.root, base="main")
    git(w.root, "checkout", "-q", "main")
    out = ship.propose(w.root)
    check("it refuses", "Not on a build branch" in out, out)
    check("and names the branch it found", "`main`" in out, out)


def test_ship_refuses_a_clean_tree():
    w = Workspace()
    w.build()
    ship.record_build_complete(w.root, base="main")
    os.remove(os.path.join(w.root, "widget.py"))
    out = ship.propose(w.root)
    check("nothing to ship", "Nothing to ship" in out, out)


def test_ship_refuses_without_an_origin():
    w = Workspace(with_remote=False)
    w.build()
    ship.record_build_complete(w.root, base="main")
    out = ship.propose(w.root)
    check("it names the missing remote", "No `origin` remote" in out, out)
    check("and says the work is safe", "safe on" in out, out)


def test_ship_refuses_a_build_branch_with_no_finished_build():
    w = Workspace()
    w.build()
    out = ship.propose(w.root)
    check("it refuses without a marker", "No finished build recorded" in out, out)


def test_the_proposal_changes_nothing():
    w = Workspace()
    w.build()
    ship.record_build_complete(w.root, base="main")
    before = git(w.root, "rev-parse", "HEAD").stdout
    out = ship.propose(w.root, "Cache the widgets")
    check("it describes the squash", "Squash-merge" in out, out)
    check("it uses the given title", "Cache the widgets" in out, out)
    check("HEAD did not move", git(w.root, "rev-parse", "HEAD").stdout == before)
    check("and nothing was staged",
          git(w.root, "diff", "--cached", "--name-only").stdout.strip() == "")


def test_the_title_comes_from_the_design_document():
    w = Workspace()
    w.build()
    ship.record_build_complete(w.root, base="main")
    out = ship.propose(w.root)
    check("the design heading is the title", "Add the widget cache" in out, out)


def test_confirm_commits_pushes_and_squash_merges():
    w = Workspace()
    w.build()
    ship.record_build_complete(w.root, base="main")
    with FakeGh() as gh:
        out = ship.confirm(w.root)
    calls = gh.calls()

    check("it reports the squash-merge", "Squash-merged into `main`" in out, out)
    check("a PR was opened", any(c.startswith("pr create") for c in calls), calls)
    check("and squash-merged", any("pr merge" in c and "--squash" in c for c in calls), calls)
    check("the branch reached origin",
          "agent/build-1700000000" in git(w.root, "ls-remote", "origin").stdout)

    committed = git(w.root, "show", "--name-only", "--format=", "agent/build-1700000000").stdout
    check("the build's file is in the commit", "widget.py" in committed, committed)
    check("the conversation transcript is not", "conversation.json" not in committed, committed)
    check("nor the design document", "distill_architect.md" not in committed, committed)
    check("the marker is spent", ship.read_marker(w.root) is None)
    check("and the workspace is back on main", w.branch() == "main", w.branch())


def test_confirm_works_when_gitignore_already_covers_the_scratch_dirs():
    """
    The shape that broke it in the field.

    `git add -A -- . \':(exclude).cline_context\'` refuses outright once
    .gitignore names that directory: git will not take a pathspec that points
    at an ignored path. Staging the listed files never names one.
    """
    w = Workspace(gitignore=".cline_context\n.cline_logs\nnode_modules/\n")
    w.build()
    w.write("node_modules/junk.js", "ignored\n")
    ship.record_build_complete(w.root, base="main")

    out = ship.propose(w.root)
    check("the proposal does not offer the ignored build output",
          "node_modules" not in out, out)

    with FakeGh():
        out = ship.confirm(w.root)
    check("the push is not refused", "Could not push" not in out, out)
    check("it merges", "Squash-merged into `main`" in out, out)

    committed = git(w.root, "show", "--name-only", "--format=", "agent/build-1700000000").stdout
    check("the build's file is committed", "widget.py" in committed, committed)
    check("the ignored directory is not", "node_modules" not in committed, committed)
    check("nor the scratch directory", ".cline_context" not in committed, committed)


def test_paths_with_spaces_and_renames_survive_staging():
    w = Workspace()
    w.write("old name.py", "x = 1\n")
    git(w.root, "add", "old name.py")
    git(w.root, "commit", "-q", "-m", "add a spaced path")
    git(w.root, "push", "-q", "origin", "main")

    w.build(files={"a file with spaces.py": "y = 2\n"})
    git(w.root, "mv", "old name.py", "new name.py")

    ship.record_build_complete(w.root, base="main")
    with FakeGh():
        out = ship.confirm(w.root)
    check("it merges", "Squash-merged into `main`" in out, out)

    committed = git(w.root, "show", "--name-only", "--format=", "agent/build-1700000000").stdout
    check("the spaced new file is committed", "a file with spaces.py" in committed, committed)
    check("the rename's new half is committed", "new name.py" in committed, committed)
    tree = git(w.root, "ls-tree", "-r", "--name-only", "agent/build-1700000000").stdout
    check("and the old half is gone from the tree", "old name.py" not in tree, tree)


def test_a_refused_merge_still_reports_the_open_pull_request():
    w = Workspace()
    w.build()
    ship.record_build_complete(w.root, base="main")
    with FakeGh(merge_fails=True):
        out = ship.confirm(w.root)

    check("it does not claim a merge", "Squash-merged" not in out, out)
    check("it gives the PR url", "https://github.test/pr/1" in out, out)
    check("it explains why", "branch protection" in out, out)
    check("and the offer is still live for a retry",
          ship.read_marker(w.root) is not None)
    check("the commit was still made",
          "widget.py" in git(w.root, "show", "--name-only", "--format=", "HEAD").stdout)


def test_confirm_ships_work_the_agent_already_committed():
    w = Workspace()
    w.build()
    git(w.root, "add", "widget.py")
    git(w.root, "commit", "-q", "-m", "agent commit")
    check("a committed branch is still recorded",
          ship.record_build_complete(w.root, base="main") == "agent/build-1700000000")
    with FakeGh() as gh:
        out = ship.confirm(w.root)
    check("it merges without a second commit", "Squash-merged" in out, out)
    check("the PR was opened", any(c.startswith("pr create") for c in gh.calls()))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n{t.__name__}")
        try:
            t()
        except Exception:
            import traceback
            traceback.print_exc()
            FAILURES.append(t.__name__)
    print("\n" + ("-" * 60))
    print("FAILED:" if FAILURES else "ALL PASSED", len(FAILURES) or "")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1 if FAILURES else 0)
