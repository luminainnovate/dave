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

    def head(self, ref="HEAD"):
        return git(self.root, "rev-parse", ref).stdout.strip()


class FakeGh:
    """
    `gh` on PATH, recording every call. Restores PATH on exit.

    Responses are scripted per argv prefix ("pr view", "run view 9 --json")
    with `script`; the longest scripted prefix wins. They can be changed
    between calls: the first queued response is used and dropped, and the last
    one repeats. Unscripted calls get the old canned answers.
    """

    def __init__(self, merge_fails=False):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "calls.log")
        self.argv_log = os.path.join(self.tmp.name, "argv.jsonl")
        self.responses = os.path.join(self.tmp.name, "responses.json")
        script = os.path.join(self.tmp.name, "gh")
        with open(script, "w") as f:
            f.write(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "d = os.path.dirname(os.path.abspath(__file__))\n"
                "args = sys.argv[1:]\n"
                "open(os.path.join(d, 'calls.log'), 'a').write(' '.join(args) + '\\n')\n"
                "open(os.path.join(d, 'argv.jsonl'), 'a').write(json.dumps(args) + '\\n')\n"
                "path = os.path.join(d, 'responses.json')\n"
                "try:\n"
                "    responses = json.load(open(path))\n"
                "except Exception:\n"
                "    responses = {}\n"
                "key = next((' '.join(args[:n]) for n in range(len(args), 1, -1)\n"
                "            if ' '.join(args[:n]) in responses), ' '.join(args[:2]))\n"
                "queue = responses.get(key)\n"
                "if queue:\n"
                "    r = queue[0]\n"
                "    if len(queue) > 1:\n"
                "        queue.pop(0)\n"
                "        json.dump(responses, open(path, 'w'))\n"
                "    sys.stdout.write(r.get('out', ''))\n"
                "    sys.stderr.write(r.get('err', ''))\n"
                "    sys.exit(r.get('code', 0))\n"
                "key = ' '.join(args[:2])\n"
                "if key == 'pr create':\n"
                "    print('https://github.test/pull/1')\n"
                "elif key == 'pr merge':\n"
                + ("    sys.stderr.write('protected branch\\n'); sys.exit(1)\n" if merge_fails
                   else "    print('merged')\n") +
                "elif key == 'pr view':\n"
                "    print('https://github.test/pull/1')\n"
            )
        os.chmod(script, os.stat(script).st_mode | stat.S_IEXEC)
        self._path = os.environ["PATH"]
        os.environ["PATH"] = self.tmp.name + os.pathsep + self._path

    def script(self, key, *responses):
        """Queue responses for a subcommand: dicts with out/err/code, or JSON-able data."""
        try:
            with open(self.responses) as f:
                current = json.load(f)
        except Exception:
            current = {}
        current[key] = [r if isinstance(r, dict) and {"out", "err", "code"} & set(r)
                        else {"out": json.dumps(r)} for r in responses]
        with open(self.responses, "w") as f:
            json.dump(current, f)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [ln.strip() for ln in f if ln.strip()]

    def argvs(self):
        if not os.path.exists(self.argv_log):
            return []
        with open(self.argv_log) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def merges(self):
        return [a for a in self.argvs() if a[:2] == ["pr", "merge"]]

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


def test_confirm_commits_pushes_and_opens_a_pr_without_merging():
    w = Workspace()
    w.build()
    ship.record_build_complete(w.root, base="main")
    with FakeGh() as gh:
        out = ship.confirm(w.root)
    calls = gh.calls()

    check("it reports the PR waiting on CI", "waiting for CI before merging" in out, out)
    check("a PR was opened", any(c.startswith("pr create") for c in calls), calls)
    check("and NOT merged before CI ran", not gh.merges(), calls)
    check("the branch reached origin",
          "agent/build-1700000000" in git(w.root, "ls-remote", "origin").stdout)

    committed = git(w.root, "show", "--name-only", "--format=", "agent/build-1700000000").stdout
    check("the build's file is in the commit", "widget.py" in committed, committed)
    check("the conversation transcript is not", "conversation.json" not in committed, committed)
    check("nor the design document", "distill_architect.md" not in committed, committed)
    marker = ship.read_marker(w.root)
    check("the marker now waits on checks",
          marker and marker["state"] == ship.STATE_AWAITING_CHECKS, marker)
    check("for the exact commit pushed",
          marker and marker["head_sha"] == w.head("agent/build-1700000000"), marker)
    check("and the workspace stays on the branch", w.branch() == "agent/build-1700000000", w.branch())


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
    check("it opens the PR", "waiting for CI before merging" in out, out)

    committed = git(w.root, "show", "--name-only", "--format=", "agent/build-1700000000").stdout
    check("the build's file is committed", "widget.py" in committed, committed)
    check("the ignored directory is not", "node_modules" not in committed, committed)
    check("nor the scratch directory", ".cline_context" not in committed, committed)


