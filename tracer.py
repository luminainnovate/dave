"""
Behind-the-scenes transcript of every exchange with the local models.

orchestrator.log records *decisions* ("Executing Tool: orchestrator_read_file(...)"),
which tells you what happened but never what was said. This module records the
conversation itself — the prompt that arrived, the routing verdict, the system
context injected, each agentic hop, the model's reasoning, the tool results fed
back in, and the final answer — so `./trace.sh` gives the same live view of the
loop that `docker logs -f` gave for the Cline container.

Output goes to its own file (trace.log) so the transcript stays readable and
orchestrator.log keeps its terse operational record.

Environment:
    BRAIN_TRACE=0          disable tracing entirely
    BRAIN_TRACE_FULL=1     never clip long blocks (files, system prompts)
    BRAIN_TRACE_MAX=1200   chars kept per block before clipping
    BRAIN_TRACE_BG=0       drop Open WebUI's background pings (titles, tags)
    BRAIN_TRACE_FILE=path  write somewhere other than ./trace.log
    BRAIN_TRACE_ROTATE_MB  rotate to trace.log.1 past this size (default 20)

Every public function is failure-isolated: a bug in here must never take down
an inference request.
"""

import contextvars
import functools
import os
import re
import secrets
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))

ENABLED = os.getenv("BRAIN_TRACE", "1") != "0"
FULL = os.getenv("BRAIN_TRACE_FULL", "0") == "1"
MAX_BLOCK = int(os.getenv("BRAIN_TRACE_MAX", "1200"))
TRACE_BG = os.getenv("BRAIN_TRACE_BG", "1") != "0"
TRACE_FILE = os.getenv("BRAIN_TRACE_FILE", os.path.join(_HERE, "trace.log"))
ROTATE_BYTES = int(float(os.getenv("BRAIN_TRACE_ROTATE_MB", "20")) * 1024 * 1024)

# Per-request state. Each FastAPI handler runs in its own context, so concurrent
# turns (a chat request and a title-generation ping) keep separate ids.
_turn_id: contextvars.ContextVar[str] = contextvars.ContextVar("turn_id", default="----")
_turn_start: contextvars.ContextVar[float] = contextvars.ContextVar("turn_start", default=0.0)
_muted: contextvars.ContextVar[bool] = contextvars.ContextVar("muted", default=False)

_write_lock = threading.Lock()

