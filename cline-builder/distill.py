#!/usr/bin/env python3
"""
Multi-Pass Context Distillation Engine

Reads a conversation JSON file, runs it through 4 expert LLM passes
(Architect → Engineer → Test Engineer → Safety Inspector), and writes a combined
.clinerules file for the Cline CLI agent.

Handles conversation chunking when content exceeds the context window.
Manages Ollama model loading/unloading between passes for VRAM safety.
Isolates context between passes using Markdown boundaries.
"""

import json
import os
import re
import time
import httpx
import threading

# --- Configuration ---
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://host.docker.internal:11434")
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://host.docker.internal:8000")
CONFIG_PATH = os.environ.get("AGENT_CONFIG_PATH", "/app/agent_config.json")
CONVERSATION_PATH = os.environ.get("CONVERSATION_FILE", "/workspace/.cline_context/conversation.json")
OUTPUT_PATH = os.environ.get("CLINERULES_PATH", "/workspace/.clinerules")
STATUS_PATH = os.environ.get("DISTILL_STATUS_PATH", "/workspace/.cline_context/distill_status")
PROJECT_NAME = os.environ.get("PROJECT_NAME", "unnamed_project")
# Settled against agent_config.json in _resolve_context_window() once the config
# is loaded. The environment wins when set, because the orchestrator injects
# EXPERT_CTX when it launches the build container.
_ENV_CONTEXT_WINDOW = os.environ.get("EXPERT_CTX", "").strip()
CONTEXT_WINDOW = int(_ENV_CONTEXT_WINDOW) if _ENV_CONTEXT_WINDOW else 16384

# Two token estimates on purpose, and the difference between them is the point.
# chunk_text slices characters, where 4 chars/token is a fair prose average.
# Budget accounting measures what a prompt will COST, and the payload is code,
# paths, JSON and tree output that tokenize nearer 3 chars/token - so accounting
# uses the smaller divisor, which rounds every estimation error toward headroom.
CHARS_PER_TOKEN = 4          # slicing
CHARS_PER_TOKEN_DENSE = 3    # accounting; deliberately conservative
CHUNK_OVERLAP_TOKENS = 200

# Chunk size sets the extraction bill: the payload is split into ceil(len/chunk)
# sequential LLM calls, so it is the dominant term in how long a pass takes. 2048
# was sized for CPU ingestion against a 16k window; on a real project it forced
# ~20 calls and made the architect pass run 5-6 minutes. Ingestion is no longer
# the binding constraint, and chunk_limit is still clamped to what the configured
# window can actually hold, so a chunk never outgrows the context.
TARGET_CHUNK_SIZE = 8192

# The tagged blocks the payload is assembled from. Chunking prefers these as split
# points and labels every chunk with the ones it covers, so the extractor knows
# whether it is reading a directory listing, a symbol map or chat history.
PAYLOAD_SECTIONS = (
    "SITUATIONAL_AWARENESS", "PROJECT_HISTORY", "BEST_PRACTICES_KNOWLEDGE_BASE",
    "PROJECT_OVERVIEW", "KNOWN_BUILD_ISSUES", "DIRECTORY_STRUCTURE",
    "SYMBOL_SKELETON", "TOOLCHAIN", "NEW_REQUEST", "FINAL_BUILD_COMMAND",
)
SECTION_OPEN_RE = re.compile(
    r"^[ \t]*<(" + "|".join(PAYLOAD_SECTIONS) + r")>", re.MULTILINE
)

# The request is the pivot every conditional record type hangs off, and it lives
# at the tail of the payload - so without this it reaches only the final chunk.
# ITERATIVE_REBUILD and FRESH_BUILD name it differently.
REQUEST_TAGS = ("NEW_REQUEST", "FINAL_BUILD_COMMAND")

# Output caps. Without these the server generates against the full context window,
# so a chunk-extraction prompt that falls into a repetition loop runs for minutes
# and gets guillotined mid-sentence by the stability budget.
#
# The extraction cap has to scale with the chunk or a larger chunk just loses
# whatever falls past the record limit - trading call count for silent fact loss.
# Both derive from the chunk at the density the original 2048/20/1024 triple was
# tuned to: ~100 input tokens per record, ~50 output tokens to write it.
#
# That density is also what makes the budget solvable in closed form. Output is a
# fixed fraction of the chunk, so the constraint
#     fixed_overhead + chunk + output(chunk) + margin <= window
# is linear in `chunk` and solve_extraction_budget() inverts it directly.
EXTRACTION_TOKENS_PER_RECORD = 100
EXTRACTION_OUTPUT_PER_RECORD = 50
EXTRACTION_OUTPUT_RATIO = EXTRACTION_OUTPUT_PER_RECORD / EXTRACTION_TOKENS_PER_RECORD

# Ceilings, reached when the window is generous. The per-call figures come from
# the solver, which never exceeds these and clamps below them on a tight window.
EXTRACTION_RECORD_CAP = TARGET_CHUNK_SIZE // EXTRACTION_TOKENS_PER_RECORD
EXTRACTION_MAX_TOKENS = EXTRACTION_RECORD_CAP * EXTRACTION_OUTPUT_PER_RECORD
ANSWER_MAX_TOKENS = 8192       # merge / single-pass, where the templated answer lives

# --- Budget model ---
# Every call must satisfy: prompt + output + margin <= CONTEXT_WINDOW, where the
# prompt is MEASURED rather than approximated by a flat reserve. The previous
# RESERVED_TOKENS=2048 stood in for four variable terms (system prompt, framing,
# the per-chunk NEW_REQUEST, and an output cap of up to 8192), so at EXPERT_CTX
# =8192 a call committed ~10.7k tokens to an 8192 window. Ollama does not error
# on that; it truncates the prompt and answers from what is left.

# Absorbs the gap between est_tokens() and the model's real tokenizer, plus the
# chat-template scaffolding the server adds and we never see.
SAFETY_FRACTION = 0.05
SAFETY_FLOOR = 256

# Below this a chunk carries too little surrounding context to extract from, so
# an infeasible budget raises instead of clamping. The old code floored the
# budget at 1000 tokens and carried on - which is how a prompt overflowed with
# no log line and produced a confident, evidence-free document.
MIN_VIABLE_CHUNK = 768

# Merge allocation. The answer is the deliverable so it is reserved first; the
# facts are the compressible term and take the remainder. 0.4 leaves the majority
# of a tight window for evidence while still guaranteeing room for the template.
MERGE_ANSWER_FRACTION = 0.4
ANSWER_FLOOR = 1024            # architect.md's seven capped sections need ~800
MIN_FACTS_TOKENS = 512         # below this there is nothing to synthesise from

# Consolidation ladder. Three independent stops guarantee termination: the round
# cap, the no-progress break, and the deterministic truncation that follows.
MAX_CONSOLIDATION_ROUNDS = 4
MIN_REDUCTION_RATIO = 0.9      # a round must remove >=10% or the ladder stops

# Chunks get a capped steering extract of prior analyses; the merge gets it all.
PRIOR_STEER_MAX_TOKENS = 400

# A server-reported prompt above this fraction of the window means it truncated.
BUDGET_BREACH_FRACTION = 0.95
# Report an under-estimate only when it is material enough to warrant calibration.
BUDGET_DRIFT_FRACTION = 0.15

# Stability Protocol: how long a stream may go with NO new token before we give up.
# This is an idle timer, not a wall-clock deadline - a healthy fast stream is never
# killed for the crime of having a lot to say.
STALL_TIMEOUT = 45.0

# --- Review Gate ---
# Which passes to run this invocation. Empty means the full 4-pass pipeline.
# The review gate sets DISTILL_PASSES=architect to stop after pass 1.
DISTILL_PASSES = os.environ.get("DISTILL_PASSES", "").strip()
# Reuse a previously saved pass result instead of regenerating it. What is on
# disk is authoritative, so a hand-edited architecture survives into the build.
DISTILL_RESUME = os.environ.get("DISTILL_RESUME", "").strip().lower() in ("1", "true", "yes")
INTERMEDIATE_DIR = os.environ.get("DISTILL_INTERMEDIATE_DIR", "/workspace/.cline_context")


class BudgetInfeasible(RuntimeError):
    """
    The configured context window cannot hold a viable call.

    Raised rather than clamped, deliberately. Clamping is what the old
    `if available_tokens < 1000: available_tokens = 1000` did: it turned a
    configuration error into a silently truncated prompt and a document that
    looked finished but was written from partial evidence. A hard failure that
    names the window you need is strictly more useful than a plausible lie.
    """

    def __init__(self, stage: str, window: int, fixed: int, margin: int, required: int):
        self.stage = stage
        self.window = window
        self.fixed = fixed
        self.margin = margin
        self.required = required
        super().__init__(
            f"{stage}: context window {window} cannot hold this call "
            f"(fixed overhead {fixed} + safety margin {margin} leaves no viable room). "
            f"Set EXPERT_CTX, or agent_config.json context_window, to at least {required}."
        )


class ExtractionFailed(RuntimeError):
    """
    One or more LLM calls in a pass failed outright.

    _single_llm_call returns "[ERROR: ...]" as a *string* when it gives up, so
    without this the marker flows into the merge like any other extracted fact.
    The merge then dutifully reports that CONTEXT contains no symbols to design
    against, and the pass ends in "# BLOCKED" - a plausible-looking answer whose
    real cause (an unreachable model) is two layers upstream and invisible.

    Same reasoning as BudgetInfeasible: fail loudly, name the cause.
    """

    def __init__(self, stage: str, failures: list, total: int):
        self.stage = stage
        self.failures = failures          # list of (label, error_text)
        self.total = total
        labels = ", ".join(label for label, _ in failures)
        errors = sorted({err for _, err in failures})
        super().__init__(
            f"{stage}: {len(failures)} of {total} LLM call(s) failed ({labels}). "
            f"Error(s): {'; '.join(errors)}"
        )


def _check_llm_result(result: str, label: str):
    """Return (label, error) if a call gave up, else None."""
    if isinstance(result, str) and result.startswith("[ERROR:"):
        return (label, result.strip()[1:-1].removeprefix("ERROR:").strip())
    return None