def test_confirm_skips_an_ignored_path_the_branch_already_touched():
    """
    `git add` is fatal on an ignored pathspec, e.g. a committed-then-untracked
    `.claude/settings.local.json` under a global ignore.
    """
    w = Workspace(gitignore=".claude/settings.local.json\n")
    w.build()
    w.write(".claude/settings.local.json", "{}\n")
    git(w.root, "add", "-f", ".claude/settings.local.json", "widget.py")
    git(w.root, "commit", "-q", "-m", "agent commit")
    git(w.root, "rm", "-q", "--cached", ".claude/settings.local.json")
    ship.record_build_complete(w.root, base="main")

    with FakeGh():
        out = ship.confirm(w.root)
    check("the push is not refused", "Could not push" not in out, out)
    check("it opens the PR", "waiting for CI before merging" in out, out)


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
    check("it opens the PR", "waiting for CI before merging" in out, out)

    committed = git(w.root, "show", "--name-only", "--format=", "agent/build-1700000000").stdout
    check("the spaced new file is committed", "a file with spaces.py" in committed, committed)
    check("the rename's new half is committed", "new name.py" in committed, committed)
    tree = git(w.root, "ls-tree", "-r", "--name-only", "agent/build-1700000000").stdout
    check("and the old half is gone from the tree", "old name.py" not in tree, tree)


def test_confirm_ships_work_the_agent_already_committed():
    w = Workspace()
    w.build()
    git(w.root, "add", "widget.py")
    git(w.root, "commit", "-q", "-m", "agent commit")
    check("a committed branch is still recorded",
          ship.record_build_complete(w.root, base="main") == "agent/build-1700000000")
    with FakeGh() as gh:
        out = ship.confirm(w.root)
    check("it ships without a second commit", "waiting for CI before merging" in out, out)
    check("the PR was opened", any(c.startswith("pr create") for c in gh.calls()))


# =============================================================================
# CI GATE
# =============================================================================

import time  # noqa: E402

RUFF_LOG = "\n".join(
    f"test-proxy\tLint with Ruff\t2026-10-28T10:00:0{i}.0000000Z {line}"
    for i, line in enumerate([
        "##[group]Run \x1b[36;1mruff check .\x1b[0m",
        "ruff check .",
        "shell: /usr/bin/bash -e {0}",
        "##[endgroup]",
        "widget.py:1:1: F401 `os` imported but unused",
        "Found 1 error.",
        "##[error]Process completed with exit code 1.",
    ])
) + "\n"

JOB_URL = "https://github.test/o/r/actions/runs/111/job/222"


def pr_view(head, checks=(), state="OPEN", mergeable="MERGEABLE", merge_oid=""):
    return {"number": 1, "url": "https://github.test/pull/1", "state": state,
            "mergeable": mergeable, "headRefOid": head,
            "statusCheckRollup": list(checks),
            "mergeCommit": {"oid": merge_oid} if merge_oid else None}


def check_run(conclusion="SUCCESS", status="COMPLETED", name="test-proxy"):
    return {"__typename": "CheckRun", "name": name, "workflowName": "CI",
            "status": status, "conclusion": conclusion if status == "COMPLETED" else "",
            "detailsUrl": JOB_URL}


def age(w, secs):
    """Pretend the current checkpoint started `secs` ago."""
    marker = ship.read_marker(w.root)
    marker["since"] = time.time() - secs
    ship._save_marker(w.root, marker)


