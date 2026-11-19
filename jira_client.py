"""
The Jira operations Agent DAVE's story runner needs, and nothing more.

Read a Story and its subtasks in the order the board shows them, move an issue
to a named status, comment, and label. Everything goes through `jira_auth`, so
nothing here knows about tokens.

Issue descriptions arrive as Atlassian Document Format (ADF), a JSON tree.
`adf_to_markdown` flattens it for the build, with one deliberate oddity: code
blocks are indented, never fenced. `mover.extract_snippets` turns every fenced
block in the conversation into a file in the workspace, so a fenced snippet in a
ticket would be written into the repository before the design pass ever read it.
"""
import re
import urllib.parse

import jira_auth

# Fields the runner reads. `parent` lets a subtask key be recognised and refused.
ISSUE_FIELDS = "summary,status,labels,issuetype,description,parent,subtasks"


class JiraError(Exception):
    """A Jira call failed. The message is fit to show the user."""


def _check(status: int, payload, what: str):
    if 200 <= status < 300:
        return payload
    detail = ""
    if isinstance(payload, dict):
        msgs = payload.get("errorMessages") or []
        errs = payload.get("errors") or {}
        detail = "; ".join([*msgs, *(f"{k}: {v}" for k, v in errs.items())])
        detail = detail or payload.get("message") or payload.get("raw") or ""
    raise JiraError(f"{what} failed (HTTP {status}){': ' + detail if detail else ''}")


class JiraClient:
    def __init__(self, auth: jira_auth.JiraAuth | None = None):
        self.auth = auth or jira_auth.JiraAuth.from_env()

    @property
    def site_url(self) -> str:
        return self.auth.site_url.rstrip("/")

    def browse_url(self, key: str) -> str:
        return f"{self.site_url}/browse/{key}"

    def _req(self, method: str, path: str, body: dict | None = None, what: str = ""):
        try:
            status, payload = self.auth.request(method, path, body)
        except jira_auth.JiraAuthError as e:
            raise JiraError(str(e)) from e
        return _check(status, payload, what or f"{method} {path}")

    # -- reading ------------------------------------------------------------

    def issue(self, key: str) -> dict:
        data = self._req("GET", f"/rest/api/3/issue/{urllib.parse.quote(key)}"
                                f"?fields={ISSUE_FIELDS}", what=f"Reading {key}")
        return simplify(data)

    def children(self, key: str) -> list:
        """The issue's subtasks, in board (rank) order."""
        jql = urllib.parse.quote(f"parent = {key} ORDER BY rank ASC")
        issues, token = [], ""
        while True:
            page = self._req("GET", f"/rest/api/3/search/jql?jql={jql}&maxResults=100"
                                    f"&fields={ISSUE_FIELDS}"
                                    + (f"&nextPageToken={urllib.parse.quote(token)}" if token else ""),
                             what=f"Listing the subtasks of {key}")
            issues += [simplify(i) for i in page.get("issues", [])]
            token = page.get("nextPageToken") or ""
            if not token or page.get("isLast", True):
                break
        return [i for i in issues if i["is_subtask"]]

    # -- writing ------------------------------------------------------------

    def transition(self, key: str, status_name: str) -> bool:
        """Move `key` to the status called `status_name`. False if already there."""
        current = self.issue(key)["status"]
        if current.lower() == status_name.lower():
            return False
        data = self._req("GET", f"/rest/api/3/issue/{key}/transitions",
                         what=f"Reading {key}'s transitions")
        for t in data.get("transitions", []):
            if (t.get("to") or {}).get("name", "").lower() == status_name.lower():
                self._req("POST", f"/rest/api/3/issue/{key}/transitions",
                          {"transition": {"id": t["id"]}},
                          what=f"Moving {key} to {status_name}")
                return True
        names = ", ".join(sorted({(t.get("to") or {}).get("name", "?")
                                  for t in data.get("transitions", [])}))
        raise JiraError(f"{key} cannot move from {current} to {status_name} "
                        f"(it can move to: {names or 'nothing'}).")

    def comment(self, key: str, text: str) -> None:
        self._req("POST", f"/rest/api/3/issue/{key}/comment", {"body": text_to_adf(text)},
                  what=f"Commenting on {key}")

    def add_label(self, key: str, label: str) -> None:
        self._req("PUT", f"/rest/api/3/issue/{key}", {"update": {"labels": [{"add": label}]}},
                  what=f"Labelling {key}")

    def remove_label(self, key: str, label: str) -> None:
        self._req("PUT", f"/rest/api/3/issue/{key}", {"update": {"labels": [{"remove": label}]}},
                  what=f"Unlabelling {key}")


def simplify(raw: dict) -> dict:
    """The parts of a REST issue the runner uses, flat."""
    f = raw.get("fields") or {}
    itype = f.get("issuetype") or {}
    status = f.get("status") or {}
    return {
        "key": raw.get("key", ""),
        "summary": f.get("summary") or "",
        "status": status.get("name") or "",
        "status_category": (status.get("statusCategory") or {}).get("key") or "",
        "labels": list(f.get("labels") or []),
        "type": itype.get("name") or "",
        "is_subtask": bool(itype.get("subtask")),
        "hierarchy": itype.get("hierarchyLevel", 0),
        "parent": (f.get("parent") or {}).get("key") or "",
        "description": adf_to_markdown(f.get("description")),
        "subtask_keys": [s.get("key") for s in (f.get("subtasks") or [])],
    }


# --------------------------------------------------------------------------- #
# ADF -> Markdown
# --------------------------------------------------------------------------- #

def _defence(text: str) -> str:
    """No triple backticks survive: mover would read them as a file to write."""
    return text.replace("```", "'''")


