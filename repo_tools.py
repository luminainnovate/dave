"""
Repository CRUD and pull-request support for conversation-bound projects.

The orchestrator binds a chat to a project directory under `conversations/`
(usually a symlink to a real working repo). This module lets the Expert model
create, edit and delete files in that project, and lets the user turn the
result into a pull request.

Safety model (mirrors the file-editing semantics of Claude Code, which edits in
place rather than branching first):

  * Write mode is opt-in per conversation (`!write`). Read-only by default, so
    ordinary discussion turns cannot mutate anything.
  * Read-before-write: a file must have been read this session before it can be
    edited or overwritten. New files may be created freely.
  * Edits are anchor-based and must match exactly once, so a bad anchor fails
    loudly instead of silently mangling code.
  * Every path is realpath-resolved and checked for containment, which catches
    symlinks inside the project that point outside it.
  * The first write to any path snapshots its exact bytes, so `!undo` restores
    the file even when it was untracked or already dirty before we touched it.

Nothing here commits or pushes on the model's behalf. Branching, committing and
PR creation happen only via the user's `!pr` command.
"""

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile

from typing import Optional

import mover

logger = logging.getLogger("Bob-Orchestrator")

# =============================================================================
# POLICY
# =============================================================================

# Directories that are never writable, matched on any path component.
DENIED_DIRS = {
    ".git", "node_modules", ".knowledge_base", ".venv", "venv",
    "__pycache__", ".cline_context", ".cline_logs",
}

# Filenames that are never writable, matched on the basename.
DENIED_FILE_RE = re.compile(
    r"(^\.env|^id_rsa|^id_ed25519|^\.netrc|^\.npmrc|\.pem$|\.key$|\.p12$|\.pfx$)",
    re.IGNORECASE,
)

MAX_WRITE_BYTES = 1_000_000
MAX_LIST_CHARS = 4000

# Where per-file checkpoints live. Outside the repo so snapshots never leak
# into a diff or a commit.
CHECKPOINT_ROOT = os.path.join(tempfile.gettempdir(), "brain_checkpoints")

# Sentinel recorded when a path did not exist at checkpoint time.
_ABSENT = "__absent__"


# =============================================================================
# SESSION STATE
# =============================================================================
# Keyed by conversation id. In-memory only: a restart resets write mode to off,
# which fails safe.
_sessions: dict[str, dict] = {}


def get_session(conv_id: str) -> dict:
    """Returns (creating if needed) the mutable state for one conversation."""
    if conv_id not in _sessions:
        _sessions[conv_id] = {
            "write_enabled": False,
            "read_paths": set(),      # abs paths read this session
            "touched": [],            # rel paths written/deleted, in order
            "snapshots": {},          # rel path -> snapshot file, or _ABSENT
            "base_branch": None,      # branch we were on when work started
        }
    return _sessions[conv_id]


def is_write_enabled(conv_id: str) -> bool:
    return get_session(conv_id)["write_enabled"] if conv_id else False


def set_write_mode(conv_id: str, enabled: bool) -> str:
    """Toggles write mode. Returns the message shown in chat."""
    session = get_session(conv_id)
    session["write_enabled"] = enabled
    if enabled:
        return (
            "✏️ **Write mode ON.** The Expert can now create, edit and delete files "
            "in the bound project.\n\n"
            "Files must be read before they can be edited. Every change is snapshotted "
            "— `!undo` reverts them, `!diff` shows them, `!pr` opens a pull request.\n\n"
            "Use `!readonly` to revoke."
        )
    return "🔒 **Write mode OFF.** The Expert is read-only again. Existing changes are untouched."


def mark_read(conv_id: str, abs_path: str):
    """Records that a file was read, satisfying the read-before-write rule."""
    if conv_id:
        get_session(conv_id)["read_paths"].add(os.path.realpath(abs_path))