def shipped(gh, design=True):
    """A workspace whose PR is open and waiting on CI."""
    w = Workspace()
    w.build()
    if not design:
        os.remove(os.path.join(w.ctx, "distill_architect.md"))
    ship.record_build_complete(w.root, base="main")
    ship.confirm(w.root)
    return w


def failed_on_pr(gh):
    w = shipped(gh)
    head = w.head()
    gh.script("pr view", pr_view(head, [check_run("FAILURE")]))
    gh.script("run view", {"out": RUFF_LOG, "err": "", "code": 0})
    ship.advance(w.root)
    return w, head


def test_pending_checks_do_not_merge():
    with FakeGh() as gh:
        w = shipped(gh)
        gh.script("pr view", pr_view(w.head(), [check_run(status="IN_PROGRESS")]))
        state = ship.advance(w.root)
    check("still waiting", state == ship.STATE_AWAITING_CHECKS, state)
    check("nothing merged", not gh.merges(), gh.calls())


def test_green_checks_merge_exactly_the_commit_they_tested():
    with FakeGh() as gh:
        w = shipped(gh)
        head = w.head()
        gh.script("pr view", pr_view(head, [check_run()]), pr_view(head, merge_oid="m" * 40))
        state = ship.advance(w.root)
    merges = gh.merges()
    check("it merged once", len(merges) == 1, merges)
    check("as a squash", merges and "--squash" in merges[0], merges)
    check("pinned to the tested head",
          merges and merges[0][merges[0].index("--match-head-commit") + 1] == head, merges)
    check("and now waits on main", state == ship.STATE_AWAITING_BASE, state)
    check("with the squash commit recorded",
          ship.read_marker(w.root)["merge_sha"] == "m" * 40)
    check("on main", w.branch() == "main", w.branch())


def test_green_main_after_merge_is_integrated_and_announced_once():
    with FakeGh() as gh:
        w = shipped(gh)
        head = w.head()
        gh.script("pr view", pr_view(head, [check_run()]), pr_view(head, merge_oid="m" * 40))
        ship.advance(w.root)
        gh.script("run list", [{"databaseId": 9, "status": "completed", "conclusion": "success",
                                "workflowName": "CI", "name": "CI", "url": "u"}])
        state = ship.advance(w.root)
        run_lists = [a for a in gh.argvs() if a[:2] == ["run", "list"]]
    check("integrated", state == ship.STATE_INTEGRATED, state)
    check("it asked about the squash commit", run_lists and "m" * 40 in run_lists[0], run_lists)
    offer = ship.pending_offer(w.root)
    check("the user is told", offer and "Integrated into `main`" in offer, offer)
    check("once", ship.pending_offer(w.root) is None)
    check("and the marker is spent", ship.read_marker(w.root) is None)


def test_a_failed_check_writes_the_root_causes_and_keeps_the_pr_open():
    with FakeGh() as gh:
        w, head = failed_on_pr(gh)
        merges = gh.merges()
    marker = ship.read_marker(w.root)
    check("not merged", not merges, merges)
    check("waiting for approval", marker["state"] == ship.STATE_CI_FAILED, marker["state"])
    with open(ship.ci_failure_path(w.root)) as f:
        doc = f.read()
    check("the doc is unresolved", "**Status:** UNRESOLVED" in doc, doc)
    check("it names the failing step", "Lint with Ruff" in doc, doc)
    check("and the command it ran", "ruff check ." in doc, doc)
    check("with the runner's colour codes stripped", "\x1b" not in doc and "[36;1m" not in doc, doc)
    check("and the error line", "F401" in doc, doc)
    check("and scopes the fix to the approved design", "ship_design.md" in doc, doc)
    notice = ship.pending_offer(w.root)
    check("chat shows the cause", notice and "F401" in notice, notice)
    check("and offers !approve", notice and "`!approve`" in notice, notice)
    check("with no fenced block mover could turn into a file",
          notice and "```" not in notice, notice)


