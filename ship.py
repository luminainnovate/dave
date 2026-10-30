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


# =============================================================================
# BUILD EVENTS
# =============================================================================
#
# What a build and the CI-fix queue did while nobody was asking: each issue
# starting and finishing, each iteration inside a container. OpenWebUI cannot
# be written to unprompted - every word reaches the chat as part of a reply -
# so events are written here the moment they happen, and the next reply in
# that conversation carries every one not yet shown, in order. On disk, next to
# the marker, so a restart between the event and the next message loses none.

EVENTS_NAME = "build_events.jsonl"
EVENTS_SEEN_NAME = "build_events.seen"

# How many unseen events one reply shows; older ones are counted, not listed.
MAX_EVENTS_SHOWN = 15


def _events_path(workspace: str, name: str = EVENTS_NAME) -> str:
    return os.path.join(os.path.abspath(workspace), CONTEXT_DIR, name)


def record_event(workspace: str, text: str) -> None:
    """Append one event. Never raises: a progress line must not cost a build."""
    if not workspace or not text:
        return
    try:
        path = _events_path(workspace)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.time(), "text": str(text)[:500]}) + "\n")
    except Exception as e:
        logger.warning(f"Could not record a build event: {e}")


def _read_events(workspace: str) -> list:
    try:
        with open(_events_path(workspace), "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def unseen_events(workspace: str) -> str:
    """Every event since the last call, formatted for chat - and marks them seen."""
    events = _read_events(workspace)
    try:
        with open(_events_path(workspace, EVENTS_SEEN_NAME), "r", encoding="utf-8") as f:
            seen = int(f.read().strip() or 0)
    except (OSError, ValueError):
        seen = 0
    if seen > len(events):  # the log was cleared under us
        seen = 0
    new = events[seen:]
    if not new:
        return ""
    try:
        with open(_events_path(workspace, EVENTS_SEEN_NAME), "w", encoding="utf-8") as f:
            f.write(str(len(events)))
    except OSError as e:
        logger.warning(f"Could not mark build events seen: {e}")
    return _format_events(new, "📋 **Since your last message:**")


def recent_events(workspace: str, count: int = 8) -> str:
    """The last few events, seen or not, for `!ship status`."""
    events = _read_events(workspace)[-count:]
    return _format_events(events, "**Recent activity:**") if events else ""


def _format_events(events: list, heading: str) -> str:
    shown = events[-MAX_EVENTS_SHOWN:]
    lines = [heading, ""]
    if len(events) > len(shown):
        lines.append(f"- …{len(events) - len(shown)} earlier")
    for event in shown:
        stamp = time.strftime("%H:%M", time.localtime(event.get("at", 0)))
        lines.append(f"- `{stamp}` {event.get('text', '')}")
    return "\n".join(lines)


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
    # Not through _git: it strips its output, and porcelain's first record can
    # begin with a space (" M path", an unstaged edit). Stripped, the status
    # shifts left by one and the path loses its first character - `ackend/x.ts`
    # - so that file silently fell out of every commit and diff built from here.
    result = subprocess.run(["git", "-C", root, "status", "--porcelain", "-uall", "-z"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return set()
    out = result.stdout

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


def _commit_and_push(root: str, branch: str, title: str, body: str, files: list,
                     push: bool = True) -> None:
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

    if push:
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
    r"|^\S+:\d+:\d+: [A-Z]{1,4}\d+|^E\s{2,}|\d+ problems? \("
    r"|^\s*[×✗✕]\s|\bFAIL\b|\w+Error:",
    re.IGNORECASE,
)
_JOB_URL_RE = re.compile(r"/actions/runs/(\d+)/job/(\d+)")

# ESLint, tsc --pretty and stylelint print a file's path on a line of its own
# and then that file's problems as bare `line:col  error  message`. Kept alone,
# those lines tell a fix build what is wrong but not where.
_FILE_HEADER_RE = re.compile(r"^(?:/home/runner/work/[^/\s]+/[^/\s]+/)?(?P<path>[\w.$@+\-]+(?:/[\w.$@+\-\[\]]+)+\.\w+)$")
_LOCATION_ONLY_RE = re.compile(r"^\d+:\d+\s")

# Lines that match _ERROR_LINE_RE without reporting a failure: a test runner's
# captured-output banners, structured log lines the code under test printed,
# and passing tests whose names happen to say "fails". On the first real backend
# run these were five of the five lines shown in chat, ahead of the failures.
# A failing test file, as vitest/jest ("FAIL  test/a.test.ts > ...") and
# pytest ("FAILED tests/test_a.py::test_x - ...") name it. A suite failing in
# several files is several problems, and is split that way into issues.
_FAILING_TEST_RE = re.compile(
    r"^(?:FAIL|FAILED)\s+(?P<file>[\w.$@+\-\[\]]+(?:/[\w.$@+\-\[\]]+)*\.\w+)")
# The runner's own totals, which belong to no single test file.
_RUNNER_SUMMARY_RE = re.compile(
    r"^\s*(?:Test Files|Tests|Test Suites|Snapshots)\b|^Process completed with exit code"
    r"|^=+ .*\b(?:failed|passed)\b")
# Source paths mentioned anywhere in a failure: test files, stack frames,
# lint headers. Handed to the fix build as what to read first - a design pass
# that has to ask for a function body it was never shown is a blocked build.
_SOURCE_PATH_RE = re.compile(
    r"(?<![\w/.])((?:[\w.$@+\-\[\]]+/)+[\w.$@+\-\[\]]+"
    r"\.(?:ts|tsx|js|jsx|mjs|cjs|py|go|rs|rb|java|kt|sql|vue|svelte|css|scss))\b")
MAX_FILES_INVOLVED = 15

_ERROR_NOISE_RE = re.compile(r"^(?:stdout|stderr) \||^[✓√]\s|^\{\s*\"")
_RUNNER_CHECKOUT_RE = re.compile(r"/home/runner/work/[^/\s]+/[^/\s]+/")


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


def _gh_retry(root: str, *args: str, attempts: int = 3) -> tuple:
    """_gh, retried on a GitHub 5xx. Those come in bursts and clear in seconds."""
    for attempt in range(attempts):
        code, out, err = _gh(root, *args)
        if code == 0 or not re.search(r"HTTP 5\d\d", err + out) or attempt == attempts - 1:
            return code, out, err
        time.sleep(3 * (attempt + 1))
    return code, out, err


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
                     "workflow": workflow or "", "job": name,
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

    errors, seen, current_file = [], set(), ""
    for line in lines:
        if line.startswith(("##[group]", "##[endgroup]")):
            continue
        header = _FILE_HEADER_RE.match(line.strip())
        if header:
            current_file = header.group("path")
            continue
        if not _ERROR_LINE_RE.search(line):
            continue
        cleaned = _RUNNER_CHECKOUT_RE.sub("", line.replace("##[error]", "")).strip()
        if _ERROR_NOISE_RE.search(cleaned):
            continue
        if current_file and _LOCATION_ONLY_RE.match(cleaned):
            cleaned = f"{current_file}:{cleaned}"
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            errors.append(cleaned)
    tail = [_RUNNER_CHECKOUT_RE.sub("", ln) for ln in lines
            if not ln.startswith(("##[group]", "##[endgroup]"))]

    # Paths per failing test file, from every line - a stack frame such as
    # `❯ src/routes/attestations.ts:120:9` is where the fix goes, and it never
    # matches the error pattern.
    paths_by_test, current = {}, None
    for line in lines:
        cleaned = _RUNNER_CHECKOUT_RE.sub("", line).strip()
        match = _FAILING_TEST_RE.match(cleaned)
        if match:
            current = match.group("file")
        if current:
            paths_by_test.setdefault(current, []).append(cleaned)

    # Grouped from every error line, not the capped list: a suite failing in six
    # files names most of them before the last fifteen lines start.
    by_test, current = {}, None
    for line in errors:
        match = _FAILING_TEST_RE.match(line)
        if match:
            current = match.group("file")
            by_test.setdefault(current, [])
        if current is None or _RUNNER_SUMMARY_RE.search(line):
            continue
        by_test[current].append(line)

    # The cap keeps the last lines, where the summary ("✖ 674 problems", the
    # exit code) is - but the count is kept, so the record never reads as if
    # the 15 shown were all there is.
    return {"command": command, "errors": errors[-MAX_ERROR_LINES:],
            "error_count": len(errors), "tail": tail[-MAX_TAIL_LINES:],
            "test_files": {f: ls[:MAX_ERROR_LINES] for f, ls in by_test.items()},
            "test_paths": {f: _paths_in([f] + paths_by_test.get(f, [])) for f in by_test},
            "files": _paths_in(errors + tail)}


def _paths_in(lines: list) -> list:
    found = []
    for line in lines:
        for path in _SOURCE_PATH_RE.findall(line):
            if "node_modules/" not in path and path not in found:
                found.append(path)
    return found[:MAX_FILES_INVOLVED * 2]


def _steps_from_log(text: str) -> list:
    return [{"job": job, "step": step, **_step_summary(lines)}
            for (job, step), lines in _parse_failed_log(text).items()]


def _failed_steps_from_full_log(root: str, run_id: str, job_id: str) -> list:
    """
    A failed job's failing steps, read from its FULL log.

    `gh run view --log-failed` returns only steps the API marks failed, and the
    API can leave a step that exited non-zero at "in_progress" after its job
    has finished - seen on a real run whose backend suite failed: the job was
    `failure`, its test step `in_progress`, every later step `pending` though
    they had run. --log-failed then printed nothing, and the record said "no
    failed step reported", giving a fix build no errors, no command and no files.

    The full log has no step columns, but every step opens with a
    `##[group]Run ...` header and a step that exits non-zero ends with an
    `##[error]` line. The segments holding one are the failed steps; each is
    named after the job's next step the API does not call successful.
    """
    code, out, _ = _gh_retry(root, "api", f"repos/{{owner}}/{{repo}}/actions/jobs/{job_id}/logs")
    if code != 0 or not out.strip():
        return []
    segments, current = [], None
    for raw in out.splitlines():
        line = _LOG_TIMESTAMP_RE.sub("", _ANSI_RE.sub("", raw))
        if line.startswith("##[group]Run "):
            current = [line]
            segments.append(current)
        elif current is not None:
            current.append(line)
    failed = [seg for seg in segments if any(ln.startswith("##[error]") for ln in seg)]
    if not failed:
        return []

    steps = [{"job": "", "step": seg[0][len("##[group]"):], **_step_summary(seg)} for seg in failed]
    return _name_steps(root, run_id, job_id, steps)


def _job_with_steps(root: str, run_id: str, job_id: str, attempts: int = 4) -> Optional[dict]:
    """
    The job, with its step list - asked for until GitHub includes one.

    Observed on one finished job, back to back: the same endpoint returned all
    16 steps, then 0, then 0. An empty list is a bad answer, not a fact.
    """
    for attempt in range(attempts):
        code, out, _ = _gh_retry(root, "api", f"repos/{{owner}}/{{repo}}/actions/jobs/{job_id}")
        try:
            job = json.loads(out) if code == 0 else None
        except ValueError:
            job = None
        if job and job.get("steps"):
            return job
        code, out, _ = _gh_retry(root, "run", "view", str(run_id), "--json", "jobs")
        try:
            jobs = (json.loads(out) if code == 0 else {}).get("jobs", [])
        except ValueError:
            jobs = []
        job = next((j for j in jobs if str(j.get("databaseId")) == str(job_id)), None)
        if job and job.get("steps"):
            return job
        if attempt < attempts - 1:
            time.sleep(2)
    logger.warning(f"CI gate: GitHub returned no steps for job {job_id}; keeping log names.")
    return None


def _name_steps(root: str, run_id: str, job_id: str, steps: list) -> list:
    """
    Give each step the name the jobs API knows it by.

    Triage matches steps against the baseline BY NAME, and the logs do not
    reliably carry it: for a step the API left "in_progress", gh labels its
    --log-failed lines with the raw `Run <command>` header, and the full log has
    no names at all. A step whose name the API does not list takes the job's
    next step that the API does not call successful.
    """
    job = _job_with_steps(root, run_id, job_id)
    if not job:
        return steps
    known = {st.get("name", "") for st in job.get("steps", [])}
    unfinished = [st.get("name", "") for st in job.get("steps", [])
                  if (st.get("conclusion") or "").lower() not in ("success", "skipped")
                  and not st.get("name", "").startswith(("Post ", "Stop containers", "Complete job"))]
    for step in steps:
        step["job"] = step.get("job") or job.get("name", "")
        if step.get("step") not in known and unfinished:
            step["step"] = unfinished.pop(0)
    return steps


def _check_causes(root: str, failed: list) -> list:
    """One cause per failed PR check, with its failed steps where Actions ran it."""
    causes = []
    for check in failed:
        cause = {**check, "steps": []}
        match = _JOB_URL_RE.search(check.get("url", ""))
        if match:
            run_id, job_id = match.groups()
            code, out, err = _gh_retry(root, "run", "view", run_id, "--job", job_id, "--log-failed")
            if code == 0:
                cause["steps"] = _steps_from_log(out)
                if any(not st["step"] or st["step"].startswith("Run ") for st in cause["steps"]):
                    cause["steps"] = _name_steps(root, run_id, job_id, cause["steps"])
            if not cause["steps"]:
                cause["steps"] = _failed_steps_from_full_log(root, run_id, job_id)
            if not cause["steps"] and code != 0:
                cause["log_error"] = (err or out).strip()
        causes.append(cause)
    return causes


def _run_causes(root: str, runs: list) -> list:
    """One cause per failed workflow run on the base branch."""
    causes = []
    for run in runs:
        cause = {"name": run.get("workflowName") or run.get("name") or "workflow",
                 "workflow": run.get("workflowName") or run.get("name") or "",
                 "url": run.get("url") or "",
                 "conclusion": (run.get("conclusion") or "").upper(), "steps": []}
        run_id = str(run.get("databaseId"))
        code, out, err = _gh(root, "run", "view", run_id, "--log-failed")
        if code == 0:
            cause["steps"] = _steps_from_log(out)
        if not cause["steps"]:
            # The same API lag as for a PR check; see _failed_steps_from_full_log.
            view, _ = _gh_json(root, "run", "view", run_id, "--json", "jobs")
            for job in (view or {}).get("jobs", []):
                if (job.get("conclusion") or "").lower() not in ("success", "skipped", "neutral"):
                    cause["steps"] += _failed_steps_from_full_log(root, run_id, str(job.get("databaseId")))
        if not cause["steps"] and code != 0:
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


# -----------------------------------------------------------------------------
# Triage
# -----------------------------------------------------------------------------
#
# Not every red check is this change's to fix. The first real run of the gate
# went red on four workflows, and every one of them had failed the same way on
# the two base commits before it: a fix build handed that record would have been
# asked to clear 665 lint errors and a CI runner image under a migration's name.
# So each failed step is placed before anything is built:
#
#   introduced   - passed on the baseline, fails here. This change's to fix.
#   preexisting  - the same workflow, job and step failed on the baseline too.
#   environment  - the log says the runner or a service failed, not the code.
#                  No code change fixes it; a human does.
#   unverified   - no completed baseline run to compare against. Treated as
#                  introduced, because the safe guess is "ours".
#
# The baseline is the base branch's head for a pull request, and the commit the
# squash landed on for a red base branch - the state of the world the change
# was made against in each case.

ORIGIN_INTRODUCED = "introduced"
ORIGIN_PREEXISTING = "preexisting"
ORIGIN_ENVIRONMENT = "environment"
ORIGIN_UNVERIFIED = "unverified"

# What a fix build is pointed at by default, and with `!approve all`.
FIXABLE_DEFAULT = (ORIGIN_INTRODUCED, ORIGIN_UNVERIFIED)
FIXABLE_ALL = (ORIGIN_INTRODUCED, ORIGIN_UNVERIFIED, ORIGIN_PREEXISTING)

# Failures of the machine rather than the code. Matched against a step's error
# lines and log tail. Kept to messages that cannot come from a project's own
# tests: "timed out" alone is as often a slow test as a slow runner.
_ENVIRONMENT_RE = re.compile(
    r"does not support|is not supported on|No space left on device"
    r"|API rate limit|secondary rate limit|ECONNRESET|Could not resolve host"
    r"|Service Unavailable|Bad Gateway|lost communication with the server"
    r"|runner has received a shutdown signal|The operation was canceled"
    r"|Unable to locate executable|exceeded the maximum execution time",
    re.IGNORECASE,
)

_ORIGIN_LABELS = {
    ORIGIN_INTRODUCED: "introduced by this change",
    ORIGIN_UNVERIFIED: "not on the baseline's record — treated as this change's",
    ORIGIN_PREEXISTING: "already failing before this change",
    ORIGIN_ENVIRONMENT: "CI environment, not code",
}


def _baseline_sha(root: str, marker: dict, scope: str, sha: str) -> str:
    """The commit this change was made against: base's head, or the squash's parent."""
    if scope == "pr":
        path = f"repos/{{owner}}/{{repo}}/commits/{marker.get('base', '')}"
        jq = ".sha"
    else:
        path = f"repos/{{owner}}/{{repo}}/commits/{sha}"
        jq = ".parents[0].sha"
    code, out, err = _gh(root, "api", path, "--jq", jq)
    value = out.strip() if code == 0 else ""
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        logger.warning(f"CI gate: no baseline commit for {sha[:7]}: {(err or out).strip()[:200]}")
        return ""
    return value


def _failing_steps_at(root: str, sha: str) -> tuple:
    """
    ({(workflow, job, step)}, {workflows with a completed run}) at `sha`.

    Read from the jobs API, not the logs: step conclusions are all triage needs,
    and logs age out long before the run record does. A job that failed with no
    failed step (a setup failure) is recorded with step "".
    """
    failing, completed = set(), set()
    if not sha:
        return failing, completed
    runs, _ = _gh_json(root, "run", "list", "--commit", sha, "--limit", "50", "--json",
                       "databaseId,workflowName,status,conclusion")
    for run in runs or []:
        if (run.get("status") or "").lower() != "completed":
            continue
        workflow = run.get("workflowName") or ""
        completed.add(workflow)
        if (run.get("conclusion") or "").upper() in _PASSING:
            continue
        view, _ = _gh_json(root, "run", "view", str(run.get("databaseId")), "--json", "jobs")
        for job in (view or {}).get("jobs", []):
            if (job.get("conclusion") or "").lower() in ("success", "skipped", "neutral"):
                continue
            steps = [st.get("name", "") for st in job.get("steps", [])
                     if (st.get("conclusion") or "").lower() == "failure"]
            for step in steps or [""]:
                failing.add((workflow, job.get("name", ""), step))
    return failing, completed


def _step_origin(workflow: str, job: str, step: dict, failing: set, completed: set) -> str:
    text = "\n".join(step.get("errors", []) + step.get("tail", []))
    if _ENVIRONMENT_RE.search(text):
        return ORIGIN_ENVIRONMENT
    if workflow not in completed:
        return ORIGIN_UNVERIFIED
    if (workflow, job, step.get("step", "")) in failing or (workflow, job, "") in failing:
        return ORIGIN_PREEXISTING
    return ORIGIN_INTRODUCED


def _triage(root: str, marker: dict, scope: str, sha: str, causes: list) -> str:
    """Label every failed step with its origin, in place. Returns the baseline sha."""
    baseline = _baseline_sha(root, marker, scope, sha)
    failing, completed = _failing_steps_at(root, baseline)
    for cause in causes:
        if cause.get("kind") == "conflict":
            cause["origin"] = ORIGIN_INTRODUCED
            for step in cause.get("steps", []):
                step["origin"] = ORIGIN_INTRODUCED
            continue
        workflow = cause.get("workflow", "")
        for step in cause.get("steps", []):
            step["origin"] = _step_origin(workflow, step.get("job") or cause.get("job", ""),
                                          step, failing, completed)
        origins = {st["origin"] for st in cause.get("steps", [])}
        if not origins:
            # No log to read: judge the check as a whole by its job.
            job = cause.get("job", "")
            origins = {ORIGIN_UNVERIFIED if workflow not in completed else
                       ORIGIN_PREEXISTING if any(f[0] == workflow and (not job or f[1] == job)
                                                 for f in failing)
                       else ORIGIN_INTRODUCED}
        # A check is as much this change's as its most-ours step.
        for origin in (ORIGIN_INTRODUCED, ORIGIN_UNVERIFIED, ORIGIN_PREEXISTING, ORIGIN_ENVIRONMENT):
            if origin in origins:
                cause["origin"] = origin
                break
    return baseline


def _select(causes: list, origins: tuple) -> list:
    """The causes, and within them the steps, whose origin is in `origins`."""
    selected = []
    for cause in causes:
        steps = [st for st in cause.get("steps", []) if st.get("origin") in origins]
        if steps or (not cause.get("steps") and cause.get("origin") in origins):
            selected.append({**cause, "steps": steps})
    return selected


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
                          if step.get("command") else "")
                       + (f" — *{_ORIGIN_LABELS[step['origin']]}*"
                          if step.get("origin") in _ORIGIN_LABELS else ""))
            for line in step.get("errors", [])[:max_errors]:
                out.append(f"     - `{_one_line(line)}`")
            total = max(step.get("error_count", 0), len(step.get("errors", [])))
            if total > max_errors:
                out.append(f"     - …and {total - max_errors} more error lines in the log")
            if fenced and step.get("tail"):
                out += ["", "     ```text"]
                out += [f"     {ln}" for ln in step["tail"]]
                out += ["     ```", ""]
    return "\n".join(out)


