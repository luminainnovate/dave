"""
Shipping the branch a build leaves behind.

The build container works in `/workspace` (the conversation's bound project),
checks out `agent/build-<epoch>` off whatever HEAD it found, and stops. Nothing
in it commits: `!approve` finishes with a dirty tree on a branch nobody has
pushed. The user's only route to `main` was to leave the chat and use git by
hand — `!pr` looks like that route but refuses, because it reads the in-memory
`touched` list that only the chat-mode Expert's write tools populate.

This module closes that gap:

  * `record_build_complete` is called when the container exits. It writes a
    marker file next to the design documents, so the offer survives an
    orchestrator restart between the build finishing and the user's next
    message — the state it records cannot live in memory, because the two
    events are minutes to hours apart.
  * `pending_offer` returns the offer text once, then marks it announced.
  * `propose` / `confirm` are the two halves of `!ship`. Squash-merging into a
    default branch is irreversible and outward-facing, so nothing here acts on
    the first call: `!ship` prints exactly what it would do, and only
    `!ship confirm` runs it.
  * `confirm` opens the pull request and stops. The merge belongs to the CI
    gate (`advance`), which squash-merges only once every check on the pushed
    commit has passed, then watches the base branch's run for the squash
    commit. A failure at either point is written to `ci_failure.md` for review,
    and `!approve` turns it into a fix build that goes back through the gate.

Every git and gh invocation lives here rather than in the orchestrator, so the
whole flow is testable against a throwaway repository.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time

from typing import Optional

from repo_tools import _current_branch, _default_branch, _git, _is_git_repo

logger = logging.getLogger("Bob-Orchestrator")

# The container's branch naming (cline-builder/entrypoint.sh). Anything else is
# a branch a human chose, and `!ship` will not commit onto it.
BUILD_BRANCH_PREFIX = "agent/build-"

# Marker recording a build that is ready to ship, alongside `.design_pass` so
# both pieces of cross-invocation state live in one place. Deliberately not
# named `.build_complete`: the container already owns a file by that name at the
# workspace root, meaning "the agent judged its own work VERIFIED and SAFE".
MARKER_NAME = ".ship_pending"

CONTEXT_DIR = ".cline_context"

# Orchestration scratch that happens to sit inside the workspace: the design
# documents, the serialized conversation, this marker. A bare `git add -A` would
# sweep the lot into the user's pull request, conversation transcript included,
# so nothing under these ever reaches the file list that gets staged.
#
# They are filtered out of that list rather than excluded with a pathspec.
# `git add -A -- . ':(exclude).cline_context'` fails outright in any repo whose
# .gitignore already covers them - git refuses a pathspec that names an ignored
# path - which is every workspace the build bootstraps a .gitignore for.
EXCLUDED_DIRS = (CONTEXT_DIR, ".cline_logs")

# The same, for the container's scratch at the workspace root. Neither is in the
# .gitignore the build bootstraps, so both used to ride along into the PR - and
# `.build_complete` is how a fix build reports success, so it must not be
# committed and then found again by the next one.
EXCLUDED_FILES = (".build_complete", ".clinerules")

# Cap on paths per `git add` invocation, so a build touching thousands of files
# cannot overflow the argument list.
ADD_BATCH = 500

MAX_TITLE_CHARS = 72

# How many files to name in chat before summarising. A build touching 200 files
# should not push its own offer off the screen.
MAX_LISTED_FILES = 20

# Where a ship is, recorded in the marker as `state`. A marker written before
# the CI gate existed has no `state` and reads as ready.
STATE_READY = "ready"                      # build finished, not shipped
STATE_AWAITING_CHECKS = "awaiting_checks"  # PR open, waiting on its checks
STATE_AWAITING_BASE = "awaiting_base_ci"   # merged, waiting on base's run of the squash commit
STATE_CI_FAILED = "ci_failed"              # root causes written, waiting for `!approve`
STATE_FIXING = "fixing"                    # a fix build is running
STATE_STALLED = "stalled"                  # needs a human: timeout, closed PR, refused merge
STATE_INTEGRATED = "integrated"            # merged, and green on the base branch
WATCHED_STATES = (STATE_AWAITING_CHECKS, STATE_AWAITING_BASE)

# The review document for a CI failure. Next to the design documents, and on
# disk rather than in memory for the same reason the marker is: a failure can
# land at 3am and be read the next morning, across any number of restarts.
CI_FAILURE_NAME = "ci_failure.md"

# The approved design a shipped PR implements, frozen at `!ship confirm`. A CI
# fix is scoped against it: the cheapest way to turn a red check green is often
# to delete the behaviour it guards, and the code alone cannot say which
# behaviour was the point. Not `distill_*.md`: every non-resume container run -
# which a fix build is - deletes that glob, design and all.
SHIP_DESIGN_NAME = "ship_design.md"

# How much of the design a fix instruction carries inline. The rest is on disk.
MAX_DESIGN_CHARS = 4000


# =============================================================================
# MARKER STATE
# =============================================================================

def marker_path(workspace: str) -> str:
    return os.path.join(os.path.abspath(workspace), CONTEXT_DIR, MARKER_NAME)


def read_marker(workspace: str) -> Optional[dict]:
    path = marker_path(workspace)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Unreadable build marker at {path}: {e}")
        return None
    return data if isinstance(data, dict) else None


def _save_marker(workspace: str, data: dict) -> None:
    path = marker_path(workspace)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not write the build marker: {e}")


def clear_marker(workspace: str) -> None:
    """Drops the marker unconditionally."""
    try:
        os.remove(marker_path(workspace))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"Could not clear the build marker: {e}")


def in_flight(marker: Optional[dict]) -> bool:
    """True once `!ship confirm` has run and the CI gate has not finished with it."""
    return bool(marker) and marker.get("state", STATE_READY) not in (STATE_READY, STATE_INTEGRATED)


def clear_offer(workspace: str) -> None:
    """
    Drops an unshipped offer when a new build starts. Leaves a ship in flight.

    A plain clear here used to be harmless: the marker only ever meant "a build
    is waiting to be shipped". It now also holds an open pull request and its CI
    record, and a `!architect` run while that PR waits on CI must not orphan it.
    """
    if not in_flight(read_marker(workspace)):
        clear_marker(workspace)


def base_branch(workspace: str) -> str:
    """
    The branch a build should merge back into.

    Read at launch, while HEAD is still the user's branch. Afterwards the answer
    is the agent branch itself, and `_default_branch` falls back to exactly that
    — so asking later would propose merging a branch into itself.
    """
    root = os.path.abspath(workspace)
    if not _is_git_repo(root):
        return ""
    current = _current_branch(root)
    if current and not current.startswith(BUILD_BRANCH_PREFIX):
        return current
    return _default_branch(root)


def record_build_complete(workspace: str, container: str = "", base: str = "") -> Optional[str]:
    """
    Note that a build finished with work on a branch. Returns the branch, or None.

    Silent about every uninteresting case — a project with no git, a build that
    never left the user's branch, a build that changed nothing. A marker written
    for those would produce an offer to ship an empty diff.
    """
    root = os.path.abspath(workspace or "")
    if not root or not _is_git_repo(root):
        return None

    branch = _current_branch(root)
    if not branch.startswith(BUILD_BRANCH_PREFIX):
        logger.info(f"Build finished on `{branch}`, not a build branch. Nothing to offer.")
        return None

    base = base or _default_branch(root)
    if not _has_work(root, base):
        logger.info(f"Build finished on `{branch}` with no changes. Nothing to offer.")
        return None

    existing = read_marker(workspace)
    if in_flight(existing):
        logger.warning(
            f"Build finished on `{branch}`, but `{existing.get('branch')}` is still "
            f"going through the CI gate ({existing.get('state')}). Not offering it."
        )
        return None

    _save_marker(workspace, {
        "state": STATE_READY,
        "container": container,
        "branch": branch,
        "base": base,
        "finished_at": time.time(),
        "announced": False,
        "title": None,
    })
    logger.info(f"Build complete: `{branch}` -> `{base}` is ready to ship.")
    return branch


def pending_offer(workspace: str) -> Optional[str]:
    """
    The one-time offer shown after a build finishes, or None.

    Marks itself announced before returning, so a build is offered once and then
    waits to be asked about. `!ship` stays available either way.
    """
    marker = read_marker(workspace)
    if not marker or marker.get("announced"):
        return None

    # The CI gate reports through the same one-time slot: a failure, a stall or
    # an integration happens in the background, and the next turn is the first
    # chance to say so.
    notice = marker.get("notice")
    if not notice and marker.get("state", STATE_READY) != STATE_READY:
        return None

    marker["announced"] = True
    if marker.get("state") == STATE_INTEGRATED:
        clear_marker(workspace)
    else:
        _save_marker(workspace, marker)
    if notice:
        return notice

    root = os.path.abspath(workspace)
    branch = marker.get("branch", "?")
    base = marker.get("base", "?")
    files = _changed_files(root, base)
    return (
        f"🌿 **The build finished on `{branch}`.**\n\n"
        f"{_file_summary(files)}\n\n"
        f"Nothing is committed yet. `!ship` shows exactly what would be committed, "
        f"pushed and squash-merged into `{base}` — it does not do it. "
        f"`!ship confirm` does."
    )


# =============================================================================
# GIT INSPECTION
# =============================================================================

def _remote_ref(root: str, base: str) -> str:
    """`origin/main` when it exists, else the local branch."""
    if _git(root, "rev-parse", "--verify", f"refs/remotes/origin/{base}")[0] == 0:
        return f"origin/{base}"
    return base


def _status_paths(root: str) -> set:
    """
    Working-tree and index paths, from `git status`.

    -uall lists an untracked directory as its files, rather than as "dir/" -
    which would tell the user a directory changed and nothing more. -z because
    the readable format quotes and escapes any path with a space or a non-ASCII
    character in it, and this list is fed straight back to `git add`.

    Ignored files never appear here at all, which is what makes staging exactly
    this list safe.
    """
    code, out = _git(root, "status", "--porcelain", "-uall", "-z")
    if code != 0:
        return set()

    paths = set()
    records = [r for r in out.split("\0") if r]
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if len(record) < 4:
            continue
        status, path = record[:2], record[3:]
        paths.add(path)
        # A rename or copy is followed by its source path in its own record.
        # Both halves matter: the old path is a deletion that has to be staged.
        if status[0] in ("R", "C") or status[1] in ("R", "C"):
            if i < len(records):
                paths.add(records[i])
                i += 1
    return paths


def _changed_files(root: str, base: str) -> list:
    """Every path this branch changes: working tree, index and commits."""
    paths = _status_paths(root)

    code, out = _git(root, "diff", "--name-only", f"{_remote_ref(root, base)}...HEAD")
    if code == 0:
        paths.update(ln.strip() for ln in out.splitlines() if ln.strip())

    return sorted(p for p in paths
                  if p.split("/")[0] not in EXCLUDED_DIRS and p not in EXCLUDED_FILES)


def _has_work(root: str, base: str) -> bool:
    return bool(_changed_files(root, base))


def _file_summary(files: list) -> str:
    if not files:
        return "*No files changed.*"
    listed = files[:MAX_LISTED_FILES]
    lines = [f"**{len(files)} file(s):**"] + [f"- `{p}`" for p in listed]
    if len(files) > len(listed):
        lines.append(f"- …and {len(files) - len(listed)} more")
    return "\n".join(lines)


def _design_title(workspace: str) -> tuple:
    """(title, document path) taken from the design pass this build resumed."""
    ctx = os.path.join(os.path.abspath(workspace), CONTEXT_DIR)
    pass_key = "architect"
    try:
        with open(os.path.join(ctx, ".design_pass"), "r", encoding="utf-8") as f:
            value = f.read().strip().lower()
        if value in ("architect", "bugfix"):
            pass_key = value
    except Exception:
        pass

    doc = os.path.join(ctx, f"distill_{pass_key}.md")
    rel_doc = f"{CONTEXT_DIR}/distill_{pass_key}.md"
    if not os.path.exists(doc):
        return "", rel_doc
    try:
        with open(doc, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return "", rel_doc

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        # The first heading is the document's own subject line; anything else
        # is prose that reads badly as a commit title.
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if title:
                return title[:MAX_TITLE_CHARS], rel_doc
            continue
        break
    return "", rel_doc


def _commit_text(workspace: str, marker: dict, files: list) -> tuple:
    """(title, body) for the commit and the pull request."""
    design_title, rel_doc = _design_title(workspace)
    title = (marker.get("title") or design_title
             or f"Build from br.ai.n on {marker.get('branch', 'an agent branch')}")
    body = "\n".join([
        "Built by br.ai.n from a reviewed design pass.",
        "",
        f"**Design document:** `{rel_doc}`",
        f"**Branch:** `{marker.get('branch')}` → `{marker.get('base')}`",
        "",
        _file_summary(files),
    ])
    return title[:MAX_TITLE_CHARS], body


# =============================================================================
# GATE
# =============================================================================

def _gate(workspace: str, need_remote: bool = True):
    """
    (marker, root, files, refusal). Exactly one of marker and refusal is set.

    Every refusal names the specific thing that is wrong, in the style of the
    `!approve` gate — "it failed" sends the user back to a shell to find out
    which of six preconditions they missed.
    """
    root = os.path.abspath(workspace or "")
    if not root or not _is_git_repo(root):
        return None, root, [], (
            f"❌ **Not a git repository:** `{root}` is not under version control, "
            f"so there is no branch to ship."
        )

    marker = read_marker(workspace)
    branch = _current_branch(root)

    if in_flight(marker):
        return None, root, [], (
            f"🚦 **Already shipping `{marker.get('branch')}`** "
            f"({_STATE_LABELS.get(marker.get('state'), marker.get('state'))}).\n\n"
            f"`!ship status` shows where it is."
        )

    if not branch.startswith(BUILD_BRANCH_PREFIX):
        return None, root, [], (
            f"🛑 **Not on a build branch.** The workspace is on `{branch}`, and "
            f"`!ship` only ever commits what a build left on `{BUILD_BRANCH_PREFIX}*`. "
            f"Run `!architect` → `!approve` first, or handle this branch yourself."
        )

    if not marker:
        # A build branch with no marker: the container is probably still running,
        # or the marker was cleared by a later build.
        return None, root, [], (
            f"⚠️ **No finished build recorded.** The workspace is on `{branch}` but "
            f"nothing has reported a completed build — check `!status`, and try "
            f"again once the container has exited."
        )

    base = marker.get("base") or _default_branch(root)
    files = _changed_files(root, base)
    if not files:
        return None, root, [], (
            f"📭 **Nothing to ship.** `{branch}` is identical to `{base}` — the "
            f"build produced no changes. `!logs` shows what the container did."
        )

    if need_remote:
        code, remotes = _git(root, "remote")
        if code != 0 or "origin" not in remotes.split():
            return None, root, files, (
                f"❌ **No `origin` remote.** A pull request needs somewhere to push. "
                f"The work is safe on `{branch}` — commit it locally with git, or add "
                f"a remote and run `!ship` again."
            )
        if not shutil.which("gh"):
            return None, root, files, (
                f"❌ **`gh` CLI not found.** Install and authenticate the GitHub CLI to "
                f"open and merge pull requests. The work is safe on `{branch}`."
            )

    return marker, root, files, None


# =============================================================================
# COMMANDS
# =============================================================================

def propose(workspace: str, title: str = "") -> str:
    """`!ship` — states exactly what would happen. Changes nothing but the title."""
    marker, root, files, refusal = _gate(workspace)
    if refusal:
        return refusal

    if title:
        marker["title"] = title[:MAX_TITLE_CHARS]
        _save_marker(workspace, marker)

    commit_title, _ = _commit_text(workspace, marker, files)
    branch, base = marker["branch"], marker["base"]
    return (
        f"🚢 **Ready to ship `{branch}` → `{base}`.**\n\n"
        f"**Title:** {commit_title}\n\n"
        f"{_file_summary(files)}\n\n"
        f"This would:\n"
        f"1. Commit those files on `{branch}` (`{CONTEXT_DIR}/` is excluded).\n"
        f"2. Push the branch to `origin`.\n"
        f"3. Open a pull request into `{base}`.\n"
        f"4. Wait for **every CI check** on that commit to pass.\n"
        f"5. **Squash-merge it**, delete the branch, and wait for `{base}`'s own CI "
        f"run on the squash commit.\n\n"
        f"If CI fails at step 4 the PR stays open and the failure is written up for "
        f"you to review. Step 5 rewrites `{base}` and cannot be undone from chat.\n\n"
        f"- **Go ahead:** `!ship confirm`\n"
        f"- **Different title:** `!ship <your title>`, then confirm\n"
        f"- **Read the diff first:** `git -C {root} diff {base}`"
    )


def _stageable(root: str, files: list) -> list:
    """
    The subset of `files` that `git add` can still be given.

    A rename the agent staged itself leaves its old path in HEAD but in neither
    the index nor the working tree, and `git add -A -- <that path>` is a fatal
    "pathspec did not match any files" that would take the whole commit down
    with it. There is nothing left to stage for those - the rename is already
    in the index - so they are dropped rather than repaired.
    """
    code, out = _git(root, "ls-files", "-z")
    indexed = {p for p in out.split("\0") if p} if code == 0 else set()
    candidates = [p for p in files
                  if p in indexed or os.path.exists(os.path.join(root, p))]

    # A path the branch's commits touched can still be ignored today, by a
    # global ignore as readily as by the repo's own (`.claude/settings.local.json`
    # is the usual one). `git add` refuses to be handed one, fatally, and an
    # untracked ignored file has nothing to stage anyway. Tracked paths are
    # never reported as ignored, so deletions of them still go through.
    unindexed = [p for p in candidates if p not in indexed]
    if not unindexed:
        return candidates
    result = subprocess.run(
        ["git", "-C", root, "check-ignore", "-z", "--stdin"],
        input="\0".join(unindexed) + "\0", capture_output=True, text=True,
    )
    ignored = {p for p in result.stdout.split("\0") if p}
    return [p for p in candidates if p not in ignored]


def _commit_and_push(root: str, branch: str, title: str, body: str, files: list) -> None:
    """Stage `files`, commit them if anything is staged, push. Raises RuntimeError."""
    # Stage exactly the paths the proposal listed. Naming them, rather than
    # sweeping the workspace and excluding the scratch directories, keeps
    # the commit identical to what the user was shown and keeps every
    # pathspec clear of anything .gitignore covers.
    stageable = _stageable(root, files)
    for i in range(0, len(stageable), ADD_BATCH):
        _git(root, "add", "-A", "--", *stageable[i:i + ADD_BATCH], check=True)

    # A fix for a merge conflict commits the merge itself. Conflict markers left
    # in a file would commit cleanly and only fail later, in CI or in production.
    if _git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0:
        _, check_out = _git(root, "diff", "--cached", "--check")
        leftovers = [ln for ln in check_out.splitlines() if "conflict marker" in ln]
        if leftovers:
            raise RuntimeError("Unresolved conflict markers:\n" + "\n".join(leftovers[:20]))

    code, staged = _git(root, "diff", "--cached", "--name-only")
    merging = _git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD")[0] == 0
    if staged.strip() or merging:
        _git(root, "commit", "-m", title, "-m", body, check=True)
    else:
        # The agent committed as it went. The branch is still ahead of base,
        # or the gate would have refused, so there is work to push.
        logger.info(f"Nothing left to stage on {branch}; it is already committed.")

    _git(root, "push", "-u", "origin", branch, check=True)


def confirm(workspace: str) -> str:
    """
    `!ship confirm` — commit, push, open the PR. The CI gate does the merge.

    Merging here, the instant the PR opened, was a merge before any workflow had
    started: with no branch protection GitHub accepts it, so CI only ever ran
    on code that was already in the base branch.
    """
    marker, root, files, refusal = _gate(workspace)
    if refusal:
        return refusal

    branch, base = marker["branch"], marker["base"]
    title, body = _commit_text(workspace, marker, files)

    try:
        _commit_and_push(root, branch, title, body, files)
    except RuntimeError as e:
        return (
            f"❌ **Could not push `{branch}`.** Nothing was merged.\n\n"
            f"```\n{str(e)[:800]}\n```"
        )

    url, pr_error = _open_pull_request(root, title, body, branch, base)
    if not url:
        return (
            f"⚠️ **Pushed `{branch}`, but the pull request could not be opened.**\n\n"
            f"The work is committed and on `origin` — nothing is lost.\n\n"
            f"```\n{pr_error[:800]}\n```"
        )

    head = _head_sha(root)
    _await_checks(marker, url, head)
    marker["design_doc"] = _freeze_design(workspace)
    _save_marker(workspace, marker)
    return (
        f"🚦 **Pull request open — waiting for CI before merging.**\n\n"
        f"- **Branch:** `{branch}` → `{base}`\n"
        f"- **Commit:** `{head[:7]}`\n"
        f"- {url}\n\n"
        f"It is squash-merged only once every check on `{head[:7]}` passes, and "
        f"then `{base}`'s own run of the squash commit is checked too. If either "
        f"fails, the failure is written up here for review and the PR stays open.\n\n"
        f"- **Where it is:** `!ship status`"
    )


def _open_pull_request(root: str, title: str, body: str, branch: str, base: str) -> tuple:
    """(url, error). An existing open PR for this branch counts as success."""
    result = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body,
         "--head", branch, "--base", base],
        cwd=root, capture_output=True, text=True,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        url = next((ln for ln in output.splitlines() if ln.startswith("http")), output)
        return url, ""

    if "already exists" in output.lower():
        view = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "url", "-q", ".url"],
            cwd=root, capture_output=True, text=True,
        )
        url = view.stdout.strip()
        if url:
            return url, ""

    logger.error(f"gh pr create failed: {output}")
    return "", output


def _squash_merge(root: str, pr: str, head_sha: str) -> tuple:
    """
    (merged, error). Branch protection failing here is a refusal, not a crash.

    --match-head-commit makes GitHub refuse the merge if the branch moved after
    CI judged it. Without it, a push landing between the last poll and this call
    would be merged on the strength of checks that never saw it.
    """
    code, out, err = _gh(root, "pr", "merge", pr, "--squash", "--delete-branch",
                         "--match-head-commit", head_sha)
    if code == 0:
        return True, ""
    output = (out + err).strip()
    logger.error(f"gh pr merge failed: {output}")
    return False, output


def _return_to_base(root: str, base: str) -> None:
    """Best effort: leave the workspace somewhere sane for the next build."""
    _git(root, "checkout", base)
    _git(root, "pull", "--ff-only", "origin", base)


def _freeze_design(workspace: str) -> str:
    """Copy the design this PR was built from to SHIP_DESIGN_NAME. Returns its path, or ""."""
    _, rel_doc = _design_title(workspace)
    source = os.path.join(os.path.abspath(workspace), rel_doc)
    target = os.path.join(os.path.abspath(workspace), CONTEXT_DIR, SHIP_DESIGN_NAME)
    try:
        shutil.copyfile(source, target)
    except OSError as e:
        logger.warning(f"No design to freeze for the CI gate ({source}): {e}")
        return ""
    return f"{CONTEXT_DIR}/{SHIP_DESIGN_NAME}"


def _design_excerpt(workspace: str, marker: dict) -> str:
    rel = marker.get("design_doc")
    if not rel:
        return ""
    try:
        with open(os.path.join(os.path.abspath(workspace), rel), "r", encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return ""
    if len(text) > MAX_DESIGN_CHARS:
        text = text[:MAX_DESIGN_CHARS].rstrip() + f"\n\n[…truncated; the full design is {rel}]"
    return text


def _head_sha(root: str) -> str:
    code, out = _git(root, "rev-parse", "HEAD")
    return out.strip() if code == 0 else ""


def _pr_number(url: str) -> str:
    match = re.search(r"/pull/(\d+)", url or "")
    return match.group(1) if match else ""


# =============================================================================
# CI GATE
# =============================================================================
#
# Two checkpoints, because "CI passed on the PR" is not "the code integrated":
#
#   1. Every check on the pull request's head commit, matched by SHA so a stale
#      result can never count. Only then `gh pr merge --squash`.
#   2. The squash commit that merge creates on the base branch. It is a new
#      commit no run has tested until the push to base triggers one, and a
#      base that moved while the PR waited can turn green-on-PR into red-on-base.
#
# A failure at either writes CI_FAILURE_NAME - which checks failed, in which
# step, running what, and the error lines from the log - and stops for review.
# `!approve` turns that document into a fix build; `fix_build_finished` pushes
# the result and puts the PR back through checkpoint 1. Nothing merges until CI
# is green on the exact commit being merged.
#
# `advance` is one poll, never a wait, so the orchestrator's watcher owns the
# timing and every step can be tested against a scripted `gh`.

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "cline-builder", "agent_config.json")

CI_DEFAULTS = {
    # Total wait for one checkpoint before the gate stops and asks.
    "ci_gate_timeout_secs": 3600,
    "ci_gate_poll_secs": 30,
    # How long "no checks reported" means "not registered yet" rather than "no
    # CI". GitHub takes a few seconds to attach checks to a new push.
    "ci_gate_appear_grace_secs": 120,
}

# A gh call that hangs must not hang the watcher with it.
GH_TIMEOUT = 90

# Per failing step, how much of the log reaches the document and the chat.
MAX_ERROR_LINES = 15
MAX_TAIL_LINES = 40
MAX_NOTICE_ERRORS = 5

_PASSING = {"SUCCESS", "NEUTRAL", "SKIPPED"}

_STATE_LABELS = {
    STATE_READY: "built, not shipped",
    STATE_AWAITING_CHECKS: "waiting for CI on the pull request",
    STATE_AWAITING_BASE: "merged, waiting for CI on the base branch",
    STATE_CI_FAILED: "CI failed, waiting for `!approve`",
    STATE_FIXING: "a fix build is running",
    STATE_STALLED: "stopped, needs you",
    STATE_INTEGRATED: "integrated",
}

# A GitHub Actions log line from `gh run view --log-failed`:
# "<job>\t<step>\t<timestamp> <text>".
_LOG_TIMESTAMP_RE = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
# Runners colour their output, and `--log-failed` keeps the escapes. Left in,
# they land in ci_failure.md and in chat as `[36;1mnpm run lint[0m`.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ERROR_LINE_RE = re.compile(
    r"##\[error\]|\berror\b|\bfailed\b|\bfailure\b|traceback|exception"
    r"|^\S+:\d+:\d+: [A-Z]{1,4}\d+|^E\s{2,}",
    re.IGNORECASE,
)
_JOB_URL_RE = re.compile(r"/actions/runs/(\d+)/job/(\d+)")


def ci_limits() -> dict:
    """CI_DEFAULTS, overridden by agent_config.json's `limits` where set."""
    limits = dict(CI_DEFAULTS)
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            configured = json.load(f).get("limits", {})
        for key in CI_DEFAULTS:
            value = configured.get(key)
            if isinstance(value, (int, float)) and value > 0:
                limits[key] = value
    except Exception as e:
        logger.warning(f"CI gate limits unreadable, using defaults: {e}")
    return limits