def test_the_approved_design_is_frozen_where_a_fix_build_cannot_delete_it():
    with FakeGh() as gh:
        w = shipped(gh)
    frozen = os.path.join(w.ctx, ship.SHIP_DESIGN_NAME)
    check("a copy exists", os.path.exists(frozen))
    import fnmatch
    check("outside the container's distill_*.md glob",
          not fnmatch.fnmatch(ship.SHIP_DESIGN_NAME, "distill_*.md"))


def test_approve_turns_the_failure_into_a_scoped_fix_build():
    with FakeGh() as gh:
        w, head = failed_on_pr(gh)
        w.write(".build_complete", "VERIFIED\nSAFE\n")  # the original build's
        instruction, env, refusal = ship.ci_fix_request(w.root)
    check("no refusal", refusal is None, refusal)
    check("it is a build trigger", instruction.startswith("!build "), instruction[:80])
    check("carrying the cause", "F401" in instruction, instruction)
    check("and the approved design as scope",
          "APPROVED DESIGN:" in instruction and "Add the widget cache" in instruction, instruction)
    check("the container stays on the PR branch",
          env == {"BUILD_BRANCH": "agent/build-1700000000"}, env)
    check("the marker is fixing", ship.read_marker(w.root)["state"] == ship.STATE_FIXING)
    check("a stale completion cannot vouch for the fix",
          not os.path.exists(os.path.join(w.root, ".build_complete")))


def test_lint_errors_keep_the_file_they_belong_to():
    """ESLint prints the path once, above bare `line:col` problems."""
    log = "\n".join(f"unit\tRun npm run lint\t2026-10-29T12:30:0{i}.0Z {line}" for i, line in enumerate([
        "##[group]Run npm run lint",
        "npm run lint",
        "##[endgroup]",
        "/home/runner/work/veriform-ui/veriform-ui/src/routes/employer-verify.$token.tsx",
        "##[error]  232:22  error  Replace this  prettier/prettier",
        "",
        "/home/runner/work/veriform-ui/veriform-ui/src/services/org.service.ts",
        "##[error]  609:30  error  Replace that  prettier/prettier",
        "\u2716 674 problems (665 errors, 9 warnings)",
    ])) + "\n"
    step = ship._steps_from_log(log)[0]
    check("the first error names its file",
          step["errors"][0].startswith("src/routes/employer-verify.$token.tsx:232:22"), step["errors"])
    check("and the second its own",
          step["errors"][1].startswith("src/services/org.service.ts:609:30"), step["errors"])
    check("the runner's checkout path is gone",
          not any("/home/runner" in e for e in step["errors"] + step["tail"]), step)
    check("a summary line is not mistaken for a location",
          any(e.startswith("\u2716 674 problems") for e in step["errors"]), step["errors"])


def test_test_runner_chatter_is_not_reported_as_an_error():
    log = step_log("backend", "Backend test suite", "npm test", [
        "stdout | test/prelude.test.ts > maps failure outcome",
        '{"msg":"prelude call","outcome":"failure"}',
        "\u2713 lets the owner write, which is the same statement that just failed 3ms",
        "\u00d7 POST /claims/:id/domain-challenges finds the owner's claim 26ms",
        "AssertionError: expected 404 to be 200",
    ])
    errors = ship._steps_from_log(log)[0]["errors"]
    check("captured output is dropped", not any(e.startswith("stdout |") for e in errors), errors)
    check("logged JSON is dropped", not any(e.startswith("{") for e in errors), errors)
    check("a passing test is dropped", not any(e.startswith("\u2713") for e in errors), errors)
    check("the failing test is kept", any("finds the owner's claim" in e for e in errors), errors)
    check("and its assertion", "AssertionError: expected 404 to be 200" in errors, errors)


def test_the_record_says_how_many_errors_were_left_out():
    step = {"job": "unit", "step": "lint", "command": "npm run lint",
            "errors": [f"e{i}" for i in range(15)], "error_count": 665, "tail": []}
    out = ship._causes_markdown([{"name": "test", "conclusion": "FAILURE", "steps": [step]}],
                                fenced=False, max_errors=5)
    check("the true remainder is reported", "…and 660 more error lines" in out, out)


