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
# documents, the serialized conversation, this marker. `git add -A` would sweep
# the lot into the user's pull request, conversation transcript included.
EXCLUDED_PATHSPECS = (f":(exclude){CONTEXT_DIR}", ":(exclude).cline_logs")

MAX_TITLE_CHARS = 72

# How many files to name in chat before summarising. A build touching 200 files
# should not push its own offer off the screen.
MAX_LISTED_FILES = 20


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
    """Drops the marker. Called when work ships, and when a new build starts."""
    try:
        os.remove(marker_path(workspace))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"Could not clear the build marker: {e}")


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

    _save_marker(workspace, {
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

    marker["announced"] = True
    _save_marker(workspace, marker)

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


def _changed_files(root: str, base: str) -> list:
    """Every path this branch changes: working tree, index and commits."""
    paths = set()

    # -uall so an untracked directory is listed as its files rather than as
    # "dir/", which would tell the user a directory changed and nothing more.
    code, out = _git(root, "status", "--porcelain", "-uall")
    if code == 0:
        for line in out.splitlines():
            # "XY path", or "XY old -> new" for a rename. Never lstrip: the
            # status columns are two characters wide and often blank.
            path = line[3:] if len(line) > 3 else ""
            path = path.split(" -> ")[-1].strip().strip('"')
            if path:
                paths.add(path)

    code, out = _git(root, "diff", "--name-only", f"{_remote_ref(root, base)}...HEAD")
    if code == 0:
        paths.update(ln.strip() for ln in out.splitlines() if ln.strip())

    return sorted(p for p in paths
                  if p.split("/")[0] not in (CONTEXT_DIR, ".cline_logs"))


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
        f"4. **Squash-merge it** and delete the branch.\n\n"
        f"Step 4 rewrites `{base}` and cannot be undone from chat.\n\n"
        f"- **Go ahead:** `!ship confirm`\n"
        f"- **Different title:** `!ship <your title>`, then confirm\n"
        f"- **Read the diff first:** `git -C {root} diff {base}`"
    )


def confirm(workspace: str) -> str:
    """`!ship confirm` — commit, push, open the PR, squash-merge it."""
    marker, root, files, refusal = _gate(workspace)
    if refusal:
        return refusal

    branch, base = marker["branch"], marker["base"]
    title, body = _commit_text(workspace, marker, files)

    try:
        # `git add -A` over the workspace, minus the orchestration scratch that
        # happens to live inside it. Everything else here is build output by
        # definition: the workspace is this conversation's own folder.
        _git(root, "add", "-A", "--", ".", *EXCLUDED_PATHSPECS, check=True)

        code, staged = _git(root, "diff", "--cached", "--name-only")
        if staged.strip():
            _git(root, "commit", "-m", title, "-m", body, check=True)
        else:
            # The agent committed as it went. The branch is still ahead of base,
            # or the gate would have refused, so there is work to push.
            logger.info(f"Nothing left to stage on {branch}; it is already committed.")

        _git(root, "push", "-u", "origin", branch, check=True)
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

    merged, merge_error = _squash_merge(root, branch)
    if not merged:
        return (
            f"🔍 **Pull request open, not merged.**\n\n"
            f"- **Branch:** `{branch}` → `{base}`\n"
            f"- {url}\n\n"
            f"The squash-merge was refused — usually branch protection, a required "
            f"review, or a conflict. Nothing was lost; merge it on GitHub when it is "
            f"ready.\n\n```\n{merge_error[:600]}\n```"
        )

    _return_to_base(root, base)
    clear_marker(workspace)
    return (
        f"✅ **Squash-merged into `{base}`.**\n\n"
        f"- **Files:** {len(files)}\n"
        f"- **Branch:** `{branch}` (deleted)\n"
        f"- {url}\n\n"
        f"The workspace is back on `{base}`."
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


def _squash_merge(root: str, branch: str) -> tuple:
    """(merged, error). Branch protection failing here is a refusal, not a crash."""
    result = subprocess.run(
        ["gh", "pr", "merge", branch, "--squash", "--delete-branch"],
        cwd=root, capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True, ""
    output = (result.stdout + result.stderr).strip()
    logger.error(f"gh pr merge failed: {output}")
    return False, output


def _return_to_base(root: str, base: str) -> None:
    """Best effort: leave the workspace somewhere sane for the next build."""
    _git(root, "checkout", base)
    _git(root, "pull", "--ff-only", "origin", base)