_THINK_RE = re.compile(r"<think>(.*?)(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)


def _safe(fn):
    """Tracing is never load-bearing: swallow anything it throws."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not ENABLED or _muted.get():
            return None
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None
    return wrapper


# =============================================================================
# FORMATTING
# =============================================================================

def _clip(text: str, limit: int = None) -> str:
    """Keep the head and tail of a long block; the middle is rarely the point."""
    text = "" if text is None else str(text)
    limit = MAX_BLOCK if limit is None else limit
    if FULL or len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    elided = len(text) - limit
    return f"{text[:head]}\n\n… [{elided:,} chars elided] …\n\n{text[-tail:]}"


def _indent(text: str, prefix: str = "    ") -> str:
    lines = str(text).rstrip().split("\n")
    out = []
    blanks = 0
    for line in lines:
        if not line.strip():
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        out.append(prefix + line.rstrip())
    return "\n".join(out)


def _size(text) -> str:
    n = text if isinstance(text, int) else len(str(text or ""))
    return f"{n/1000:.1f}k chars" if n >= 1000 else f"{n} chars"


def _secs(elapsed) -> str:
    if elapsed is None:
        return ""
    if elapsed >= 60:
        return f"{int(elapsed // 60)}m{int(elapsed % 60):02d}s"
    return f"{elapsed:.1f}s"


def split_thinking(content: str) -> tuple[str, str]:
    """Separate <think> reasoning from the answer the user actually sees."""
    content = str(content or "")
    thoughts = "\n".join(m.strip() for m in _THINK_RE.findall(content))
    visible = _THINK_RE.sub("", content).strip()
    return thoughts.strip(), visible


def _preview(text: str, width: int = 100) -> str:
    """One-line summary for headers."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


# =============================================================================
# SINK
# =============================================================================

def _rotate_if_needed():
    try:
        if ROTATE_BYTES > 0 and os.path.getsize(TRACE_FILE) > ROTATE_BYTES:
            os.replace(TRACE_FILE, TRACE_FILE + ".1")
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _emit(line: str):
    stamp = time.strftime("%H:%M:%S")
    tid = _turn_id.get()
    with _write_lock:
        with open(TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {tid} {line}\n")
            f.flush()


def _emit_block(header: str, body: str = ""):
    _emit(header)
    if body:
        stamp = " " * 8
        tid = " " * len(_turn_id.get())
        with _write_lock:
            with open(TRACE_FILE, "a", encoding="utf-8") as f:
                for line in _indent(body).split("\n"):
                    f.write(f"{stamp} {tid} {line}\n")
                f.write("\n")
                f.flush()


# =============================================================================
# TRACE POINTS
# =============================================================================

def start_turn(prompt: str = "", conv_id: str = "", streaming: bool = False,
               native: bool = False, kind: str = "chat", role: str = "user") -> str:
    """Open a new turn. Returns the turn id (also used to correlate later lines)."""
    if not ENABLED:
        return "----"
    is_bg = kind != "chat"
    if is_bg and not TRACE_BG:
        _muted.set(True)
        return "----"
    try:
        _muted.set(False)
        tid = secrets.token_hex(2)
        _turn_id.set(tid)
        _turn_start.set(time.monotonic())
        _rotate_if_needed()

        meta = [kind]
        if conv_id:
            meta.append(f"conv {conv_id}")
        meta.append("stream" if streaming else "one-shot")
        meta.append("native" if native else "openai")

        _emit("")
        _emit("═" * 78)
        _emit(f"TURN · {' · '.join(meta)}")
        _emit("═" * 78)
        if prompt:
            _emit_block(f"▸ {role.upper()}  ({_size(prompt)})", _clip(prompt, MAX_BLOCK))
        return tid
    except Exception:
        return "----"


@_safe
def route(model: str, reason: str, detail: str = ""):
    line = f"▸ ROUTE  → {model}   ({reason})"
    if detail:
        line += f"  [{detail}]"
    _emit(line)


@_safe
def note(text: str):
    _emit(f"▸ {text}")


@_safe
def triage(sent: str, verdict, elapsed: float = None):
    """The Router model's hidden complexity call — invisible in the UI."""
    _emit_block(f"▸ TRIAGE (router) {_secs(elapsed)}", _clip(sent, 400))
    _emit(f"  ↳ verdict: {verdict}")


@_safe
def system_prompt(content: str, source: str = ""):
    label = f"▸ SYSTEM PROMPT ({_size(content)})"
    if source:
        label += f"  from {source}"
    _emit_block(label, _clip(content, MAX_BLOCK))


@_safe
def hop_start(n: int, total: int, messages: list = None, tools: list = None, model: str = ""):
    messages = messages or []
    chars = sum(len(str(m.get("content", "") or "")) for m in messages)
    bits = [f"{len(messages)} msgs", _size(chars)]
    if tools:
        bits.append(f"{len(tools)} tool{'' if len(tools) == 1 else 's'}")
    _emit("")
    _emit(f"── HOP {n}/{total} ──  {' · '.join(bits)}  ·  waiting on {model or 'model'} …")


@_safe
def model_reply(content: str = "", thinking: str = None, tool_calls: list = None,
                elapsed: float = None):
    # Ollama hands back reasoning in a separate `thinking` field for models that
    # support it, and inline <think> tags for those that don't. Handle both.
    inline, content = split_thinking(content)
    thinking = (thinking or "").strip() or inline
    if thinking:
        _emit_block(f"◂ THINKING ({_size(thinking)}, {_secs(elapsed)})", _clip(thinking))
    if content:
        _emit_block(f"◂ MODEL ({_size(content)}, {_secs(elapsed)})", _clip(content))
    if not thinking and not content:
        _emit(f"◂ MODEL (no prose, {len(tool_calls or [])} tool call(s), {_secs(elapsed)})")


@_safe
def tool_call(name: str, args: dict):
    pretty = ", ".join(f"{k}={_preview(v, 60)}" for k, v in (args or {}).items())
    _emit(f"⚙ CALL   {name}({pretty})")


@_safe
def tool_result(name: str, result: str, elapsed: float = None):
    ok = not str(result or "").startswith("Error")
    mark = "↩" if ok else "✗"
    _emit_block(f"{mark} RESULT {name} · {_size(result)} · {_secs(elapsed)}",
                _clip(result, min(MAX_BLOCK, 700)))


@_safe
def final(content: str, elapsed: float = None, label: str = "FINAL",
          thinking: str = None):
    inline, visible = split_thinking(content)
    thinking = (thinking or "").strip() or inline
    if thinking:
        _emit_block(f"◂ THINKING ({_size(thinking)})", _clip(thinking))
    bits = [_size(visible)]
    if elapsed is not None:
        bits.append(f"generated in {_secs(elapsed)}")
    if _turn_start.get():
        bits.append(f"turn took {_secs(time.monotonic() - _turn_start.get())}")
    _emit_block(f"★ {label} · {' · '.join(bits)}", _clip(visible))


@_safe
def stream_done(content: str, thinking: str = "", first_token: float = None,
                total: float = None):
    """Assembled transcript of a streamed reply, recorded once the stream closes."""
    if not content and not thinking:
        return
    if thinking:
        _emit_block(f"◂ THINKING ({_size(thinking)})", _clip(thinking))
    inline, visible = split_thinking(content)
    if inline and not thinking:
        _emit_block(f"◂ THINKING ({_size(inline)})", _clip(inline))
    timing = f"first token {_secs(first_token)} · {_secs(total)}" if first_token else _secs(total)
    _emit_block(f"★ STREAMED REPLY ({_size(visible)} · {timing})", _clip(visible))


@_safe
def error(text: str):
    _emit(f"✗ ERROR  {text}")


def current_turn() -> str:
    """Id of the turn in scope, for handing to code that runs in another context."""
    return "----" if _muted.get() else _turn_id.get()


def bind_turn(tid: str):
    """Re-attach to a turn opened elsewhere (streaming generators, threads)."""
    if not ENABLED:
        return
    try:
        if tid:
            _turn_id.set(tid)
            _muted.set(tid == "----")
    except Exception:
        pass