def _was_read(conv_id: str, abs_path: str) -> bool:
    return os.path.realpath(abs_path) in get_session(conv_id)["read_paths"]


# =============================================================================
# PATH SAFETY
# =============================================================================

def resolve_safe_path(project_dir: str, rel_path: str) -> tuple[str, str]:
    """
    Resolves a model-supplied path against the project root.

    Returns (abs_path, clean_rel_path). Raises ValueError if the path escapes
    the project or hits the deny-list.

    `project_dir` is typically a symlink, so the root is realpath'd first and
    the candidate realpath'd after — that is what catches a symlink *inside* the
    project pointing somewhere else. sanitize_path alone only stops `..`.
    """
    if not rel_path:
        raise ValueError("No path supplied.")

    clean = mover.sanitize_path(rel_path)
    if not clean:
        raise ValueError(f"`{rel_path}` is not a usable path.")

    parts = clean.split(os.sep)
    for part in parts:
        if part in DENIED_DIRS:
            raise ValueError(f"`{clean}` is inside a protected directory (`{part}`).")
    if DENIED_FILE_RE.search(parts[-1]):
        raise ValueError(f"`{clean}` is a protected file type and cannot be written.")

    root = os.path.realpath(project_dir)
    # realpath resolves the existing prefix and appends the rest, so this works
    # for files that do not exist yet.
    resolved = os.path.realpath(os.path.join(root, clean))
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError(f"`{clean}` resolves outside the project directory.")

    return resolved, clean


def _looks_binary(abs_path: str) -> bool:
    try:
        with open(abs_path, "rb") as f:
            return b"\x00" in f.read(8192)
    except Exception:
        return False


# =============================================================================
# CHECKPOINTS
# =============================================================================

def _snapshot(conv_id: str, abs_path: str, rel_path: str):
    """
    Captures a file's bytes before its first modification this session.

    Byte snapshots rather than `git stash create` because the bound repo may
    hold untracked files (a stash commit would not contain them, and restoring
    from it would delete them) and may already be dirty before we start.
    """
    session = get_session(conv_id)
    if rel_path in session["snapshots"]:
        return  # already captured; keep the earliest state

    if not os.path.exists(abs_path):
        session["snapshots"][rel_path] = _ABSENT
        return

    conv_dir = os.path.join(CHECKPOINT_ROOT, conv_id)
    os.makedirs(conv_dir, exist_ok=True)
    digest = hashlib.md5(rel_path.encode("utf-8")).hexdigest()
    dest = os.path.join(conv_dir, digest)
    shutil.copy2(abs_path, dest)
    session["snapshots"][rel_path] = dest


def _record_touch(conv_id: str, rel_path: str):
    session = get_session(conv_id)
    if rel_path not in session["touched"]:
        session["touched"].append(rel_path)


def undo_changes(project_dir: str, conv_id: str) -> str:
    """Restores every file this session modified to its pre-session bytes."""
    session = get_session(conv_id)
    snapshots = session["snapshots"]
    if not snapshots:
        return "⚠️ **Nothing to undo.** No files have been modified in this conversation."

    root = os.path.realpath(project_dir)
    restored, deleted, failed = [], [], []

    for rel_path, snap in snapshots.items():
        abs_path = os.path.join(root, rel_path)
        try:
            if snap == _ABSENT:
                # The file did not exist before we touched it.
                if os.path.exists(abs_path):
                    os.remove(abs_path)
                deleted.append(rel_path)
            else:
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                shutil.copy2(snap, abs_path)
                restored.append(rel_path)
        except Exception as e:
            failed.append(f"{rel_path} ({e})")

    session["snapshots"] = {}
    session["touched"] = []

    lines = ["↩️ **Undo complete.**"]
    if restored:
        lines.append(f"\n**Restored ({len(restored)}):**")
        lines.extend(f"- `{p}`" for p in restored)
    if deleted:
        lines.append(f"\n**Removed (created this session) ({len(deleted)}):**")
        lines.extend(f"- `{p}`" for p in deleted)
    if failed:
        lines.append(f"\n**Failed ({len(failed)}):**")
        lines.extend(f"- `{p}`" for p in failed)
    lines.append("\nPre-existing changes in the repo were not affected.")
    return "\n".join(lines)