def test_without_a_design_the_fix_is_not_handed_an_empty_scope():
    with FakeGh() as gh:
        w = shipped(gh, design=False)
        gh.script("pr view", pr_view(w.head(), [check_run("FAILURE")]))
        gh.script("run view", {"out": RUFF_LOG, "err": "", "code": 0})
        ship.advance(w.root)
        instruction, _, _ = ship.ci_fix_request(w.root)
    check("no design block", "APPROVED DESIGN" not in instruction, instruction)


def test_a_fix_that_failed_its_own_gates_is_not_pushed():
    with FakeGh() as gh:
        w, head = failed_on_pr(gh)
        ship.ci_fix_request(w.root)
        w.write("widget.py", "cache = {}  # half done\n")
        state = ship.fix_build_finished(w.root)
    remote = git(w.root, "ls-remote", "origin", "agent/build-1700000000").stdout.split()[0]
    check("back to waiting for approval", state == ship.STATE_CI_FAILED, state)
    check("the PR branch did not move", remote == head, (remote, head))
    notice = ship.pending_offer(w.root)
    check("the user is told", notice and "without passing its own gates" in notice, notice)


def test_a_passing_fix_is_pushed_to_the_same_pr_and_rechecked():
    with FakeGh() as gh:
        w, head = failed_on_pr(gh)
        ship.ci_fix_request(w.root)
        w.write("widget.py", "cache = {}\n# fixed\n")
        w.write(".build_complete", "VERIFIED\nSAFE\n")
        state = ship.fix_build_finished(w.root)
        new_head = w.head()
        creates = [c for c in gh.calls() if c.startswith("pr create")]
        gh.script("pr view", pr_view(new_head, [check_run()]),
                  pr_view(new_head, merge_oid="m" * 40))
        ship.advance(w.root)
        merges = gh.merges()
    check("waiting on CI again", state == ship.STATE_AWAITING_CHECKS, state)
    check("a new commit", new_head != head)
    check("on the same PR, not a new one", len(creates) == 1, creates)
    remote = git(w.root, "ls-remote", "origin", "agent/build-1700000000").stdout.split()[0]
    check("pushed", remote == new_head)
    committed = git(w.root, "show", "--name-only", "--format=", new_head).stdout
    check("the completion flag is not committed", ".build_complete" not in committed, committed)
    check("merged only now, at the fixed head",
          len(merges) == 1 and new_head in merges[0], merges)
    with open(ship.ci_failure_path(w.root)) as f:
        doc = f.read()
    check("and the failure record is resolved", "**Status:** RESOLVED" in doc, doc[:200])


def test_red_main_after_merge_is_a_failure_with_a_fix_loop():
    with FakeGh() as gh:
        w = shipped(gh)
        head = w.head()
        gh.script("pr view", pr_view(head, [check_run()]), pr_view(head, merge_oid="m" * 40))
        ship.advance(w.root)
        gh.script("run list", [{"databaseId": 9, "status": "completed", "conclusion": "failure",
                                "workflowName": "CI", "name": "CI", "url": "u"}])
        gh.script("run view", {"out": RUFF_LOG, "err": "", "code": 0})
        state = ship.advance(w.root)
        notice = ship.pending_offer(w.root)
        instruction, env, refusal = ship.ci_fix_request(w.root)
    check("a failure", state == ship.STATE_CI_FAILED, state)
    check("that says main is red", notice and "`main` is red" in notice, notice)
    check("and how to revert", notice and "git revert mmmmmmm" in notice, notice)
    check("the fix starts from main", refusal is None and w.branch() == "main", refusal)
    check("on a branch of its own", env == {}, env)