def _gh(root: str, *args: str) -> tuple:
    """(returncode, stdout, stderr), never raising."""
    try:
        result = subprocess.run(["gh", *args], cwd=root, capture_output=True,
                                text=True, timeout=GH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, "", f"gh {' '.join(args[:2])} timed out after {GH_TIMEOUT}s"
    except FileNotFoundError:
        return 127, "", "gh not found"
    return result.returncode, result.stdout, result.stderr


def _gh_json(root: str, *args: str) -> tuple:
    """(parsed, error). parsed is None on any failure."""
    code, out, err = _gh(root, *args)
    if code != 0:
        return None, (err or out).strip()
    try:
        return json.loads(out or "null"), ""
    except ValueError as e:
        return None, f"unparseable gh output: {e}"


def _has_workflows(root: str) -> bool:
    folder = os.path.join(root, ".github", "workflows")
    try:
        return any(n.endswith((".yml", ".yaml")) for n in os.listdir(folder))
    except OSError:
        return False


def _await_checks(marker: dict, url: str, head: str) -> None:
    """Point the marker at checkpoint 1 for `head`. The caller saves it."""
    marker.update({
        "state": STATE_AWAITING_CHECKS,
        "pr_url": url,
        "pr_number": _pr_number(url) or marker.get("pr_number", ""),
        "head_sha": head,
        "since": time.time(),
        "notice": None,
        # The reply to `!ship confirm` (or the fix notice) says it all already.
        "announced": True,
    })


def _notify(workspace: str, marker: dict, state: str, notice: str, **fields) -> None:
    marker.update(fields)
    marker.update({"state": state, "notice": notice, "announced": False})
    _save_marker(workspace, marker)
    logger.info(f"CI gate: {marker.get('branch')} -> {state}")


def _pr_label(marker: dict) -> str:
    number = marker.get("pr_number")
    return f"PR #{number}" if number else "the pull request"


# -----------------------------------------------------------------------------
# Polling
# -----------------------------------------------------------------------------

def advance(workspace: str) -> str:
    """
    One poll of the CI gate. Returns the state afterwards, or "" with no marker.

    Transient gh trouble is not a verdict: it is logged, and the next poll asks
    again, until the checkpoint's own timeout turns persistent trouble into a
    stall a human can see.
    """
    marker = read_marker(workspace)
    if not marker:
        return ""
    state = marker.get("state", STATE_READY)
    try:
        if state == STATE_AWAITING_CHECKS:
            _advance_checks(workspace, marker)
        elif state == STATE_AWAITING_BASE:
            _advance_base(workspace, marker)
    except Exception as e:
        logger.error(f"CI gate poll failed for {workspace}: {e}")
    marker = read_marker(workspace)
    return marker.get("state", STATE_READY) if marker else ""


def _timed_out(marker: dict) -> bool:
    return time.time() - marker.get("since", time.time()) > ci_limits()["ci_gate_timeout_secs"]


def _in_grace(marker: dict) -> bool:
    return time.time() - marker.get("since", time.time()) < ci_limits()["ci_gate_appear_grace_secs"]


def _stall(workspace: str, marker: dict, headline: str, detail: str, error: str = "") -> None:
    lines = [f"⏸️ **{headline}**", "", detail]
    if error:
        lines += ["", f"> {_one_line(error, 400)}"]
    lines.append("")
    if marker.get("pr_url"):
        lines.append(f"- {marker['pr_url']}")
    lines += ["- **Wait again:** `!ship watch`", "- **Where it is:** `!ship status`"]
    _notify(workspace, marker, STATE_STALLED, "\n".join(lines), stall_reason=headline)


def _classify_checks(rollup) -> tuple:
    """(pending, failed, passed), each a list of {name, url, conclusion}."""
    pending, failed, passed = [], [], []
    for item in rollup or []:
        if item.get("__typename") == "StatusContext":
            entry = {"name": item.get("context") or "status",
                     "url": item.get("targetUrl") or "",
                     "conclusion": (item.get("state") or "").upper()}
            bucket = (pending if entry["conclusion"] in ("", "PENDING", "EXPECTED")
                      else passed if entry["conclusion"] == "SUCCESS" else failed)
        else:
            name = item.get("name") or "check"
            workflow = item.get("workflowName")
            entry = {"name": f"{workflow} / {name}" if workflow else name,
                     "url": item.get("detailsUrl") or "",
                     "conclusion": (item.get("conclusion") or "").upper()}
            if (item.get("status") or "").upper() != "COMPLETED":
                bucket = pending
            else:
                bucket = passed if entry["conclusion"] in _PASSING else failed
        bucket.append(entry)
    return pending, failed, passed


def _advance_checks(workspace: str, marker: dict) -> None:
    """Checkpoint 1: the pull request's head commit."""
    root = os.path.abspath(workspace)
    pr = marker.get("pr_number") or marker.get("branch")
    expected = marker.get("head_sha", "")

    data, err = _gh_json(root, "pr", "view", str(pr), "--json",
                         "number,url,state,mergeable,headRefOid,statusCheckRollup,mergeCommit")
    if data is None:
        logger.warning(f"CI gate: could not read {pr}: {err}")
        if _timed_out(marker):
            _stall(workspace, marker, f"Could not read {_pr_label(marker)} from GitHub.",
                   "Nothing was merged. The gate kept asking until it timed out.", err)
        return

    pr_state = (data.get("state") or "").upper()
    if pr_state == "MERGED":
        # Merged by hand on GitHub. Checkpoint 2 still applies.
        _merged(workspace, marker, ((data.get("mergeCommit") or {}).get("oid") or ""))
        return
    if pr_state == "CLOSED":
        _stall(workspace, marker, f"{_pr_label(marker)} was closed without merging.",
               f"Nothing was merged. The work is still on `{marker.get('branch')}`.")
        return

    head = data.get("headRefOid") or ""
    if head != expected:
        if _in_grace(marker):
            return  # GitHub has not caught up with the push yet.
        _stall(workspace, marker, f"`{marker.get('branch')}` moved under the gate.",
               f"It now points at `{head[:7]}`, not the `{expected[:7]}` this gate "
               f"pushed, so its checks are not for the code this gate is shipping. "
               f"Nothing was merged. `!ship watch` adopts `{head[:7]}` and waits for "
               f"CI on that instead.")
        return

    if (data.get("mergeable") or "").upper() == "CONFLICTING":
        _fail(workspace, marker, "pr", head, [_conflict_cause(root, marker)])
        return

    pending, failed, passed = _classify_checks(data.get("statusCheckRollup"))
    if pending:
        if not _timed_out(marker):
            return
        if not failed:
            names = ", ".join(f"`{c['name']}`" for c in pending[:5])
            _stall(workspace, marker, "CI did not finish in time.",
                   f"Still running on `{head[:7]}` after "
                   f"{int(ci_limits()['ci_gate_timeout_secs'])}s: {names}. Nothing was merged.")
            return

    if failed:
        _fail(workspace, marker, "pr", head, _check_causes(root, failed))
        return

    if not passed:
        if _in_grace(marker) or (_has_workflows(root) and not _timed_out(marker)):
            return
        _stall(workspace, marker, f"No CI ran on {_pr_label(marker)}.",
               f"GitHub reported no checks for `{head[:7]}`"
               + (" although the branch has workflows under `.github/workflows/`."
                  if _has_workflows(root) else
                  " and the branch has no workflows under `.github/workflows/`.")
               + " The merge waits on CI passing, so nothing was merged. Add a "
               "workflow that runs on `pull_request`, or merge it yourself on GitHub.")
        return

    merged, merge_error = _squash_merge(root, str(pr), head)
    if not merged:
        _stall(workspace, marker, "CI passed, but GitHub refused the merge.",
               f"Every check on `{head[:7]}` passed. Nothing was merged.", merge_error)
        return

    view, _ = _gh_json(root, "pr", "view", str(pr), "--json", "mergeCommit")
    _merged(workspace, marker, (((view or {}).get("mergeCommit") or {}).get("oid") or ""))


def _merged(workspace: str, marker: dict, merge_sha: str) -> None:
    root = os.path.abspath(workspace)
    _return_to_base(root, marker.get("base", ""))
    _mark_failure_resolved(workspace, marker, f"{_pr_label(marker)} passed CI and was merged")
    marker.update({
        "state": STATE_AWAITING_BASE,
        "merge_sha": merge_sha,
        "merged_at": time.time(),
        "since": time.time(),
    })
    _save_marker(workspace, marker)
    logger.info(f"CI gate: merged {marker.get('branch')} as {merge_sha[:7] or '?'}")


def _advance_base(workspace: str, marker: dict) -> None:
    """Checkpoint 2: the squash commit, on the base branch."""
    root = os.path.abspath(workspace)
    base = marker.get("base", "")
    sha = marker.get("merge_sha", "")

    if not sha:
        # gh can report MERGED before it reports the merge commit.
        view, _ = _gh_json(root, "pr", "view", str(marker.get("pr_number")), "--json", "mergeCommit")
        sha = ((view or {}).get("mergeCommit") or {}).get("oid") or ""
        if not sha:
            if _timed_out(marker):
                _stall(workspace, marker, f"Merged, but `{base}`'s commit is unknown.",
                       "GitHub never reported the merge commit, so its CI cannot be checked.")
            return
        marker["merge_sha"] = sha
        _save_marker(workspace, marker)

    runs, err = _gh_json(root, "run", "list", "--commit", sha, "--limit", "50", "--json",
                         "databaseId,status,conclusion,workflowName,name,url")
    if runs is None:
        logger.warning(f"CI gate: could not list runs for {sha[:7]}: {err}")
        if _timed_out(marker):
            _stall(workspace, marker, f"Could not read `{base}`'s CI from GitHub.",
                   f"Merged as `{sha[:7]}`; whether `{base}` is green is unknown.", err)
        return

    if not runs:
        if _in_grace(marker):
            return
        _integrated(workspace, marker, sha,
                    f"No workflow ran on `{base}` for `{sha[:7]}` — the pull request's "
                    f"checks are the last CI this code saw.")
        return

    pending = [r for r in runs if (r.get("status") or "").lower() != "completed"]
    failed = [r for r in runs if (r.get("conclusion") or "").upper() not in _PASSING]
    if pending:
        if _timed_out(marker):
            _stall(workspace, marker, f"`{base}`'s CI did not finish in time.",
                   f"Merged as `{sha[:7]}`; its run on `{base}` is still going.")
        return
    if failed:
        _fail(workspace, marker, "base", sha, _run_causes(root, failed))
        return
    _integrated(workspace, marker, sha, "")


def _integrated(workspace: str, marker: dict, sha: str, note: str) -> None:
    base = marker.get("base", "")
    lines = [
        f"✅ **Integrated into `{base}`.**",
        "",
        f"- {_pr_label(marker)} passed CI and was squash-merged as `{sha[:7]}`.",
        f"- `{base}` is green on that commit." if not note else f"- {note}",
    ]
    if marker.get("pr_url"):
        lines.append(f"- {marker['pr_url']}")
    lines += ["", f"The workspace is on `{base}`."]
    _notify(workspace, marker, STATE_INTEGRATED, "\n".join(lines))


# -----------------------------------------------------------------------------
# Root causes
# -----------------------------------------------------------------------------

def _one_line(text: str, limit: int = 200) -> str:
    """Single line, no backticks: safe inside inline code in chat."""
    flat = " ".join((text or "").split()).replace("`", "'")
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def _parse_failed_log(text: str) -> dict:
    """{(job, step): [lines]} from `gh run view --log-failed`, timestamps stripped."""
    steps = {}
    for raw in (text or "").splitlines():
        parts = raw.split("\t", 2)
        if len(parts) != 3:
            continue
        job, step, rest = parts
        steps.setdefault((job, step), []).append(
            _LOG_TIMESTAMP_RE.sub("", _ANSI_RE.sub("", rest)))
    return steps


def _step_summary(lines: list) -> dict:
    """{command, errors, tail} for one failed step's log."""
    command = ""
    for i, line in enumerate(lines):
        if line.startswith("##[group]Run "):
            body = []
            for follow in lines[i + 1:]:
                if follow.startswith(("shell:", "env:", "##[endgroup]", "with:")):
                    break
                body.append(follow.strip())
            command = "\n".join(b for b in body if b)[:500] or line[len("##[group]Run "):]
            break

    errors, seen = [], set()
    for line in lines:
        if line.startswith(("##[group]", "##[endgroup]")) or not _ERROR_LINE_RE.search(line):
            continue
        cleaned = line.replace("##[error]", "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            errors.append(cleaned)
    tail = [ln for ln in lines if not ln.startswith(("##[group]", "##[endgroup]"))]
    return {"command": command, "errors": errors[-MAX_ERROR_LINES:],
            "tail": tail[-MAX_TAIL_LINES:]}


def _steps_from_log(text: str) -> list:
    return [{"job": job, "step": step, **_step_summary(lines)}
            for (job, step), lines in _parse_failed_log(text).items()]


def _check_causes(root: str, failed: list) -> list:
    """One cause per failed PR check, with its failed steps where Actions ran it."""
    causes = []
    for check in failed:
        cause = {**check, "steps": []}
        match = _JOB_URL_RE.search(check.get("url", ""))
        if match:
            run_id, job_id = match.groups()
            code, out, err = _gh(root, "run", "view", run_id, "--job", job_id, "--log-failed")
            if code == 0:
                cause["steps"] = _steps_from_log(out)
            else:
                cause["log_error"] = (err or out).strip()
        causes.append(cause)
    return causes


def _run_causes(root: str, runs: list) -> list:
    """One cause per failed workflow run on the base branch."""
    causes = []
    for run in runs:
        cause = {"name": run.get("workflowName") or run.get("name") or "workflow",
                 "url": run.get("url") or "",
                 "conclusion": (run.get("conclusion") or "").upper(), "steps": []}
        code, out, err = _gh(root, "run", "view", str(run.get("databaseId")), "--log-failed")
        if code == 0:
            cause["steps"] = _steps_from_log(out)
        else:
            cause["log_error"] = (err or out).strip()
        causes.append(cause)
    return causes


def _conflict_cause(root: str, marker: dict) -> dict:
    base = marker.get("base", "")
    _git(root, "fetch", "origin", base)
    code, out = _git(root, "diff", "--name-only", f"HEAD...origin/{base}")
    theirs = set(out.splitlines()) if code == 0 else set()
    code, out = _git(root, "diff", "--name-only", f"origin/{base}...HEAD")
    ours = set(out.splitlines()) if code == 0 else set()
    both = sorted(p for p in theirs & ours if p)
    return {
        "name": f"merge conflict with `{base}`", "url": marker.get("pr_url", ""),
        "conclusion": "CONFLICTING", "kind": "conflict",
        "steps": [{"job": "merge", "step": f"merge `{base}` into `{marker.get('branch')}`",
                   "command": f"git merge origin/{base}",
                   "errors": [f"changed on both sides: {p}" for p in both[:MAX_ERROR_LINES]]
                   or [f"`{base}` has moved and no longer merges cleanly"],
                   "tail": []}],
    }


def _causes_markdown(causes: list, fenced: bool, max_errors: int = MAX_ERROR_LINES) -> str:
    """
    The "what has to change" list.

    `fenced=False` is the form for chat. A fenced block in an assistant message
    is something mover.extract_snippets may later write to disk as a file - a
    ruff line naming `orchestrator.py` is exactly the header it looks for - so
    chat gets inline code only, and the fenced log tails stay in the document.
    """
    out = []
    for n, cause in enumerate(causes, 1):
        conclusion = cause.get("conclusion", "").lower() or "failed"
        out.append(f"{n}. **{_one_line(cause['name'], 120)}** — {conclusion}")
        if cause.get("url"):
            out.append(f"   - log: {cause['url']}")
        if cause.get("log_error"):
            out.append(f"   - the failed log could not be fetched: "
                       f"`{_one_line(cause['log_error'], 160)}`")
        if not cause.get("steps") and not cause.get("log_error"):
            out.append("   - no failed step reported; open the log above")
        for step in cause.get("steps", []):
            out.append(f"   - step `{_one_line(step['step'], 100)}`"
                       + (f" running `{_one_line(step['command'], 160)}`"
                          if step.get("command") else ""))
            for line in step.get("errors", [])[:max_errors]:
                out.append(f"     - `{_one_line(line)}`")
            if len(step.get("errors", [])) > max_errors:
                out.append(f"     - …and {len(step['errors']) - max_errors} more")
            if fenced and step.get("tail"):
                out += ["", "     ```text"]
                out += [f"     {ln}" for ln in step["tail"]]
                out += ["     ```", ""]
    return "\n".join(out)


def ci_failure_path(workspace: str) -> str:
    return os.path.join(os.path.abspath(workspace), CONTEXT_DIR, CI_FAILURE_NAME)


def _fail(workspace: str, marker: dict, scope: str, sha: str, causes: list) -> None:
    """Write the failure document, and stop for review."""
    attempt = int(marker.get("fix_attempts", 0)) + 1
    base, branch = marker.get("base", ""), marker.get("branch", "")
    where = (f"{_pr_label(marker)} (`{branch}` → `{base}`), head `{sha[:7]}`"
             if scope == "pr" else
             f"`{base}` after merging {_pr_label(marker)}, squash commit `{sha[:7]}`")
    if scope == "pr":
        resolve = (f"builds a fix on `{branch}`, pushes it to {_pr_label(marker)}, and "
                   f"merges only once CI passes on the new commit.")
    else:
        resolve = (f"builds a fix on a new branch off `{base}`, opens a pull request "
                   f"for it, and merges that only once CI passes on it.")

    doc = "\n".join([
        f"# CI failure — {where}",
        "",
        "**Status:** UNRESOLVED",
        f"**Detected:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"**Failure:** {attempt}" + (" (after a fix attempt)" if attempt > 1 else ""),
        f"**Pull request:** {marker.get('pr_url', '?')}",
        "",
        "## What has to change for CI to pass",
        "",
        "Each item is a check that failed, the step inside it that failed, the "
        "command that step ran, and the error lines from its log. Fix the cause; "
        "do not skip, weaken or delete a check to make it pass.",
        "",
        _causes_markdown(causes, fenced=True),
        "",
        "## Scope",
        "",
        (f"The fix is held to the approved design this PR implements, "
         f"`{marker['design_doc']}`: every requirement in it must still hold once "
         f"CI passes. A failure that cannot be fixed without breaking one is a "
         f"question for you, not something the build settles."
         if marker.get("design_doc") else
         "No approved design was recorded for this PR, so the fix is held only to "
         "the instruction below: the smallest change that makes CI pass."),
        "",
        "## Next",
        "",
        f"- `!approve` {resolve}",
        "- Edit this file first if the causes above are wrong or incomplete: the fix "
        "build reads it as written.",
        "",
    ])
    path = ci_failure_path(workspace)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)
    except Exception as e:
        logger.error(f"Could not write {path}: {e}")

    if scope == "pr":
        headline = (f"🔴 **CI failed on {_pr_label(marker)} — not merged.**\n\n"
                    f"The pull request stays open. Nothing merges until every check "
                    f"passes on the commit being merged.")
    else:
        headline = (f"🔴 **`{base}` is red after merging {_pr_label(marker)}.**\n\n"
                    f"The squash commit `{sha[:7]}` failed CI on `{base}`. To undo it "
                    f"now instead of fixing forward: `git revert {sha[:7]}`.")
    notice = "\n".join([
        headline, "",
        "**What has to change:**", "",
        _causes_markdown(causes, fenced=False, max_errors=MAX_NOTICE_ERRORS), "",
        f"**Full record:** `{CONTEXT_DIR}/{CI_FAILURE_NAME}`", "",
        f"- **Resolve it:** `!approve` {resolve}",
        "- **Re-read this:** `!ship status`",
        (f"- **Fix it yourself:** push to `{branch}`, then `!ship watch`"
         if scope == "pr" else
         f"- **Fix it yourself:** merge a fix to `{base}` by hand, then `!ship watch`"),
    ])
    _notify(workspace, marker, STATE_CI_FAILED, notice,
            failure={"scope": scope, "sha": sha, "causes": causes,
                     "conflict": any(c.get("kind") == "conflict" for c in causes),
                     "summary": _causes_markdown(causes, fenced=False,
                                                 max_errors=MAX_NOTICE_ERRORS)})


def _mark_failure_resolved(workspace: str, marker: dict, how: str) -> None:
    """Flip an open failure document to RESOLVED, once CI has actually passed."""
    if not marker.get("failure"):
        return
    path = ci_failure_path(workspace)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if "**Status:** UNRESOLVED" in content:
            stamp = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())
            with open(path, "w", encoding="utf-8") as f:
                f.write(content.replace("**Status:** UNRESOLVED",
                                        f"**Status:** RESOLVED {stamp} — {how}", 1))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"Could not mark {path} resolved: {e}")
    marker.pop("failure", None)