def _inline(nodes: list) -> str:
    out = []
    for n in nodes or []:
        kind = n.get("type")
        if kind == "text":
            text = n.get("text", "")
            for mark in n.get("marks") or []:
                m = mark.get("type")
                if m == "code":
                    text = f"`{text}`"
                elif m == "strong":
                    text = f"**{text}**"
                elif m == "em":
                    text = f"*{text}*"
                elif m == "strike":
                    text = f"~~{text}~~"
                elif m == "link":
                    href = (mark.get("attrs") or {}).get("href", "")
                    if href and href != text:
                        text = f"[{text}]({href})"
            out.append(text)
        elif kind == "hardBreak":
            out.append("\n")
        elif kind == "mention":
            out.append((n.get("attrs") or {}).get("text", "@someone"))
        elif kind == "emoji":
            out.append((n.get("attrs") or {}).get("text") or (n.get("attrs") or {}).get("shortName", ""))
        elif kind in ("inlineCard", "blockCard", "embedCard"):
            out.append((n.get("attrs") or {}).get("url", ""))
        elif kind == "status":
            out.append(f"[{(n.get('attrs') or {}).get('text', '')}]")
        elif kind == "date":
            out.append(str((n.get("attrs") or {}).get("timestamp", "")))
        elif n.get("content"):
            out.append(_inline(n["content"]))
    return "".join(out)


def _blocks(nodes: list, depth: int = 0) -> list:
    """Markdown chunks, one per block, for joining with blank lines."""
    chunks = []
    for n in nodes or []:
        kind = n.get("type")
        content = n.get("content") or []
        if kind == "paragraph":
            text = _inline(content)
            if text.strip():
                chunks.append(text)
        elif kind == "heading":
            level = min(6, max(1, int((n.get("attrs") or {}).get("level", 2))))
            chunks.append("#" * level + " " + _inline(content))
        elif kind in ("bulletList", "orderedList", "taskList"):
            lines = []
            for i, item in enumerate(content, 1):
                if kind == "orderedList":
                    bullet = f"{i}."
                elif kind == "taskList":
                    done = (item.get("attrs") or {}).get("state") == "DONE"
                    bullet = "- [x]" if done else "- [ ]"
                else:
                    bullet = "-"
                inner = (_blocks(item.get("content") or [], depth + 1)
                         if item.get("type") != "taskItem"
                         else [_inline(item.get("content") or [])])
                first, *rest = inner or [""]
                pad = "  " * depth
                lines.append(f"{pad}{bullet} " + first.replace("\n", "\n" + pad + "  "))
                lines += [r if r.lstrip().startswith(("-", "1.", "- [")) else pad + "  " + r
                          for r in rest]
            chunks.append("\n".join(lines))
        elif kind == "codeBlock":
            code = "".join(c.get("text", "") for c in content)
            lang = (n.get("attrs") or {}).get("language") or ""
            label = f"Code ({lang}):" if lang else "Code:"
            chunks.append(label + "\n\n" + "\n".join("    " + ln for ln in code.splitlines()))
        elif kind == "blockquote":
            inner = "\n\n".join(_blocks(content, depth))
            chunks.append("\n".join("> " + ln for ln in inner.splitlines()))
        elif kind == "panel":
            ptype = (n.get("attrs") or {}).get("panelType", "info")
            inner = "\n\n".join(_blocks(content, depth))
            chunks.append(f"**{ptype.capitalize()}:** " + inner)
        elif kind == "rule":
            chunks.append("---")
        elif kind == "table":
            rows = []
            for row in content:
                cells = [" ".join(_blocks(c.get("content") or [], depth)).replace("\n", " ").replace("|", "\\|")
                         for c in row.get("content") or []]
                rows.append("| " + " | ".join(cells) + " |")
            if rows:
                width = rows[0].count(" | ") + 1
                rows.insert(1, "|" + " --- |" * width)
            chunks.append("\n".join(rows))
        elif kind in ("mediaSingle", "mediaGroup", "media"):
            chunks.append("(attachment not included)")
        elif kind in ("expand", "nestedExpand"):
            title = (n.get("attrs") or {}).get("title") or ""
            inner = "\n\n".join(_blocks(content, depth))
            chunks.append((f"**{title}**\n\n" if title else "") + inner)
        elif content:
            chunks += _blocks(content, depth)
    return chunks


def adf_to_markdown(doc) -> str:
    if not doc:
        return ""
    if isinstance(doc, str):  # API v2, or a plain-text field
        return _defence(doc.strip())
    return _defence("\n\n".join(_blocks(doc.get("content") or [])).strip())


# --------------------------------------------------------------------------- #
# Plain text -> ADF (for comments)
# --------------------------------------------------------------------------- #

_URL_RE = re.compile(r"https?://[^\s)>\]]+")


def _text_nodes(line: str) -> list:
    nodes, pos = [], 0
    for m in _URL_RE.finditer(line):
        if m.start() > pos:
            nodes.append({"type": "text", "text": line[pos:m.start()]})
        url = m.group(0).rstrip(".,;:")
        nodes.append({"type": "text", "text": url, "marks": [{"type": "link", "attrs": {"href": url}}]})
        pos = m.start() + len(url)
    if pos < len(line):
        nodes.append({"type": "text", "text": line[pos:]})
    return nodes


def text_to_adf(text: str) -> dict:
    """Paragraphs on blank lines, line breaks kept, URLs made links."""
    content = []
    for para in re.split(r"\n\s*\n", (text or "").strip()):
        inline = []
        for i, line in enumerate(para.splitlines()):
            if i:
                inline.append({"type": "hardBreak"})
            inline += _text_nodes(line)
        if inline:
            content.append({"type": "paragraph", "content": inline})
    return {"type": "doc", "version": 1,
            "content": content or [{"type": "paragraph", "content": []}]}