def test_a_red_main_fix_opens_its_own_pull_request():
    with FakeGh() as gh:
        w = shipped(gh)
        head = w.head()
        gh.script("pr view", pr_view(head, [check_run()]), pr_view(head, merge_oid="m" * 40))
        ship.advance(w.root)
        gh.script("run list", [{"databaseId": 9, "status": "completed", "conclusion": "failure",
                                "workflowName": "CI", "name": "CI", "url": "u"}])
        ship.advance(w.root)
        ship.ci_fix_request(w.root)
        git(w.root, "checkout", "-q", "-b", "agent/build-1700000999")
        w.write("widget.py", "cache = {}\n# fixed on main\n")
        w.write(".build_complete", "VERIFIED\nSAFE\n")
        gh.script("pr create", {"out": "https://github.test/pull/2\n", "err": "", "code": 0})
        state = ship.fix_build_finished(w.root)
    marker = ship.read_marker(w.root)
    check("waiting on the new PR", state == ship.STATE_AWAITING_CHECKS, state)
    check("PR #2", marker["pr_number"] == "2", marker.get("pr_number"))
    check("for the fix branch", marker["branch"] == "agent/build-1700000999", marker["branch"])


def test_a_conflict_with_main_is_reported_before_waiting_on_ci():
    with FakeGh() as gh:
        w = shipped(gh)
        gh.script("pr view", pr_view(w.head(), [check_run(status="QUEUED")],
                                     mergeable="CONFLICTING"))
        state = ship.advance(w.root)
    check("a failure", state == ship.STATE_CI_FAILED, state)
    check("marked as a conflict", ship.read_marker(w.root)["failure"]["conflict"])
    check("not merged", not gh.merges())


def test_a_branch_moved_by_someone_else_stalls_until_adopted():
    with FakeGh() as gh:
        w = shipped(gh)
        gh.script("pr view", pr_view("f" * 40, [check_run()]))
        check("waits during the grace period",
              ship.advance(w.root) == ship.STATE_AWAITING_CHECKS)
        age(w, 1000)
        state = ship.advance(w.root)
        check("then stalls", state == ship.STATE_STALLED, state)
        check("without merging code it never checked", not gh.merges(), gh.calls())
        gh.script("pr view", {"out": json.dumps({"headRefOid": "f" * 40,
                                                 "url": "https://github.test/pull/1"}),
                              "err": "", "code": 0})
        ship.resume_watch(w.root)
    marker = ship.read_marker(w.root)
    check("!ship watch adopts the new head",
          marker["state"] == ship.STATE_AWAITING_CHECKS and marker["head_sha"] == "f" * 40, marker)


def test_no_checks_and_no_workflows_never_merges():
    with FakeGh() as gh:
        w = shipped(gh)
        gh.script("pr view", pr_view(w.head(), []))
        age(w, 1000)
        state = ship.advance(w.root)
    check("stalls", state == ship.STATE_STALLED, state)
    check("not merged", not gh.merges())


def test_a_refused_merge_stalls_with_the_pr_still_open():
    with FakeGh(merge_fails=True) as gh:
        w = shipped(gh)
        gh.script("pr view", pr_view(w.head(), [check_run()]))
        state = ship.advance(w.root)
    notice = ship.pending_offer(w.root)
    check("stalled", state == ship.STATE_STALLED, state)
    check("it gives the PR url", notice and "https://github.test/pull/1" in notice, notice)
    check("and GitHub's reason", notice and "protected branch" in notice, notice)


def test_a_new_build_does_not_orphan_a_ship_in_flight():
    with FakeGh() as gh:
        w = shipped(gh)
    ship.clear_offer(w.root)
    check("the CI record survives a new build starting",
          ship.read_marker(w.root)["state"] == ship.STATE_AWAITING_CHECKS)
    check("and that build's finish does not overwrite it",
          ship.record_build_complete(w.root, base="main") is None)
    check("!ship refuses to start a second ship", "Already shipping" in ship.propose(w.root))


# =============================================================================
# TRIAGE
# =============================================================================

BASE_SHA = "b" * 40


def step_log(job, step, command, lines):
    rows = [f"##[group]Run {command}", command, "shell: /usr/bin/bash -e {0}", "##[endgroup]"]
    rows += lines + ["##[error]Process completed with exit code 1."]
    return "\n".join(f"{job}\t{step}\t2026-10-29T12:30:00.0Z {r}" for r in rows) + "\n"


def triage_check(workflow, job, run_id, job_id):
    return {"__typename": "CheckRun", "name": job, "workflowName": workflow,
            "status": "COMPLETED", "conclusion": "FAILURE",
            "detailsUrl": f"https://github.test/o/r/actions/runs/{run_id}/job/{job_id}"}