# -----------------------------------------------------------------------------
# The fix loop
# -----------------------------------------------------------------------------

def ci_fix_request(workspace: str) -> Optional[tuple]:
    """
    For `!approve`: (instruction, extra_env, refusal), or None if no CI failure waits.

    Puts the workspace where the fix belongs - the PR's branch, or a fresh base
    for a red base branch - and moves the marker to FIXING. The instruction is a
    `!build` message for the conversation the container reads: the root causes
    inline, the full record by path. No fenced blocks, for the reason in
    `_causes_markdown`.
    """
    marker = read_marker(workspace)
    if not marker or marker.get("state") != STATE_CI_FAILED:
        return None
    root = os.path.abspath(workspace)
    failure = marker.get("failure") or {}
    scope, base, branch = failure.get("scope", "pr"), marker.get("base", ""), marker.get("branch", "")

    try:
        _git(root, "fetch", "origin", check=True)
        if scope == "pr":
            if _current_branch(root) != branch:
                _git(root, "checkout", branch, check=True)
            if failure.get("conflict"):
                # Leaves conflict markers for the build to resolve; the merge is
                # committed with the fix (see _commit_and_push).
                _git(root, "merge", "--no-edit", f"origin/{base}")
        else:
            if _current_branch(root) != base:
                _git(root, "checkout", base, check=True)
            _git(root, "pull", "--ff-only", "origin", base, check=True)
    except RuntimeError as e:
        return "", {}, (
            f"❌ **Could not prepare the fix.** The workspace could not be put on "
            f"`{branch if scope == 'pr' else base}`. Nothing was built.\n\n"
            f"> {_one_line(str(e), 400)}"
        )

    where = (f"pull request {_pr_label(marker)} (branch `{branch}`, into `{base}`)"
             if scope == "pr" else f"the `{base}` branch after merging {_pr_label(marker)}")
    instruction = "\n".join([
        f"!build Make CI pass on {where}. GitHub Actions failed on commit "
        f"`{failure.get('sha', '')[:7]}`. The full record, including log excerpts, is "
        f"`{CONTEXT_DIR}/{CI_FAILURE_NAME}`. Fix the root cause of every failure "
        f"below and change nothing else: no refactors and no unrelated improvements. "
        f"Do not skip, weaken or delete a check, test or lint rule to make it pass.",
        "",
        "What has to change:",
        _causes_markdown(failure.get("causes", []), fenced=False),
        *_scope_lines(workspace, marker),
    ]).replace("--repo", "-repo").replace("--kb", "-kb")

    # The container clears this itself, but only once it gets that far. One that
    # dies earlier must not be read as passing on the strength of the original
    # build's VERIFIED SAFE.
    try:
        os.remove(os.path.join(root, ".build_complete"))
    except OSError:
        pass

    marker.update({"state": STATE_FIXING, "fix_started": time.time(),
                   "fix_attempts": int(marker.get("fix_attempts", 0)) + 1,
                   "notice": None, "announced": True})
    _save_marker(workspace, marker)
    env = {"BUILD_BRANCH": branch} if scope == "pr" else {}
    return instruction, env, None