# =============================================================================
# GIT HELPERS
# =============================================================================

def _git(root: str, *args: str, check: bool = False) -> tuple[int, str]:
    """Runs a git command in the project root. Returns (returncode, output)."""
    result = subprocess.run(
        ["git", "-C", root, *args],
        capture_output=True, text=True,
    )
    output = (result.stdout + result.stderr).strip()
    if check and result.returncode != 0:
        raise RuntimeError(output or f"git {' '.join(args)} failed")
    return result.returncode, output


def _is_git_repo(root: str) -> bool:
    code, out = _git(root, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out == "true"


def _current_branch(root: str) -> str:
    _, out = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return out


def _default_branch(root: str) -> str:
    """Best-effort detection of the remote's default branch."""
    code, out = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if code == 0 and "/" in out:
        return out.split("/", 1)[1]
    for candidate in ("main", "master"):
        if _git(root, "rev-parse", "--verify", f"refs/heads/{candidate}")[0] == 0:
            return candidate
    return _current_branch(root)


def _work_branch(conv_id: str) -> str:
    return f"brain/{conv_id}"


# =============================================================================
# WRITE TOOLS
# =============================================================================
# Each returns a plain string: the tool result fed back into the agentic loop.

def write_file(project_dir: str, conv_id: str, path: str, content: str) -> str:
    """Creates a new file, or overwrites one that has already been read."""
    try:
        abs_path, rel_path = resolve_safe_path(project_dir, path)
    except ValueError as e:
        return f"Error: {e}"

    if content is None:
        return "Error: No content supplied."
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return f"Error: Refusing to write more than {MAX_WRITE_BYTES} bytes to `{rel_path}`."

    exists = os.path.exists(abs_path)
    if exists:
        if os.path.isdir(abs_path):
            return f"Error: `{rel_path}` is a directory."
        if not _was_read(conv_id, abs_path):
            return (
                f"Error: `{rel_path}` already exists and has not been read this session. "
                f"Call orchestrator_read_file on it first, then use orchestrator_edit_file "
                f"to change only what needs changing."
            )
        if _looks_binary(abs_path):
            return f"Error: `{rel_path}` looks like a binary file."

    try:
        _snapshot(conv_id, abs_path, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return f"Error writing `{rel_path}`: {e}"

    _record_touch(conv_id, rel_path)
    mark_read(conv_id, abs_path)
    lines = content.count("\n") + 1
    verb = "Updated" if exists else "Created"
    logger.info(f"Repo tool: {verb} {rel_path} ({lines} lines)")
    return f"{verb} `{rel_path}` ({lines} lines).\n{_change_summary(project_dir, conv_id)}"


def edit_file(project_dir: str, conv_id: str, path: str, old_string: str,
              new_string: str, replace_all: bool = False) -> str:
    """Replaces an exact anchor in a file that has already been read."""
    try:
        abs_path, rel_path = resolve_safe_path(project_dir, path)
    except ValueError as e:
        return f"Error: {e}"

    if not os.path.exists(abs_path):
        return f"Error: `{rel_path}` not found. Use orchestrator_write_file to create it."
    if os.path.isdir(abs_path):
        return f"Error: `{rel_path}` is a directory."
    if not _was_read(conv_id, abs_path):
        return (
            f"Error: `{rel_path}` has not been read this session. "
            f"Call orchestrator_read_file on it before editing."
        )
    if not old_string:
        return "Error: old_string is required and must not be empty."
    if old_string == new_string:
        return "Error: old_string and new_string are identical; nothing to do."

    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return f"Error reading `{rel_path}`: {e}"

    occurrences = content.count(old_string)
    if occurrences == 0:
        return (
            f"Error: old_string was not found in `{rel_path}`. It must match the file "
            f"exactly, including indentation and line breaks. Re-read the file and copy "
            f"the anchor verbatim."
        )
    if occurrences > 1 and not replace_all:
        return (
            f"Error: old_string appears {occurrences} times in `{rel_path}`. Extend it "
            f"with surrounding lines until it is unique, or pass replace_all=true to "
            f"change every occurrence."
        )

    updated = content.replace(old_string, new_string) if replace_all \
        else content.replace(old_string, new_string, 1)

    try:
        _snapshot(conv_id, abs_path, rel_path)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(updated)
    except Exception as e:
        return f"Error writing `{rel_path}`: {e}"

    _record_touch(conv_id, rel_path)
    changed = occurrences if replace_all else 1
    logger.info(f"Repo tool: Edited {rel_path} ({changed} replacement(s))")
    return (
        f"Edited `{rel_path}` ({changed} replacement"
        f"{'s' if changed != 1 else ''}).\n{_change_summary(project_dir, conv_id)}"
    )


def delete_file(project_dir: str, conv_id: str, path: str) -> str:
    """Deletes a file that has already been read."""
    try:
        abs_path, rel_path = resolve_safe_path(project_dir, path)
    except ValueError as e:
        return f"Error: {e}"

    if not os.path.exists(abs_path):
        return f"Error: `{rel_path}` not found."
    if os.path.isdir(abs_path):
        return f"Error: `{rel_path}` is a directory. Only files can be deleted."
    if not _was_read(conv_id, abs_path):
        return (
            f"Error: `{rel_path}` has not been read this session. "
            f"Call orchestrator_read_file on it before deleting."
        )

    try:
        _snapshot(conv_id, abs_path, rel_path)
        os.remove(abs_path)
    except Exception as e:
        return f"Error deleting `{rel_path}`: {e}"

    _record_touch(conv_id, rel_path)
    logger.info(f"Repo tool: Deleted {rel_path}")
    return f"Deleted `{rel_path}`.\n{_change_summary(project_dir, conv_id)}"


def list_changes(project_dir: str, conv_id: str) -> str:
    """Reports the files this session has changed, and their diff stat."""
    session = get_session(conv_id)
    touched = session["touched"]
    if not touched:
        return "No files have been changed in this conversation yet."

    lines = [f"Files changed this session ({len(touched)}):"]
    lines.extend(f"- {p}" for p in touched)

    root = os.path.realpath(project_dir)
    if _is_git_repo(root):
        code, out = _git(root, "diff", "--stat", "--", *touched)
        if code == 0 and out:
            lines.append("\nDiff stat:\n" + out)
    return "\n".join(lines)


def _change_summary(project_dir: str, conv_id: str) -> str:
    """One-line running tally appended to each write result."""
    touched = get_session(conv_id)["touched"]
    return f"[Session changes: {len(touched)} file(s) — {', '.join(touched)}]"


# =============================================================================
# USER COMMANDS
# =============================================================================

def diff_changes(project_dir: str, conv_id: str) -> str:
    """`!diff` — full diff of this session's changes, for review before `!pr`."""
    session = get_session(conv_id)
    touched = session["touched"]
    if not touched:
        return "⚠️ **No changes yet.** The Expert has not modified any files in this conversation."

    root = os.path.realpath(project_dir)
    if not _is_git_repo(root):
        return "⚠️ **Not a git repository.** Changed files:\n" + \
            "\n".join(f"- `{p}`" for p in touched)

    # --intent-to-add makes new files show up in the diff without staging them.
    _git(root, "add", "--intent-to-add", "--", *touched)
    code, out = _git(root, "diff", "--", *touched)

    header = f"📝 **Session changes ({len(touched)} file(s))**\n"
    if code != 0 or not out:
        return header + "\n".join(f"- `{p}`" for p in touched)
    if len(out) > 15000:
        out = out[:15000] + "\n... [diff truncated]"
    return f"{header}\n```diff\n{out}\n```"


def create_pull_request(project_dir: str, conv_id: str, title: str) -> str:
    """
    `!pr` — commits this session's files onto a work branch and opens a PR.

    Only the files this session touched are staged; anything else that was
    already dirty in the repo is deliberately left alone.
    """
    session = get_session(conv_id)
    touched = list(session["touched"])
    if not touched:
        return "⚠️ **Nothing to raise.** The Expert has not modified any files in this conversation."

    root = os.path.realpath(project_dir)
    if not _is_git_repo(root):
        return f"❌ **Not a git repository:** `{root}` is not under version control."

    code, remotes = _git(root, "remote")
    if code != 0 or "origin" not in remotes.split():
        return "❌ **No `origin` remote.** A pull request needs a remote to push to."

    if not shutil.which("gh"):
        return "❌ **`gh` CLI not found.** Install and authenticate the GitHub CLI to raise pull requests."

    title = (title or "").strip() or f"Changes from br.ai.n conversation {conv_id}"

    branch = _work_branch(conv_id)
    current = _current_branch(root)
    base = session["base_branch"] or (_default_branch(root) if current == branch else current)
    session["base_branch"] = base

    try:
        # Move onto the work branch, carrying the uncommitted edits across.
        # The repo's other dirty files come too but are never staged below.
        if current != branch:
            if _git(root, "rev-parse", "--verify", f"refs/heads/{branch}")[0] == 0:
                _git(root, "checkout", branch, check=True)
            else:
                _git(root, "checkout", "-b", branch, check=True)

        _git(root, "add", "--", *touched, check=True)

        code, staged = _git(root, "diff", "--cached", "--name-only")
        if not staged.strip():
            return (
                "⚠️ **Nothing to commit.** The session's files match the last commit — "
                "the changes may already have been committed."
            )

        body_lines = [
            "Generated from a br.ai.n conversation via the Open WebUI chat interface.",
            "",
            "**Files changed:**",
            *[f"- `{p}`" for p in touched],
        ]
        body = "\n".join(body_lines)

        _git(root, "commit", "-m", title, "-m", body, check=True)
        _git(root, "push", "-u", "origin", branch, check=True)

        result = subprocess.run(
            ["gh", "pr", "create",
             "--title", title,
             "--body", body,
             "--head", branch,
             "--base", base],
            cwd=root, capture_output=True, text=True,
        )
        output = (result.stdout + result.stderr).strip()

        if result.returncode != 0:
            if "already exists" in output.lower():
                view = subprocess.run(
                    ["gh", "pr", "view", branch, "--json", "url", "-q", ".url"],
                    cwd=root, capture_output=True, text=True,
                )
                url = view.stdout.strip()
                return (
                    f"✅ **Pushed to `{branch}`.** An open pull request already exists, "
                    f"so the commit was added to it.\n\n{url}"
                )
            logger.error(f"gh pr create failed: {output}")
            return (
                f"⚠️ **Pushed to `{branch}`, but the pull request could not be opened.**\n\n"
                f"```\n{output[:800]}\n```"
            )

        url = next((ln for ln in output.splitlines() if ln.startswith("http")), output)
        # Committed work is now in git history; the snapshots are spent.
        session["snapshots"] = {}
        session["touched"] = []
        logger.info(f"Repo tool: Opened PR for {branch} -> {base}")
        return (
            f"✅ **Pull request opened.**\n\n"
            f"- **Branch:** `{branch}` → `{base}`\n"
            f"- **Files:** {len(touched)}\n"
            f"- **Repo:** `{os.path.basename(root)}`\n\n{url}\n\n"
            f"Your other uncommitted changes in this repo were left untouched. "
            f"You are now on `{branch}` — switch back with `git checkout {base}`."
        )

    except RuntimeError as e:
        logger.error(f"PR creation failed: {e}")
        return f"❌ **Pull request failed:**\n\n```\n{str(e)[:800]}\n```"
    except Exception as e:
        logger.error(f"PR creation failed: {e}")
        return f"❌ **Pull request failed:** {e}"


# =============================================================================
# TOOL SCHEMAS
# =============================================================================

WRITE_TOOL_NAMES = (
    "orchestrator_write_file",
    "orchestrator_edit_file",
    "orchestrator_delete_file",
    "orchestrator_list_changes",
)


def write_tool_schemas() -> list:
    """Tool declarations injected only when write mode is on."""
    return [
        {
            "type": "function",
            "function": {
                "name": "orchestrator_edit_file",
                "description": (
                    "Replaces an exact block of text in an existing project file. This is the "
                    "preferred way to change existing code. You MUST call orchestrator_read_file "
                    "on the file first. old_string must match the file exactly (including "
                    "indentation) and appear exactly once, otherwise the edit is rejected."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to the file"},
                        "old_string": {"type": "string", "description": "Exact existing text to replace"},
                        "new_string": {"type": "string", "description": "Replacement text"},
                        "replace_all": {
                            "type": "boolean",
                            "description": "Replace every occurrence instead of requiring a unique match",
                        },
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "orchestrator_write_file",
                "description": (
                    "Writes a file's full contents. Use this to create NEW files. To change an "
                    "existing file, prefer orchestrator_edit_file — overwriting an existing file "
                    "requires reading it first and risks losing content."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to the file"},
                        "content": {"type": "string", "description": "Complete file contents"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "orchestrator_delete_file",
                "description": (
                    "Deletes a file from the project. You MUST call orchestrator_read_file on it "
                    "first."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path to the file"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "orchestrator_list_changes",
                "description": (
                    "Lists the files changed so far in this conversation, with a diff stat. Use "
                    "this to confirm your edits landed before telling the user you are done."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    ]


WRITE_MODE_GUIDANCE = (
    "\n<WRITE_MODE>\n"
    "You can modify this project. Rules:\n"
    "1. Always call orchestrator_read_file on a file before editing or deleting it.\n"
    "2. Use orchestrator_edit_file for existing files; orchestrator_write_file is for new files.\n"
    "3. Make the smallest edit that achieves the goal. Never rewrite a whole file to change a few lines.\n"
    "4. Match the surrounding code's style, naming and comment density.\n"
    "5. Call orchestrator_list_changes when finished, then summarise what you changed.\n"
    "You cannot commit or open pull requests. Tell the user to run !pr when they are happy, "
    "or !undo to revert.\n"
    "</WRITE_MODE>\n"
)


def execute(name: str, args: dict, project_dir: str, conv_id: str) -> Optional[str]:
    """
    Dispatches a write tool. Returns None if `name` is not a write tool, so the
    orchestrator can fall through to its own handlers.
    """
    if name not in WRITE_TOOL_NAMES:
        return None

    if not is_write_enabled(conv_id):
        return "Error: Write mode is off. The user must enable it with !write before files can be changed."
    if not project_dir:
        return "Error: No project is bound to this conversation."

    if name == "orchestrator_write_file":
        return write_file(project_dir, conv_id, args.get("path", ""), args.get("content"))
    if name == "orchestrator_edit_file":
        return edit_file(
            project_dir, conv_id,
            args.get("path", ""),
            args.get("old_string", ""),
            args.get("new_string", ""),
            bool(args.get("replace_all", False)),
        )
    if name == "orchestrator_delete_file":
        return delete_file(project_dir, conv_id, args.get("path", ""))
    if name == "orchestrator_list_changes":
        return list_changes(project_dir, conv_id)

    return f"Error: Unknown tool `{name}`"