def red_pr(gh, baseline=True):
    """
    The veriform-ui shape: lint already red on main, an RLS test this change
    broke, and a runner image Playwright will not install on.
    """
    w = shipped(gh)
    head = w.head()
    gh.script("pr view", pr_view(head, [
        triage_check("test", "unit", 11, 21),
        triage_check("tenant-isolation", "isolation", 12, 22),
        triage_check("ui", "verify", 13, 23),
    ]))
    gh.script("run view 11 --job 21 --log-failed", {"out": step_log(
        "unit", "Run npm run lint", "npm run lint",
        ["/home/runner/work/r/r/src/services/org.service.ts",
         "##[error]  609:30  error  Replace that  prettier/prettier"]), "err": "", "code": 0})
    gh.script("run view 12 --job 22 --log-failed", {"out": step_log(
        "isolation", "Routes under enforced RLS", "npm run test:rls",
        ["FAIL  test/rls-enforced.test.ts > finds the owner's claim"]), "err": "", "code": 0})
    gh.script("run view 13 --job 23 --log-failed", {"out": step_log(
        "verify", "Install Chromium", "npx playwright install --with-deps chromium",
        ["Error: ERROR: Playwright does not support chromium-headless-shell on ubuntu26.04-x64"]),
        "err": "", "code": 0})
    if baseline:
        gh.script("api repos/{owner}/{repo}/commits/main", {"out": BASE_SHA + "\n", "err": "", "code": 0})
        gh.script(f"run list --commit {BASE_SHA}", [
            {"databaseId": 1, "workflowName": "test", "status": "completed", "conclusion": "failure"},
            {"databaseId": 2, "workflowName": "tenant-isolation", "status": "completed", "conclusion": "success"},
            {"databaseId": 3, "workflowName": "ui", "status": "completed", "conclusion": "failure"},
        ])
        gh.script("run view 1 --json", {"jobs": [{"name": "unit", "conclusion": "failure", "steps": [
            {"name": "Run npm run lint", "conclusion": "failure"}]}]})
        gh.script("run view 3 --json", {"jobs": [{"name": "verify", "conclusion": "failure", "steps": [
            {"name": "Install Chromium", "conclusion": "failure"}]}]})
    ship.advance(w.root)
    return w


def origins(w):
    causes = ship.read_marker(w.root)["failure"]["causes"]
    return {c["workflow"]: c.get("origin") for c in causes}


def test_failures_are_triaged_against_the_base_branch():
    with FakeGh() as gh:
        w = red_pr(gh)
    found = origins(w)
    check("lint red on main too is pre-existing", found.get("test") == "preexisting", found)
    check("the RLS test is this change's", found.get("tenant-isolation") == "introduced", found)
    check("the runner image is the environment", found.get("ui") == "environment", found)


def test_the_record_separates_what_is_ours_from_what_was_already_red():
    with FakeGh() as gh:
        w = red_pr(gh)
    with open(ship.ci_failure_path(w.root)) as f:
        doc = f.read()
    ours = doc.index("## What has to change")
    before = doc.index("## Already failing on `main`")
    machine = doc.index("## CI environment, not code")
    check("this change's failure is under what has to change",
          ours < doc.index("test:rls") < before, doc)
    check("lint is under already failing", before < doc.index("npm run lint") < machine, doc)
    check("the runner is under environment", doc.index("playwright install") > machine, doc)
    check("it names the baseline it compared against", "`bbbbbbb`" in doc, doc[:600])


def test_approve_fixes_only_what_this_change_broke():
    with FakeGh() as gh:
        w = red_pr(gh)
        instruction, _, refusal = ship.ci_fix_request(w.root)
    check("no refusal", refusal is None, refusal)
    check("the RLS failure is in scope", "test:rls" in instruction, instruction)
    check("main's lint debt is not", "npm run lint" not in instruction, instruction)
    check("nor the runner image", "playwright" not in instruction, instruction)
    check("the fix must run CI's own command before it is done",
          "VERIFY:" in instruction and "- `npm run test:rls`" in instruction, instruction)