def _scope_lines(workspace: str, marker: dict) -> list:
    """
    The approved design, as a constraint on the fix rather than a new request.

    Without it the design pass sees only the failure, and a failure is a
    request to change code - so "make the test pass" competes on equal terms
    with "keep what the test protects", and the shortest diff wins.
    """
    design = _design_excerpt(workspace, marker)
    if not design:
        return []
    return [
        "",
        f"SCOPE: this pull request implements the approved design below (full copy: "
        f"`{marker['design_doc']}`). It is already built; do not redesign or extend it. "
        f"Every requirement in it must still hold after the fix. If a failure cannot be "
        f"fixed without dropping or changing one of those requirements, do not make that "
        f"change: report it as a blocker instead.",
        "",
        "APPROVED DESIGN:",
        design,
    ]


def abort_fix(workspace: str) -> None:
    """The fix build never started: back to waiting for `!approve`."""
    marker = read_marker(workspace)
    if marker and marker.get("state") == STATE_FIXING:
        marker["state"] = STATE_CI_FAILED
        marker["fix_attempts"] = max(0, int(marker.get("fix_attempts", 1)) - 1)
        _save_marker(workspace, marker)


def note_fix_container(workspace: str, container: str) -> None:
    marker = read_marker(workspace)
    if marker and marker.get("state") == STATE_FIXING:
        marker["container"] = container
        _save_marker(workspace, marker)