def ci_failure_path(workspace: str) -> str:
    return os.path.join(os.path.abspath(workspace), CONTEXT_DIR, CI_FAILURE_NAME)


def _fail(workspace: str, marker: dict, scope: str, sha: str, causes: list) -> None:
    """Triage the failures, write the failure document, and stop for review."""
    root = os.path.abspath(workspace)
    baseline = _triage(root, marker, scope, sha, causes)
    attempt = int(marker.get("fix_attempts", 0)) + 1
    base, branch = marker.get("base", ""), marker.get("branch", "")
    where = (f"{_pr_label(marker)} (`{branch}` → `{base}`), head `{sha[:7]}`"
             if scope == "pr" else
             f"`{base}` after merging {_pr_label(marker)}, squash commit `{sha[:7]}`")
    ours = _select(causes, FIXABLE_DEFAULT)
    before = _select(causes, (ORIGIN_PREEXISTING,))
    machine = _select(causes, (ORIGIN_ENVIRONMENT,))

    if scope == "pr":
        resolve = (f"builds a fix on `{branch}`, pushes it to {_pr_label(marker)}, and "
                   f"merges only once CI passes on the new commit.")
    else:
        resolve = (f"builds a fix on a new branch off `{base}`, opens a pull request "
                   f"for it, and merges that only once CI passes on it.")
    baseline_label = f"`{baseline[:7]}`" if baseline else "unknown"

    def section(title, intro, items):
        if not items:
            return []
        return [f"## {title}", "", intro, "", _causes_markdown(items, fenced=True), ""]

    doc = "\n".join([
        f"# CI failure — {where}",
        "",
        "**Status:** UNRESOLVED",
        f"**Detected:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        f"**Failure:** {attempt}" + (" (after a fix attempt)" if attempt > 1 else ""),
        f"**Pull request:** {marker.get('pr_url', '?')}",
        f"**Compared against:** {baseline_label} "
        + ("(the head of " if scope == "pr" else "(the commit the squash landed on, on ")
        + f"`{base}`)",
        "",
        *section(
            "What has to change for CI to pass",
            "Failures this change introduced: each failed check, the step inside it, the "
            "command that step ran, and the error lines from its log. `!approve` fixes "
            "these. Fix the cause; do not skip, weaken or delete a check to make it pass.",
            ours),
        *section(
            f"Already failing on `{base}` before this change",
            f"The same workflow, job and step failed on {baseline_label} too, so nothing "
            f"in this change caused them. `!approve` leaves them alone; `!approve all` "
            f"takes them on as well, which widens this change beyond its design.",
            before),
        *section(
            "CI environment, not code",
            "The log shows the runner or a service failing, not the project. No code "
            "change fixes these and no fix build is pointed at them: they need a person, "
            "usually a workflow or runner change.",
            machine),
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
        (f"- `!approve` {resolve}" if ours else
         "- `!approve` has nothing to fix: no failure here is this change's."),
        *([f"- `!approve all` does the same, including the {len(before)} check(s) that "
           f"were already failing."] if before else []),
        "- `!ship recheck` re-reads CI and rewrites this file.",
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
    elif ours:
        headline = (f"🔴 **`{base}` is red after merging {_pr_label(marker)}.**\n\n"
                    f"The squash commit `{sha[:7]}` failed CI on `{base}`. To undo it "
                    f"now instead of fixing forward: `git revert {sha[:7]}`.")
    else:
        headline = (f"🟠 **`{base}` is red after merging {_pr_label(marker)} — but it "
                    f"was red before, too.**\n\n"
                    f"Every failure on `{sha[:7]}` also failed on {baseline_label}, or "
                    f"is the CI environment. This merge did not cause them.")
    counts = [f"{len(ours)} introduced by this change", f"{len(before)} already failing",
              f"{len(machine)} CI environment"]
    summary_causes = ours or before or machine
    notice = "\n".join([
        headline, "",
        f"**Failing checks:** {', '.join(counts)}.", "",
        ("**What has to change:**" if ours else
         "**Already failing:**" if before else "**CI environment:**"), "",
        _causes_markdown(summary_causes, fenced=False, max_errors=MAX_NOTICE_ERRORS), "",
        f"**Full record:** `{CONTEXT_DIR}/{CI_FAILURE_NAME}`", "",
        *([f"- **Resolve it:** `!approve` {resolve}"] if ours else []),
        *(["- **Fix the older failures too:** `!approve all`"] if before else []),
        *(["- **Needs a person:** the CI environment failures — no build can fix those."]
          if machine else []),
        "- **Re-read CI:** `!ship recheck`",
        (f"- **Fix it yourself:** push to `{branch}`, then `!ship watch`"
         if scope == "pr" else
         f"- **Fix it yourself:** merge a fix to `{base}` by hand, then `!ship watch`"),
    ])
    _notify(workspace, marker, STATE_CI_FAILED, notice,
            failure={"scope": scope, "sha": sha, "baseline": baseline, "causes": causes,
                     "conflict": any(c.get("kind") == "conflict" for c in causes),
                     "summary": _causes_markdown(summary_causes, fenced=False,
                                                 max_errors=MAX_NOTICE_ERRORS)})


def recheck(workspace: str) -> str:
    """
    `!ship recheck` — read CI again for the recorded failure and rewrite the record.

    For a record written by an older parser, or after a check was re-run by hand
    on GitHub. A recheck that finds the failing checks now passing hands the ship
    back to the watcher rather than writing an empty failure.
    """
    marker = read_marker(workspace)
    if not marker or marker.get("state") != STATE_CI_FAILED:
        return status(workspace)
    root = os.path.abspath(workspace)
    failure = marker.get("failure") or {}
    scope, sha = failure.get("scope", "pr"), failure.get("sha", "")

    if scope == "pr":
        data, err = _gh_json(root, "pr", "view", str(marker.get("pr_number") or marker.get("branch")),
                             "--json", "headRefOid,mergeable,statusCheckRollup")
        if data is None:
            return f"❌ **Could not read {_pr_label(marker)} from GitHub.**\n\n> {_one_line(err, 400)}"
        sha = data.get("headRefOid") or sha
        if (data.get("mergeable") or "").upper() == "CONFLICTING":
            causes = [_conflict_cause(root, marker)]
        else:
            pending, failed, _ = _classify_checks(data.get("statusCheckRollup"))
            causes = _check_causes(root, failed)
            if not failed and not pending:
                marker["head_sha"] = sha
                _await_checks(marker, marker.get("pr_url", ""), sha)
                _save_marker(workspace, marker)
                return (f"✅ **{_pr_label(marker)}'s checks pass now.** Back on the gate: it "
                        f"merges once every check on `{sha[:7]}` is green.")
    else:
        runs, err = _gh_json(root, "run", "list", "--commit", sha, "--limit", "50", "--json",
                             "databaseId,status,conclusion,workflowName,name,url")
        if runs is None:
            return f"❌ **Could not read `{marker.get('base')}`'s CI from GitHub.**\n\n> {_one_line(err, 400)}"
        failed = [r for r in runs if (r.get("status") or "").lower() == "completed"
                  and (r.get("conclusion") or "").upper() not in _PASSING]
        causes = _run_causes(root, failed)
        if not failed:
            marker.update({"state": STATE_AWAITING_BASE, "since": time.time(),
                           "notice": None, "announced": True})
            _save_marker(workspace, marker)
            return (f"✅ **Nothing is failing on `{sha[:7]}` now.** Back on the gate, "
                    f"waiting for `{marker.get('base')}`'s runs to finish.")

    _fail(workspace, marker, scope, sha, causes)
    marker = read_marker(workspace) or marker
    marker["announced"] = True
    _save_marker(workspace, marker)
    return marker.get("notice") or status(workspace)


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

# One build per issue. Handing a fix build every failure at once failed the
# first time it was tried: three unrelated problems - a backend suite, 667 lint
# errors, an RLS test - went into one design pass, the pass blocked on one of
# them, and the build died with nothing committed for the other two. Each issue
# now gets a build of its own on the same branch, a fixed issue is committed
# before the next starts, and a failed one is kept as a diff and rolled back so
# it cannot leak into the next. CI judges the whole round once it is pushed;
# anything still red becomes the next round's record.

ISSUE_PENDING = "pending"
ISSUE_FIXED = "fixed"
ISSUE_UNCHANGED = "unchanged"   # the build passed its gates without changing anything
ISSUE_NOT_FIXED = "not fixed"   # the build never reached VERIFIED, SAFE and its tests
ISSUE_SKIPPED = "skipped"       # the queue stopped before reaching it

# Lint and formatting run last: they touch the most files, and going last means
# the code the other issues changed is formatted too.
_LINT_STEP_RE = re.compile(r"\b(?:lint|eslint|prettier|ruff|black|flake8|format|stylelint)\b",
                           re.IGNORECASE)


def _plan_issues(causes: list) -> list:
    """Split triaged causes into issues: one per failed step, or per failing test file."""
    issues = []
    for cause in causes:
        steps = cause.get("steps") or [{"step": "", "command": "", "errors": [],
                                        "origin": cause.get("origin")}]
        for step in steps:
            common = {"check": cause.get("name", ""), "url": cause.get("url", ""),
                      "origin": step.get("origin") or cause.get("origin", ""),
                      "step": step.get("step", ""), "command": step.get("command", ""),
                      "kind": cause.get("kind", "")}
            label = f"{cause.get('name', 'check')} › {step.get('step') or 'failed'}"
            tests = step.get("test_files") or {}
            if len(tests) > 1:
                for test_file, lines in tests.items():
                    issues.append({**common, "title": f"{label}: {test_file}",
                                   "test_file": test_file, "errors": lines,
                                   "error_count": len(lines),
                                   "files": (step.get("test_paths") or {}).get(test_file)
                                   or _paths_in([test_file] + lines)})
            else:
                issues.append({**common, "title": label,
                               "test_file": next(iter(tests), ""),
                               "errors": step.get("errors", []),
                               "error_count": step.get("error_count", len(step.get("errors", []))),
                               "files": step.get("files", [])})
    issues.sort(key=lambda i: bool(_LINT_STEP_RE.search(f"{i['step']} {i['command']}")))
    for n, issue in enumerate(issues, 1):
        issue.update({"id": n, "status": ISSUE_PENDING})
    return issues


def _plan_markdown(issues: list) -> str:
    marks = {ISSUE_PENDING: "⏳", ISSUE_FIXED: "✅", ISSUE_UNCHANGED: "➖",
             ISSUE_NOT_FIXED: "⚠️", ISSUE_SKIPPED: "⏭️"}
    out = []
    for issue in issues:
        line = f"{issue['id']}. {marks.get(issue['status'], '')} {_one_line(issue['title'], 160)}"
        if issue["status"] != ISSUE_PENDING:
            line += f" — {issue['status']}"
        if issue.get("commit"):
            line += f" (`{issue['commit'][:7]}`)"
        if issue.get("note"):
            line += f": {_one_line(issue['note'], 200)}"
        out.append(line)
    return "\n".join(out)


def _resolve_paths(root: str, paths: list) -> list:
    """
    Paths as the fix build will find them in the workspace.

    CI prints paths relative to the step's working directory - `test/x.test.ts`
    for a job that runs in `Backend/` - so a path that does not exist at the
    root is matched against tracked files by suffix, and kept only if that
    match is unique.
    """
    code, out = _git(root, "ls-files")
    tracked = out.splitlines() if code == 0 else []
    resolved = []
    for path in paths:
        if os.path.exists(os.path.join(root, path)):
            hit = path
        else:
            matches = [t for t in tracked if t.endswith("/" + path)]
            hit = matches[0] if len(matches) == 1 else ""
        if hit and hit not in resolved:
            resolved.append(hit)
        if len(resolved) >= MAX_FILES_INVOLVED:
            break
    return resolved


# Error codes such as DOMAIN_NOT_A_VERIFIED_CUSTOMER. A failure that names one
# names the code that raises it, even when no stack frame does: the RLS issue's
# log pointed only at its test file, and the design pass blocked asking for the
# function that throws the code.
_ERROR_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){2,}\b")
_TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|\.(test|spec)\.", re.IGNORECASE)
MAX_RAISING_FILES = 5


def _raising_files(root: str, errors: list) -> list:
    """Non-test source files that contain an error code these errors report."""
    codes = []
    for line in errors:
        for code in _ERROR_CODE_RE.findall(line):
            if code not in codes:
                codes.append(code)
    found = []
    for code in codes[:5]:
        rc, out = _git(root, "grep", "-l", "-F", code)
        for path in (out.splitlines() if rc == 0 else []):
            if _TEST_PATH_RE.search(path) or not _SOURCE_PATH_RE.fullmatch(path):
                continue
            if path not in found:
                found.append(path)
    return found[:MAX_RAISING_FILES]


def _formatting_only(issue: dict) -> bool:
    errors = [e for e in issue.get("errors", []) if re.search(r":\d+:\d+\s", e)]
    return bool(errors) and all("prettier/prettier" in e for e in errors)


def _issue_instruction(workspace: str, marker: dict, issue: dict, total: int) -> str:
    root = os.path.abspath(workspace)
    failure = marker.get("failure") or {}
    scope, base, branch = failure.get("scope", "pr"), marker.get("base", ""), marker.get("branch", "")
    where = (f"pull request {_pr_label(marker)} (branch `{branch}`, into `{base}`)"
             if scope == "pr" else f"the `{base}` branch after merging {_pr_label(marker)}")
    lines = [
        f"!build Fix CI issue {issue['id']} of {total} on {where}: {issue['title']}. "
        f"Fix only this issue. The other failing checks are separate issues, each "
        f"getting its own build: do not touch them. Change nothing else: no refactors, "
        f"no unrelated improvements. Do not skip, weaken or delete a check, test or "
        f"lint rule to make it pass. The full CI record is `{CONTEXT_DIR}/{CI_FAILURE_NAME}`.",
        "",
        f"Failing step: `{_one_line(issue['step'], 120)}`"
        + (f" running `{_one_line(issue['command'], 300)}`" if issue.get("command") else ""),
    ]
    if issue.get("test_file"):
        lines.append(f"Failing test file: `{issue['test_file']}`")
    if issue.get("errors"):
        lines += ["", "Error lines from the CI log:"]
        lines += [f"- `{_one_line(e, 300)}`" for e in issue["errors"]]
        if issue.get("error_count", 0) > len(issue["errors"]):
            lines.append(f"- …and {issue['error_count'] - len(issue['errors'])} more in the log")
    if _formatting_only(issue):
        lines += ["", "FORMATTING ONLY: every error above is the prettier/prettier rule. Run "
                  "the project's formatter over the affected files (its lint --fix or "
                  "prettier --write) rather than editing by hand, and change no logic."]
    files = _resolve_paths(root, issue.get("files", []))
    files += [f for f in _raising_files(root, issue.get("errors", [])) if f not in files]
    if files:
        lines += ["", "FILES INVOLVED - read these before designing the fix; they are where "
                  "the failure is reported or raised:"]
        lines += [f"- `{f}`" for f in files]
    if issue.get("command"):
        lines += ["", "VERIFY: before writing .build_complete, run the failing command the way "
                  "CI ran it (in the directory its workflow step uses) and confirm this issue "
                  "no longer fails. A command that cannot run here - a missing tool or an "
                  "unreachable service - is reported as such, not as passing.",
                  f"- `{_one_line(issue['command'], 300)}`"]
        if issue.get("test_file"):
            lines.append(f"- To iterate faster, run the same runner on `{issue['test_file']}` alone.")
    lines += _scope_lines(workspace, marker)
    return "\n".join(lines).replace("--repo", "-repo").replace("--kb", "-kb")


def _issue_env(marker: dict) -> dict:
    """Every issue after the first continues on the branch the first one started."""
    branch = (marker.get("fix") or {}).get("branch", "")
    return {"BUILD_BRANCH": branch} if branch else {}


def _clear_build_complete(root: str) -> None:
    # The container clears this itself, but only once it gets that far. One that
    # dies earlier must not be read as passing on the strength of an earlier
    # build's VERIFIED SAFE.
    try:
        os.remove(os.path.join(root, ".build_complete"))
    except OSError:
        pass


def ci_fix_request(workspace: str, include_preexisting: bool = False) -> Optional[tuple]:
    """
    For `!approve`: (instruction, extra_env, refusal) for the first issue, or None
    if no CI failure waits.

    Only the failures this change introduced are planned, unless
    `include_preexisting` (`!approve all`). CI-environment failures never are:
    no edit to the project fixes a runner image. The instruction is a `!build`
    message for the conversation the container reads. No fenced blocks, for the
    reason in `_causes_markdown`.
    """
    marker = read_marker(workspace)
    if not marker or marker.get("state") != STATE_CI_FAILED:
        return None
    root = os.path.abspath(workspace)
    failure = marker.get("failure") or {}
    scope, base, branch = failure.get("scope", "pr"), marker.get("base", ""), marker.get("branch", "")

    causes = failure.get("causes", [])
    if any("origin" in c for c in causes):
        targets = _select(causes, FIXABLE_ALL if include_preexisting else FIXABLE_DEFAULT)
    else:
        targets = causes  # Recorded before triage existed: all of it, as before.
    if not targets:
        before = _select(causes, (ORIGIN_PREEXISTING,))
        return "", {}, (
            "🟠 **Nothing here is this change's to fix.**\n\n"
            + (f"{len(before)} failing check(s) were already failing before it, and the "
               f"rest are the CI environment. `!approve all` points a fix build at the "
               f"older failures anyway — that widens this change beyond its design.\n\n"
               if before else
               "Every failure is the CI environment, which no code change fixes.\n\n")
            + f"- **Read them:** `{CONTEXT_DIR}/{CI_FAILURE_NAME}`\n"
            "- **Re-read CI:** `!ship recheck`"
        )

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

    issues = _plan_issues(targets)
    marker.update({
        "state": STATE_FIXING, "fix_started": time.time(),
        "fix_attempts": int(marker.get("fix_attempts", 0)) + 1,
        "fix": {"issues": issues, "current": 0,
                "branch": branch if scope == "pr" else "",
                "start_sha": _head_sha(root),
                "include_preexisting": include_preexisting},
        "notice": None, "announced": True,
    })
    _save_marker(workspace, marker)
    _clear_build_complete(root)
    record_event(workspace, f"🔧 Fix round {marker['fix_attempts']} planned: {len(issues)} issue(s), "
                            f"one build each.")
    record_event(workspace, f"▶️ Issue 1 of {len(issues)} started: {issues[0]['title']}")
    return _issue_instruction(workspace, marker, issues[0], len(issues)), _issue_env(marker), None


def next_fix_request(workspace: str) -> Optional[tuple]:
    """(instruction, extra_env) for the queue's current issue, or None if none is due."""
    marker = read_marker(workspace)
    if not marker or marker.get("state") != STATE_FIXING:
        return None
    fix = marker.get("fix") or {}
    issues, current = fix.get("issues", []), fix.get("current", 0)
    if current >= len(issues):
        return None
    root = os.path.abspath(workspace)
    _clear_build_complete(root)
    fix["start_sha"] = _head_sha(root)
    _save_marker(workspace, marker)
    record_event(workspace, f"▶️ Issue {current + 1} of {len(issues)} started: {issues[current]['title']}")
    return _issue_instruction(workspace, marker, issues[current], len(issues)), _issue_env(marker)


def fix_queue_progress(workspace: str) -> str:
    """One line for chat when the next issue's build starts."""
    marker = read_marker(workspace) or {}
    fix = marker.get("fix") or {}
    issues, current = fix.get("issues", []), fix.get("current", 0)
    if current >= len(issues):
        return ""
    return f"Issue {current + 1} of {len(issues)}: {issues[current]['title']}"


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


def _still_red(marker: dict) -> str:
    if (marker.get("failure") or {}).get("scope") == "base":
        return f"`{marker.get('base')}` is still red after {_pr_label(marker)}"
    return f"CI is still failing on {_pr_label(marker)} and nothing was merged"


def _fix_did_not_land(workspace: str, marker: dict, headline: str, detail: str) -> str:
    summary = (marker.get("failure") or {}).get("summary", "")
    plan = (marker.get("fix") or {}).get("issues")
    notice = "\n".join([
        f"⚠️ **{headline}**", "", detail, "",
        f"{_still_red(marker)}.", "",
        *(["**Issues this round:**", "", _plan_markdown(plan), ""] if plan else []),
        "**What still has to change:**", "", summary, "",
        f"- **Try again:** `!approve` (edit `{CONTEXT_DIR}/{CI_FAILURE_NAME}` first to steer it)",
        "- **See what the build did:** `!logs`",
    ])
    _notify(workspace, marker, STATE_CI_FAILED, notice)
    return STATE_CI_FAILED


def _set_aside(workspace: str, issue: dict, start_sha: str = "") -> str:
    """
    Keep a failed issue's edits as a diff, and return the branch to its last commit.

    The next issue's build starts from this tree. Leaving a half-done attempt in
    it would hand that build someone else's broken edits, and commit them under
    the next issue's name if it passed.
    """
    root = os.path.abspath(workspace)
    rel = f"{CONTEXT_DIR}/ci_issue_{issue['id']}.diff"
    # Only the build's own paths. A blanket `git add -N .` would also mark the
    # orchestration scratch in any workspace whose .gitignore does not cover it,
    # and `reset --hard` deletes whatever the index has that HEAD does not -
    # the CI record and the design documents included.
    paths = [p for p in _status_paths(root)
             if p.split("/")[0] not in EXCLUDED_DIRS and p not in EXCLUDED_FILES]
    code, out = _git(root, "ls-files", "-z")
    tracked = {p for p in out.split("\0") if p} if code == 0 else set()
    untracked = [p for p in paths if p not in tracked and os.path.exists(os.path.join(root, p))]
    for i in range(0, len(untracked), ADD_BATCH):
        _git(root, "add", "--intent-to-add", "--", *untracked[i:i + ADD_BATCH])
    # From the issue's starting commit, so commits the build made itself are in
    # the kept diff and are rolled back with everything else.
    base_ref = start_sha or "HEAD"
    code, diff = _git(root, "diff", base_ref, "--", *paths) if paths else (0, "")
    if start_sha:
        rc, committed = _git(root, "diff", start_sha, "HEAD")
        if rc == 0 and committed.strip() and committed.strip() not in diff:
            diff = (committed + "\n" + diff) if diff.strip() else committed
    if code == 0 and diff.strip():
        try:
            with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
                f.write(diff + "\n")
        except OSError as e:
            logger.warning(f"Could not keep the diff for issue {issue['id']}: {e}")
            rel = ""
    else:
        rel = ""
    _git(root, "reset", "-q", "--hard", base_ref)
    # reset drops the intent-to-add entries but leaves their files behind.
    for path in untracked:
        try:
            os.remove(os.path.join(root, path))
        except OSError:
            pass
    return rel


def _consume_build_complete(root: str) -> bool:
    """
    Read the build's verdict and delete it.

    It is never committed (EXCLUDED_FILES), so a fixed issue's VERIFIED SAFE
    would otherwise still be on disk when the next issue's build ends - and
    vouch for that one too if it died before clearing it.
    """
    passed = _build_passed_its_gates(root)
    _clear_build_complete(root)
    return passed


def _dirty(root: str) -> bool:
    return any(p.split("/")[0] not in EXCLUDED_DIRS and p not in EXCLUDED_FILES
               for p in _status_paths(root))


def fix_build_finished(workspace: str) -> str:
    """
    Called when an issue's fix build exits. Returns the state afterwards.

    STATE_FIXING means another issue is due, and the caller starts its build
    (`next_fix_request`). Otherwise the round is over: what was fixed is pushed
    and goes back through checkpoint 1 - `!approve` was the consent to that, and
    the merge is still CI's to allow.
    """
    marker = read_marker(workspace)
    if not marker or marker.get("state") != STATE_FIXING:
        return marker.get("state", "") if marker else ""
    root = os.path.abspath(workspace)
    failure = marker.get("failure") or {}
    scope, base = failure.get("scope", "pr"), marker.get("base", "")
    fix = marker.setdefault("fix", {})
    if not fix.get("issues"):
        # Started before the queue existed: one build for everything.
        fix.update({"issues": [{"id": 1, "title": "all selected failures", "status": ISSUE_PENDING,
                                "step": "", "command": "", "errors": [], "files": []}],
                    "current": 0, "branch": marker.get("branch", "") if scope == "pr" else ""})
    issues, current = fix["issues"], fix.get("current", 0)
    issue = issues[current]
    branch = _current_branch(root)
    # The commit this issue's build started from. An agent that commits its own
    # work leaves nothing for the commit step below, and judging by that step
    # alone called a fixed issue "unchanged" - and, never recording the branch,
    # sent the next issue's build off to start a second one.
    start_sha = fix.get("start_sha", "")
    on_fix_branch = (branch == marker.get("branch")) if scope == "pr" \
        else branch.startswith(BUILD_BRANCH_PREFIX)

    if not on_fix_branch:
        issue.update({"status": ISSUE_NOT_FIXED,
                      "note": f"the build ended on `{branch}`, not a fix branch"})
        if _dirty(root):
            # Nothing here is ours to roll back. Stop rather than build on it.
            for later in issues[current + 1:]:
                later["status"] = ISSUE_SKIPPED
            fix["current"] = len(issues)
            _save_marker(workspace, marker)
            return _finish_fix_queue(workspace, marker)
    elif _consume_build_complete(root):
        before = _head_sha(root)
        title = f"Fix CI: {_one_line(issue['title'], MAX_TITLE_CHARS - 9)}"[:MAX_TITLE_CHARS]
        body = "\n".join([
            "Built by br.ai.n to resolve one CI failure.", "",
            f"**Issue {issue['id']} of {len(issues)}:** {issue['title']}",
            f"**Failure record:** `{CONTEXT_DIR}/{CI_FAILURE_NAME}`",
        ])
        try:
            _commit_and_push(root, branch, title, body, _changed_files(root, base), push=False)
        except RuntimeError as e:
            issue.update({"status": ISSUE_NOT_FIXED, "note": f"could not commit: {e}",
                          "diff": _set_aside(workspace, issue, start_sha)})
        else:
            head = _head_sha(root)
            if head == (start_sha or before):
                issue["status"] = ISSUE_UNCHANGED
            else:
                issue.update({"status": ISSUE_FIXED, "commit": head})
                if head == before:
                    issue["note"] = "committed by the build itself"
                fix["branch"] = branch
    else:
        issue.update({"status": ISSUE_NOT_FIXED,
                      "note": "never reached VERIFIED, SAFE and a passing test gate "
                              "(a design-pass blocker is one way; `!logs` names it)",
                      "diff": _set_aside(workspace, issue, start_sha)})

    marks = {ISSUE_FIXED: "✅", ISSUE_UNCHANGED: "➖", ISSUE_NOT_FIXED: "⚠️"}
    outcome = issue["status"] + (f" (`{issue['commit'][:7]}`)" if issue.get("commit") else "")
    if issue.get("diff"):
        outcome += f"; its edits are kept in `{issue['diff']}`"
    record_event(workspace, f"{marks.get(issue['status'], '•')} Issue {current + 1} of {len(issues)} "
                            f"{outcome}: {issue['title']}")

    fix["current"] = current + 1
    # The next issue starts here, whatever launches it. A stale start would let
    # the next failed issue roll back past this one's fix.
    fix["start_sha"] = _head_sha(root)
    _save_marker(workspace, marker)
    if fix["current"] < len(issues):
        return STATE_FIXING
    return _finish_fix_queue(workspace, marker)


def _finish_fix_queue(workspace: str, marker: dict) -> str:
    """Push what this round fixed, and hand it to CI."""
    root = os.path.abspath(workspace)
    failure = marker.get("failure") or {}
    scope, base = failure.get("scope", "pr"), marker.get("base", "")
    fix = marker.get("fix") or {}
    issues = fix.get("issues", [])
    fixed = [i for i in issues if i["status"] == ISSUE_FIXED]
    kept = [f"`{i['diff']}`" for i in issues if i.get("diff")]

    if not fixed:
        return _fix_did_not_land(
            workspace, marker,
            f"None of this round's {len(issues)} issue(s) was fixed.",
            "Nothing was committed or pushed."
            + (f" Each failed attempt's edits are kept: {', '.join(kept)}." if kept else ""))

    branch = fix.get("branch") or _current_branch(root)
    try:
        _git(root, "push", "-u", "origin", branch, check=True)
    except RuntimeError as e:
        return _fix_did_not_land(workspace, marker, "The fixes could not be pushed.",
                                 f"> {_one_line(str(e), 400)}")

    plan = _plan_markdown(issues)
    tail = ("CI judges the round as a whole. If anything is still red, a new record is "
            "written and `!approve` starts the next round.")
    if scope == "pr":
        head = _head_sha(root)
        _await_checks(marker, marker.get("pr_url", ""), head)
        marker.update({"announced": False, "notice": "\n".join([
            f"🔁 **{len(fixed)} of {len(issues)} issue(s) fixed and pushed to "
            f"{_pr_label(marker)} as `{head[:7]}` — waiting for CI.**", "",
            plan, "",
            *([f"Failed attempts are kept: {', '.join(kept)}.", ""] if kept else []),
            f"It merges only once every check on `{head[:7]}` passes. {tail}", "",
            "- **Where it is:** `!ship status`"])})
        _save_marker(workspace, marker)
        return STATE_AWAITING_CHECKS

    title = f"Fix {base} CI: {len(fixed)} issue(s) after {_pr_label(marker)}"[:MAX_TITLE_CHARS]
    body = "\n".join(["Built by br.ai.n, one build per CI issue.", "",
                      f"**Failure record:** `{CONTEXT_DIR}/{CI_FAILURE_NAME}`", "", plan])
    url, pr_error = _open_pull_request(root, title, body, branch, base)
    marker["branch"] = branch
    if not url:
        return _fix_did_not_land(workspace, marker,
                                 "The fixes were pushed, but their pull request could not be opened.",
                                 f"> {_one_line(pr_error, 400)}")
    _await_checks(marker, url, _head_sha(root))
    marker.update({"announced": False, "notice": "\n".join([
        f"🔁 **{len(fixed)} of {len(issues)} issue(s) fixed for `{base}`, opened as "
        f"{_pr_label(marker)} — waiting for CI.**", "",
        f"- {url}", "", plan, "",
        *([f"Failed attempts are kept: {', '.join(kept)}.", ""] if kept else []),
        f"It merges only once every check on it passes. {tail}", "",
        "- **Where it is:** `!ship status`"])})
    _save_marker(workspace, marker)
    return STATE_AWAITING_CHECKS


def fix_launch_failed(workspace: str, reply: str) -> None:
    """The next issue's build could not be started: end the round with what is done."""
    marker = read_marker(workspace)
    if not marker or marker.get("state") != STATE_FIXING:
        return
    fix = marker.get("fix") or {}
    issues = fix.get("issues", [])
    for later in issues[fix.get("current", 0):]:
        later.update({"status": ISSUE_SKIPPED, "note": _one_line(reply, 160)})
    fix["current"] = len(issues)
    _save_marker(workspace, marker)
    _finish_fix_queue(workspace, marker)


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
        fix = marker.get("fix") or {}
        lines.append(f"- Round {marker.get('fix_attempts', 1)}: building in "
                     f"`{marker.get('container', '?')}`. `!logs` follows it.")
        if fix.get("issues"):
            lines += ["", _plan_markdown(fix["issues"])]
    elif state == STATE_READY:
        lines.append("- `!ship` shows what would be shipped.")
    activity = recent_events(workspace)
    if activity:
        lines += ["", activity]
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