def est_tokens(text: str) -> int:
    """
    Over-estimate a string's token cost.

    Used for every prompt-side measurement. Rounds up, and divides by the dense
    figure rather than the prose one, so budget errors always fail toward
    headroom instead of toward a truncated prompt.
    """
    if not text:
        return 0
    return -(-len(text) // CHARS_PER_TOKEN_DENSE)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Cut a string down to an estimated token budget, on a line boundary if one is near."""
    if max_tokens <= 0:
        return ""
    if est_tokens(text) <= max_tokens:
        return text
    limit = max_tokens * CHARS_PER_TOKEN_DENSE
    cut = text[:limit]
    newline = cut.rfind("\n")
    return cut[:newline] if newline > limit // 2 else cut


def safety_margin(window: int) -> int:
    """Headroom held back from every call, never spent."""
    return max(SAFETY_FLOOR, int(window * SAFETY_FRACTION))


def slice_tokens(budget_tokens: int) -> int:
    """
    Convert an accounting-token budget into chunk_text's prose-token unit.

    chunk_text slices characters at CHARS_PER_TOKEN (4); the budget is measured
    with the conservative CHARS_PER_TOKEN_DENSE (3). Without this conversion a
    chunk solved at N tokens gets sliced to N*4 characters and then costs N*4/3
    to send - a 33% overrun that lands straight back inside the window, which is
    precisely the class of error the two divisors exist to prevent.
    """
    return max(1, budget_tokens * CHARS_PER_TOKEN_DENSE // CHARS_PER_TOKEN)


def solve_extraction_budget(window: int, fixed_overhead: int) -> tuple[int, int, int]:
    """
    Solve `fixed + chunk + output(chunk) + margin <= window` for the chunk size.

    Returns (chunk_tokens, record_cap, output_tokens).

    output(chunk) is not a constant: the extractor emits one ~50-token record per
    ~100 input tokens, so output tracks the chunk at EXTRACTION_OUTPUT_RATIO. The
    old code derived the output cap from the TARGET_CHUNK_SIZE constant while
    deriving the input from the window, so a chunk clamped to 6144 still reserved
    output sized for 8192. Both now come from the same solved quantity.

    Substituting output = ratio * chunk makes the constraint linear:

        fixed + chunk * (1 + ratio) <= window - margin
        chunk <= (window - margin - fixed) / (1 + ratio)

    TARGET_CHUNK_SIZE then applies as a ceiling - a latency preference for fewer,
    larger calls - and can never push the call past what the window holds.
    """
    margin = safety_margin(window)
    spare = window - margin - fixed_overhead
    chunk = min(TARGET_CHUNK_SIZE, int(spare / (1 + EXTRACTION_OUTPUT_RATIO)))

    if chunk < MIN_VIABLE_CHUNK:
        required = int(
            fixed_overhead + margin + MIN_VIABLE_CHUNK * (1 + EXTRACTION_OUTPUT_RATIO)
        ) + 1
        raise BudgetInfeasible("extraction", window, fixed_overhead, margin, required)

    record_cap = max(1, chunk // EXTRACTION_TOKENS_PER_RECORD)
    return chunk, record_cap, record_cap * EXTRACTION_OUTPUT_PER_RECORD


def solve_merge_budget(window: int, fixed_overhead: int) -> tuple[int, int]:
    """
    Split what the window leaves between the answer and the facts supporting it.

    Returns (facts_budget_tokens, answer_tokens).

    The answer is reserved first because it is the deliverable, and it has a hard
    floor: architect.md's seven capped sections cannot be written in less than
    ANSWER_FLOOR whatever the window. Facts are the compressible term and take
    the remainder, which is the target the consolidation ladder compresses to.

    By construction fixed + answer + facts + margin == window exactly.
    """
    margin = safety_margin(window)
    remainder = window - margin - fixed_overhead

    if remainder < ANSWER_FLOOR + MIN_FACTS_TOKENS:
        required = fixed_overhead + margin + ANSWER_FLOOR + MIN_FACTS_TOKENS
        raise BudgetInfeasible("merge", window, fixed_overhead, margin, required)

    answer = max(ANSWER_FLOOR, min(ANSWER_MAX_TOKENS, int(remainder * MERGE_ANSWER_FRACTION)))
    return remainder - answer, answer


def _resolve_context_window(config: dict) -> None:
    """
    Settle the three-way disagreement about how big the window actually is.

    Precedence: EXPERT_CTX (injected by the orchestrator when it launches the
    build container) > agent_config.json `context_window` > the module default.
    The config key was read by nobody, so a 131072-token configuration silently
    ran at whatever the environment said - 8192, under docker-compose.
    """
    global CONTEXT_WINDOW
    source = "default"
    if _ENV_CONTEXT_WINDOW:
        source = "EXPERT_CTX"
    elif config.get("context_window"):
        CONTEXT_WINDOW = int(config["context_window"])
        source = "agent_config.json"
    print(f"📐 Context window: {CONTEXT_WINDOW} tokens (source: {source})", flush=True)


def load_config() -> dict:
    """Load the agent configuration file."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_prompt(value: str, pass_key: str) -> str:
    """
    Resolve a configured prompt to its text.

    Prompt bodies live in Markdown files alongside agent_config.json, and the
    config holds a path relative to that config file. An inline prompt string is
    still honoured, so older configs keep working unchanged.
    """
    if not isinstance(value, str) or not value.strip():
        return value

    candidate = value.strip()
    # Anything with a newline or angle bracket is prompt text, not a path.
    if "\n" in candidate or "<" in candidate:
        return value

    path = candidate
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(CONFIG_PATH)), path)

    if not os.path.exists(path):
        print(f"  ⚠ Prompt file for '{pass_key}' not found at {path}; using the configured value as literal text.", flush=True)
        return value

    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            return f.read()
    except Exception as e:
        print(f"  ⚠ Could not read prompt file {path}: {e}", flush=True)
        return value


def load_prompts(config: dict) -> dict:
    """Load every configured prompt, resolving file references to their contents."""
    resolved = {}
    for pass_key, value in config.get("prompts", {}).items():
        resolved[pass_key] = resolve_prompt(value, pass_key)
    return resolved


def load_conversation() -> list:
    """Load the conversation messages from the JSON file."""
    with open(CONVERSATION_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def conversation_to_text(messages: list) -> str:
    """Flatten conversation messages into a readable text block."""
    parts = []
    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        if content.strip():
            parts.append(f"[{role}]\n{content}")
    return "\n\n---\n\n".join(parts)


def _resolve_model_config(model_entry, default_host: str = None) -> dict:
    """
    Resolve a model entry from config into a normalized dict.
    Supports both legacy string format and new object format.
    """
    if default_host is None:
        default_host = OLLAMA_HOST
    if isinstance(model_entry, str):
        return {"model": model_entry, "provider": "ollama", "base_url": default_host}
    return {
        "model": model_entry.get("model", ""),
        "provider": model_entry.get("provider", "ollama"),
        "base_url": model_entry.get("base_url", default_host),
        "args": model_entry.get("args", []),
    }


def extract_request(text: str) -> str:
    """
    Pull the build request out of an assembled payload, or "" if it has none.

    Returns the request body only. An empty result is normal for callers that pass
    something other than a full payload, and the chunk prompt simply omits the
    section rather than asserting a request that isn't there.
    """
    for tag in REQUEST_TAGS:
        match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return ""


def extract_mode(text: str) -> str:
    """
    Pull the build MODE out of an assembled payload, or "" if it has none.

    The architect hangs R7 and R8 off MODE and the engineer gates scaffolding on
    it, but it is stated once in the payload head and no extraction record type
    carries it - so, like the request, the map-reduce has to forward it by hand.
    Empty is normal for callers passing something other than a full payload.
    """
    match = re.search(r"<MODE>(.*?)</MODE>", text, re.DOTALL)
    return match.group(1).strip() if match else ""


# Headings that carry orientation rather than payload. The extractor system
# prompt names these as non-sources, so the set has to be stated once and shared
# rather than restated per call site - restating it per block is how NEW_REQUEST
# came to carry an exclusion and PREVIOUS ANALYSES did not.
ORIENTATION_HEADINGS = ("PREVIOUS ANALYSES", "NEW_REQUEST")


def _context_block(heading: str, body: str, purpose: str) -> str:
    """
    Render one orientation block.

    Every non-payload block goes through here, so a block cannot be added without
    inheriting the contract the extractor prompt states about ORIENTATION_HEADINGS.
    """
    return f"### {heading}\n{purpose}\n\n{body}\n\n---\n\n"


# Architect template sections 2 and 4: the paths in play and the contracts on
# them. Stops at the next heading of any level, so a following "#### ENGINEER
# ANALYSIS" wrapper terminates the capture rather than being swallowed by it.
_STEER_SECTION_RE = re.compile(
    r"^#\s*(?:2\.\s*Directory Structure|4\.\s*Contracts)\b.*?(?=^#{1,6}\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def steering_extract(prior_context: str, max_tokens: int = PRIOR_STEER_MAX_TOKENS) -> str:
    """
    Reduce prior analyses to the part a fact extractor can actually act on.

    The chunk loop's job is verbatim extraction. A previous pass's design prose
    cannot change what a chunk says - only which of its facts are worth emitting -
    so the chunks get the paths and contracts and nothing else, capped. The full
    text still reaches the merge, which is where synthesis happens.

    This also stops the whole architect document being re-sent on all N chunks,
    where it inflated the fixed overhead of every single call.
    """
    if not prior_context:
        return ""
    wanted = [m.strip() for m in _STEER_SECTION_RE.findall(prior_context)]
    return truncate_to_tokens("\n".join(wanted) or prior_context, max_tokens)


def _extraction_system_prompt(record_cap: int) -> str:
    """
    The relaxed extractor prompt, with the record cap the solver actually allowed.

    A cap baked in as a literal would promise more records than the solved output
    budget can hold on a tight window, so the model would be cut off mid-record.
    """
    orientation = " and ".join(f"### {h}" for h in ORIENTATION_HEADINGS)
    return (
        "Fact extractor in a map-reduce pipeline. Records are merged verbatim and consumed by "
        "an Architect, Engineer, Test Engineer and Auditor. Extract only: never summarise, "
        "infer, judge or design.\n"
        "\n"
        "SOURCE BOUNDARY: records come ONLY from the text under ### CURRENT TASK.\n"
        f"{orientation} are orientation - they tell you which facts matter. They are "
        "NEVER a source of records. A record whose payload appears only in an orientation "
        "block is a defect.\n"
        "\n"
        "Output newline-separated records only — no preamble, headings, fences or blank lines. "
        f"Max {record_cap}, in order of appearance, no repeats. Every line:\n"
        "  TYPE | <path>:<line> | <verbatim payload> | <note, max 10 words>\n"
        "Use `-` when no line number. Omit a type entirely if absent — never write 'none'.\n"
        "\n"
        "PAYLOAD SECTIONS IN THIS PART names the blocks this part covers "
        "(PROJECT_HISTORY, DIRECTORY_STRUCTURE, SYMBOL_SKELETON, ...). Use it to pick the "
        "record TYPE — the same string means different things in a symbol map and in chat history.\n"
        "\n"
        "ALWAYS capture:\n"
        "  DEP    declared dependency/runtime/framework + version\n"
        "  CMD    runnable script or command from a manifest\n"
        "  TEST   test path, runner, assertion library, or fixture location\n"
        "  CONFIG env var, feature flag, or config key the code reads\n"
        "Capture when present, listing anything NEW_REQUEST references, calls or imports first. "
        "You see one part of a larger payload, so you cannot trace what NEW_REQUEST reaches "
        "through code you cannot see: when unsure, include it. A later step filters:\n"
        "  PATH   existing source file + one-sentence responsibility\n"
        "  SYM    exported function/class/type/constant, full signature\n"
        "  DATA   persisted or returned field name + declared type\n"
        "  AUTH   authority decision point, or untrusted input entry\n"
        "  RULE   requirement, constraint or invariant stated in the text\n"
        "  GAP    symbol/path/config referenced here but not defined here\n"
        "\n"
        "1. VERBATIM: copy signatures, names, types, versions, commands, paths "
        "character-for-character. Never normalise or correct.\n"
        "2. OBSERVED ONLY: no purpose, quality, intent, risk, or 'appears to'. Not written "
        "in the chunk means it does not exist. No findings or recommendations.\n"
        "3. One fact per record. Never merge two symbols, paths or commands.\n"
        "4. Cut off at the chunk boundary: copy what is present, append ` ~TRUNCATED`.\n"
        "\n"
        "SYM | src/http/routes/search.ts:22 | export async function search(q: string, key: string): Promise<Result[]> | public entrypoint\n"
        "CMD | package.json:8 | npm run test:unit | vitest, unit suite\n"
        "AUTH | src/http/routes/search.ts:19 | req.headers['x-api-key'] | untrusted, keys the lookup\n"
        "GAP | src/http/routes/search.ts:31 | resolveTenant | imported from ../auth, not in chunk\n"
    )


CONSOLIDATION_SYSTEM_PROMPT = (
    "Deduplicating filter for extracted records. Input is newline-separated records:\n"
    "  TYPE | location | payload | note\n"
    "\n"
    "Output the SAME record lines, dropping only:\n"
    "  - exact duplicates\n"
    "  - records whose payload is a strict substring of another record's payload\n"
    "\n"
    "Never rewrite, merge, shorten, reorder, reword or summarise a record. Copy every "
    "surviving line character-for-character. These records feed an Architect that must "
    "quote signatures, versions and paths verbatim, so a paraphrase is a defect.\n"
    "Output records only: no preamble, headings, fences or blank lines."
)


def _section_boundaries(text: str) -> list[tuple[int, str]]:
    """
    Locate the payload's top-level section openings, in order.

    The distillation payload is a sequence of tagged blocks (PROJECT_HISTORY,
    DIRECTORY_STRUCTURE, SYMBOL_SKELETON, NEW_REQUEST, ...). Nothing else in the
    text starts a line with one of these tags, so a line match is a reliable
    boundary without paying for a real parse.
    """
    return [(m.start(), m.group(1)) for m in SECTION_OPEN_RE.finditer(text)]


def _sections_covered(boundaries: list[tuple[int, str]], start: int, end: int) -> str:
    """Name the payload sections a [start, end) slice touches."""
    if not boundaries:
        return "UNLABELLED"

    covered = []
    current = "PREAMBLE"
    for offset, tag in boundaries:
        if offset <= start:
            current = tag
        elif offset < end:
            covered.append(tag)
    covered.insert(0, current)

    seen = []
    for tag in covered:
        if tag not in seen:
            seen.append(tag)
    return " → ".join(seen)


def chunk_text(text: str, max_tokens: int) -> list[tuple[str, str]]:
    """
    Split text into chunks that fit within the token budget.

    Returns (chunk, section_label) pairs. The label matters as much as the split:
    a slice taken blindly out of the middle of the payload is an unlabelled slab
    of text, and the extractor cannot tell a directory listing from a symbol map
    from chat history - which is exactly the distinction its record types encode.

    Section openings are therefore preferred over blank lines as break points, and
    a chunk that starts on a section boundary carries no overlap, so the model
    sees the block from its first line instead of mid-way through the previous one.
    """
    max_chars = max_tokens * CHARS_PER_TOKEN
    overlap_chars = CHUNK_OVERLAP_TOKENS * CHARS_PER_TOKEN
    boundaries = _section_boundaries(text)

    if len(text) <= max_chars:
        return [(text, _sections_covered(boundaries, 0, len(text)))]

    chunks = []
    start = 0
    while start < len(text):
        end = start + max_chars

        if end >= len(text):
            chunks.append((text[start:], _sections_covered(boundaries, start, len(text))))
            break

        floor = start + max_chars // 2
        # A section opening beats any prose break: it keeps whole blocks together
        # and starts the next chunk on a labelled line.
        section_break = max(
            (offset for offset, _ in boundaries if floor <= offset < end),
            default=-1,
        )

        if section_break != -1:
            end = section_break
            next_start = end          # no overlap - the boundary is the context
        else:
            break_point = text.rfind("\n\n", floor, end)
            if break_point == -1:
                break_point = text.rfind("\n", floor, end)
            if break_point == -1:
                break_point = text.rfind(". ", floor, end)
            if break_point != -1:
                end = break_point + 1
            # Rewinding a fixed number of characters lands mid-word, so the next
            # chunk opens on a fragment. Snap forward to the following line start;
            # a partial identifier is worse than slightly less overlap.
            next_start = end - overlap_chars
            newline = text.find("\n", next_start)
            if newline != -1 and newline < end:
                next_start = newline + 1

        chunks.append((text[start:end], _sections_covered(boundaries, start, end)))

        if next_start <= start:
            next_start = start + 1
        start = next_start

    return chunks


def unload_model(client: httpx.Client, model_config: dict):
    """
    Unload a model from VRAM using the appropriate provider method.
    For Ollama: direct API call. For others: calls the orchestrator management API.
    """
    model_name = model_config.get("model", "") if isinstance(model_config, dict) else model_config
    provider = model_config.get("provider", "ollama") if isinstance(model_config, dict) else "ollama"
    base_url = model_config.get("base_url", OLLAMA_HOST) if isinstance(model_config, dict) else OLLAMA_HOST

    try:
        if provider == "ollama":
            client.post(
                f"{base_url}/api/generate",
                json={"model": model_name, "keep_alive": 0},
                timeout=10.0
            )
            print(f"  ↳ Unloaded model (Ollama): {model_name}")
        else:
            # Non-Ollama: call the orchestrator's management API
            client.post(
                f"{ORCHESTRATOR_URL}/internal/model/unload",
                json=model_config if isinstance(model_config, dict) else {"model": model_config, "provider": "ollama"},
                timeout=30.0
            )
            print(f"  ↳ Unloaded model ({provider}): {model_name}")
        time.sleep(1)
    except Exception as e:
        print(f"  ⚠ Failed to unload {model_name}: {e}")


def _intermediate_path(pass_key: str) -> str:
    """Where a single pass's result is written between runs."""
    return os.path.join(INTERMEDIATE_DIR, f"distill_{pass_key}.md")


# Sentinel that marks an intermediate file as an abort report rather than a
# result. load_saved_pass() refuses to reuse anything carrying it.
ABORT_MARKER = "## ❌ ABORTED — the pass could not run"


def _write_pass_failure(pass_key: str, model_config, exc: "ExtractionFailed"):
    """
    Write an abort report to the pass's intermediate path.

    That path is what `!architect` polls in chat, so this is the difference
    between the user seeing the real fault in seconds and waiting out the full
    review-gate timeout for "still running".
    """
    if isinstance(model_config, dict):
        endpoint = model_config.get("base_url", "?")
        model_name = model_config.get("model", "?")
    else:
        endpoint = OLLAMA_HOST
        model_name = str(model_config)

    lines = [
        f"# Distillation Intermediate: {pass_key.title()}",
        "",
        ABORT_MARKER,
        "",
        f"**{exc.stage}** failed: {len(exc.failures)} of {exc.total} call(s) to the model "
        "returned an error, so the extracted context would have been incomplete.",
        "",
        "This is **not** a finding about your codebase. No architecture was produced.",
        "",
        "| | |",
        "|---|---|",
        f"| Model | `{model_name}` |",
        f"| Endpoint | `{endpoint}` |",
        f"| Failed calls | {len(exc.failures)} of {exc.total} |",
        "",
        "### Errors",
        "",
    ]
    for label, err in exc.failures:
        lines.append(f"- **{label}** — `{err}`")
    lines += [
        "",
        "### What to check",
        "",
        f"1. Is a server actually listening at `{endpoint}`?",
        "2. From inside this container, `localhost` is the container — host "
        "services need `host.docker.internal`.",
        "3. Check `llama-server.log` / `orchestrator.log` on the host for a "
        "model that died or was auto-unloaded mid-run.",
        "",
        "Fix the cause, then re-run this pass. Nothing was overwritten.",
    ]

    path = _intermediate_path(pass_key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"  ↳ Wrote abort report to {path}", flush=True)
    except Exception as e:
        print(f"  ⚠ Could not write abort report: {e}", flush=True)


def load_saved_pass(pass_key: str):
    """
    Load a previously saved pass result, or None if there isn't a usable one.

    Strips the header line the writer prepends. The file may have been edited by
    hand between the review gate and the approve run, so its contents are treated
    as authoritative rather than as a cache of what the model said.
    """
    path = _intermediate_path(pass_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"  ⚠ Could not read {path}: {e}", flush=True)
        return None

    if ABORT_MARKER in content:
        # An abort report wears the same header as a real result, so without this
        # a later run would "reuse" a failure notice as if it were an approved
        # architecture - exactly the silent-bad-context problem this guard exists
        # to prevent.
        print(f"  ⚠ {path} holds an abort report, not a result; re-running the pass.",
              flush=True)
        return None

    lines = content.split("\n")
    if lines and lines[0].startswith("# Distillation Intermediate:"):
        content = "\n".join(lines[1:])
    content = content.strip()
    return content or None


def _select_passes(all_passes: list) -> list:
    """
    Narrow the pipeline to the passes named in DISTILL_PASSES.

    Ordering always comes from the canonical list, never from the env var, so a
    caller cannot accidentally run the engineer before the architect.
    """
    if not DISTILL_PASSES:
        return all_passes

    wanted = {p.strip() for p in DISTILL_PASSES.split(",") if p.strip()}
    known = {key for key, _ in all_passes}
    unknown = wanted - known
    if unknown:
        print(f"  ⚠ Ignoring unknown pass name(s): {', '.join(sorted(unknown))}", flush=True)

    selected = [(key, label) for key, label in all_passes if key in wanted]
    if not selected:
        print(f"  ⚠ DISTILL_PASSES='{DISTILL_PASSES}' matched no passes; running the full pipeline.", flush=True)
        return all_passes
    return selected


def _name_matches(name: str, loaded_names) -> bool:
    """
    Match a configured model name against reported residency.

    Ollama reports an untagged model as 'name:latest', so accept that form too.
    Deliberately does NOT strip tags - 'gemma4:26b' must never match 'gemma4:31b'.
    """
    return name in loaded_names or f"{name}:latest" in loaded_names


def get_loaded_models(client: httpx.Client, models: dict) -> dict:
    """
    Report which models are currently holding VRAM, as {name: unload_config}.

    Ollama hosts are queried directly because distill knows its own base_urls.
    llama.cpp residency lives in the orchestrator's managed-process table, which
    it already exposes on /health.
    """
    loaded = {}

    ollama_urls = set()
    for entry in models.values():
        cfg = _resolve_model_config(entry)
        if cfg.get("provider", "ollama") == "ollama":
            ollama_urls.add(cfg.get("base_url", OLLAMA_HOST))
    ollama_urls.add(OLLAMA_HOST)

    for url in ollama_urls:
        try:
            resp = client.get(f"{url}/api/ps", timeout=5.0)
            if resp.status_code == 200:
                for entry in resp.json().get("models", []):
                    name = entry.get("name", "")
                    if name:
                        loaded[name] = {"model": name, "provider": "ollama", "base_url": url}
        except Exception as e:
            print(f"  ⚠ Could not query {url}/api/ps: {e}", flush=True)

    # Non-Ollama residents (managed llama.cpp servers) come from the orchestrator.
    # Describe them using the configured entry so we know how to unload them.
    by_name = {}
    for entry in models.values():
        cfg = _resolve_model_config(entry)
        if cfg.get("model"):
            by_name[cfg["model"]] = cfg
    try:
        resp = client.get(f"{ORCHESTRATOR_URL}/health", timeout=5.0)
        if resp.status_code == 200:
            for name in resp.json().get("loaded_models", []):
                if not name or name in loaded:
                    continue
                if name in by_name:
                    loaded[name] = by_name[name]
                else:
                    print(f"  ⚠ {name} is resident but not in agent_config; cannot unload it safely.", flush=True)
    except Exception as e:
        print(f"  ⚠ Could not query orchestrator /health: {e}", flush=True)

    return loaded


def evict_stale_models(client: httpx.Client, models: dict, keep_config: dict):
    """
    Free the GPU before the FIRST pass, but only where it actually buys something.

    The per-pass swap in the main loop only fires when the model name changes
    between passes, so pass 1 - which has no predecessor - never evicted anything.
    Whatever the orchestrator left resident (typically a llama.cpp server holding
    ~20GB) stayed put, and the first pass got pushed onto CPU or off the GPU
    entirely. That is what starved the architect pass.

    Residency is checked first so we never pay to unload and reload the model the
    first pass is about to use, and never issue unloads for models that are not
    holding VRAM in the first place.
    """
    keep_name = keep_config.get("model", "")
    loaded = get_loaded_models(client, models)

    if not loaded:
        print("  ↳ GPU already clear - nothing to evict.", flush=True)
        return

    print(f"  ↳ Currently resident: {', '.join(sorted(loaded))}", flush=True)

    if _name_matches(keep_name, loaded):
        print(f"  ↳ {keep_name} is already loaded and needed next - keeping it.", flush=True)

    evicted = 0
    for name, cfg in loaded.items():
        if _name_matches(keep_name, {name}):
            continue
        print(f"  ↳ Freeing GPU: unloading {name} ({cfg.get('provider', 'ollama')})", flush=True)
        unload_model(client, cfg)
        evicted += 1

    if not evicted:
        print("  ↳ Nothing to evict - GPU already holds only what pass 1 needs.", flush=True)


def _build_chunk_prompt(steer: str, new_request: str, section_label: str,
                        index: int, total: int, chunk: str) -> str:
    """
    Assemble one extraction prompt.

    Also used with an empty chunk to MEASURE the per-call overhead, so the figure
    the budget solver works from is the real assembled framing rather than a
    constant that drifts every time this text is edited.
    """
    parts = []
    if steer:
        parts.append(_context_block(
            "PREVIOUS ANALYSES", steer,
            "What earlier passes established. Orientation only: it tells you which "
            "facts matter. Never emit a record sourced from it.",
        ))
    if new_request:
        parts.append(_context_block(
            "NEW_REQUEST", new_request,
            "The change being planned. Repeated in every part. Orientation only: "
            "never emit a record sourced from it.",
        ))
    parts.append(
        f"### CURRENT TASK\n"
        f"Extract records from this part of the payload. This is the only source of records.\n"
        f"PAYLOAD SECTIONS IN THIS PART: {section_label}\n\n"
        f"{chunk}\n\n"
        f"---\n"
        f"CHUNK IDENTIFIER: PART {index} OF {total}"
    )
    return "".join(parts)


def _pack_buckets(parts: list, budget_tokens: int) -> list:
    """Group parts into buckets that each fit the per-call budget."""
    buckets, current, used = [], [], 0
    for part in parts:
        part = truncate_to_tokens(part, budget_tokens)
        cost = est_tokens(part)
        if current and used + cost > budget_tokens:
            buckets.append(current)
            current, used = [], 0
        current.append(part)
        used += cost
    if current:
        buckets.append(current)
    return buckets


def _consolidate_round(client, model_config, parts: list, window: int) -> list:
    """
    One deduplication pass over the extracted records.

    The filter is monotonically reducing by construction - its output is a subset
    of its input lines - which is what lets the ladder above it terminate. The
    previous implementation asked for a "concise summary", which paraphrases: it
    could grow, and it destroyed the verbatim property that the Architect's
    Contracts section depends on.
    """
    system_tokens = est_tokens(CONSOLIDATION_SYSTEM_PROMPT)
    # Output cannot exceed input for a filter, so splitting the spare window in
    # half between the two is always safe: input + output <= 2 * input <= spare.
    spare = window - safety_margin(window) - system_tokens - 64
    bucket_budget = max(MIN_FACTS_TOKENS, spare // 2)

    buckets = _pack_buckets(parts, bucket_budget)
    consolidated = []
    for bi, bucket in enumerate(buckets):
        bucket_text = "\n\n".join(bucket)
        result = _single_llm_call(
            client, model_config, CONSOLIDATION_SYSTEM_PROMPT, bucket_text,
            f"Consolidation {bi + 1}/{len(buckets)}",
            max_output_tokens=max(256, min(bucket_budget, est_tokens(bucket_text))),
        )
        consolidated.append(result)
    return consolidated


def _fit_facts_to_budget(client, model_config, parts: list,
                         facts_budget: int, window: int) -> list:
    """
    Compress extracted records until they fit the merge's facts budget.

    Termination is guaranteed three ways: the round cap, the no-progress break,
    and the deterministic truncation that follows. The old code checked a fixed
    60000-char threshold once, before consolidating, and never re-checked - so a
    consolidation that failed to shrink the facts still went to the merge.
    """
    rounds = 0
    while est_tokens("\n\n".join(parts)) > facts_budget and rounds < MAX_CONSOLIDATION_ROUNDS:
        before = est_tokens("\n\n".join(parts))
        parts = _consolidate_round(client, model_config, parts, window)
        after = est_tokens("\n\n".join(parts))
        rounds += 1
        print(f"    ↳ Consolidation round {rounds}: {before} → {after} tokens "
              f"(budget {facts_budget})", flush=True)
        if after > before * MIN_REDUCTION_RATIO:
            print("    ↳ Consolidation is not converging; stopping the ladder.", flush=True)
            break

    if est_tokens("\n\n".join(parts)) <= facts_budget:
        return parts

    # Deterministic tail. Always marked: the architect has to be able to tell
    # "the codebase has no auth layer" from "the auth records fell off the end",
    # because R9 and R10 are exactly the rules for reasoning under missing facts.
    def marker(dropped: int) -> str:
        return (
            f"[TRUNCATED: {dropped} extracted-fact block(s) dropped — the records exceeded "
            f"the {facts_budget}-token merge budget. CONTEXT IS INCOMPLETE: prefer an "
            f"ASSUMED: bullet over asserting a fact you cannot see.]"
        )

    # Reserve what the marker actually costs, sized with the largest count it can
    # carry. A constant here would be the same mistake as the old flat reserve:
    # the marker is ~70 tokens, so a 48-token guess puts the result back over.
    keep_budget = facts_budget - est_tokens(marker(len(parts))) - 2
    kept, used, dropped = [], 0, 0
    for part in parts:
        cost = est_tokens(part)
        if used + cost <= keep_budget:
            kept.append(part)
            used += cost
        else:
            dropped += 1
    if not kept and parts:
        kept = [truncate_to_tokens(parts[0], keep_budget)]
        dropped = len(parts) - 1

    print(f"    ⚠ Facts still over budget after consolidation: dropping {dropped} "
          f"block(s) to fit {facts_budget} tokens.", flush=True)
    kept.append(marker(dropped))
    return kept


def call_llm(client: httpx.Client, model_config, system_prompt: str, user_content: str, prior_context: str = "") -> str:
    """
    Send a synchronous chat completion request to the configured provider.
    Uses generic extraction prompts for chunks to prevent template deadlocks.
    Accepts model_config as either a string (legacy Ollama) or a dict with provider info.

    Every call is sized by the budget solver rather than a flat reserve, so the
    prompt plus its output cannot exceed CONTEXT_WINDOW. An infeasible window
    raises BudgetInfeasible instead of silently overflowing the server.
    """
    # Pivots. Both sit at the tail of the payload and neither survives extraction,
    # so the map-reduce has to forward them by hand - the request to every chunk,
    # both to the merge.
    new_request = extract_request(user_content)
    mode = extract_mode(user_content)

    # A single call is merge-shaped: real system prompt, full prior context, full
    # answer budget. Size it against that, not against the extraction budget - a
    # payload can clear the chunk limit and still not fit here, which is how the
    # old single-call path overflowed on a tight window.
    single_prior = _context_block(
        "PREVIOUS ANALYSES", prior_context,
        "What earlier passes established.",
    ) if prior_context else ""
    single_fixed = est_tokens(system_prompt) + est_tokens(single_prior) + est_tokens("### CURRENT TASK\n")
    single_facts, single_answer = solve_merge_budget(CONTEXT_WINDOW, single_fixed)

    # Chunk overhead is measured from the real assembled framing, with the ceiling
    # record cap (the longest prompt), so the solved chunk can only be conservative.
    steer = steering_extract(prior_context)
    chunk_fixed = (
        est_tokens(_extraction_system_prompt(EXTRACTION_RECORD_CAP))
        + est_tokens(_build_chunk_prompt(steer, new_request, "X" * 64, 99, 99, ""))
    )
    chunk_tokens, record_cap, extraction_tokens = solve_extraction_budget(CONTEXT_WINDOW, chunk_fixed)

    chunks = chunk_text(user_content, slice_tokens(chunk_tokens))

    print(f"  ↳ Input: {len(user_content) + len(prior_context)} chars "
          f"(~{est_tokens(user_content) + est_tokens(prior_context)} tok). "
          f"Ctx {CONTEXT_WINDOW}, margin {safety_margin(CONTEXT_WINDOW)}.", flush=True)

    if len(chunks) == 1 and est_tokens(user_content) <= single_facts:
        full_input = single_prior + f"### CURRENT TASK\n{user_content}"
        print(f"    ↳ Preparing Single-pass ({len(full_input)} chars; "
              f"budget {single_facts} tok in / {single_answer} tok out)...", flush=True)
        result = _single_llm_call(client, model_config, system_prompt, full_input,
                                  max_output_tokens=single_answer)
        failure = _check_llm_result(result, "Single-pass")
        if failure:
            raise ExtractionFailed("Single-pass", [failure], 1)
        return result

    print(f"    ↳ Processing into {len(chunks)} parts "
          f"(chunk {chunk_tokens} tok, cap {record_cap} records, "
          f"out {extraction_tokens} tok, fixed {chunk_fixed} tok)...", flush=True)

    if not new_request:
        print("  ⚠ No NEW_REQUEST/FINAL_BUILD_COMMAND found in the payload; "
              "chunks will be extracted without one.", flush=True)

    # A relaxed, generic system prompt for the chunks so the extractor doesn't
    # deadlock trying to fill out a template it has no data for.
    chunk_system_prompt = _extraction_system_prompt(record_cap)

    partial_results = []
    failures = []
    for i, (chunk, section_label) in enumerate(chunks):
        part_label = f"Part {i + 1}/{len(chunks)}"
        chunk_prompt = _build_chunk_prompt(
            steer, new_request, section_label, i + 1, len(chunks), chunk
        )
        print(f"    ↳ Preparing {part_label} (Payload: {len(chunk_prompt)} chars)...", flush=True)
        result = _single_llm_call(client, model_config, chunk_system_prompt, chunk_prompt,
                                  part_label, max_output_tokens=extraction_tokens)
        failure = _check_llm_result(result, part_label)
        if failure:
            # Stop at the first dead part rather than grinding through the rest.
            # Every remaining part will hit the same unreachable server, and the
            # merge cannot be trusted once any facts are missing.
            failures.append(failure)
            raise ExtractionFailed("Chunk extraction", failures, len(chunks))
        partial_results.append(result)

    print("    ↳ All parts finished. Starting Merge Pass...", flush=True)

    # MODE and NEW_REQUEST have to be re-stated here. Neither survives extraction
    # by design: the chunk loop shows the request to every part but forbids
    # emitting records from it, and MODE is never shown to the extractor at all.
    # So the records describe the codebase and say nothing about what to do with
    # it, and a merge pass given only records has no goal to design against -
    # architect.md R10 then fires and the pass returns "# BLOCKED" instead of a
    # design. Mirrors the framing the chunk loop uses so the two agree.
    merge_head = ""
    if mode:
        merge_head += f"### MODE\n{mode}\n\n---\n\n"
    if new_request:
        merge_head += (
            f"### NEW_REQUEST\n"
            f"The change being planned. The extracted facts below describe the "
            f"codebase it lands in.\n\n"
            f"{new_request}\n\n---\n\n"
        )

    # Prior analyses reach the merge in full. The chunks saw only a capped steering
    # extract and were forbidden from extracting records out of it, so this is the
    # only point at which an earlier pass's design is actually read - and the
    # engineer's whole job is mapping the architect's design onto files.
    if prior_context:
        merge_head += _context_block(
            "PREVIOUS ANALYSES", prior_context,
            "What earlier passes established. Design against it; the extracted "
            "facts below describe the codebase it lands in.",
        )

    pivots = [name for name, value in (("MODE", mode), ("NEW_REQUEST", new_request),
                                       ("PREVIOUS ANALYSES", prior_context)) if value]
    sources = "these details"
    if pivots:
        sources += f" and the {' and '.join(pivots)} above"

    merge_head += (
        "You previously extracted technical details from a larger conversation in parts. "
        "Below are the raw extracted bullet points.\n\n"
        f"Using ONLY {sources}, write your final response. "
        "You MUST strictly adhere to your system prompt instructions and template formatting.\n\n"
    )

    # The merge is where the document is actually written, and it was the one call
    # with no budget check at all: the real system prompt, every extracted record,
    # and an 8192-token answer target, unbounded against the window.
    merge_fixed = (
        est_tokens(system_prompt)
        + est_tokens(merge_head)
        + est_tokens("#### EXTRACTED FACTS (PART 99)\n\n") * len(partial_results)
    )
    facts_budget, answer_tokens = solve_merge_budget(CONTEXT_WINDOW, merge_fixed)
    print(f"    ↳ Merge budget: {facts_budget} tok facts / {answer_tokens} tok answer "
          f"(fixed {merge_fixed} tok)", flush=True)

    partial_results = _fit_facts_to_budget(
        client, model_config, partial_results, facts_budget, CONTEXT_WINDOW
    )

    merge_prompt = merge_head
    for i, part in enumerate(partial_results):
        merge_prompt += f"#### EXTRACTED FACTS (PART {i + 1})\n{part}\n\n"

    # Use the REAL system prompt here
    merged = _single_llm_call(client, model_config, system_prompt, merge_prompt, "Merging Parts",
                              max_output_tokens=answer_tokens)
    failure = _check_llm_result(merged, "Merge")
    if failure:
        raise ExtractionFailed("Merge pass", [failure], 1)
    return merged


def _extract_delta(line: str, is_ollama: bool):
    """
    Pull (answer_token, reasoning_token, done) out of one streamed line.

    Thinking models split their output across two channels. Ollama returns
    reasoning in message.thinking; llama.cpp returns it in delta.reasoning_content
    (or delta.reasoning on some builds). Reading only the answer channel makes a
    healthy stream look completely dead, which is why a working generation could
    report "Salvaging 0 tokens".

    Returns (None, None, False) for lines that carry no delta at all.
    """
    if is_ollama:
        try:
            chunk_data = json.loads(line)
        except Exception:
            return None, None, False
        message = chunk_data.get("message", {})
        return (
            message.get("content", ""),
            message.get("thinking", ""),
            chunk_data.get("done", False),
        )

    if not line.startswith("data: "):
        # Orchestrator heartbeats and non-data SSE events
        return None, None, False
    if line == "data: [DONE]":
        return None, None, True
    try:
        chunk_data = json.loads(line[6:])
        choices = chunk_data.get("choices", [{}])
        if not choices:
            return "", "", False
        delta = choices[0].get("delta", {})
        return (
            delta.get("content", ""),
            delta.get("reasoning_content", "") or delta.get("reasoning", ""),
            choices[0].get("finish_reason") is not None,
        )
    except Exception:
        # Malformed chunk or internal proxy metadata
        return None, None, False


def _extract_prompt_tokens(line: str, is_ollama: bool):
    """
    Pull the server's own count of how many prompt tokens it ingested, if present.

    Ollama reports prompt_eval_count on the final chunk; llama.cpp reports
    usage.prompt_tokens when stream_options.include_usage is set. This is the only
    ground truth available about whether the budget held - est_tokens is an
    estimate, and a server that truncates does so silently.
    """
    try:
        if is_ollama:
            return json.loads(line).get("prompt_eval_count")
        if line.startswith("data: ") and line != "data: [DONE]":
            return (json.loads(line[6:]).get("usage") or {}).get("prompt_tokens")
    except Exception:
        return None
    return None


def _check_prompt_budget(label: str, server_tokens, estimated: int, max_output: int):
    """
    Compare the estimate against what the server actually ingested.

    Closes the loop the old code left open: a prompt over num_ctx was truncated by
    the server with no error and no log line, and the only symptom was a document
    written from evidence that never arrived.
    """
    if not server_tokens:
        return
    if server_tokens > CONTEXT_WINDOW * BUDGET_BREACH_FRACTION:
        print(f"\n      ⚠ BUDGET BREACH [{label}]: server ingested {server_tokens} prompt tokens "
              f"against num_ctx={CONTEXT_WINDOW} (estimated {estimated}). The prompt was "
              f"truncated — treat this result as unreliable.", flush=True)
    elif estimated and server_tokens > estimated * (1 + BUDGET_DRIFT_FRACTION):
        print(f"\n      ↳ [budget] {label}: estimated {estimated} prompt tokens, server counted "
              f"{server_tokens}. Headroom {CONTEXT_WINDOW - server_tokens - max_output}. "
              f"Consider lowering CHARS_PER_TOKEN_DENSE.", flush=True)


def _salvage_note(answer_tokens: list, reasoning_tokens: list) -> str:
    """Describe what we actually managed to keep, so failures are not silent."""
    if answer_tokens:
        return f"Salvaging {len(answer_tokens)} answer tokens."
    if reasoning_tokens:
        return (
            f"Got {len(reasoning_tokens)} reasoning tokens but ZERO answer tokens - "
            "the server ignored the thinking-disable request. Salvaging nothing."
        )
    return "No tokens of any kind received. Salvaging nothing."


def _single_llm_call(client: httpx.Client, model_config, system_prompt: str, user_content: str,
                     label: str = "Inference", max_output_tokens: int = ANSWER_MAX_TOKENS) -> str:
    """
    Execute a single LLM API call with streaming for live feedback.
    Supports both Ollama native and OpenAI-compatible streaming formats.

    Args:
        model_config: Either a string (model name, Ollama) or a dict with provider info.
        max_output_tokens: Hard cap on generated tokens. Extraction passes want a
            tight cap; the merge pass needs room for the full templated answer.
    """
    # Normalize config
    if isinstance(model_config, str):
        cfg = {"model": model_config, "provider": "ollama", "base_url": OLLAMA_HOST}
    else:
        cfg = model_config

    model_name = cfg.get("model", "")
    provider = cfg.get("provider", "ollama")
    base_url = cfg.get("base_url", OLLAMA_HOST)
    is_ollama = provider == "ollama"

    # Distillation passes want the templated answer, not the reasoning trace.
    # Thinking models spend their entire per-part budget in the reasoning channel
    # before emitting a single answer token, so we turn it off at the source.
    if is_ollama:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "stream": True,
            "think": False,
            "options": {
                "num_ctx": CONTEXT_WINDOW,
                "temperature": 0.3,
                "num_predict": max_output_tokens,
            },
            "keep_alive": "3m"
        }
        url = f"{base_url}/api/chat"
    else:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "stream": True,
            "temperature": 0.3,
            "max_tokens": max_output_tokens,
            # llama.cpp honours template kwargs; ignored harmlessly by servers that don't.
            "chat_template_kwargs": {"enable_thinking": False},
            # Makes the server report its real prompt token count, which is what
            # _check_prompt_budget verifies the estimate against.
            "stream_options": {"include_usage": True},
        }
        url = f"{base_url}/v1/chat/completions"

    # What the budget solver assumed this call would cost. Measured the same way
    # here as there, so a mismatch against the server points at the estimator.
    estimated_prompt_tokens = est_tokens(system_prompt) + est_tokens(user_content)

    max_retries = 3
    for attempt in range(max_retries):
        first_token_received = threading.Event()
        
        def heartbeat():
            start_wait = time.time()
            while not first_token_received.is_set():
                time.sleep(5)
                if not first_token_received.is_set():
                    elapsed = int(time.time() - start_wait)
                    print(f"      ↳ [Waiting for LLM... {elapsed}s]", flush=True)

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()

        try:
            full_response = []
            reasoning_response = []
            print(f"    {label:15} [Generating...]\n    ↳ ", end="", flush=True)
            start_time = time.time()
            
            with httpx.Client() as stream_client:
                # Disable orchestrator scrubbing for distillation passes
                headers = {"X-No-Scrub": "true"}
                # Stability Protocol: the budget is idle time, not total time. Connect
                # fast, then allow STALL_TIMEOUT between tokens; max_output_tokens is
                # what stops a runaway generation, not the clock.
                stream_timeout = httpx.Timeout(STALL_TIMEOUT, connect=15.0)
                try:
                    with stream_client.stream("POST", url, json=payload, headers=headers, timeout=stream_timeout) as resp:
                        if resp.status_code == 503:
                            first_token_received.set()
                            print(f"\n  ⚠ Orchestrator is busy (503). Retrying in 10s... (Attempt {attempt+1}/{max_retries})")
                            time.sleep(10)
                            continue

                        if resp.status_code != 200:
                            first_token_received.set()
                            print("\n  ✗ LLM returned status", resp.status_code)
                            # Read error if possible
                            try:
                                err_body = resp.read().decode()
                                print(f"    Error: {err_body[:200]}")
                            except Exception:
                                pass
                            return f"[ERROR: LLM returned status {resp.status_code}]"
                        
                        dot_count = 0
                        last_progress = time.time()
                        server_prompt_tokens = None
                        for line in resp.iter_lines():
                            # Stability Protocol: only a STALLED stream is a failure.
                            # A stream that is producing tokens is doing its job however
                            # long it takes; max_output_tokens bounds the total.
                            if time.time() - last_progress > STALL_TIMEOUT:
                                # Reported by the ReadTimeout handler below, which sees
                                # transport-level stalls too. Printing here as well is
                                # what produced the duplicated warnings in the logs.
                                first_token_received.set()
                                raise httpx.ReadTimeout("Stream stalled")

                            if not line:
                                continue

                            if not first_token_received.is_set():
                                first_token_received.set()

                            # Cheap substring guard first: the usage figure appears
                            # on one line out of thousands, and parsing every line
                            # twice would double the streaming cost for nothing.
                            if server_prompt_tokens is None and (
                                "prompt_eval_count" in line or '"usage"' in line
                            ):
                                server_prompt_tokens = _extract_prompt_tokens(line, is_ollama)

                            token, reasoning, done = _extract_delta(line, is_ollama)
                            if token is None and reasoning is None:
                                if done:
                                    break
                                continue

                            if reasoning:
                                # Kept out of the result, but tracked so a reasoning-only
                                # stream is visibly distinct from a stalled one.
                                reasoning_response.append(reasoning)
                                last_progress = time.time()
                                dot_count += 1
                                if dot_count % 20 == 0:
                                    print("~", end="", flush=True)

                            if token:
                                full_response.append(token)
                                last_progress = time.time()
                                dot_count += 1
                                if dot_count % 20 == 0:
                                    print(".", end="", flush=True)

                            if done:
                                break
                    
                    elapsed = time.time() - start_time
                    print(f" ✓ ({elapsed:.1f}s)", flush=True)
                    _check_prompt_budget(label, server_prompt_tokens,
                                         estimated_prompt_tokens, max_output_tokens)
                    return "".join(full_response)

                except httpx.ReadTimeout:
                    first_token_received.set()
                    salvaged = "".join(full_response)
                    if salvaged:
                        # The prompt and sampler settings are unchanged, so a retry
                        # reproduces the same stall and throws away this partial on
                        # the way. Keep what the model actually produced.
                        print(f"\n      ✗ [Stability Protocol] Stream stalled ({STALL_TIMEOUT:.0f}s idle). "
                              f"{_salvage_note(full_response, reasoning_response)} Not retrying - identical prompt.")
                        return salvaged
                    if attempt < max_retries - 1:
                        print(f"\n      ⚠ [Stability Protocol] Stalled with no output. Retrying part ({attempt+2}/{max_retries})...")
                        continue
                    print(f"\n      ✗ [Stability Protocol] Stalled on FINAL ATTEMPT. {_salvage_note(full_response, reasoning_response)}")
                    return "[ERROR: ReadTimeout]"

        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
            first_token_received.set()
            # If we get a 'Server disconnected' or 'Remote protocol error', it's often transient
            is_reset = "RemoteProtocolError" in str(type(e)) or "disconnected" in str(e).lower()
            
            if attempt < max_retries - 1:
                wait_time = 10 if is_reset else 5
                print(f"\n  ⚠ Network/Protocol error: {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            print(f"\n  ❌ LLM request failed after {max_retries} attempts: {e}")
            return f"[ERROR: {e}]"
        except Exception as e:
            first_token_received.set()
            print(f"\n  ❌ Unexpected error: {e}")
            return f"[ERROR: {e}]"

    return "[ERROR: Max retries exceeded]"


def update_status(status: str):
    """Write current status to a file in the workspace."""
    try:
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            f.write(status)
    except Exception as e:
        print(f"  ⚠ Failed to update status file: {e}")


# --- Stop words for KB keyword matching ---
STOP_WORDS = {"the","a","an","is","it","to","and","or","of","in","on","for",
              "with","this","that","from","be","as","at","by","we","do","if",
              "not","but","so","make","sure","lets","fix","then","can","will",
              "should","must","have","has","been","was","are","also","any",
              "all","just","get","set","use","new","add","now","our","its"}


def select_relevant_kb(kb_dir: str, instruction: str, max_chars: int = 100000) -> str:
    """Score and select only relevant KB files based on keyword matching."""
    import glob
    import re

    # 1. Extract keywords from instruction (3+ chars, not stop words)
    raw_words = re.findall(r'[a-zA-Z0-9_]+', instruction.lower())
    keywords = {w for w in raw_words if len(w) >= 3 and w not in STOP_WORDS}

    if not keywords:
        print("  📖 KB: No meaningful keywords found in instruction. Skipping KB.", flush=True)
        return ""

    print(f"  📖 KB Keywords: {', '.join(sorted(keywords)[:15])}", flush=True)

    # 2. Score each file
    scored_files = []
    for md_file in glob.glob(f"{kb_dir}/**/*.md", recursive=True):
        score = 0
        basename = os.path.basename(md_file).lower()

        # Filename match = high relevance
        for kw in keywords:
            if kw in basename:
                score += 10

        # Content peek match (first 500 chars only)
        try:
            with open(md_file, "r", encoding="utf-8") as f:
                peek = f.read(500).lower()
            for kw in keywords:
                if kw in peek:
                    score += 5
        except Exception:
            continue

        scored_files.append((score, md_file))

    # 3. Sort by score (highest first)
    scored_files.sort(key=lambda x: -x[0])

    # 4. Inject full content for matches, filenames-only for the rest
    selected_content = []
    collateral_names = []
    total_chars = 0

    for score, path in scored_files:
        if score > 0 and total_chars < max_chars:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                if total_chars + len(content) <= max_chars:
                    selected_content.append(f"### KB: {os.path.basename(path)}\n{content}")
                    total_chars += len(content)
                else:
                    remaining = max_chars - total_chars
                    selected_content.append(
                        f"### KB: {os.path.basename(path)} [TRUNCATED]\n{content[:remaining]}"
                    )
                    total_chars = max_chars
            except Exception:
                continue
        else:
            collateral_names.append(os.path.basename(path))

    result = "\n\n".join(selected_content)
    if collateral_names:
        result += "\n\n### Other KB Files (not loaded - request if needed):\n"
        result += ", ".join(collateral_names[:50])

    matched = len(selected_content)
    total = len(scored_files)
    print(f"  📖 KB Selection: {matched}/{total} files matched, {total_chars} chars injected (cap: {max_chars})", flush=True)
    return result


def detect_project_toolchain(project_dir: str) -> str:
    """Detect language, package manager, test runner, and formatter from project markers."""
    markers = {
        "pyproject.toml":   {"lang": "Python", "pkg": "poetry/pip", "fmt": "black/ruff", "test": "pytest"},
        "setup.py":         {"lang": "Python", "pkg": "pip", "fmt": "black", "test": "pytest"},
        "requirements.txt": {"lang": "Python", "pkg": "pip", "fmt": "black", "test": "pytest"},
        "Pipfile":          {"lang": "Python", "pkg": "pipenv", "fmt": "black", "test": "pytest"},
        "package.json":     {"lang": "JavaScript/TypeScript", "pkg": "npm/yarn", "fmt": "prettier", "test": "jest/vitest"},
        "tsconfig.json":    {"lang": "TypeScript", "pkg": "npm", "fmt": "prettier", "test": "jest"},
        "Cargo.toml":       {"lang": "Rust", "pkg": "cargo", "fmt": "rustfmt", "test": "cargo test"},
        "go.mod":           {"lang": "Go", "pkg": "go mod", "fmt": "gofmt", "test": "go test"},
        "CMakeLists.txt":   {"lang": "C/C++", "pkg": "cmake", "fmt": "clang-format", "test": "ctest/make test"},
        "Makefile":         {"lang": "C/C++", "pkg": "make", "fmt": "clang-format", "test": "make test"},
        "pom.xml":          {"lang": "Java", "pkg": "maven", "fmt": "google-java-format", "test": "mvn test"},
        "build.gradle":     {"lang": "Java/Kotlin", "pkg": "gradle", "fmt": "spotless", "test": "gradle test"},
    }

    detected = []
    seen_langs = set()
    for marker, info in markers.items():
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in ["node_modules", ".git", "venv", ".venv", "__pycache__"]]
            if marker in files and info["lang"] not in seen_langs:
                detected.append(info)
                seen_langs.add(info["lang"])
                break

    if not detected:
        return ""

    result = "<TOOLCHAIN>\n"
    for d in detected:
        result += f"  Language: {d['lang']}\n"
        result += f"  Package Manager: {d['pkg']}\n"
        result += f"  Formatter: {d['fmt']}\n"
        result += f"  Test Runner: {d['test']}\n"
        if len(detected) > 1:
            result += "  ---\n"
    result += "</TOOLCHAIN>"
    print(f"  🔧 Toolchain: {', '.join(d['lang'] for d in detected)}", flush=True)
    return result


# Declaration modifiers that may sit between the line start and the keyword.
# The original pattern allowed only whitespace, so every `export function X` and
# `export interface X` in a TypeScript codebase was invisible - which is how the
# architect ended up reporting "Missing CONTEXT" for symbols that were right
# there in the tree it had been given.
_SYM_MODIFIERS = (
    r"(?P<mods>(?:export\s+default\s+|export\s+|declare\s+|public\s+|private\s+"
    r"|protected\s+|static\s+|abstract\s+|async\s+|pub\s+)*)"
)
_SYM_KEYWORDS = r"(?:class|def|function|interface|type|enum|struct|trait|impl|fn|func)"

# `class Foo`, `export interface Bar`, `pub fn baz`, `export type Qux = ...`
SIGNATURE_RE = re.compile(
    rf"^\s*{_SYM_MODIFIERS}{_SYM_KEYWORDS}\s+(?P<name>[A-Za-z0-9_]+)",
    re.MULTILINE,
)
# `export const Foo = () => ...` / `const bar = async function ...`. Modern TS and
# React declare a large share of their public surface this way, so a skeleton that
# only understands the `function` keyword misses most components and hooks.
ARROW_RE = re.compile(
    rf"^\s*{_SYM_MODIFIERS}(?:const|let|var)\s+(?P<name>[A-Za-z0-9_]+)\s*"
    rf"(?::[^=\n]+)?=\s*(?:async\s+)?"
    rf"(?:function\b|\([^)]*\)[^=\n]*=>|[A-Za-z0-9_]+\s*=>)",
    re.MULTILINE,
)
IMPORT_RE = re.compile(
    r"^\s*(?:import\s+.+|from\s+\S+\s+import\s+.+|#include\s+.+|require\(.+\))",
    re.MULTILINE,
)

# ~7.5k tokens at the dense rate. The old 15000 was set against an 8k window; at
# 64k it is affordable to give the architect a map it can actually navigate.
MAX_SKELETON_CHARS = 30000
SKELETON_SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "__pycache__",
                      "dist", "build", "public", ".knowledge_base",
                      ".cline_context", ".cline_logs"}
SKELETON_EXTS = (".py", ".ts", ".js", ".tsx", ".jsx", ".go", ".rs", ".java",
                 ".c", ".cpp", ".h")


def _scan_symbols(content: str):
    """Split a file's declarations into (exported, internal), preserving order."""
    exported, internal = [], []
    seen = set()
    for pattern in (SIGNATURE_RE, ARROW_RE):
        for m in pattern.finditer(content):
            name = m.group("name")
            if name in seen:
                continue
            seen.add(name)
            mods = m.group("mods") or ""
            (exported if ("export" in mods or "pub" in mods) else internal).append(name)
    return exported, internal


def _skeleton_block(rel_path: str, line_count: int, imports: list,
                    exported: list, internal: list, full: bool) -> str:
    """
    Render one file's entry.

    `full` includes imports and internal helpers; the slim form keeps only the
    exported surface, which is what a design pass actually needs to reference.
    """
    block = [f"\n{rel_path} ({line_count} lines)"]
    if full and imports:
        block.append("  imports:")
        for imp in imports[:5]:
            block.append(f"    {imp.strip()}")
        if len(imports) > 5:
            block.append(f"    ... +{len(imports) - 5} more")
    if exported:
        block.append("  exports:")
        block.extend(f"    - {s}" for s in exported)
    if full and internal:
        block.append("  internal:")
        block.extend(f"    - {s}" for s in internal)
    return "\n".join(block)


def get_symbol_skeleton(project_dir: str) -> str:
    """
    Build a navigable map of the project's declarations.

    Two-tier under the size cap: the full map (imports + exported + internal) if
    it fits, otherwise exports only. Truncating mid-walk - as this used to - drops
    whole files off the end of the directory walk, so the architect silently never
    learns that, say, engagement-card.tsx exists. Shedding detail before shedding
    files keeps every file represented.
    """
    files_data = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in SKELETON_SKIP_DIRS]
        for file in sorted(files):
            if not file.endswith(SKELETON_EXTS):
                continue
            rel_path = os.path.relpath(os.path.join(root, file), project_dir)
            try:
                with open(os.path.join(root, file), "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue
            imports = IMPORT_RE.findall(content)
            exported, internal = _scan_symbols(content)
            if imports or exported or internal:
                files_data.append((rel_path, content.count("\n") + 1,
                                   imports, exported, internal))

    for full in (True, False):
        blocks = [_skeleton_block(*fd, full=full) for fd in files_data]
        total = sum(len(b) for b in blocks)
        if total <= MAX_SKELETON_CHARS:
            header = "[PROJECT SYMBOL SKELETON]"
            if not full:
                header += "\n(exported symbols only - imports and internal helpers omitted for size)"
            return "\n".join([header] + blocks)

    # Even exports-only overflows: keep as many whole files as fit, and say how
    # many were dropped rather than trailing off mid-walk.
    skeleton, total, kept = ["[PROJECT SYMBOL SKELETON]"], 0, 0
    for block in [_skeleton_block(*fd, full=False) for fd in files_data]:
        if total + len(block) > MAX_SKELETON_CHARS:
            break
        skeleton.append(block)
        total += len(block)
        kept += 1
    skeleton.append(f"\n... [Skeleton truncated: {kept} of {len(files_data)} files shown]")
    return "\n".join(skeleton)


def run_distillation():
    """Execute the 4-pass distillation pipeline."""
    print("=" * 60, flush=True)
    print("🧠 Multi-Pass Context Distillation Engine", flush=True)
    print("=" * 60, flush=True)

    config = load_config()
    _resolve_context_window(config)
    models = config.get("models", {})
    prompts = load_prompts(config)
    messages = load_conversation()

    def read_workspace_file(rel_path: str) -> str:
        """Helper to read a file from the workspace if it exists."""
        full_path = os.path.join("/workspace", rel_path)
        if os.path.exists(full_path):
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                pass
        return ""
    
    has_git = os.path.exists("/workspace/.git")
    has_code = any(f for f in os.listdir("/workspace") if f not in [".cline_context", ".cline_logs", ".knowledge_base", "conversation.json"])
    is_rebuild = os.path.exists(OUTPUT_PATH) or has_git or has_code
    
    if is_rebuild:
        status_text = "ALREADY PARTIALLY IMPLEMENTED" if not has_git else "EXISTING REPOSITORY DETECTED"
        print(f"\n🔄 {status_text} for {PROJECT_NAME}: Using structured context and latest instruction.", flush=True)
        import subprocess
        try:
            tree_output = subprocess.check_output(
                ["tree", "/workspace", "-I", "node_modules|.git|venv|.venv|.cline_context|.cline_logs|__pycache__|dist|build|public|.knowledge_base"], 
                text=True, stderr=subprocess.DEVNULL
            )
        except Exception:
            tree_output = "(Could not generate directory tree)"
            
        symbol_skeleton = get_symbol_skeleton("/workspace")
        toolchain_info = detect_project_toolchain("/workspace")
        
        latest_instruction = ""
        user_directives = ""
        for msg in reversed(messages):
            if msg.get("role") == "user" and "!build" in msg.get("content", "").lower():
                content = msg.get("content", "")
                latest_instruction = content
                # Extract directives: remove !build and flags
                import re
                directives = re.sub(r'!build|--repo\s+\S+|--kb\s+\S+', '', content, flags=re.IGNORECASE).strip()
                if directives:
                    user_directives = f"\n  <USER_DIRECTIVES>\n{directives}\n  </USER_DIRECTIVES>\n"
                break
                
        if not latest_instruction and messages:
            latest_instruction = messages[-1].get("content", "")

        readme_content = read_workspace_file("README.md")
        issues_content = read_workspace_file(".cline_context/.build_issues.md")
        
        kb_content = ""
        kb_dir = "/workspace/.knowledge_base"
        if os.path.exists(kb_dir):
            kb_content = select_relevant_kb(kb_dir, latest_instruction)
            
        conversation_text = (
            f"<SITUATIONAL_AWARENESS>\n"
            f"  <MODE>ITERATIVE_REBUILD</MODE>\n"
            f"  <STATUS>This project is ALREADY PARTIALLY IMPLEMENTED. Use the provided DIRECTORY_STRUCTURE and SYMBOL_SKELETON to understand the current state.</STATUS>\n"
            f"  <DIRECTIVES>\n"
            f"    1. [P0] PRESERVATION: Prioritize building on top of existing code. Maintain the current file organization and design idioms. Rework is strictly prohibited.\n"
            f"    2. [P0] CONTINUITY: Read the 'PROJECT_HISTORY' to pick up exactly where the last agent left off.\n"
            f"    3. [P0] NAVIGATION: Use the SYMBOL_SKELETON to map out dependencies before reading files.\n"
            f"    4. [P0] ANALYZE: Carefully examine the 'DIRECTORY_STRUCTURE', 'SYMBOL_SKELETON', and 'PROJECT_OVERVIEW' blocks below before planning any code changes.\n"
            f"    5. [P0] NON-REDUNDANT_PLANNING: DO NOT plan for or recreate files that already exist in the structure unless the 'NEW_REQUEST' explicitly requires a logic change in them.\n"
            f"    6. [P0] FILE_STATUS_AWARENESS: If the 'ARCHITECTURE' section (developed by the architect) mentions a file that is NOT present in the 'DIRECTORY_STRUCTURE', it is a NEW component. You MUST create it.\n"
            f"    7. [P0] CONTEXT_ALIGNMENT: Use the 'PROJECT_HISTORY' to understand the intent and reasoning behind the current request.\n"
            f"    8. [P1] SCOPE_FOCUS: Focus exclusively on fulfilling the 'NEW_REQUEST' and resolving the 'KNOWN_BUILD_ISSUES'.\n"
            f"  </DIRECTIVES>\n"
            f"{user_directives}"
            f"</SITUATIONAL_AWARENESS>\n\n"
            f"<PROJECT_DATA>\n"
            f"  <NAME>{PROJECT_NAME}</NAME>\n"
            f"  <PROJECT_HISTORY>\n"
            f"{conversation_to_text(messages[:-1])}\n"
            f"  </PROJECT_HISTORY>\n\n"
        )
        
        if kb_content:
            conversation_text += f"\n<BEST_PRACTICES_KNOWLEDGE_BASE>\n{kb_content}\n</BEST_PRACTICES_KNOWLEDGE_BASE>\n\n"
        
        if readme_content:
            conversation_text += f"  <PROJECT_OVERVIEW>\n```markdown\n{readme_content}\n```\n  </PROJECT_OVERVIEW>\n\n"
            
        if issues_content:
            conversation_text += f"  <KNOWN_BUILD_ISSUES>\n```markdown\n{issues_content}\n```\n  </KNOWN_BUILD_ISSUES>\n\n"

        conversation_text += (
            f"  <DIRECTORY_STRUCTURE>\n```\n{tree_output}\n```\n  </DIRECTORY_STRUCTURE>\n\n"
            f"  <SYMBOL_SKELETON>\n{symbol_skeleton}\n  </SYMBOL_SKELETON>\n\n"
        )
        if toolchain_info:
            conversation_text += f"  {toolchain_info}\n\n"
        conversation_text += (
            f"  <NEW_REQUEST>\n{latest_instruction}\n  </NEW_REQUEST>\n"
            f"</PROJECT_DATA>"
        )
    else:
        print(f"\n✨ Fresh build detected for {PROJECT_NAME}. Assembling historical context...", flush=True)
        
        # Assemble the historical conversation
        history = []
        final_command = ""
        
        # We assume the last message containing !build is the 'trigger'
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if "!build" in msg.get("content", "").lower():
                final_command = msg.get("content", "")
                history = messages[:i] # Everything before the trigger
                break
        
        if not final_command and messages:
            final_command = messages[-1].get("content", "")
            history = messages[:-1]

        conversation_text = (
            f"<SITUATIONAL_AWARENESS>\n"
            f"  <MODE>FRESH_BUILD</MODE>\n"
            f"  <STATUS>This is a NEW PROJECT START. Establish the foundational structure and implementation plan.</STATUS>\n"
            f"  <DIRECTIVES>\n"
            f"    1. [P0] FOUNDATION: Read the 'PROJECT_HISTORY' to understand the core vision, tech stack, and requirements.\n"
            f"    2. [P0] PRESERVATION: Maintain consistency with any existing patterns established in the project history.\n"
            f"    3. [P0] EXECUTION: Treat the 'FINAL_BUILD_COMMAND' as your immediate tactical mission.\n"
            f"    4. [P1] ALIGNMENT: Ensure your output fulfills both the historical vision and the final instruction based strictly on your assigned system role.\n"
            f"    5. [P1] CLARITY: Ensure the foundational structure is clean and well-documented.\n"
            f"  </DIRECTIVES>\n"
            f"</SITUATIONAL_AWARENESS>\n\n"
            f"<PROJECT_DATA>\n"
            f"  <NAME>{PROJECT_NAME}</NAME>\n"
            f"  <PROJECT_HISTORY>\n"
            f"{conversation_to_text(history)}\n"
            f"  </PROJECT_HISTORY>\n\n"
            f"  <FINAL_BUILD_COMMAND>\n{final_command}\n  </FINAL_BUILD_COMMAND>\n"
            f"</PROJECT_DATA>"
        )
        
    print(f"📄 Context size: {len(conversation_text)} chars", flush=True)

    all_passes = [
        ("architect",     "🏗️  Pass 1/4: System Architect"),
        ("engineer",      "⚙️  Pass 2/4: Engineer"),
        ("test_engineer", "🧪  Pass 3/4: Test Engineer"),
        ("safety",        "🛡️  Pass 4/4: Safety Inspector"),
    ]
    passes = _select_passes(all_passes)
    if len(passes) < len(all_passes):
        print(f"⏸️  Review gate: running only {', '.join(k for k, _ in passes)}", flush=True)
    if DISTILL_RESUME:
        print("♻️  Resume enabled: saved pass results will be reused instead of regenerated.", flush=True)

    results = {}
    previous_model_config = None

    with httpx.Client() as client:
        # Two of the four passes are Ollama models, so the pipeline must be able to
        # evict a resident llama.cpp server mid-run regardless of which model the
        # architect uses. Do it once up front so the first pass starts on a clear GPU.
        # Pick the keeper from the first pass that will actually call an LLM - a
        # resumed pass reads from disk and needs no model at all.
        keep_key = None
        for pass_key, _ in passes:
            if DISTILL_RESUME and load_saved_pass(pass_key):
                continue
            keep_key = pass_key
            break
        if keep_key:
            evict_stale_models(
                client, models,
                _resolve_model_config(models.get(keep_key, models.get("architect")))
            )
        else:
            print("  ↳ Every pass is resuming from disk; no model needed.", flush=True)

        for pass_key, pass_label in passes:
            print(f"\n{pass_label}", flush=True)
            print("-" * 40, flush=True)
            update_status(f"Distilling: {pass_label}")

            # Resume before any model swap or preload, so a reused pass costs nothing.
            if DISTILL_RESUME:
                saved = load_saved_pass(pass_key)
                if saved:
                    print(f"  ♻️  Reusing reviewed result from {_intermediate_path(pass_key)} ({len(saved)} chars)", flush=True)
                    results[pass_key] = saved
                    continue

            model_entry = models.get(pass_key, models.get("architect"))
            model_config = _resolve_model_config(model_entry)
            model_name = model_config.get("model", "")
            prompt = prompts.get(pass_key, "Analyze the following conversation.")

            # Check if we need to switch models (compare by model name)
            prev_name = previous_model_config.get("model", "") if previous_model_config else None
            if prev_name and prev_name != model_name:
                print(f"  ↳ Switching model: {prev_name} → {model_name} ({model_config.get('provider', 'ollama')})")
                unload_model(client, previous_model_config)

            # Pre-load non-Ollama models via orchestrator
            if model_config.get("provider", "ollama") != "ollama":
                max_retries = 2
                for attempt in range(max_retries):
                    try:
                        print(f"  ↳ Pre-loading model ({model_config.get('provider')}): {model_name} (Attempt {attempt+1}/{max_retries})")
                        # Pass ctx_size so orchestrator spawns llama-server with correct -c flag
                        load_payload = {**model_config, "ctx_size": CONTEXT_WINDOW}
                        client.post(
                            f"{ORCHESTRATOR_URL}/internal/model/load",
                            json=load_payload,
                            timeout=120.0
                        )
                        break # Success
                    except Exception as e:
                        if attempt < max_retries - 1:
                            print(f"  ⚠ Pre-load retryable error: {e}. Retrying in 5s...")
                            time.sleep(5)
                        else:
                            print(f"  ❌ Pre-load failed after {max_retries} attempts: {e}")
                            if "101" in str(e) or "Network is unreachable" in str(e):
                                print("    TIP: This usually means the host orchestrator is restarting the model. Check orchestrator.log on host.")

            prior_context = ""
            if results:
                prior_context = "\n\n".join(
                    f"#### {k.upper()} ANALYSIS\n{v}" 
                    for k, v in results.items()
                )

            if pass_key in ["architect", "engineer"]:
                target_content = conversation_text
            else:
                # Passes 3 and 4 only read the previous plans. They do not get chunked!
                target_content = "Review the PREVIOUS ANALYSES provided above based strictly on your system role and required template format. Do not invent new features or write source code."

            try:
                result = call_llm(client, model_config, prompt, target_content, prior_context)
            except BudgetInfeasible as e:
                # A misconfigured window is an operator problem, not something to
                # paper over. Stop here with the arithmetic rather than writing a
                # .clinerules assembled from a truncated pass.
                update_status(f"Aborted: {e}")
                print(f"\n  ❌ {pass_key}: {e}", flush=True)
                print("  ↳ Aborting before .clinerules is written; nothing was overwritten.",
                      flush=True)
                raise SystemExit(2)
            except ExtractionFailed as e:
                # The chat review gate (!architect) polls _intermediate_path for
                # this pass and shows whatever lands there. Writing the diagnostic
                # to that same path turns a silent 680s wait into an immediate,
                # accurate report of why the pass could not run.
                update_status(f"Aborted: {e}")
                _write_pass_failure(pass_key, model_config, e)
                print(f"\n  ❌ {pass_key}: {e}", flush=True)
                print("  ↳ Aborting before .clinerules is written; nothing was overwritten.",
                      flush=True)
                raise SystemExit(2)
            results[pass_key] = result
            previous_model_config = model_config
            print(f"  ✓ Complete ({len(result)} chars)")

            intermediate_path = _intermediate_path(pass_key)
            try:
                # Ensure context directory exists inside workspace in case running raw
                os.makedirs(os.path.dirname(intermediate_path), exist_ok=True)
                with open(intermediate_path, "w", encoding="utf-8") as f:
                    f.write(f"# Distillation Intermediate: {pass_key.title()}\n\n{result}")
                print(f"  ↳ Saved intermediate result to {intermediate_path}")
            except Exception as e:
                print(f"  ⚠ Failed to save intermediate result: {e}")

        # We no longer unload the model at the end of distillation.
        # This keeps it 'warm' for Phase 2 (the Cline Build cycle).
        # if previous_model_config:
        #     unload_model(client, previous_model_config)

    # A partial run (the review gate) must not overwrite .clinerules with an
    # incomplete ruleset - the approve run assembles the real one.
    missing = [key for key, _ in all_passes if key not in results]
    if missing:
        update_status("Awaiting review.")
        print(f"\n⏸️  Partial distillation complete. Skipped: {', '.join(missing)}", flush=True)
        print(f"  ↳ .clinerules NOT written; review {_intermediate_path(passes[0][0])} then approve.", flush=True)
        print("=" * 60, flush=True)
        print("✅ Review gate reached", flush=True)
        print("=" * 60, flush=True)
        return

    print(f"\n📝 Writing {OUTPUT_PATH}", flush=True)
    update_status("Assembling .clinerules...")
    clinerules = assemble_clinerules(results, config, messages)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(clinerules)

    update_status("Distillation complete.")
    print(f"  ✓ Written ({len(clinerules)} chars)", flush=True)
    print("=" * 60, flush=True)
    print("✅ Distillation complete", flush=True)
    print("=" * 60, flush=True)


def assemble_clinerules(results: dict, config: dict, messages: list) -> str:
    """Combine the 4-pass results into a structured .clinerules document."""
    limits = config.get("limits", {})

    # Try to extract the target objective from the messages
    target_obj = "Complete the implementation roadmap as specified."
    for msg in reversed(messages):
        if "!build" in msg.get("content", "").lower():
            import re
            content = msg.get("content", "")
            # Filter out !build and flags
            target_obj = re.sub(r'!build|--repo\s+\S+|--kb\s+\S+', '', content, flags=re.IGNORECASE).strip()
            if not target_obj:
                target_obj = "Process project requirements and implement planned architecture."
            break

    doc = [
        "# Project Build Specification",
        "",
        "## 🎯 Current Target Objective",
        f"> {target_obj}",
        "",
        "> Auto-generated by Multi-Agent Distillation Pipeline",
        f"> Timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "",
        "## ⚠️ CORE DIRECTIVES (High Priority)",
        "The following requirements are non-negotiable and must be prioritized over all other implementation details:",
        "",
    ]

    if "test_engineer" in results:
        doc.append("### 🧪 Critical Test & Quality Gates")
        doc.append(results["test_engineer"])
        doc.append("")

    if "safety" in results:
        doc.append("### 🛡️ Safety & Security Mitigations")
        doc.append(results["safety"])
        doc.append("")

    doc.extend([
        "<operational_constraints>",
        f"- Max project size: {limits.get('max_project_size_mb', 4096)} MB",
        f"- Max build iterations: {limits.get('max_build_iterations', 5)}",
        "- PRESERVATION POLICY: Prioritize building on top of existing code. Maintain the current file structure and design patterns. Unsolicited rework, file-splitting, or structural optimization is strictly forbidden.",
        "- ANTI-LOOP RULE: Never attempt the same bug fix more than twice.",
        "- FOCUS REMINDER: Keep the main goal in mind. Do not get distracted by hypothetical features.",
        "- TASK COMPLETION: Relentlessly work through your checklist. Mark impossible tasks as blocked and move on.",
        "- If a test fails repeatedly, comment it out, add a TODO.",
        "- Finishing the checklist is more important than passing every test.",
        "- Verify each major component after implementation.",
        "- Run all safety checks before declaring the build complete.",
        "- CRITICAL CONTEXT RULE: NEVER search, read, or modify `node_modules/`, `.git/`, `__pycache__/` or `.venv/`.",
        "- PORT MANAGEMENT: If a port is in use, YOU MUST ONLY use `npx kill-port <port>` to free it. NEVER use pkill or kill commands.",
        "- DAEMON EXECUTION (CRITICAL): NEVER run `python3 -m http.server`, `npm start`, or ANY server command directly. It will hang the terminal and break the pipeline. You MUST use background processes: `python3 -m http.server 8000 &` or `nohup npm start &`.",
        "- REASONING: Before executing any terminal command or modifying files, you must write out a brief step-by-step logical analysis of your plan.",
        "- CONTEXT PRESERVATION: Your context window is limited. NEVER read more than 300 lines at once. Use searchFiles to locate specific code before reading.",
        "- EXTERNAL MEMORY: After analyzing any file, append a 3-line summary to '.cline_context/analysis_notes.md'. This is your long-term memory.",
        "- ANTI-AMNESIA: If you feel lost or unsure what you've done, read '.cline_context/.session_state.md' and '.cline_context/analysis_notes.md' BEFORE doing anything else.",
        "- SYMBOL SKELETON FIRST: Your .clinerules contains a Symbol Skeleton with imports and function names. Use this to navigate, not readFile.",
        "- DEBUG-FIRST: When you need to understand how code works, write a small probe script, run it, and read the output. This is faster and more accurate than reading 500 lines of source code.",
        "- MANDATORY TEST GATE: After editing ANY file, run the project's test suite. If tests fail after your edit, fix the regression BEFORE moving to the next task.",
        "</operational_constraints>",
        "",
    ])

    section_map = {
        "architect": ("Architecture & Directory Structure", "🏗️"),
        "engineer": ("Implementation Roadmap", "⚙️"),
    }

    for key, (title, icon) in section_map.items():
        if key in results:
            doc.extend([
                f"## {icon} {title}",
                "",
                results[key],
                "",
            ])

    return "\n".join(doc)


if __name__ == "__main__":
    run_distillation()