def _build_passed_its_gates(root: str) -> bool:
    try:
        with open(os.path.join(root, ".build_complete"), "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return False
    return "VERIFIED" in content and "SAFE" in content


def _fix_did_not_land(workspace: str, marker: dict, headline: str, detail: str) -> str:
    summary = (marker.get("failure") or {}).get("summary", "")
    notice = "\n".join([
        f"⚠️ **{headline}**", "", detail, "",
        f"CI is still failing on {_pr_label(marker)} and nothing was merged.", "",
        "**What still has to change:**", "", summary, "",
        f"- **Try again:** `!approve` (edit `{CONTEXT_DIR}/{CI_FAILURE_NAME}` first to steer it)",
        "- **See what the build did:** `!logs`",
    ])
    _notify(workspace, marker, STATE_CI_FAILED, notice)
    return STATE_CI_FAILED


def fix_build_finished(workspace: str) -> str:
    """
    Called when a fix build's container exits. Returns the state afterwards.

    A fix that passed the container's own gates is pushed and goes straight back
    through checkpoint 1 - `!approve` was the consent to that, and the merge is
    still CI's to allow. A fix that did not pass them is not pushed at all.
    """
    marker = read_marker(workspace)
    if not marker or marker.get("state") != STATE_FIXING:
        return marker.get("state", "") if marker else ""
    root = os.path.abspath(workspace)
    failure = marker.get("failure") or {}
    scope, base = failure.get("scope", "pr"), marker.get("base", "")
    attempt = marker.get("fix_attempts", 1)

    if not _build_passed_its_gates(root):
        return _fix_did_not_land(
            workspace, marker, "The fix build ended without passing its own gates.",
            "It never reached VERIFIED, SAFE and a passing test gate, so nothing was "
            "committed or pushed. If the design pass stopped on a blocker - a fix that "
            "would break the approved design is one - `!logs` names it.")

    branch = _current_branch(root)
    files = _changed_files(root, base)
    title = f"Resolve CI failure (attempt {attempt})"
    body = "\n".join([
        "Built by br.ai.n to resolve a CI failure.", "",
        f"**Failure record:** `{CONTEXT_DIR}/{CI_FAILURE_NAME}`", "",
        failure.get("summary", ""),
    ])

    if scope == "pr":
        if branch != marker.get("branch"):
            return _fix_did_not_land(
                workspace, marker, "The fix build finished on the wrong branch.",
                f"It ended on `{branch}`, not `{marker.get('branch')}`, so nothing was "
                f"pushed to {_pr_label(marker)}.")
        before = marker.get("head_sha", "")
        try:
            _commit_and_push(root, branch, title, body, files)
        except RuntimeError as e:
            return _fix_did_not_land(workspace, marker, "The fix could not be pushed.",
                                     f"> {_one_line(str(e), 400)}")
        head = _head_sha(root)
        if head == before:
            return _fix_did_not_land(workspace, marker, "The fix build changed nothing.",
                                     f"`{branch}` is still at `{before[:7]}`.")
        _await_checks(marker, marker.get("pr_url", ""), head)
        marker.update({"announced": False, "notice": (
            f"🔁 **Fix pushed to {_pr_label(marker)} as `{head[:7]}` — waiting for CI.**\n\n"
            f"It merges only once every check on `{head[:7]}` passes.\n\n"
            f"- **Where it is:** `!ship status`")})
        _save_marker(workspace, marker)
        return STATE_AWAITING_CHECKS

    # A red base: the fix ships as its own pull request.
    if not branch.startswith(BUILD_BRANCH_PREFIX) or not files:
        return _fix_did_not_land(
            workspace, marker, "The fix build left nothing to ship.",
            f"It ended on `{branch}` with {len(files)} changed file(s).")
    try:
        _commit_and_push(root, branch, title, body, files)
    except RuntimeError as e:
        return _fix_did_not_land(workspace, marker, "The fix could not be pushed.",
                                 f"> {_one_line(str(e), 400)}")
    url, pr_error = _open_pull_request(root, f"Fix {base} CI: {title}"[:MAX_TITLE_CHARS],
                                       body, branch, base)
    if not url:
        marker["branch"] = branch
        return _fix_did_not_land(workspace, marker,
                                 "The fix was pushed, but its pull request could not be opened.",
                                 f"> {_one_line(pr_error, 400)}")
    marker["branch"] = branch
    _await_checks(marker, url, _head_sha(root))
    marker.update({"announced": False, "notice": (
        f"🔁 **Fix for `{base}` opened as {_pr_label(marker)} — waiting for CI.**\n\n"
        f"- {url}\n\nIt merges only once every check on it passes.\n\n"
        f"- **Where it is:** `!ship status`")})
    _save_marker(workspace, marker)
    return STATE_AWAITING_CHECKS


# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------

def resume_watch(workspace: str) -> str:
    """
    `!ship watch` — put a stalled or hand-fixed ship back on the gate.

    The explicit human step behind "the branch moved": it adopts whatever head
    the PR has now, so a commit pushed by hand gets its own CI verdict. Returns
    the reply; the caller starts the watcher.
    """
    marker = read_marker(workspace)
    if not marker or marker.get("state") not in (STATE_STALLED, STATE_CI_FAILED, *WATCHED_STATES):
        return status(workspace)
    root = os.path.abspath(workspace)

    if marker.get("merge_sha") or marker.get("state") == STATE_AWAITING_BASE \
            or (marker.get("failure") or {}).get("scope") == "base":
        # Past the merge: base is what is being watched. A hand-merged fix to
        # base is a new commit, so watch base's head.
        _git(root, "fetch", "origin", marker.get("base", ""))
        code, out = _git(root, "rev-parse", f"origin/{marker.get('base', '')}")
        sha = out.strip() if code == 0 else marker.get("merge_sha", "")
        marker.update({"state": STATE_AWAITING_BASE, "merge_sha": sha,
                       "since": time.time(), "notice": None, "announced": True})
        _save_marker(workspace, marker)
        return f"👀 **Watching `{marker.get('base')}` CI on `{sha[:7]}` again.**"

    data, err = _gh_json(root, "pr", "view", str(marker.get("pr_number") or marker.get("branch")),
                         "--json", "headRefOid,url")
    if data is None:
        return f"❌ **Could not read {_pr_label(marker)} from GitHub.**\n\n> {_one_line(err, 400)}"
    _await_checks(marker, data.get("url") or marker.get("pr_url", ""), data.get("headRefOid", ""))
    _save_marker(workspace, marker)
    return (f"👀 **Waiting for CI on {_pr_label(marker)} again**, at "
            f"`{marker['head_sha'][:7]}`. It merges only once every check passes.")


def status(workspace: str) -> str:
    """`!ship status` — where the ship is, polled fresh where that is possible."""
    marker = read_marker(workspace)
    if not marker:
        return "📭 **Nothing is shipping.** A finished build is offered here; `!ship` ships it."
    if marker.get("state") in WATCHED_STATES:
        advance(workspace)
        marker = read_marker(workspace) or marker
    state = marker.get("state", STATE_READY)

    # A pending notice is the fullest account of a state change; show it, and
    # count it as seen.
    if marker.get("notice") and state not in (STATE_READY, *WATCHED_STATES):
        pending_offer(workspace)
        return marker["notice"]

    lines = [f"🚦 **{_STATE_LABELS.get(state, state)}** — `{marker.get('branch')}` → "
             f"`{marker.get('base')}`", ""]
    if marker.get("pr_url"):
        lines.append(f"- {marker['pr_url']}")
    if state == STATE_AWAITING_CHECKS:
        waited = int(time.time() - marker.get("since", time.time()))
        lines.append(f"- Waiting {waited}s for every check on `{marker.get('head_sha', '')[:7]}`.")
    elif state == STATE_AWAITING_BASE:
        lines.append(f"- Merged as `{marker.get('merge_sha', '')[:7]}`; waiting for "
                     f"`{marker.get('base')}`'s run of it.")
    elif state == STATE_FIXING:
        lines.append(f"- Fix attempt {marker.get('fix_attempts', 1)} is building in "
                     f"`{marker.get('container', '?')}`. `!logs` follows it.")
    elif state == STATE_READY:
        lines.append("- `!ship` shows what would be shipped.")
    return "\n".join(lines)


def watched_workspaces(conversations_dir: str) -> list:
    """(workspace, state, marker) for every marker the orchestrator should pick back up."""
    found = []
    try:
        names = sorted(os.listdir(conversations_dir))
    except OSError:
        return found
    for name in names:
        workspace = os.path.join(conversations_dir, name)
        marker = read_marker(workspace)
        if marker and marker.get("state") in (*WATCHED_STATES, STATE_FIXING):
            found.append((os.path.abspath(workspace), marker["state"], marker))
    return found