def test_approve_all_takes_on_the_older_failures_but_never_the_runner():
    with FakeGh() as gh:
        w = red_pr(gh)
        instruction, _, _ = ship.ci_fix_request(w.root, include_preexisting=True)
    check("lint is now in scope", "npm run lint" in instruction, instruction)
    check("the runner is still not", "playwright" not in instruction, instruction)


def test_a_red_main_that_was_already_red_is_not_blamed_on_the_merge():
    with FakeGh() as gh:
        w = shipped(gh)
        head = w.head()
        merge = "m" * 40
        gh.script("pr view", pr_view(head, [check_run()]), pr_view(head, merge_oid=merge))
        ship.advance(w.root)
        gh.script(f"run list --commit {merge}", [{"databaseId": 9, "status": "completed",
                                                  "conclusion": "failure", "workflowName": "test",
                                                  "name": "test", "url": "u"}])
        gh.script("run view 9 --log-failed", {"out": step_log(
            "unit", "Run npm run lint", "npm run lint", ["src/a.ts", "1:1  error  x"]),
            "err": "", "code": 0})
        gh.script(f"api repos/{{owner}}/{{repo}}/commits/{merge}",
                  {"out": BASE_SHA + "\n", "err": "", "code": 0})
        gh.script(f"run list --commit {BASE_SHA}", [
            {"databaseId": 1, "workflowName": "test", "status": "completed", "conclusion": "failure"}])
        gh.script("run view 1 --json", {"jobs": [{"name": "unit", "conclusion": "failure", "steps": [
            {"name": "Run npm run lint", "conclusion": "failure"}]}]})
        state = ship.advance(w.root)
        notice = ship.pending_offer(w.root)
        _, _, refusal = ship.ci_fix_request(w.root)
    check("still recorded as a failure", state == ship.STATE_CI_FAILED, state)
    check("compared against the commit before the squash",
          ship.read_marker(w.root)["failure"]["baseline"] == BASE_SHA)
    check("the notice says it was red before", notice and "was red before" in notice, notice)
    check("and does not suggest reverting the merge", notice and "git revert" not in notice, notice)
    check("plain !approve has nothing to fix", refusal and "Nothing here is this change's" in refusal, refusal)
    check("and points at !approve all", refusal and "`!approve all`" in refusal, refusal)


def test_without_a_baseline_every_failure_is_treated_as_ours():
    with FakeGh() as gh:
        w = red_pr(gh, baseline=False)
        instruction, _, _ = ship.ci_fix_request(w.root)
    found = origins(w)
    check("unverified, not pre-existing", found.get("test") == "unverified", found)
    check("so lint stays in scope", "npm run lint" in instruction, instruction)
    check("the runner is still recognised from its log", found.get("ui") == "environment", found)


def test_recheck_rewrites_a_record_from_before_triage():
    with FakeGh() as gh:
        w = red_pr(gh)
        marker = ship.read_marker(w.root)
        for cause in marker["failure"]["causes"]:  # what an old parser left
            cause.pop("origin", None)
            for st in cause["steps"]:
                st.pop("origin", None)
        ship._save_marker(w.root, marker)
        with open(ship.ci_failure_path(w.root), "w") as f:
            f.write("# old record\n")
        out = ship.recheck(w.root)
    with open(ship.ci_failure_path(w.root)) as f:
        doc = f.read()
    check("the record is rewritten", doc.startswith("# CI failure"), doc[:80])
    check("with triage", "## Already failing on `main`" in doc, doc)
    check("and the reply is the new summary", "introduced by this change" in out, out)
    check("still waiting for approval",
          ship.read_marker(w.root)["state"] == ship.STATE_CI_FAILED)


def test_recheck_hands_a_now_green_pr_back_to_the_gate():
    with FakeGh() as gh:
        w = red_pr(gh)
        gh.script("pr view", pr_view(w.head(), [check_run()]))
        out = ship.recheck(w.root)
    check("back to waiting on checks",
          ship.read_marker(w.root)["state"] == ship.STATE_AWAITING_CHECKS, out)


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
