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

import collections
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
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
    "SYMBOL_SKELETON", "SYMBOL_INDEX", "CALL_GRAPH", "SURVEYED_SOURCE",
    "TOOLCHAIN", "NEW_REQUEST", "FINAL_BUILD_COMMAND",
)
SECTION_OPEN_RE = re.compile(
    r"^[ \t]*<(" + "|".join(PAYLOAD_SECTIONS) + r")>", re.MULTILINE
)

# The request is the pivot every conditional record type hangs off, and it lives
# at the tail of the payload - so without this it reaches only the final chunk.
# ITERATIVE_REBUILD and NEW_BUILD name it differently.
REQUEST_TAGS = ("NEW_REQUEST", "FINAL_BUILD_COMMAND")

# Assistant turns the orchestrator injects as command acknowledgements. They are
# chat chrome, not conversation, and conversation_to_text drops them.
RECEIPT_MARKERS = ("**Build pipeline triggered.**",)
SUPERSEDED_MARKER = "(superseded - identical to a later response below)"

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

# --- Reasoning effort ---
# Per-pass, set as "reasoning" on a model entry in agent_config.json.
#
# The values are the model template's own, not ours: it resolves
# reasoning_effort to 'xhigh' when unset, aliases 'high' to 'xhigh', and calls
# raise_exception on anything outside xhigh/medium/low. "off" is not one of them
# - the template expresses off as enable_thinking=false, which prefills an empty
# <think></think> - so it is translated rather than passed through. Validating
# here turns a config typo into a startup error instead of an HTTP 500 several
# minutes into a pass.
REASONING_LEVELS = ("off", "low", "medium", "xhigh")
DEFAULT_REASONING = "low"

# What a thinking pass is allowed to spend on the reasoning channel.
#
# Reasoning tokens are charged against the SAME output cap and the SAME KV as
# the answer, so a pass that thinks with the budget solved for its answer alone
# spends the cap on thought and emits zero answer tokens - the exact failure
# _salvage_note() describes. The reserve is therefore paid twice, symmetrically:
# subtracted from the window before the budget is solved, and added to the cap
# sent to the server. That keeps fixed + prompt + answer + reasoning + margin
# <= window true by construction.
#
# Sized for 'low'. Raising a pass to xhigh without raising this reproduces the
# runaway that cost the build agent a whole 64k slot.
REASONING_RESERVE_TOKENS = 4096

# ...which is exactly what happened next, because the reserve was one number for
# every thinking level. `low` and `xhigh` drew the same 4096, so raising the
# bugfix pass to xhigh raised how much it thought without raising where it was
# allowed to think. On a ~70k-token evidence payload it spent the whole output
# cap - measured at exactly ANSWER_MAX_TOKENS + 4096 - on reasoning, and the
# document was cut off before section 2. The harness then reported "section 2
# declared no COMMAND", which is true, useless, and three retries deep into the
# same wall.
#
# The reserve is what a level is EXPECTED to think, so it scales with the level.
# The multipliers are deliberately generous: over-reserving costs prompt budget,
# which the solver reports and truncates cleanly, while under-reserving costs the
# answer itself and is only visible as a downstream parse failure. Those two
# failures are not symmetric, so this errs upward.
REASONING_RESERVE_MULTIPLIER = {
    "off": 0.0,
    "low": 1.0,
    "medium": 2.0,
    "xhigh": 4.0,
}

# --- Sampling ---
# Per phase, read from the `sampling` block of agent_config.json. It used to be
# four module constants, which meant every pass sampled identically: the engineer
# - the one pass that is transcribing an architecture into a file map rather than
# reasoning about it - ran at the thinking preset's temperature 1.0 along with
# everything else.
#
# The two presets below are the model card's own, and they are not
# interchangeable: thinking mode wants a wide, unpenalised distribution because
# the reasoning channel needs room to explore, while instruct mode wants a
# narrower one with a presence penalty to stop a non-thinking pass restating
# itself. min_p 0.0 disables it, as the card specifies - llama.cpp defaults it
# to 0.05 and Ollama to 0.0.
SAMPLING_MODES = {
    "thinking": {
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
    },
    "instruct": {
        "temperature": 0.7,
        "top_p": 0.80,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repetition_penalty": 1.0,
    },
}
SAMPLING_PARAMS = tuple(SAMPLING_MODES["thinking"])

# Which preset each phase runs in when the config names none. `engineer` is the
# only instruct phase: it emits a file-by-file build map from an architecture
# that has already been decided, and thinking-mode sampling on that job produces
# invention where transcription was wanted. Every other phase, including the
# `bugfix` design pass and the Cline build agent's own turns, is reasoning.
DEFAULT_SAMPLING_MODES = {
    "cline_startup": "thinking",
    "architect": "thinking",
    "engineer": "instruct",
    "safety": "thinking",
    "test_engineer": "thinking",
    "bugfix": "thinking",
}
# What an unrecognised phase gets. Thinking, because every pass in the pipeline
# except one is a thinking pass and a new one is far more likely to be another.
FALLBACK_SAMPLING_MODE = "thinking"

# pass key -> resolved parameter dict. Settled against agent_config.json in
# _resolve_sampling() once the config is read; the built-in presets stand until
# then, so a caller that never loads a config still samples sanely.
SAMPLING_BY_PASS: dict = {}

# Absolute ceiling on knowledge-base injection. The real limit is solved per run
# by solve_kb_budget(); this only stops a very large window from pulling in an
# unbounded KB just because it can.
KB_MAX_CHARS = 100000

# Marks where the knowledge base goes while the rest of the payload is still
# being assembled. Never appears in a payload that reaches a model.
KB_PLACEHOLDER = "\x00KNOWLEDGE_BASE\x00"

# Holds the surveyed source's place while the rest of the payload is built.
# The survey needs an HTTP client and a model, which only exist inside the pass
# loop, so the block is written long after the payload around it is assembled.
SURVEY_PLACEHOLDER = "\x00SURVEYED_SOURCE\x00"

# A server-reported prompt above this fraction of the window means it truncated.
BUDGET_BREACH_FRACTION = 0.95
# Report an under-estimate only when it is material enough to warrant calibration.
BUDGET_DRIFT_FRACTION = 0.15

# Stability Protocol: how long a stream may go with NO new token before we give up.
# This is an idle timer, not a wall-clock deadline - a healthy fast stream is never
# killed for the crime of having a lot to say.
#
# It only behaves as an idle timer once the model is RESIDENT. httpx's read timeout
# is the gap between socket reads, and before the first token there are no reads,
# so on a cold model this budget silently covers eviction, an 18GB load from disk,
# a 64k KV-cache allocation and the whole prompt eval. That is how a healthy model
# burns three attempts at 45s each and reports "No tokens of any kind received".
# preload_model() exists to take the load out of this budget - keep them paired.
#
# Sized from observation, not taste. At 45.0 the test_engineer pass was measured
# completing in 44.1s on one run and failing all three attempts on the next: the
# real time-to-first-token was sitting ON the boundary, so the pass was a coin
# flip. Cold load is not the cause (measured at 6.7s for an 18GB model) and
# neither is thinking (first byte at 4.1s on an 11.6k-char prompt, idle GPU) -
# what is left is contention, which is exactly what a first-token budget should
# absorb rather than fail on. Raise this before suspecting the model.
STALL_TIMEOUT = float(os.environ.get("STALL_TIMEOUT", "150"))

# How long a cold model may take to become resident. Loading is disk- and
# VRAM-bound and has nothing to do with generation speed, so it gets its own
# budget rather than borrowing the stall timer's.
MODEL_LOAD_TIMEOUT = float(os.environ.get("MODEL_LOAD_TIMEOUT", "600"))

# --- Design pass ---
# Pass 1 answers "what should this codebase become". There are two ways to ask
# that and they want different roles, not different phrasings of one role:
# `!architect` designs new structure, `!bugfix` removes one defect and touches
# nothing else. They are alternatives, never stages - a bug report handed to the
# architect comes back as a refactor, and a feature request handed to the bugfix
# pass comes back BLOCKED for want of a symptom.
#
# So the pipeline is parameterised on which one occupies slot 1. Everything
# downstream - the intermediate path, resume, the re-plan, the .clinerules
# assembly - follows this key, and the default leaves `!build` and `!architect`
# running exactly the pipeline they ran before this existed.
DESIGN_PASSES = ("architect", "bugfix")
DISTILL_DESIGN_PASS = os.environ.get("DISTILL_DESIGN_PASS", "").strip().lower() or "architect"
if DISTILL_DESIGN_PASS not in DESIGN_PASSES:
    raise SystemExit(
        f"DISTILL_DESIGN_PASS='{DISTILL_DESIGN_PASS}' is not a design pass. "
        f"Expected one of: {', '.join(DESIGN_PASSES)}."
    )

# --- Review Gate ---
# Which passes to run this invocation. Empty means the full 4-pass pipeline.
# The review gate sets DISTILL_PASSES to the design pass to stop after pass 1.
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

    def __init__(self, stage: str, failures: list, total: int,
                 summary: str = "", checks: list = None):
        self.stage = stage
        self.failures = failures          # list of (label, error_text)
        self.total = total
        # An abort report that describes the wrong fault is worse than a generic
        # one: "is a server listening?" sends the operator to check a server that
        # answered perfectly well and returned nothing. A raiser that knows better
        # says so here; everything else keeps the connectivity wording.
        self.summary = summary
        self.checks = checks
        labels = ", ".join(label for label, _ in failures)
        errors = sorted({err for _, err in failures})
        super().__init__(
            f"{stage}: {len(failures)} of {total} LLM call(s) failed ({labels}). "
            f"Error(s): {'; '.join(errors)}"
        )


def _looks_like_llm_error(result) -> bool:
    """True for the sentinel string a call returns when it gave up."""
    return isinstance(result, str) and result.startswith("[ERROR:")


def _check_llm_result(result: str, label: str):
    """Return (label, error) if a call gave up, else None."""
    if _looks_like_llm_error(result):
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


def solve_addendum_budget(window: int, system_tokens: int, payload_tokens: int) -> int:
    """
    Characters still spendable on extra payload without leaving the single-pass path.

    Shared by the two things that get appended to an already-assembled payload:
    the knowledge base and the evidence read to clear a blocker. Both have the
    same constraint - fit alongside the payload, the system prompt and the answer
    the pass still has to write, inside one merge-shaped call.

    ANSWER_MAX_TOKENS is held back on top of the solver's own answer reserve: the
    engineer pass sees this payload plus the architect's full document as prior
    context, and it has to fit too.
    """
    fixed = system_tokens + est_tokens("### CURRENT TASK\n")
    try:
        facts, _answer = solve_merge_budget(window, fixed)
    except BudgetInfeasible:
        return 0
    return max(0, (facts - payload_tokens - ANSWER_MAX_TOKENS) * CHARS_PER_TOKEN_DENSE)


def solve_kb_budget(window: int, system_tokens: int, payload_tokens: int) -> int:
    """
    Decide how many characters of knowledge base the window can still hold.

    Returns a character budget for select_relevant_kb.

    The KB cap used to be a 100000-character literal - 33k tokens at the dense
    rate, over half of a 64k window - set with no reference to the budget solver
    that sizes every other part of the call. It could not overflow the server,
    because call_llm re-measures and falls back, but that fallback is the point:
    a payload that fits one merge-shaped call is sent whole and loses nothing,
    while anything larger goes through chunked extraction and reaches the merge
    as capped bullet records. A large KB could therefore silently downgrade the
    architect from the lossless path to the lossy one - the KB itself displacing
    the codebase facts it was added to inform.

    So the KB takes what is genuinely spare after the rest of the payload, and
    nothing more. ANSWER_MAX_TOKENS is held back on top: the engineer pass sees
    this same payload plus the architect's full answer as prior context, and it
    has to fit single-pass too.
    """
    return min(KB_MAX_CHARS, solve_addendum_budget(window, system_tokens, payload_tokens))


def _validate_sampling(entry, pass_key: str, modes: dict) -> dict:
    """
    Resolve one phase's `sampling` entry into a full parameter set.

    An entry names a `mode` - one of the presets - and may override individual
    parameters on top of it. Naming the mode is the point: it says what kind of
    pass this is, so the six numbers stay consistent when a preset is retuned,
    and an override is visibly a deviation from it rather than a fresh set of
    numbers nobody can compare against anything.

    Validated here rather than at the call site, because a typo in a sampler name
    is otherwise silently dropped by every server involved and shows up only as a
    pass that samples wrong - which looks exactly like a bad prompt.
    """
    mode = DEFAULT_SAMPLING_MODES.get(pass_key, FALLBACK_SAMPLING_MODE)
    overrides = {}

    if entry is not None:
        if not isinstance(entry, dict):
            raise ValueError(
                f"sampling.{pass_key} must be an object, got {type(entry).__name__}."
            )
        if "mode" in entry:
            mode = str(entry["mode"]).strip().lower()
        # Leading underscores are the convention this config uses for operator
        # notes; they are documentation, not parameters.
        overrides = {k: v for k, v in entry.items()
                     if k != "mode" and not k.startswith("_")}
        unknown = [k for k in overrides if k not in SAMPLING_PARAMS]
        if unknown:
            raise ValueError(
                f"Unknown sampling parameter(s) {', '.join(sorted(unknown))} in "
                f"sampling.{pass_key}. Supported: {', '.join(SAMPLING_PARAMS)}."
            )

    if mode not in modes:
        raise ValueError(
            f"Unknown sampling mode '{mode}' for '{pass_key}'. "
            f"Supported: {', '.join(sorted(modes))}."
        )

    params = dict(modes[mode])
    for key, value in overrides.items():
        try:
            params[key] = float(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"sampling.{pass_key}.{key} must be a number, got {value!r}."
            )
    # top_k is a count of candidates, not a probability; both servers reject a float.
    params["top_k"] = int(params["top_k"])
    return params


def _resolve_sampling(config: dict) -> None:
    """
    Settle every phase's sampling parameters against agent_config.json.

    Two levels, both optional. `sampling._modes` retunes the presets themselves,
    which is how an operator moves every thinking pass at once; `sampling.<phase>`
    picks a preset for one phase and overrides parameters on it. A config with no
    `sampling` block at all gets the built-in presets under the default
    mode-per-phase map, which is what every existing config does.
    """
    global SAMPLING_BY_PASS

    block = config.get("sampling") or {}
    if not isinstance(block, dict):
        raise ValueError(f"`sampling` must be an object, got {type(block).__name__}.")

    modes = {name: dict(params) for name, params in SAMPLING_MODES.items()}
    for name, override in (block.get("_modes") or {}).items():
        # Underscore keys are operator notes, here as everywhere else in this
        # config. Without this a comment beside a preset is read as a preset.
        if name.startswith("_"):
            continue
        if not isinstance(override, dict):
            raise ValueError(f"sampling._modes.{name} must be an object.")
        unknown = [k for k in override if k not in SAMPLING_PARAMS]
        if unknown:
            raise ValueError(
                f"Unknown sampling parameter(s) {', '.join(sorted(unknown))} in "
                f"sampling._modes.{name}. Supported: {', '.join(SAMPLING_PARAMS)}."
            )
        modes.setdefault(name, dict(SAMPLING_MODES[FALLBACK_SAMPLING_MODE])).update(
            {k: float(v) for k, v in override.items()}
        )

    configured = {k: v for k, v in block.items() if not k.startswith("_")}
    resolved = {}
    for pass_key in list(DEFAULT_SAMPLING_MODES) + list(configured):
        if pass_key in resolved:
            continue
        resolved[pass_key] = _validate_sampling(configured.get(pass_key), pass_key, modes)

    SAMPLING_BY_PASS = resolved
    source = "agent_config.json" if configured or block.get("_modes") else "defaults"
    summary = ", ".join(
        f"{k} t={v['temperature']:g}/p={v['top_p']:g}" for k, v in resolved.items()
    )
    print(f"🎲 Sampling (source: {source}): {summary}", flush=True)


def sampling_for(pass_key) -> dict:
    """
    The resolved sampling set for a phase, or the default for its kind.

    Falls back to the built-in presets rather than raising, so a code path that
    resolves a model config before _resolve_sampling() has run - or for a phase
    the config never names - still sends a coherent set.
    """
    if pass_key in SAMPLING_BY_PASS:
        return dict(SAMPLING_BY_PASS[pass_key])
    mode = DEFAULT_SAMPLING_MODES.get(pass_key, FALLBACK_SAMPLING_MODE)
    return dict(SAMPLING_MODES[mode])


def sampling_payload(sampling: dict) -> dict:
    """
    Wire names for a resolved sampling set.

    The config says `repetition_penalty` because that is what the model card
    calls it; llama.cpp and Ollama both call the same knob `repeat_penalty`, and
    a request that sends the card's name simply has it ignored.
    """
    wire = {k: sampling[k] for k in SAMPLING_PARAMS if k != "repetition_penalty"}
    wire["repeat_penalty"] = sampling["repetition_penalty"]
    return wire


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


# How many turns of PROJECT_HISTORY a rebuild pass actually reads.
#
# It was every message. Measured on a live workspace: 100 messages, 241335
# characters, 80445 tokens - 61% of the window and, with the README and the
# skeleton alongside it, the reason the payload overran the merge budget by 12k
# tokens and starved the blocker-resolution read to zero.
#
# The design pass does not need the transcript. What the project IS comes from
# the skeleton and the directory structure; what to build comes from
# NEW_REQUEST, which is extracted separately and never subject to this cap. The
# history's job is the last few turns of intent around the current request, and
# past that it is re-litigating decisions already in the code.
HISTORY_MAX_MESSAGES = 5


def conversation_to_text(messages: list, max_messages: int = None) -> str:
    """
    Flatten conversation messages into a readable text block.

    Two reductions, both lossless for a design pass. PROJECT_HISTORY is the
    single largest element of the payload - measured at 52% of it - and most of
    what makes it large is not conversation.

    Orchestrator receipts ("Build pipeline triggered") are UI acknowledgements
    echoed back into the transcript. They repeat verbatim once per build and say
    nothing about the design.

    Superseded assistant turns are the other half: re-running !architect on a
    refined prompt produces a byte-identical proposal often enough that the same
    multi-kilobyte block lands in the history several times. The last occurrence
    is kept in place - it sits nearest the current request - and earlier copies
    collapse to a one-line marker, so the user turn they answered still has a
    visible reply and the turn structure survives intact.
    """
    parts = []
    last_seen = {}
    elided = 0
    if max_messages is not None and len(messages) > max_messages:
        elided = len(messages) - max_messages
        messages = messages[-max_messages:]
    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        if not content.strip():
            continue
        if role == "ASSISTANT" and any(m in content for m in RECEIPT_MARKERS):
            continue
        if role == "ASSISTANT":
            key = hashlib.md5(content.encode("utf-8")).hexdigest()
            if key in last_seen:
                parts[last_seen[key]] = f"[{role}]\n{SUPERSEDED_MARKER}"
            last_seen[key] = len(parts)
        parts.append(f"[{role}]\n{content}")
    if elided:
        # Stated, not silently dropped. A pass that believes it is reading the
        # whole conversation will treat an absent decision as one never made,
        # and R9 tells it to record an assumption rather than block - which is
        # the wrong move when the answer is one turn above the window.
        parts.insert(0, f"[HISTORY]\n... [{elided} earlier message(s) elided; "
                        f"the {len(parts)} most recent are shown]")
    return "\n\n---\n\n".join(parts)


# --- Blocker protocol ---------------------------------------------------------
#
# Every prompt defines a way for a pass to refuse. architect.md R10 emits a bare
# two-line "# BLOCKED"; the other three emit "# 1. Blockers" with "- BLOCKER: ...
# | NEEDS: ..." lines and, per their output contract, stop there.
#
# Nothing read any of it. A blocked architect was assembled into .clinerules
# verbatim, every downstream pass then blocked on the missing specification, and
# the build loop ran five full build/verify/safety iterations against a document
# whose first heading was "# BLOCKED" - measured at four hours of GPU time on a
# 27B model. Meanwhile the architect's stated need ("the actual mock data shapes,
# service endpoint contracts") was sitting on the mounted volume, and the payload
# was using 21k of a 52k budget, so there was room to simply show it the files.
#
# So a blocker is now a request, not an epitaph: resolve it once by reading what
# the pass says it needs, and if it still cannot proceed, stop before writing
# .clinerules rather than spending hours implementing a refusal.

_ARCHITECT_BLOCKED_RE = re.compile(r"^#\s*BLOCKED\b", re.MULTILINE)
_BLOCKER_LINE_RE = re.compile(r"^\s*-\s*BLOCKER:\s*(.+)$", re.MULTILINE)

# A pass with nothing to report is told to emit "- none", but the only line shape
# its template ever shows is "- BLOCKER: ...". Models resolve that ambiguity by
# writing "- BLOCKER: - none" - a declaration of no blockers that reads as one, and
# stops a run whose four passes all succeeded. Treat an empty statement as empty.
_EMPTY_BLOCKER_RE = re.compile(
    r"^[-\s*]*(none|n/?a|nil|null|empty|no blockers?)\b[\s.:|-]*"
    r"(needs:\s*(none|n/?a|-)?\s*)?$",
    re.IGNORECASE,
)

# Only the passes that receive the payload can be unblocked by reading files.
# Passes 3 and 4 see a ~90-token instruction to review the earlier analyses, so
# their blockers are always "the previous specification is missing" - which is
# fixed by unblocking the pass upstream, not by handing them source code.
EVIDENCE_RETRY_PASSES = ("architect", "bugfix", "engineer")
EVIDENCE_MAX_FILES = 12
EVIDENCE_MAX_FILE_CHARS = 24000
# Smallest slice of a file worth attaching. The failure this guards is a leftover
# budget that covers the truncation marker and little else: the block carries no
# content, teaches the pass nothing, and still counts the path as delivered -
# burning it out of the retry loop for good. Leftovers this small go to
# <NOT_READ> instead, so the next round can spend a whole budget on them.
#
# Deliberately low. "Truncated beats skipped" is the rule this function is built
# on - a partial interface still beats the skeleton's bare symbol name - so this
# is a floor against empty blocks, not a quality bar on the slice.
EVIDENCE_MIN_SLICE_CHARS = 200
BLOCKER_RESOLVE_MAX_TOKENS = 300

# Evidence the retry is guaranteed, whatever solve_addendum_budget says.
#
# The solver measures the spare window against the UNREDUCED payload. A payload
# larger than the merge budget does not fail - call_llm sends it down the chunked
# extraction path instead - so the pass runs, blocks, and then asks for files
# against a budget that has already been computed as negative and clamped to
# zero. Measured on a 1596-file workspace: payload 122439 tok against a facts
# budget of 110635, addendum budget exactly 0, and read_evidence returned on its
# first line. The resolver had already confirmed all nine requested files were on
# disk; not one byte of any of them was read.
#
# The perverse part is the direction. The bigger the project, the more certain
# the architect is to block on a bare symbol name - and the more certain the
# budget is to be zero when it does. The protocol switched itself off precisely
# where it was needed, and reported it as "no readable evidence", which reads as
# the files being absent.
#
# So evidence gets a floor rather than a share. One file's worth is ~8k tokens
# on a payload that is already going through extraction, where it is one more
# chunk and changes nothing structurally - against a blocked document, which
# costs the whole run.
EVIDENCE_MIN_BUDGET_CHARS = EVIDENCE_MAX_FILE_CHARS

# How many times a pass may block, be shown files, and try again.
#
# Diagnosis is the shape that follows a trail - reading files IS the work, and
# each round narrows the search rather than re-asking the same question - so the
# bugfix pass gets three. The cost is a full pass per round, so it is bounded
# rather than open.
#
# The architect had one, on the reasoning that "a gap that survives being shown
# the files is a gap the workspace does not contain". A measured run refuted
# that. It blocked for three files, was shown six, and came back asking for
# `config.yaml` and the job-radar install script instead - a narrower question
# than the one it started with, which is a trail and not a wall. It had no round
# left to follow it, and stopped at the review gate one step short.
#
# That rationale was written when the retry had no budget to spend anyway: the
# same run measured 170484 chars available and used 52737. Being shown the files
# now changes the question often enough to be worth a second pass.
EVIDENCE_ROUNDS = {"bugfix": 3, "architect": 2}
EVIDENCE_ROUNDS_DEFAULT = 1


def detect_blockers(result: str) -> list:
    """
    Return the blocker statements a pass emitted, or [] if it produced a design.

    Handles both refusal shapes: the architect's bare "# BLOCKED" document and
    the "- BLOCKER: ... | NEEDS: ..." bullets the other three use. "- none" is
    the healthy value of that section and never matches, whether it arrives as a
    bare bullet or stuffed into the BLOCKER slot.
    """
    if not result:
        return []
    blockers = [m.strip() for m in _BLOCKER_LINE_RE.findall(result)
                if not _EMPTY_BLOCKER_RE.match(m.strip())]
    if blockers:
        return blockers
    if _ARCHITECT_BLOCKED_RE.search(result):
        # R10's second line carries the reason; fall back to the whole document
        # if the model emitted the heading without one.
        reasons = [ln.strip(" -\t") for ln in result.splitlines()
                   if ln.strip().startswith("-") and ln.strip() != "- none"]
        return reasons or [result.strip()]
    return []


BlockerPaths = collections.namedtuple("BlockerPaths", ("present", "absent"))

# What read_evidence actually put in the payload, as opposed to what was asked
# for. The two diverge whenever the budget runs out mid-set, and the caller has
# to mark only `included` as seen - see resolve_pass_blockers.
Evidence = collections.namedtuple("Evidence", ("text", "included"))


def _looks_like_path(candidate: str) -> bool:
    """True for something worth reporting as absent rather than as model noise."""
    return "/" in candidate or re.search(r"\.[A-Za-z0-9]{1,5}$", candidate) is not None


def resolve_blocker_paths(client, model_config, blockers: list,
                          skeleton: str, project_dir: str) -> BlockerPaths:
    """
    Ask the model which workspace files would clear its own blockers.

    A regex over the blocker text will not do this. The real one read "lacks the
    actual mock data shapes, service endpoint contracts and frontend consumption
    patterns" - concepts, not paths. Mapping those onto files is exactly what the
    symbol skeleton plus a model is for, and the model is already resident.

    Returns present paths and absent ones separately. Absence is an answer, not a
    dead end: a pass that blocks on "is schema.ts already partially defined?" is
    resolved by "that file does not exist", so discarding the miss sends the retry
    back in knowing no more than it did the first time.
    """
    system = (
        "You map blockers onto files. Given blockers from a design pass and a "
        "symbol skeleton of the repository, list the repository-relative paths "
        "whose CONTENTS would resolve them.\n"
        f"Output at most {EVIDENCE_MAX_FILES} paths, one per line, nothing else. "
        "No commentary, no bullets, no backticks. If no file would help, output "
        "exactly: NONE"
    )
    user = (
        "### BLOCKERS\n" + "\n".join(f"- {b}" for b in blockers) +
        "\n\n### REPOSITORY SYMBOL SKELETON\n" + skeleton +
        "\n\n### CURRENT TASK\nList the paths.\n"
    )
    raw = _single_llm_call(client, model_config, system, user, "Blocker resolution",
                           max_output_tokens=BLOCKER_RESOLVE_MAX_TOKENS)
    if _check_llm_result(raw, "Blocker resolution"):
        print("  ⚠ Blocker resolution call failed; continuing without evidence.", flush=True)
        # BlockerPaths, not a bare list. The caller reads `.present` OUTSIDE the
        # try that guards this call, so returning [] here raised AttributeError
        # and took down the pass on the one path that exists to survive a failed
        # resolution.
        return BlockerPaths([], [])

    paths, absent = [], []
    for line in raw.splitlines():
        candidate = line.strip().strip("-*` \t")
        if not candidate or candidate.upper() == "NONE" or " " in candidate:
            continue
        candidate = candidate.lstrip("./")
        full = os.path.join(project_dir, candidate)
        if os.path.isfile(full):
            if candidate not in paths:
                paths.append(candidate)
        elif _looks_like_path(candidate) and candidate not in absent:
            absent.append(candidate)
        if len(paths) >= EVIDENCE_MAX_FILES:
            break
    return BlockerPaths(paths, absent[:EVIDENCE_MAX_FILES])


# A blocker names `<path>::<symbol>` because that is the shape the prompts ask
# for. Keeping the symbol lets the reader slice a large file around the thing
# actually being asked about instead of taking whatever happens to be at the top.
_BLOCKER_SYMBOL_RE = re.compile(r"([\w./-]+\.[A-Za-z0-9]{1,5})::([A-Za-z_]\w*)")

# What a definition of `X` looks like across the languages this pipeline sees.
# Preferred over a bare name match because the first mention of a symbol in a
# file is usually an import or a reference, and slicing around an import teaches
# the pass nothing about the shape it asked for.
_DEFINITION_RE_TMPL = (
    r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?"
    r"(?:async\s+)?(?:public\s+|private\s+|protected\s+)?"
    r"(?:interface|type|class|enum|function|const|let|var|def|struct|impl)\s+{}\b"
)


# Everything a blocker wraps in backticks. The prompts mark paths AND symbols
# this way, so the two are separated below by shape rather than by delimiter.
_BLOCKER_TICKED_RE = re.compile(r"`([^`\n]{1,120})`")
_LOOKS_LIKE_PATH_RE = re.compile(r"[/\\]|\.[A-Za-z0-9]{1,5}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*$")


def blocker_symbol_hints(blockers: list) -> dict:
    """
    Map path -> [symbol] for the symbols a blocker asked about.

    Two forms, because the model uses both and only one was handled at first.
    B2 asks for `<path>::<symbol>` and sometimes that is what arrives:

        `src/types/domain.ts::DomainControlChallenge` — need its full field definition

    and sometimes the same request arrives as prose:

        `src/types/domain.ts` — need the `AttestationRequest` interface to find ...

    The second form produced no hints at all, so the slice fell back to the head
    of the file and cut `AttestationRequest` off at byte 24000 of a 34636-byte
    file - the exact failure the symbol-aware slice was built to prevent, missed
    on a punctuation difference. A pass then blocked for a file it had been
    given, twice.

    In the prose form there is nothing tying a symbol to a particular path, so
    every symbol in a statement is offered to every path in that same statement.
    A wrong pairing costs nothing: a symbol that is not defined in a file simply
    does not match, and the slice falls back to the head as before.
    """
    hints = {}
    for b in blockers or []:
        text = b or ""
        # Precise form first, so an explicit pairing leads the list.
        paired = _BLOCKER_SYMBOL_RE.findall(text)
        for path, symbol in paired:
            hints.setdefault(path.lstrip("./"), []).append(symbol)

        ticked = _BLOCKER_TICKED_RE.findall(text)
        paths = [t.lstrip("./") for t in ticked if _LOOKS_LIKE_PATH_RE.search(t)]
        symbols = [t for t in ticked
                   if not _LOOKS_LIKE_PATH_RE.search(t) and _IDENTIFIER_RE.match(t)]
        for path in paths:
            # `path::symbol` also matches the ticked-path pattern; strip the
            # suffix so both forms key the same entry.
            key = path.split("::", 1)[0]
            for symbol in symbols:
                if symbol not in hints.setdefault(key, []):
                    hints[key].append(symbol)
    return hints


# "lines ~1040-1100", "line 1040", "lines 1040 to 1100", "L1040-L1100". A blocker
# that names a region is giving a better anchor than any symbol guess, and until
# this parsed it the text was read past: the run that asked for
# `Backend/src/routes/users.ts` lines ~1040-1100 was handed the first 24000
# characters of a 46738-character file, and the region it named starts at 42012.
_BLOCKER_LINE_RANGE_RE = re.compile(
    r"lines?\s*~?\s*L?(\d{1,6})\s*(?:[-\u2013\u2014]|\bto\b)\s*~?L?(\d{1,6})"
    r"|lines?\s*~?\s*L?(\d{1,6})",
    re.IGNORECASE,
)


def blocker_line_hints(blockers: list) -> dict:
    """
    Map path -> (start_line, end_line) for regions a blocker named in prose.

    Paired with the ticked paths in the same statement, the same way
    blocker_symbol_hints pairs symbols. One statement usually names one file;
    where it names several, the region applies to each, which is the honest
    reading of "these files, around here".
    """
    hints = {}
    for statement in blockers or []:
        text = statement if isinstance(statement, str) else str(statement)
        match = _BLOCKER_LINE_RANGE_RE.search(text)
        if not match:
            continue
        if match.group(1):
            start, end = int(match.group(1)), int(match.group(2))
        else:
            start = end = int(match.group(3))
        if start > end:
            start, end = end, start
        for ticked in _BLOCKER_TICKED_RE.findall(text):
            if not _LOOKS_LIKE_PATH_RE.search(ticked):
                continue
            hints[ticked.lstrip("./").split("::", 1)[0]] = (start, end)
    return hints


def _find_definition(content: str, symbols) -> "int | None":
    """
    Offset of the earliest DEFINITION of any named symbol, else any mention.

    Two passes, not one per symbol. Hints are now gathered loosely from prose, so
    a statement can offer several symbols for one file and only one of them will
    be defined there. Falling back to a bare-name match per symbol let a passing
    mention of an unrelated hint - an import, a type reference - outrank a real
    definition further down, and aim the slice at the wrong part of the file.
    Every definition is considered before any mention is.
    """
    best = None
    for sym in symbols or []:
        m = re.search(_DEFINITION_RE_TMPL.format(re.escape(sym)), content,
                      re.MULTILINE)
        if m and (best is None or m.start() < best):
            best = m.start()
    if best is not None:
        return best

    # Mentions, but never the import that brought the name into the file.
    #
    # A symbol this file uses without defining - `externalJobs`, a table imported
    # from a schema module - has its first mention in the import block at the top
    # and its real uses hundreds of lines below. Measured: a blocker asked for the
    # handler at lines 1040-1100 of a 46738-char file, the only hint resolvable
    # was such a symbol, its first mention sat at byte 763, and the 24000-char
    # slice centred there did not contain the requested region at all. The pass
    # got a plausible slice of the right file, which is indistinguishable from
    # having been answered.
    import_only = None
    for sym in symbols or []:
        first = None
        for m in re.finditer(rf"\b{re.escape(sym)}\b", content):
            if first is None:
                first = m.start()
            line_end = content.find("\n", m.start())
            line = content[content.rfind("\n", 0, m.start()) + 1:
                           line_end if line_end != -1 else len(content)]
            if IMPORT_RE.match(line):
                continue
            if best is None or m.start() < best:
                best = m.start()
            break
        # Every mention was an import line. Keep it only as a last resort: the
        # top of the file still beats nothing when there is no other anchor.
        if first is not None and (import_only is None or first < import_only):
            import_only = first
    return best if best is not None else import_only


def _line_offset(content: str, line_number: int) -> int:
    """Byte offset of a 1-indexed line, clamped to the file."""
    if line_number <= 1:
        return 0
    offset = 0
    for _ in range(line_number - 1):
        nxt = content.find("\n", offset)
        if nxt == -1:
            return len(content)
        offset = nxt + 1
    return offset


def _slice_around_symbols(content: str, keep: int, symbols,
                          line_range: tuple = None) -> str:
    """
    Take `keep` characters centred on the symbol the blocker asked about.

    The head slice this replaces made a whole class of blocker unanswerable. An
    architect asked for `src/types/domain.ts::DomainControlChallenge`; the
    definition sits at byte 27473 of 33736 and the cap is 24000, so it was handed
    24k of the file that did NOT contain the one symbol it named. It blocked
    again, in the same words, with its single retry round already spent - and the
    failure is invisible, because a plausible slice of the right file looks like
    the request was honoured.

    Falls back to the head slice when no symbol was named or none is found, which
    is the old behaviour and the right default: the top of a file is where its
    imports and principal declarations usually are.
    """
    if len(content) <= keep:
        return content

    tail_marker = "\n... [truncated: {} more characters]"
    head_marker = "... [{} earlier characters omitted]\n"

    def head_slice() -> str:
        body = content[:max(0, keep - len(tail_marker.format(len(content))))]
        return body + tail_marker.format(len(content) - len(body))

    # A stated line range outranks a symbol guess: it is what the pass asked for,
    # in its own words, rather than what a name search happened to find.
    hit = None
    if line_range:
        hit = _line_offset(content, line_range[0])
    if hit is None:
        hit = _find_definition(content, symbols)
    if hit is None:
        return head_slice()

    # Reserve against the untruncated length so the reservation can only ever be
    # too large - the same defensive sizing the caller uses, for the same reason.
    reserve = len(head_marker.format(len(content))) + len(tail_marker.format(len(content)))
    window = keep - reserve
    if window < EVIDENCE_MIN_SLICE_CHARS:
        return head_slice()

    # A quarter of the window ahead of the definition, so the surrounding context
    # comes too; the rest follows it, which is where a body or field list lives.
    start = max(0, hit - window // 4)
    end = min(len(content), start + window)
    start = max(0, end - window)

    out = head_marker.format(start) if start > 0 else ""
    out += content[start:end]
    if end < len(content):
        out += tail_marker.format(len(content) - end)
    return out


EVIDENCE_PREAMBLE = (
    "You previously reported these blockers. The findings below were read from "
    "the workspace to resolve them. Design against them; do not block on facts "
    "they now supply."
)


def read_evidence(project_dir: str, paths: list, budget_chars: int,
                  absent: list = None, symbol_hints: dict = None,
                  tag: str = "REQUESTED_EVIDENCE",
                  preamble: str = EVIDENCE_PREAMBLE,
                  line_hints: dict = None) -> "Evidence":
    """
    Read the requested files into a payload block, within budget.

    Budget is shared across the set and spent in the order the resolver returned,
    which is its own relevance ordering. A file that does not fit whole is
    truncated with a marker rather than skipped - a partial interface is still
    more than the skeleton's bare symbol name.

    `absent` names files the pass asked for that do not exist. They are stated
    explicitly and cost almost no budget, and they are worth a block on their own:
    "that file is not there" can be the whole answer.

    Returns an Evidence(text, included). `included` is the subset actually read:
    a set that overruns the budget leaves the rest in <NOT_READ>, and the caller
    must not record those as supplied or the pass can never ask for them again.
    """
    absent = absent or []
    # Both of these used to return a bare Evidence("", []), which the caller
    # reports as "No readable evidence identified for these blockers" - wording
    # that reads as the files being missing, when the resolver has just confirmed
    # they are on disk. Say which of the two it was.
    if not paths and not absent:
        print("  ⚠ Evidence: the resolver named no readable path.", flush=True)
        return Evidence("", [])
    if budget_chars <= 0:
        print(f"  ⚠ Evidence: budget is {budget_chars} chars — the payload fills "
              f"the window and nothing can be attached. {len(paths)} file(s) were "
              f"found on disk and left unread.", flush=True)
        return Evidence("", [])

    blocks, remaining, included, skipped = [], budget_chars, [], []
    for rel in paths:
        if remaining <= 0:
            skipped.append(rel)
            continue
        try:
            with open(os.path.join(project_dir, rel), "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            skipped.append(rel)
            continue
        cap = min(remaining, EVIDENCE_MAX_FILE_CHARS)
        if len(content) > cap:
            # The marker is spent from the budget too. Slicing to `cap` and then
            # appending it put every truncated file over its own cap, driving
            # `remaining` negative and skipping the rest of the request set on
            # the next iteration. Sizing the reservation against the untruncated
            # length only ever over-reserves, so the block cannot exceed `cap`.
            marker = "\n... [truncated: {} more characters]"
            keep = cap - len(marker.format(len(content)))
            if keep < EVIDENCE_MIN_SLICE_CHARS:
                skipped.append(rel)
                continue
            # Centred on the symbol the blocker named, not the top of the file.
            content = _slice_around_symbols(
                content, keep, (symbol_hints or {}).get(rel),
                (line_hints or {}).get(rel),
            )
        remaining -= len(content)
        included.append(rel)
        blocks.append(f'<file path="{rel}">\n{content}\n</file>')

    if not blocks and not absent:
        # The third silent path: every file was unreadable, or every slice came
        # out under EVIDENCE_MIN_SLICE_CHARS on a budget too small to carry one.
        print(f"  ⚠ Evidence: {len(skipped)} file(s) named but none attached "
              f"within {budget_chars} chars — {', '.join(skipped)}", flush=True)
        return Evidence("", [])
    note = ""
    if absent:
        note += ("\n  <ABSENT>These paths do not exist in the workspace. That is "
                 "the verified answer, not a gap: treat each as a file to be "
                 "created from scratch, with no existing contents to reconcile. "
                 "Do not block on them again: " + ", ".join(absent) + "</ABSENT>\n")
    if skipped:
        note += ("\n  <NOT_READ>Requested but not read (budget spent): "
                 + ", ".join(skipped) + "</NOT_READ>\n")
    print(f"  📎 Evidence: {len(included)} file(s), "
          f"{budget_chars - remaining} chars — {', '.join(included) or 'none'}", flush=True)
    if absent:
        print(f"  📭 Confirmed absent: {', '.join(absent)}", flush=True)
    return Evidence(
        f"\n\n  <{tag}>\n  {preamble}\n"
        + "\n".join(blocks) + note +
        f"  </{tag}>\n",
        included,
    )


def print_blocker(index: int, text: str, width: int = 100) -> None:
    """
    Print one blocker in full, wrapped, to the build log.

    This used to be clipped to 150 characters. A blocker is a single line that
    names the artefact the pass needs AND why it cannot proceed without it - and
    150 characters reliably landed mid-sentence, in the middle of the "why". That
    is the half worth reading: it distinguishes a gap the workspace can close
    (a file exists but was not in CONTEXT) from one it cannot (the request never
    said what the expected behaviour was), and only the first is worth spending
    another round on.
    """
    body = " ".join(text.split())
    lines = textwrap.wrap(body, width=width) or [""]
    print(f"     · [{index}] {lines[0]}", flush=True)
    for continuation in lines[1:]:
        print(f"           {continuation}", flush=True)


def resolve_pass_blockers(client, pass_key: str, model_config, prompt: str,
                          target_content: str, prior_context: str,
                          symbol_skeleton: str, result: str,
                          max_rounds: int = None) -> str:
    """
    Satisfy a pass's blockers once, and return whatever it produced afterwards.

    Returns the original result unchanged when there is nothing to do, so the
    caller can apply it unconditionally. Shared by the initial distillation and
    by the re-plan, which is just as capable of asking for a file it cannot see.

    Runs up to EVIDENCE_ROUNDS rounds. Evidence accumulates across them: a pass
    that blocks twice is following a trail, and dropping round one's files to
    make room for round two's would walk it back to the start.

    `max_rounds` overrides that budget. The reproduction loop passes 1: the pass
    has already had its full allowance on the first attempt, so a re-run that
    blocks is correcting a diagnosis rather than starting one, and letting each
    of three attempts buy three more rounds turns one bug into twelve LLM calls.
    """
    if pass_key not in EVIDENCE_RETRY_PASSES or not symbol_skeleton:
        return result

    if max_rounds is None:
        max_rounds = EVIDENCE_ROUNDS.get(pass_key, EVIDENCE_ROUNDS_DEFAULT)
    seen = set()
    evidence = ""

    for round_no in range(1, max_rounds + 1):
        blockers = detect_blockers(result)
        if not blockers:
            return result

        print(f"  🚧 {pass_key} reported {len(blockers)} blocker(s) "
              f"(round {round_no}/{max_rounds}); resolving against the workspace...",
              flush=True)
        for i, b in enumerate(blockers, 1):
            print_blocker(i, b)
        try:
            found = resolve_blocker_paths(
                client, model_config, blockers, symbol_skeleton, "/workspace"
            )
        except Exception as e:
            print(f"  ⚠ Blocker resolution errored ({e}); continuing.", flush=True)
            found = BlockerPaths([], [])

        # A pass that re-asks for a file it has already been shown is stuck, not
        # progressing. Reading it a second time would spend the budget to hand
        # back a byte-identical payload, so only genuinely new paths count.
        fresh = [p for p in found.present if p not in seen]
        fresh_absent = [p for p in found.absent if p not in seen]

        # What the resolver made of the blockers, before any of it is acted on.
        # Without this the log jumps from the pass's request to a file count,
        # and a path that was classified absent, or held back as already-seen,
        # is indistinguishable from one the budget simply could not reach.
        repeated = [p for p in (list(found.present) + list(found.absent)) if p in seen]
        if fresh:
            print(f"     ↳ on disk, will read: {', '.join(fresh)}", flush=True)
        if fresh_absent:
            print(f"     ↳ not in the workspace (answers the blocker): "
                  f"{', '.join(fresh_absent)}", flush=True)
        if repeated:
            print(f"     ↳ already supplied in an earlier round: "
                  f"{', '.join(repeated)}", flush=True)

        if not fresh and not fresh_absent:
            print(f"  ⚠ Round {round_no} asked only for paths already supplied; "
                  f"stopping. The pass has the contents and still cannot place "
                  f"the defect - the gap is in the report, not the workspace.",
                  flush=True)
            return result

        solved = solve_addendum_budget(
            CONTEXT_WINDOW, est_tokens(prompt),
            est_tokens(target_content) + est_tokens(prior_context) + est_tokens(evidence),
        )
        # Never below the floor - see EVIDENCE_MIN_BUDGET_CHARS. A zero here is
        # not "there is no room", it is "the payload was already over the merge
        # budget", and the answer to that is extraction, not a blocked document.
        budget = max(solved, EVIDENCE_MIN_BUDGET_CHARS)
        if solved < EVIDENCE_MIN_BUDGET_CHARS:
            print(f"     ↳ window has {solved} chars spare; taking the "
                  f"{EVIDENCE_MIN_BUDGET_CHARS}-char floor. The payload "
                  f"(~{est_tokens(target_content) + est_tokens(prior_context)} tok) "
                  f"already exceeds the merge budget, so this call extracts either way.",
                  flush=True)
        found_evidence = read_evidence("/workspace", fresh, budget, fresh_absent,
                                       symbol_hints=blocker_symbol_hints(blockers),
                                       line_hints=blocker_line_hints(blockers))
        addendum = found_evidence.text
        if not addendum:
            print("  ⚠ No readable evidence identified for these blockers.", flush=True)
            return result

        # The partial-delivery case, which is invisible otherwise: read_evidence
        # spends one shared budget in order and leaves the rest in <NOT_READ>.
        # Saying so here is what makes the next round's repeat request legible as
        # progress rather than the pass going in circles.
        unread = [p for p in fresh if p not in found_evidence.included]
        if unread:
            print(f"     ⏭ budget spent before reading: {', '.join(unread)} "
                  f"— deferred to round {round_no + 1}"
                  + (" (none left; raise DISTILL_CTX or EVIDENCE_ROUNDS)"
                     if round_no >= max_rounds else ""), flush=True)

        # Only what was actually read. Marking the whole `fresh` set seen meant a
        # request the budget could not satisfy in one round was recorded as
        # answered: the pass re-asked for the files it never received, every one
        # of them failed the `not in seen` test, and the loop stopped on "asked
        # only for paths already supplied" with rounds still on the clock. The
        # unread ones stay unseen so the next round can spend a fresh budget on
        # them. `fresh_absent` is different - a confirmed absence is a complete
        # answer, costs no budget, and is always reported in full.
        seen.update(found_evidence.included)
        seen.update(fresh_absent)
        evidence += addendum

        print(f"  ↻ Re-running {pass_key} with the evidence attached "
              f"(budget {budget} chars)...", flush=True)
        update_status(f"Resolving blockers: {pass_key} (round {round_no})")
        try:
            result = call_llm(client, model_config, prompt,
                              target_content + evidence, prior_context)
        except (BudgetInfeasible, ExtractionFailed) as e:
            print(f"  ⚠ Retry failed ({e}); keeping the blocked result.", flush=True)
            return result

        if not detect_blockers(result):
            print(f"  ✓ {pass_key} unblocked by the evidence.", flush=True)
            return result

    print(f"  ⚠ {pass_key} is still blocked after {max_rounds} round(s) of evidence "
          f"({len(seen)} path(s) supplied).", flush=True)
    return result


# --- Survey protocol ----------------------------------------------------------
#
# The design pass had one way to see source code: block, and be shown the file it
# named. That is a whole 27B pass spent to ask a question, and it only ever fired
# after the pass had already failed to design.
#
# R17 says "survey before you design", and until now nothing could. The survey is
# that step, run before the design rather than after its failure: read the
# request, map it onto files, CHECK the mapping against the workspace, and put
# the verified source in the payload.
#
# Verification is the part that makes it more than retrieval. A model naming a
# plausible file is not evidence that the file contains what it claims; the
# check is deterministic, and a refuted claim is reported rather than dropped.
# "job-radar's scraping config is not in questions.ts" is a fact the design needs
# - it is the answer to the question the pass would otherwise have blocked on.
SURVEY_MAX_FILES = EVIDENCE_MAX_FILES
SURVEY_RESOLVE_MAX_TOKENS = 400

# A claim is `path::symbol`; the symbol half is optional because some requests
# turn on a file rather than a symbol in it ("the route that renders this page").
_SURVEY_CLAIM_RE = re.compile(r"^([\w./-]+\.[A-Za-z0-9]{1,5})(?:::([A-Za-z_]\w*))?$")

SurveyClaim = collections.namedtuple("SurveyClaim", ("path", "symbol", "verdict"))


def parse_survey_claims(raw: str) -> list:
    """Pull `path::symbol` claims out of a model's answer, in the order given."""
    claims, seen = [], set()
    for line in (raw or "").splitlines():
        candidate = line.strip().strip("-*`, \t")
        if not candidate or candidate.upper() == "NONE" or " " in candidate:
            continue
        match = _SURVEY_CLAIM_RE.match(candidate.lstrip("./"))
        if not match:
            continue
        key = (match.group(1), match.group(2))
        if key in seen:
            continue
        seen.add(key)
        claims.append(SurveyClaim(match.group(1), match.group(2), None))
    return claims


def verify_survey_claims(project_dir: str, claims: list) -> list:
    """
    Check each claim against the workspace. Returns claims with a verdict set.

    Three verdicts, and all three are facts worth carrying:
      VERIFIED - the file exists and defines the symbol.
      REFUTED  - the file exists and does not. The file is still read: it is
                 what settles the question, and withholding it would send the
                 pass back to blocking for the file it was just denied.
      ABSENT   - no such file. R10 answers "is X already implemented?" with this.
    """
    verified = []
    for claim in claims:
        full = os.path.join(project_dir, claim.path)
        if not os.path.isfile(full):
            verified.append(claim._replace(verdict="ABSENT"))
            continue
        if not claim.symbol:
            verified.append(claim._replace(verdict="VERIFIED"))
            continue
        try:
            with open(full, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            verified.append(claim._replace(verdict="ABSENT"))
            continue
        found = _find_definition(content, [claim.symbol]) is not None
        verified.append(claim._replace(verdict="VERIFIED" if found else "REFUTED"))
    return verified


def render_survey_findings(claims: list) -> str:
    """The verification report that heads the surveyed source."""
    lines = ["The request was mapped onto these files and every claim was checked "
             "against the workspace before this block was written."]
    for verdict, explain in (
        ("VERIFIED", "defined in that file"),
        ("REFUTED", "that file exists and does NOT define this - treat the "
                    "symbol as not existing there"),
        ("ABSENT", "no such file in the workspace - to be created, with nothing "
                   "existing to reconcile"),
    ):
        hits = [c for c in claims if c.verdict == verdict]
        if not hits:
            continue
        rendered = ", ".join(f"{c.path}::{c.symbol}" if c.symbol else c.path
                             for c in hits)
        lines.append(f"  {verdict} ({explain}): {rendered}")
    return "\n  ".join(lines)


def survey_codebase(client, model_config, instruction: str, symbol_index: str,
                    budget_chars: int, project_dir: str = "/workspace") -> str:
    """
    Locate, verify and read the files this request turns on. Returns a payload block.

    Returns "" when the survey finds nothing, which is not a failure: a vague
    request maps onto nothing in particular, and the design pass still has the
    symbol index, the call graph and - if it needs a specific file after all -
    the blocker protocol behind it.
    """
    if not instruction or not symbol_index or budget_chars <= 0:
        return ""

    system = (
        "You locate the code a change will touch. Given a change request and an "
        "index of every exported symbol in the repository, list the files whose "
        "CONTENTS the designer must read.\n"
        f"Output at most {SURVEY_MAX_FILES} lines, each `path::symbol` naming the "
        "one symbol that makes the file relevant, or a bare `path` when the file "
        "matters as a whole. Nothing else: no commentary, no bullets, no "
        "backticks. Output exactly NONE if the request touches no existing file.\n"
        "Name the file the behaviour would pass through, and the file that "
        "already does the nearest thing to it. A guess is checked against the "
        "workspace and reported as wrong, so name what you can point to."
    )
    user = ("### CHANGE REQUEST\n" + instruction +
            "\n\n### EXPORTED SYMBOL INDEX\n" + symbol_index +
            "\n\n### CURRENT TASK\nList the paths.\n")

    print("  🔍 Surveying the codebase for the files this request turns on...",
          flush=True)
    raw = _single_llm_call(client, model_config, system, user, "Codebase survey",
                           max_output_tokens=SURVEY_RESOLVE_MAX_TOKENS)
    if _check_llm_result(raw, "Codebase survey"):
        print("  ⚠ Survey call failed; the design pass proceeds on the indexes "
              "alone and may block.", flush=True)
        return ""

    claims = verify_survey_claims(project_dir, parse_survey_claims(raw))
    if not claims:
        print("  🔍 Survey named no file in the workspace; proceeding on the "
              "indexes alone.", flush=True)
        return ""

    for claim in claims:
        mark = {"VERIFIED": "✓", "REFUTED": "✗", "ABSENT": "∅"}[claim.verdict]
        target = f"{claim.path}::{claim.symbol}" if claim.symbol else claim.path
        print(f"     {mark} {claim.verdict:<8} {target}", flush=True)

    # A refuted claim is still read: the file is what settles the question, and
    # denying it sends the pass straight back to blocking for the same path.
    readable = [c.path for c in claims if c.verdict in ("VERIFIED", "REFUTED")]
    absent = [c.path for c in claims if c.verdict == "ABSENT"]
    hints = {}
    for claim in claims:
        if claim.symbol and claim.verdict == "VERIFIED":
            hints.setdefault(claim.path, []).append(claim.symbol)

    evidence = read_evidence(
        project_dir, list(dict.fromkeys(readable)), budget_chars, absent,
        symbol_hints=hints, tag="SURVEYED_SOURCE",
        preamble=render_survey_findings(claims),
    )
    return evidence.text


# --- Reproduction protocol ----------------------------------------------------
#
# A distillation pass is a stateless call with no tools. It cannot run anything,
# so "I have verified this bug is reproducible" is, from a pass, an opinion - the
# same opinion the test gate exists to stop the pipeline accepting about its own
# output. On a hard bug that is exactly where a model is least reliable.
#
# So the bugfix prompt does not assert reproducibility, it declares it: one
# COMMAND and one SIGNATURE (bugfix.md B5). This runs the command and looks for
# the signature. A diagnosis whose repro does not fail as described is not a
# diagnosis, and what actually happened goes back to the pass as evidence.
#
# The command is never handed to a shell. shlex + a first-token allowlist means
# the prohibition on pipes and redirection in B5 is enforced by the runner rather
# than trusted to the model - the pass names a program and its arguments, and
# that is the whole of what it can cause to happen.
REPRO_ALLOWED_BINARIES = frozenset({
    "npm", "npx", "node", "python", "python3", "pytest",
    "go", "cargo", "mvn", "gradle",
})

# Substrings that identify a broken build rather than a specific bug. A pass that
# offers one of these as its SIGNATURE has declared a repro that any failure at
# all would satisfy, which would verify a diagnosis at random.
REPRO_WEAK_SIGNATURES = frozenset({
    "error", "errors", "failed", "failure", "failures", "fail", "1 failed",
    "exception", "traceback", "assertionerror", "test failed", "tests failed",
    "non-zero", "exit 1", "false", "undefined", "null", "nan",
})
REPRO_MIN_SIGNATURE_CHARS = 8

# A runner that is not installed is not a reproduced bug. Same classification the
# build loop's test gate applies, for the same reason: it says nothing about the
# code, so treating it as a failing repro would confirm any diagnosis on a box
# that happens to be missing a dependency.
#
# The connection-refused clauses matter more than they look. This container
# reaches the host at the docker bridge address, and a database published as
# `127.0.0.1:5432` on the host is not listening there - so every DB-backed
# reproduction dies with ECONNREFUSED. Without these patterns that lands in the
# "failed, but not with the declared signature" branch, whose observation tells
# the pass its root cause is wrong and to diagnose something else. That is the
# one failure mode worse than not verifying: the harness actively steering a
# correct diagnosis off the causal path over an environment fault. Classified
# here it becomes "the environment could not run this", which is the truth.
#
# "no test files found" is the same class of lie: the runner started, matched
# nothing, and exited non-zero. It says the COMMAND's path or --root is wrong,
# not that the code is broken.
_REPRO_UNRUNNABLE_RE = re.compile(
    r"no module named|command not found|could not determine executable|"
    r"npm error|cannot find module|is not recognized as|no such file or directory|"
    r"econnrefused|connection refused|could not connect to server|"
    r"getaddrinfo|eai_again|no test files found",
    re.IGNORECASE,
)

# Horizontal whitespace only. `\s` would match the newline, so `- COMMAND:` with
# an empty value silently captured the SIGNATURE line below it - a malformed
# document that parsed cleanly into the wrong command.
_REPRO_COMMAND_RE = re.compile(r"^[ \t]*[-*]?[ \t]*COMMAND:[ \t]*([^\n]*)$",
                               re.MULTILINE | re.IGNORECASE)
_REPRO_SIGNATURE_RE = re.compile(r"^[ \t]*[-*]?[ \t]*SIGNATURE:[ \t]*([^\n]*)$",
                                 re.MULTILINE | re.IGNORECASE)

# `- COMMAND: `pytest -q`.` is a correct command wearing prose punctuation. The
# full stop sits outside the backticks, so it has to come off before they do.
_TRAILING_PROSE_RE = re.compile(r"([`*_])[ \t]*\.[ \t]*$")


def _unwrap_field(value: str) -> str:
    """Strip the markdown wrapper and trailing prose a model puts round a value."""
    value = value.strip()
    value = _TRAILING_PROSE_RE.sub(r"\1", value)
    for wrapper in ("`", "**", "*", "_"):
        span = 2 * len(wrapper)
        while len(value) > span and value.startswith(wrapper) and value.endswith(wrapper):
            value = value[len(wrapper):-len(wrapper)].strip()
    return value

# --- REPRO_FILE ---------------------------------------------------------------
#
# The gate above assumes the bug already has a failing test. Most do not.
#
# A green suite on a broken feature is the normal case, not a strange one: the
# suite encodes the fixtures its author wrote, and the defect is usually in the
# gap between those fixtures and what the real caller sends. So requiring
# section 2's COMMAND to fail against the UNMODIFIED tree - which is the only
# tree the harness has, since nothing is written before it runs - made the gate
# unsatisfiable for exactly the bugs it was built to catch. The pass could only
# block, or declare a command it hoped would fail, three times, and end
# UNVERIFIED. Both outcomes were read as "the diagnosis is bad" when what
# actually happened was "the project has no test for this yet".
#
# So the reproduction may carry the test it needs. The pass names a path and
# supplies a body; the harness writes it, runs COMMAND, and removes it again.
# Three properties make that safe to do to somebody's checkout:
#   - it refuses to overwrite anything that already exists, so no source file
#     can be replaced by a diagnosis;
#   - the path must resolve inside the project root, so `..` and absolute paths
#     cannot escape the workspace;
#   - removal is in a finally, so a timeout or a crash still leaves the tree as
#     it was found.
# The body stays in the saved document, so `!approve` can recreate it as the
# regression test B9 already asks for - the reproduction and the regression test
# are the same artefact, which is what they should have been all along.
_REPRO_FILE_RE = re.compile(r"^[ \t]*[-*]?[ \t]*REPRO_FILE:[ \t]*([^\n]*)$",
                            re.MULTILINE | re.IGNORECASE)
# The body is the first fenced block after the REPRO_FILE line. An info string
# (```ts) is common and ignored.
_FENCE_RE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```[ \t]*$",
                       re.MULTILINE | re.DOTALL)

# A test that asserts nothing cannot fail for the right reason. This is not a
# quality bar, it is a floor against a pass satisfying the gate with an empty
# file or a bare `throw` that would "fail" whatever the code did.
REPRO_FILE_MIN_CHARS = 40
REPRO_FILE_MAX_CHARS = 20000


def parse_repro_file(document: str):
    """
    Return (path, body) for a declared REPRO_FILE, or (None, reason_or_None).

    A reason of None means the document simply did not declare one, which is
    fine - an existing failing test is still the better reproduction. A non-None
    reason means it tried to and the declaration is unusable, which the caller
    reports back rather than silently ignoring.
    """
    match = _REPRO_FILE_RE.search(document or "")
    if not match:
        return None, None

    path = _unwrap_field(match.group(1))
    if not path:
        return None, "REPRO_FILE was declared with no path"

    fence = _FENCE_RE.search(document, match.end())
    if not fence:
        return None, (f"REPRO_FILE named '{path}' but no fenced code block follows "
                      f"it, so there is no file body to write")
    body = fence.group(1)
    if len(body.strip()) < REPRO_FILE_MIN_CHARS:
        return None, (f"REPRO_FILE '{path}' has a {len(body.strip())}-character body; "
                      f"too small to be a test that asserts anything")
    if len(body) > REPRO_FILE_MAX_CHARS:
        return None, (f"REPRO_FILE '{path}' is {len(body)} characters; a reproduction "
                      f"is one focused test, not a module")
    return (path, body), None


def validate_repro_file(path: str, cwd: str = "/workspace"):
    """Return None if the path is a safe, non-destructive place to write, else why not."""
    if os.path.isabs(path):
        return f"REPRO_FILE '{path}' is absolute; it must be relative to the project root"
    root = os.path.realpath(cwd)
    full = os.path.realpath(os.path.join(root, path))
    if full != root and not full.startswith(root + os.sep):
        return f"REPRO_FILE '{path}' resolves outside the project root"
    if os.path.exists(full):
        return (f"REPRO_FILE '{path}' already exists. A reproduction may not overwrite "
                f"a file in the project; name a new path, or run the existing test "
                f"instead of supplying one")
    parent = os.path.dirname(full)
    if parent and not os.path.isdir(parent):
        return f"REPRO_FILE '{path}' is in a directory that does not exist"
    return None


ReproResult = collections.namedtuple("ReproResult", ("verified", "reason", "observation"))

# Marks a bugfix document whose reproduction never failed as declared. Wears the
# same shape as ABORT_MARKER and is refused by load_saved_pass for the same
# reason: `!approve` must not build a fix for a bug nobody reproduced.
UNVERIFIED_MARKER = "## ⚠ UNVERIFIED — the declared reproduction did not fail as described"


def _normalise_output(text: str) -> str:
    """Collapse whitespace and case so a signature match survives reformatting."""
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def parse_reproduction(document: str):
    """
    Return (command, signature) from a bugfix document's section 2, or (None, reason).

    Tolerant of the markup a model wraps around a command - backticks, a bold
    label, a trailing full stop - because rejecting a correct command over a pair
    of backticks would burn a full pass to be told the same thing again.
    """
    cmd_match = _REPRO_COMMAND_RE.search(document or "")
    sig_match = _REPRO_SIGNATURE_RE.search(document or "")
    if not cmd_match:
        return None, "section 2 declared no COMMAND"
    if not sig_match:
        return None, "section 2 declared no SIGNATURE"

    command = _unwrap_field(cmd_match.group(1))
    signature = _unwrap_field(sig_match.group(1))
    if not command:
        return None, "COMMAND was empty"
    if not signature:
        return None, "SIGNATURE was empty"
    return (command, signature), None


def validate_reproduction(command: str, signature: str):
    """Return None if the declared repro is runnable and specific, else the reason."""
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return f"COMMAND is not a well-formed command line ({e})"
    if not argv:
        return "COMMAND was empty"
    binary = os.path.basename(argv[0])
    if binary not in REPRO_ALLOWED_BINARIES:
        return (f"COMMAND starts with '{binary}', which is not one of the allowed "
                f"runners: {', '.join(sorted(REPRO_ALLOWED_BINARIES))}")

    normalised = _normalise_output(signature)
    if len(normalised) < REPRO_MIN_SIGNATURE_CHARS:
        return (f"SIGNATURE '{signature}' is {len(normalised)} characters; too short "
                f"to identify one failure rather than any failure")
    if normalised in REPRO_WEAK_SIGNATURES:
        return (f"SIGNATURE '{signature}' matches any broken build, not this bug")
    return None


def run_reproduction(command: str, signature: str, timeout_secs: float,
                     cwd: str = "/workspace", repro_file=None) -> ReproResult:
    """
    Run the declared command in the workspace and decide whether the bug appeared.

    Verified means three things together: the command ran, it exited non-zero,
    and the declared signature is in its output. Any one alone is not enough - a
    non-zero exit from an uninstalled runner is the case this exists to reject.

    `repro_file` is an optional (path, body) the pass supplied because no
    existing test reproduces the bug. It is written before the command and
    removed after, always - see the REPRO_FILE note above.
    """
    argv = shlex.split(command)
    written = None
    if repro_file:
        rel, body = repro_file
        full = os.path.join(cwd, rel)
        try:
            with open(full, "w", encoding="utf-8") as f:
                f.write(body)
            written = full
            print(f"  📝 Wrote reproduction test {rel} ({len(body)} chars)", flush=True)
        except OSError as e:
            return ReproResult(False, f"could not write REPRO_FILE '{rel}' ({e})",
                               f"The reproduction test could not be written: {e}")
    try:
        return _run_reproduction_command(argv, command, signature, timeout_secs, cwd)
    finally:
        # Unconditional: a timeout, a signature miss and a clean verification all
        # leave the checkout exactly as it was found. The body survives in the
        # saved document, which is what `!approve` builds the regression test from.
        if written:
            try:
                os.remove(written)
                print(f"  🧹 Removed reproduction test {repro_file[0]}", flush=True)
            except OSError as e:
                print(f"  ⚠ Could not remove {repro_file[0]}: {e}", flush=True)


def _run_reproduction_command(argv, command: str, signature: str,
                              timeout_secs: float, cwd: str) -> ReproResult:
    """Run the command and classify the outcome. See run_reproduction."""
    print(f"  🔁 Reproduction: {command} (timeout {int(timeout_secs)}s)", flush=True)
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True,
            timeout=timeout_secs, check=False,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        exit_code = proc.returncode
    except FileNotFoundError:
        return ReproResult(False, f"'{argv[0]}' is not installed in this image",
                           f"The command could not start: '{argv[0]}' was not found.")
    except subprocess.TimeoutExpired as e:
        partial = ((e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        detail = partial.decode("utf-8", "replace") if isinstance(partial, bytes) else partial
        return ReproResult(False, f"timed out after {int(timeout_secs)}s",
                           f"The command did not finish within {int(timeout_secs)}s. "
                           f"Partial output:\n{detail[-2000:]}")
    except Exception as e:
        return ReproResult(False, f"could not run ({type(e).__name__}: {e})",
                           f"The command could not be run: {e}")

    tail = output[-4000:]
    if exit_code == 127 or (exit_code != 0 and _REPRO_UNRUNNABLE_RE.search(output)):
        return ReproResult(
            False, f"the runner could not execute (exit {exit_code})",
            f"The command exited {exit_code}, but the output shows a missing runner, "
            f"module, service or test file rather than the bug. This is an environment "
            f"or command fault and says NOTHING about the code — in particular it is "
            f"not evidence against your root cause, so do not change the diagnosis to "
            f"account for it. Fix the COMMAND so it runs: check the test runner's root "
            f"in a multi-package repo (`--root <dir>` or `npm --prefix <dir>`), and "
            f"prefer a reproduction that needs no database, server or network.\n"
            f"Output:\n{tail}",
        )
    if exit_code == 0:
        return ReproResult(
            False, "the command succeeded; the bug did not appear",
            f"The command exited 0. The behaviour you diagnosed did not occur. Either "
            f"the reproduction does not exercise it, or the root cause is wrong.\n"
            f"Output:\n{tail}",
        )
    if _normalise_output(signature) not in _normalise_output(output):
        return ReproResult(
            False, "it failed, but not with the declared signature",
            f"The command exited {exit_code}, so something failed, but "
            f"'{signature}' does not appear in its output. This is a different "
            f"failure from the one you diagnosed.\nOutput:\n{tail}",
        )
    return ReproResult(True, f"failed with the declared signature (exit {exit_code})", "")


def build_repro_observation(command: str, signature: str, attempt: int,
                            total: int, result: ReproResult) -> str:
    """Frame a failed reproduction as evidence for the next attempt."""
    return (
        "\n\n  <REPRO_OBSERVATION>\n"
        f"  Attempt {attempt} of {total}. The harness ran the reproduction you "
        "declared in section 2. It did not fail as you described, so the diagnosis "
        "is not yet supported by anything that happened.\n"
        f"  <COMMAND>{command}</COMMAND>\n"
        f"  <EXPECTED_SIGNATURE>{signature}</EXPECTED_SIGNATURE>\n"
        f"  <WHAT_HAPPENED>\n{result.observation}\n  </WHAT_HAPPENED>\n"
        "  Treat this as fact about the system, and read it before deciding what "
        "to change. If the command never really ran — a missing module, an "
        "unreachable database, no test files matched — then only section 2 is "
        "wrong; keep the diagnosis and fix the command, or supply a REPRO_FILE "
        "that needs no external service. If the command ran and the code behaved, "
        "the diagnosis is what is wrong; revise sections 3, 4 and 5, or emit "
        "BLOCKED naming what you need. Either way, do not emit the same COMMAND "
        "and the same root cause again.\n"
        "  </REPRO_OBSERVATION>\n"
    )


def verify_bugfix_reproduction(client, model_config, prompt: str, target_content: str,
                               prior_context: str, symbol_skeleton: str, result: str,
                               max_attempts: int, timeout_secs: float) -> tuple:
    """
    Loop the bugfix pass until its declared reproduction actually fails.

    Returns (document, verified). An unverified document is still returned - the
    diagnosis is often most of the way there and worth reading - but it carries
    UNVERIFIED_MARKER, which stops `!approve` building from it.
    """
    if max_attempts <= 0:
        print("  ⏭️  Reproduction check disabled (bugfix_max_repro_attempts=0); "
              "the diagnosis is self-reported.", flush=True)
        return result, True

    observations = ""
    for attempt in range(1, max_attempts + 1):
        if detect_blockers(result):
            # Blocked already, and evidence retry has had its rounds. There is no
            # section 2 to run and nothing a repro could add.
            return result, False

        parsed, reason = parse_reproduction(result)
        repro_file = None
        if parsed:
            command, signature = parsed
            reason = validate_reproduction(command, signature)
            if not reason:
                repro_file, file_reason = parse_repro_file(result)
                # A malformed REPRO_FILE is reported, not dropped. Running the
                # command without the test it was written for would fail for a
                # reason the pass never predicted, and the observation would send
                # it chasing that instead of fixing the declaration.
                reason = file_reason
                if repro_file and not reason:
                    reason = validate_repro_file(repro_file[0])
        else:
            command = signature = ""

        if reason:
            print(f"  ⚠ Attempt {attempt}/{max_attempts}: {reason}", flush=True)
            outcome = ReproResult(
                False, reason,
                f"The reproduction was rejected before it ran: {reason}",
            )
        else:
            update_status(f"Verifying reproduction ({attempt}/{max_attempts})")
            outcome = run_reproduction(command, signature, timeout_secs,
                                       repro_file=repro_file)
            if outcome.verified:
                print(f"  ✅ Reproduced: {outcome.reason}", flush=True)
                return result, True
            print(f"  ❌ Not reproduced: {outcome.reason}", flush=True)

        if attempt == max_attempts:
            break

        observations += build_repro_observation(
            command, signature, attempt, max_attempts, outcome
        )
        print(f"  ↻ Re-running bugfix with the observation attached "
              f"(attempt {attempt + 1}/{max_attempts})...", flush=True)
        try:
            result = call_llm(client, model_config, prompt,
                              target_content + observations, prior_context)
        except (BudgetInfeasible, ExtractionFailed) as e:
            print(f"  ⚠ Re-run failed ({e}); keeping the unverified diagnosis.",
                  flush=True)
            return result, False
        result = resolve_pass_blockers(
            client, "bugfix", model_config, prompt,
            target_content + observations, prior_context, symbol_skeleton, result,
            max_rounds=1,
        )

    print(f"  ⚠ The bug was not reproduced in {max_attempts} attempt(s). The "
          f"diagnosis is recorded but not verified.", flush=True)
    return result, False


def mark_unverified(document: str, attempts: int) -> str:
    """Prepend the refusal banner `!approve` reads before building anything."""
    return (
        f"{UNVERIFIED_MARKER}\n\n"
        f"The reproduction declared below was run {attempts} time(s) and never failed "
        f"in the way this diagnosis predicts. Nothing here has been confirmed against "
        f"running code.\n\n"
        f"- If the diagnosis is right, fix section 2 by hand, delete this banner, and "
        f"run `!approve`.\n"
        f"- Otherwise re-run `!bugfix` with a sharper symptom.\n"
        f"- `!approve` refuses while this banner is present: building from it would be "
        f"a fix for a bug nobody has seen happen.\n\n"
        f"---\n\n{document}"
    )


def _resolve_model_config(model_entry, default_host: str = None,
                          pass_key: str = None) -> dict:
    """
    Resolve a model entry from config into a normalized dict.
    Supports both legacy string format and new object format.

    `pass_key` is which phase this config is for. Sampling is per phase and the
    model entry does not carry it - two phases routinely share one model and want
    different sampling - so it is attached here from the `sampling` block. A
    caller with no phase in hand (residency checks, evictions) omits it and gets
    the default set, which those paths never send anywhere.
    """
    if default_host is None:
        default_host = OLLAMA_HOST
    if isinstance(model_entry, str):
        return {"model": model_entry, "provider": "ollama", "base_url": default_host,
                "sampling": sampling_for(pass_key)}
    return {
        "model": model_entry.get("model", ""),
        "provider": model_entry.get("provider", "ollama"),
        "base_url": model_entry.get("base_url", default_host),
        "args": model_entry.get("args", []),
        "reasoning": _validate_reasoning(model_entry.get("reasoning", DEFAULT_REASONING),
                                         model_entry.get("model", "")),
        "sampling": sampling_for(pass_key),
    }


def _validate_reasoning(value, model_name: str = "") -> str:
    """Normalise a pass's `reasoning` setting, or fail loudly on a typo."""
    level = str(value).strip().lower()
    if level == "high":
        level = "xhigh"  # the template's own alias; accept it rather than reject it
    if level not in REASONING_LEVELS:
        raise ValueError(
            f"Unknown reasoning level '{value}'"
            + (f" for model '{model_name}'" if model_name else "")
            + f". Supported: {', '.join(REASONING_LEVELS)} (or 'high', an alias for 'xhigh')."
        )
    return level


def _reasoning_spec(cfg: dict) -> tuple[str, int]:
    """
    (level, reserve_tokens) for a resolved model config.

    The reserve scales with the level, because the level is precisely a statement
    about how much the model will think. Zero when thinking is off, so a
    non-thinking pass keeps every token of the budget it had before this existed.
    """
    level = cfg.get("reasoning", DEFAULT_REASONING) if isinstance(cfg, dict) else DEFAULT_REASONING
    multiplier = REASONING_RESERVE_MULTIPLIER.get(level, 1.0)
    return level, int(REASONING_RESERVE_TOKENS * multiplier)


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


def preload_model(client: httpx.Client, model_config: dict) -> bool:
    """
    Make a model resident before it is asked to generate anything.

    Ollama loads a model on its first request, and with no prompt it loads and
    returns without generating. Doing that here moves eviction, the disk read and
    the KV-cache allocation out of the inference call's stall budget, which is
    sized for gaps between tokens rather than for an 18GB cold start.

    Best-effort: a failure here is not fatal, because the inference call will load
    the model itself. It just does so on a 45s clock instead of this one.
    """
    if not isinstance(model_config, dict):
        model_config = {"model": model_config, "provider": "ollama"}
    model_name = model_config.get("model", "")
    provider = model_config.get("provider", "ollama")
    base_url = model_config.get("base_url", OLLAMA_HOST)

    if provider != "ollama" or not model_name:
        # llama.cpp servers are started by the orchestrator, which already blocks
        # until the model is up; there is nothing to warm here.
        return False

    print(f"  ↳ Loading {model_name} into VRAM (up to {MODEL_LOAD_TIMEOUT:.0f}s)...",
          flush=True)
    done = threading.Event()

    def heartbeat(own_event=done):
        start = time.time()
        while not own_event.wait(10):
            print(f"      ↳ [Loading model... {int(time.time() - start)}s]", flush=True)

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()
    started = time.time()
    try:
        resp = client.post(
            f"{base_url}/api/generate",
            json={"model": model_name, "prompt": "", "stream": False},
            timeout=httpx.Timeout(MODEL_LOAD_TIMEOUT, connect=15.0),
        )
        if resp.status_code != 200:
            print(f"  ⚠ Preload returned {resp.status_code}; the inference call will "
                  f"load {model_name} on its own clock.", flush=True)
            return False
        print(f"  ✓ {model_name} resident ({time.time() - started:.1f}s)", flush=True)
        return True
    except Exception as e:
        print(f"  ⚠ Preload of {model_name} failed ({type(e).__name__}); the inference "
              f"call will load it on its own clock.", flush=True)
        return False
    finally:
        done.set()
        hb.join(timeout=11)


def _empty_pass_failure(pass_key: str, where: str) -> "ExtractionFailed":
    """
    The exception for a pass that ran cleanly and produced nothing.

    Distinct wording from a connectivity failure because it IS a distinct fault:
    the model answered, the answer was empty, and every generic remedy about
    unreachable endpoints points the operator away from the cause. See the
    "empty answer" note above _rescue_empty_answer.
    """
    return ExtractionFailed(
        f"{pass_key.title()} pass", [(where, "empty answer")], 1,
        summary=(
            f"**{pass_key}** returned an EMPTY document. The model was reachable and "
            "answered; it just produced no content — on the measured case, several "
            "thousand tokens on the thinking channel and zero on the content "
            "channel. It has already been re-run once with thinking off."),
        checks=[
            "Look for a `reasoning_*.md` trace beside this file. If the document "
            "was written without a closing `</think>`, the whole thing is in there.",
            "The prompt for this pass may simply be too large to answer — check the "
            "`Input:` line in the build log against the context window.",
            "`--reasoning-format deepseek` routes everything before `</think>` to "
            "the reasoning channel; a model that never closes the tag looks exactly "
            "like a model that said nothing.",
        ],
    )


def _abort_pass(pass_key: str, model_config, exc: "ExtractionFailed"):
    """Write the abort report and stop, before .clinerules is assembled."""
    update_status(f"Aborted: {exc}")
    _write_pass_failure(pass_key, model_config, exc)
    print(f"\n  ❌ {pass_key}: {exc}", flush=True)
    print("  ↳ Aborting before .clinerules is written; nothing was overwritten.",
          flush=True)
    raise SystemExit(2)


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
        exc.summary or
        (f"**{exc.stage}** failed: {len(exc.failures)} of {exc.total} call(s) to the "
         "model returned an error, so the extracted context would have been "
         "incomplete."),
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
    checks = exc.checks or [
        f"Is a server actually listening at `{endpoint}`?",
        "From inside this container, `localhost` is the container — host "
        "services need `host.docker.internal`.",
        "Check `llama-server.log` / `orchestrator.log` on the host for a "
        "model that died or was auto-unloaded mid-run.",
    ]
    lines += ["", "### What to check", ""]
    lines += [f"{i}. {c}" for i, c in enumerate(checks, 1)]
    lines += [
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

    if UNVERIFIED_MARKER in content:
        # A diagnosis whose reproduction never failed. Re-running is the right
        # answer rather than reusing it: the workspace may have changed, and the
        # verification loop gets another go at proving it. Deleting the banner by
        # hand is the documented way to say "I checked this myself".
        print(f"  ⚠ {path} holds an unverified diagnosis; re-running the pass.",
              flush=True)
        return None

    if detect_blockers(content):
        # Same reasoning one step further on. A blocked pass is a question, not a
        # design; reusing it would make every later run inherit the same dead end
        # without ever re-asking. Answer it in the file by hand and it resumes.
        print(f"  ⚠ {path} still reports blockers; re-running the pass. "
              f"(Edit the file to answer them and it will be reused as-is.)",
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

    # Reasoning shares the window with the prompt and the answer, so a thinking
    # pass solves its budget against a window shortened by the same reserve that
    # _single_llm_call() adds to the output cap. Off costs nothing.
    _reasoning, reserve = _reasoning_spec(model_config if isinstance(model_config, dict) else {})
    window = CONTEXT_WINDOW - reserve

    # A single call is merge-shaped: real system prompt, full prior context, full
    # answer budget. Size it against that, not against the extraction budget - a
    # payload can clear the chunk limit and still not fit here, which is how the
    # old single-call path overflowed on a tight window.
    single_prior = _context_block(
        "PREVIOUS ANALYSES", prior_context,
        "What earlier passes established.",
    ) if prior_context else ""
    single_fixed = est_tokens(system_prompt) + est_tokens(single_prior) + est_tokens("### CURRENT TASK\n")
    single_facts, single_answer = solve_merge_budget(window, single_fixed)

    # Chunk overhead is measured from the real assembled framing, with the ceiling
    # record cap (the longest prompt), so the solved chunk can only be conservative.
    steer = steering_extract(prior_context)
    chunk_fixed = (
        est_tokens(_extraction_system_prompt(EXTRACTION_RECORD_CAP))
        + est_tokens(_build_chunk_prompt(steer, new_request, "X" * 64, 99, 99, ""))
    )
    chunk_tokens, record_cap, extraction_tokens = solve_extraction_budget(window, chunk_fixed)

    chunks = chunk_text(user_content, slice_tokens(chunk_tokens))

    print(f"  ↳ Input: {len(user_content) + len(prior_context)} chars "
          f"(~{est_tokens(user_content) + est_tokens(prior_context)} tok). "
          f"Ctx {CONTEXT_WINDOW}, margin {safety_margin(window)}, "
          f"reasoning {_reasoning} (reserve {reserve}).", flush=True)

    # Chunk COUNT must not decide this. TARGET_CHUNK_SIZE is a latency ceiling on
    # how much one extraction call ingests, not a statement about what the window
    # holds - so a 15k-token payload against a 64k window was being split into two
    # chunks and sent through map-reduce for no reason. That path is lossy by
    # construction: the merge sees only the extractor's capped bullet records, so
    # any detail it failed to capture (the JSX the architect kept asking for) is
    # gone before the design pass starts. If the whole payload fits one
    # merge-shaped call, send it whole and skip extraction entirely.
    if est_tokens(user_content) <= single_facts:
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
    facts_budget, answer_tokens = solve_merge_budget(window, merge_fixed)
    print(f"    ↳ Merge budget: {facts_budget} tok facts / {answer_tokens} tok answer "
          f"(fixed {merge_fixed} tok)", flush=True)

    partial_results = _fit_facts_to_budget(
        client, model_config, partial_results, facts_budget, window
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
            return None, None, False, None
        message = chunk_data.get("message", {})
        return (
            message.get("content", ""),
            message.get("thinking", ""),
            chunk_data.get("done", False),
            chunk_data.get("done_reason"),
        )

    if not line.startswith("data: "):
        # Orchestrator heartbeats and non-data SSE events
        return None, None, False, None
    if line == "data: [DONE]":
        return None, None, True, None
    try:
        chunk_data = json.loads(line[6:])
        choices = chunk_data.get("choices", [{}])
        if not choices:
            return "", "", False, None
        delta = choices[0].get("delta", {})
        # The VALUE of finish_reason, not just its presence. "length" means the
        # server stopped because the cap was reached, not because the model had
        # finished - a truncated answer that looks exactly like a complete one to
        # everything downstream. Discarding it is how a document cut off before
        # section 2 came back as "section 2 declared no COMMAND".
        finish = choices[0].get("finish_reason")
        return (
            delta.get("content", ""),
            delta.get("reasoning_content", "") or delta.get("reasoning", ""),
            finish is not None,
            finish,
        )
    except Exception:
        # Malformed chunk or internal proxy metadata
        return None, None, False, None


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


def _report_truncation(label: str, finish_reason, output_cap: int,
                       content: list, reasoning: list, cfg: dict) -> None:
    """
    Say so when the server stopped because the cap was reached, not because the
    model was done.

    The symmetric hole to _check_prompt_budget. That one closed the loop on a
    prompt silently truncated on the way IN; this closes it on an answer silently
    truncated on the way OUT. Both used to surface only as a downstream parse
    failure describing the wrong thing — "section 2 declared no COMMAND" for a
    document the model never got to finish writing.

    The reasoning/content split is the whole diagnosis. A cap hit with a long
    answer wants a bigger ANSWER_MAX_TOKENS; a cap hit with almost no answer and
    thousands of reasoning tokens wants a lower thinking level, and no amount of
    answer budget will fix it.
    """
    if finish_reason != "length":
        return
    answer_tokens = est_tokens("".join(content))
    thought_tokens = est_tokens("".join(reasoning))
    level = cfg.get("reasoning", DEFAULT_REASONING) if isinstance(cfg, dict) else DEFAULT_REASONING
    print(f"\n      ⚠ TRUNCATED [{label}]: the server stopped at the {output_cap}-token "
          f"output cap, not because the model finished. The answer is cut off and "
          f"anything parsed from it is a fragment.", flush=True)
    print(f"        ↳ ~{thought_tokens} tokens of reasoning, ~{answer_tokens} of answer, "
          f"at reasoning '{level}'.", flush=True)
    if thought_tokens <= answer_tokens:
        print(f"        ↳ The answer itself hit the cap. Raise ANSWER_MAX_TOKENS, or "
              f"ask this pass for a shorter document. The continuation will finish "
              f"it either way.", flush=True)
    elif level in ("low", "off"):
        # The advice used to be "lower the reasoning level" unconditionally, which
        # is useless at the floor - and the floor is exactly where a model that
        # over-thinks a hard prompt lands you. reasoning_effort is a hint the
        # model may ignore; only max_tokens binds. Continuation is the answer.
        print(f"        ↳ Thinking outweighed the answer at '{level}', which is "
              f"already the lowest thinking level — reasoning_effort is a hint, "
              f"not a bound. Nothing in the config fixes this; the continuation "
              f"round exists for it and runs with thinking off.", flush=True)
    else:
        print(f"        ↳ Thinking outweighed the answer. Lower this pass's reasoning "
              f"level in agent_config.json (currently '{level}'); raising the answer "
              f"cap will not help.", flush=True)


def report_server_state(model_config) -> None:
    """
    Say what the inference server was actually doing when a call stalled.

    A stall with zero tokens is indistinguishable, from the client side, between
    "the model was never resident", "something evicted it mid-run" and "it was
    resident and merely slow". Those have different fixes and the log recorded
    none of them, which is how two plausible diagnoses survived a whole debugging
    session. Ask the server; it knows.
    """
    if not isinstance(model_config, dict):
        model_config = {"model": model_config, "provider": "ollama"}
    if model_config.get("provider", "ollama") != "ollama":
        return
    base_url = model_config.get("base_url", OLLAMA_HOST)
    wanted = model_config.get("model", "")
    try:
        with httpx.Client() as c:
            data = c.get(f"{base_url}/api/ps", timeout=10.0).json()
    except Exception as e:
        print(f"      ↳ [diagnostic] /api/ps unreachable ({type(e).__name__}); "
              f"the server itself may be down.", flush=True)
        return

    loaded = data.get("models") or []
    if not loaded:
        print("      ↳ [diagnostic] server reports NO model resident - the stall was "
              "a load, not slow generation.", flush=True)
        return
    for m in loaded:
        name = m.get("name") or m.get("model") or "?"
        vram = (m.get("size_vram") or 0) / 1e9
        ctx = m.get("context_length", "?")
        mark = "  <-- the one this call wanted" if wanted in (name, m.get("model")) else ""
        print(f"      ↳ [diagnostic] resident: {name} ctx={ctx} vram={vram:.1f}GB{mark}",
              flush=True)
    if not any(wanted in (m.get("name"), m.get("model")) for m in loaded):
        print(f"      ↳ [diagnostic] {wanted} is NOT resident - it was evicted or never "
              f"loaded. Check VRAM pressure against OLLAMA_MAX_LOADED_MODELS.", flush=True)


def _salvage_note(answer_tokens: list, reasoning_tokens: list) -> str:
    """Describe what we actually managed to keep, so failures are not silent."""
    if answer_tokens:
        return f"Salvaging {len(answer_tokens)} answer tokens."
    if reasoning_tokens:
        return (
            f"Got {len(reasoning_tokens)} reasoning tokens but ZERO answer tokens - "
            "the server ignored the thinking-disable request. Salvaging nothing."
        )
    return (
        "No tokens of any kind received - the server never started responding, so "
        f"this is a load or availability problem, not a slow generation. Check that "
        f"the model is resident (preload should have made it so within "
        f"{MODEL_LOAD_TIMEOUT:.0f}s) and that nothing evicted it in between. "
        "Salvaging nothing."
    )


# --- The empty answer -----------------------------------------------------------
#
# Measured on the engineer pass: 82,435 prompt tokens in, 5,810 tokens out, all
# of them on the reasoning channel, ZERO on the content channel, ending on a
# clean stop 2,382 tokens short of the cap. The pass reported "Complete (0
# chars)" and saved an empty intermediate that passes 3 and 4 then designed
# against.
#
# Three guards were the wrong shape for it. _report_truncation returns early
# unless finish_reason is "length", and this was a stop. The continuation round -
# which exists precisely to re-run with thinking off - was gated on "length" too,
# so a model that thought its whole turn away walked straight past the one
# mechanism built for it. And _check_llm_result only matches "[ERROR:", so ""
# is a valid result all the way to disk.
#
# The server runs with --reasoning-format deepseek, which routes everything
# before </think> into delta.reasoning_content. So an answer that was written but
# never had its think block closed is indistinguishable here from one that was
# never written - and in both cases the text is sitting in the reasoning channel
# that _stream_llm_once used to drop on the floor. Hence the trace file: the
# retry is the fix, but 5,810 tokens of thought is the only evidence of what went
# wrong, and it costs nothing to keep.

_reasoning_trace_seq = 0


def _write_reasoning_trace(label: str, reasoning: str) -> str:
    """
    Dump a call's thinking channel next to the intermediates. Returns the path.

    Only called when the answer came back empty, where the trace is the only
    record of the call. Empty string if there was nothing to write or the write
    failed - a diagnostic must never be the thing that kills the run.
    """
    if not reasoning.strip():
        return ""
    global _reasoning_trace_seq
    _reasoning_trace_seq += 1
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "call"
    path = os.path.join(INTERMEDIATE_DIR, f"reasoning_{slug}_{_reasoning_trace_seq}.md")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                f"# Reasoning trace: {label}\n\n"
                "The model produced this on the thinking channel and then returned an "
                "EMPTY answer. Kept because a document written without a closing "
                "`</think>` lands here in full, and because it is the only evidence "
                "of what the call was doing.\n\n"
                "This is a trace, not a result. Nothing downstream reads it.\n\n"
                "---\n\n"
                f"{reasoning}\n"
            )
        return path
    except Exception as e:
        print(f"      ⚠ Could not write reasoning trace: {e}", flush=True)
        return ""


def _rescue_empty_answer(client: httpx.Client, model_config, system_prompt: str,
                         user_content: str, label: str, max_output_tokens: int,
                         assistant_prefix: str, thoughts: str, finish) -> tuple:
    """
    Re-run a call that returned no answer, with thinking off. Returns (text, finish).

    The retry is the whole remedy: with enable_thinking=false the model has no
    reasoning channel to disappear into, so the same prompt that produced 5,810
    tokens of silence produces the document instead. It is deliberately not part
    of the continuation budget - this is a failed call being re-run, not a long
    answer being extended.

    Nothing is retried if the call was already running without thinking; there is
    no lever left to pull and a second identical call would just cost another
    prompt evaluation.
    """
    level, _ = _reasoning_spec(model_config if isinstance(model_config, dict) else {})
    thinking_was_off = level == "off" or bool(assistant_prefix)
    stopped = f"finish_reason={finish!r}"
    print(f"\n      ⚠ EMPTY ANSWER [{label}]: ~{est_tokens(thoughts)} tokens of "
          f"reasoning, 0 of answer, at reasoning '{level}' ({stopped}). The model "
          f"spent the turn on the thinking channel and never wrote to the content "
          f"channel.", flush=True)

    trace = _write_reasoning_trace(label, thoughts)
    if trace:
        print(f"        ↳ Thinking saved to {trace} — if the document was written "
              f"without closing `</think>`, it is in there.", flush=True)

    if thinking_was_off:
        print(f"        ↳ Thinking was already off for this call; nothing left to "
              f"retry with. Returning empty and letting the pass fail.", flush=True)
        return "", finish

    print(f"        ↳ Re-running once with thinking OFF.", flush=True)
    text, retry_finish, retry_thoughts = _stream_llm_once(
        client, model_config, system_prompt, user_content,
        f"{label} (no-think)", max_output_tokens,
        assistant_prefix=assistant_prefix, force_no_thinking=True,
    )
    if _looks_like_llm_error(text):
        print(f"      ✗ Thinking-off retry errored ({text[:60]}).", flush=True)
        return text, retry_finish
    if not text.strip():
        retry_trace = _write_reasoning_trace(f"{label} no-think", retry_thoughts)
        if retry_trace:
            print(f"        ↳ Retry trace: {retry_trace}", flush=True)
        print(f"      ✗ Still empty with thinking off. This is not a reasoning-level "
              f"problem — the prompt itself is not producing an answer.", flush=True)
        return "", retry_finish
    print(f"      ✓ Thinking-off retry produced {len(text)} characters.", flush=True)
    return text, retry_finish


# --- Continuation --------------------------------------------------------------
#
# `reasoning_effort` is a hint, not a bound. Measured on the engineer pass at
# level "low": ~13,300 tokens of thinking and ~1,390 of answer, against a 12,288
# cap. The document was cut off after a page and a half, saved, and handed
# downstream as though it were finished - and there was no lower level left to
# drop to. The only hard bound on this model is max_tokens.
#
# So truncation is handled rather than prevented. The partial answer goes back as
# an assistant turn and the model is asked to resume from exactly where it
# stopped, WITH THINKING OFF: the reasoning has already happened, and repeating
# it would spend the whole budget again and return a second fragment. That turns
# a cap-length round into ~12k tokens of pure answer.
#
# Bounded, because a model that will not stop is a worse failure than a short
# document, and each round costs a full prompt evaluation.
LLM_MAX_CONTINUATIONS = 3

CONTINUE_INSTRUCTION = (
    "Your previous message was cut off mid-flow because it hit the output limit. "
    "It was not finished.\n"
    "Continue it from EXACTLY where it stops, and nothing else:\n"
    "- Do not repeat any text you have already written.\n"
    "- Do not restart, re-introduce, summarise or apologise.\n"
    "- Do not add a preamble such as 'continuing' — your first character is the "
    "next character of the document.\n"
    "- Resume mid-word or mid-sentence if that is where it ended.\n"
    "- Keep the same format and heading structure, and finish the document."
)

# How far back to look for the model repeating itself at the seam. Generous
# enough to catch a restated heading or paragraph, small enough that a document
# which legitimately repeats a line is not silently cut.
CONTINUATION_OVERLAP_WINDOW = 600

# Shortest repeat worth removing. The two ways to get this wrong are not
# symmetric: too high and a restated heading survives into the middle of the
# document, which is ugly but visible; too low and legitimately repeated text is
# eaten, which is silent corruption. So it sits just under a typical heading -
# "# 6. Fix Plan" is 13 characters, and an earlier floor of 20 let exactly that
# through. For a false strip the continuation would have to open with the same
# dozen characters the previous chunk closed on, which is a repeat, not a
# coincidence.
CONTINUATION_MIN_OVERLAP = 12


def _join_continuation(prev: str, nxt: str) -> str:
    """
    Append `nxt` to `prev`, dropping any text the model repeated at the seam.

    Instructed not to repeat itself, a model usually complies and sometimes
    restates the last line or heading anyway. Concatenating blindly leaves a
    duplicated fragment in the middle of the document, which is both wrong and
    hard to spot in a 4,000-character plan.
    """
    if not prev:
        return nxt
    if not nxt:
        return prev
    window = prev[-CONTINUATION_OVERLAP_WINDOW:]
    for size in range(min(len(window), len(nxt)), CONTINUATION_MIN_OVERLAP - 1, -1):
        if nxt.startswith(window[-size:]):
            return prev + nxt[size:]
    return prev + nxt


def _single_llm_call(client: httpx.Client, model_config, system_prompt: str, user_content: str,
                     label: str = "Inference", max_output_tokens: int = ANSWER_MAX_TOKENS) -> str:
    """
    One logical model call, continued across the output cap if it truncates.

    Returns the whole answer as a string, so every caller is unchanged. See the
    note above for why continuation exists rather than a bigger cap or a lower
    thinking level.
    """
    answer, finish = "", None
    for round_no in range(LLM_MAX_CONTINUATIONS + 1):
        text, finish, thoughts = _stream_llm_once(
            client, model_config, system_prompt, user_content,
            label if not answer else f"{label} (cont.{round_no})",
            max_output_tokens,
            assistant_prefix=answer,
            force_no_thinking=bool(answer),
        )
        if not text.strip() and not _looks_like_llm_error(text):
            # Not gated on finish_reason. A cap-length round with an empty answer
            # is the same failure as a clean stop with an empty answer, and the
            # continuation below would have re-run it WITH thinking on (its
            # force_no_thinking is bool(answer), which is False while the answer
            # is empty) - thinking away a second budget to return a second blank.
            text, finish = _rescue_empty_answer(
                client, model_config, system_prompt, user_content,
                label if not answer else f"{label} (cont.{round_no})",
                max_output_tokens, answer, thoughts, finish,
            )
        if _looks_like_llm_error(text):
            # A failed continuation must not discard a good partial: what we
            # already have is strictly better than an error string.
            if answer:
                print(f"      ⚠ Continuation {round_no} failed ({text[:60]}); keeping "
                      f"the {len(answer)}-character partial answer.", flush=True)
                return answer
            return text

        if not text.strip():
            # _rescue_empty_answer has already re-run this round with thinking
            # off and still got nothing. Falling through would loop and re-run it
            # a third time with thinking back ON. An empty first round returns
            # "" for the pass-level check to abort on; an empty continuation
            # returns the partial, which is everything there is.
            return answer

        answer = _join_continuation(answer, text)
        if finish != "length":
            return answer
        if round_no == LLM_MAX_CONTINUATIONS:
            break
        print(f"      ↩ Answer hit the output cap; continuing it with thinking off "
              f"(round {round_no + 1}/{LLM_MAX_CONTINUATIONS}, {len(answer)} chars so far)...",
              flush=True)

    print(f"      ⚠ Still incomplete after {LLM_MAX_CONTINUATIONS} continuation(s); "
          f"returning {len(answer)} characters. Downstream will see a partial document.",
          flush=True)
    return answer


def _stream_llm_once(client: httpx.Client, model_config, system_prompt: str, user_content: str,
                     label: str = "Inference", max_output_tokens: int = ANSWER_MAX_TOKENS,
                     assistant_prefix: str = "", force_no_thinking: bool = False) -> tuple:
    """
    Execute a single LLM API call with streaming for live feedback.
    Supports both Ollama native and OpenAI-compatible streaming formats.

    Returns (text, finish_reason, reasoning). A finish_reason of "length" means
    the server stopped at the cap rather than because the model was done, which
    is what _single_llm_call continues from. Error paths return (message, None,
    ...) — an error is not something to continue.

    `reasoning` is the thinking channel, returned rather than dropped because an
    empty answer beside 5,000 tokens of thought is a specific, recoverable
    failure and the thought is the only evidence of what the model was doing.
    See _rescue_empty_answer().

    Args:
        model_config: Either a string (model name, Ollama) or a dict with provider info.
        max_output_tokens: Hard cap on generated tokens. Extraction passes want a
            tight cap; the merge pass needs room for the full templated answer.
        assistant_prefix: A partial answer to resume. Sent as an assistant turn
            so the model sees what it already wrote.
        force_no_thinking: Suppress reasoning regardless of the pass's configured
            level. Used for continuations, where the thinking has already been
            done and repeating it would consume the budget a second time.
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

    # Reasoning effort is per pass. The reserve is added to the cap here and
    # subtracted from the window in call_llm(), so the two stay in step: without
    # the addition the pass would spend its answer allowance on thought, and
    # without the subtraction the larger cap would overrun the window.
    reasoning, reserve = _reasoning_spec(cfg)
    # Sampling is settled at config-resolution time and travels on the config, so
    # a continuation samples exactly as the call it is continuing did.
    sampling = cfg.get("sampling") or sampling_for(None)
    if force_no_thinking:
        # A continuation inherits the reasoning the first call already did. Left
        # on, it would spend the whole cap thinking again and return another
        # fragment - which is exactly the loop this exists to break.
        reasoning, reserve = "off", 0
    output_cap = max_output_tokens + reserve

    def _messages() -> list:
        msgs = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        if assistant_prefix:
            msgs.append({"role": "assistant", "content": assistant_prefix})
            msgs.append({"role": "user", "content": CONTINUE_INSTRUCTION})
        return msgs

    if is_ollama:
        payload = {
            "model": model_name,
            "messages": _messages(),
            "stream": True,
            # Ollama's think flag is a boolean for most models - the effort
            # levels are a llama.cpp/template concept and do not cross over, so
            # anything other than "off" is simply "on" here.
            "think": reasoning != "off",
            "options": {
                "num_ctx": CONTEXT_WINDOW,
                **sampling_payload(sampling),
                "num_predict": output_cap,
            },
            "keep_alive": "3m"
        }
        url = f"{base_url}/api/chat"
    else:
        payload = {
            "model": model_name,
            "messages": _messages(),
            "stream": True,
            **sampling_payload(sampling),
            "max_tokens": output_cap,
            # llama.cpp honours template kwargs; ignored harmlessly by servers that
            # don't. Off is enable_thinking=false rather than a reasoning_effort
            # value - the template raises on any effort outside xhigh/medium/low.
            "chat_template_kwargs": (
                {"enable_thinking": False} if reasoning == "off"
                else {"reasoning_effort": reasoning}
            ),
            # Makes the server report its real prompt token count, which is what
            # _check_prompt_budget verifies the estimate against.
            "stream_options": {"include_usage": True},
        }
        url = f"{base_url}/v1/chat/completions"

    # What the budget solver assumed this call would cost. Measured the same way
    # here as there, so a mismatch against the server points at the estimator.
    estimated_prompt_tokens = est_tokens(system_prompt) + est_tokens(user_content)

    max_retries = 3
    # Bound outside the loop: the error returns below have to name what the
    # thinking channel produced, and an exception raised before the per-attempt
    # reset would otherwise leave these unbound.
    full_response = []
    reasoning_response = []
    for attempt in range(max_retries):
        first_token_received = threading.Event()

        # `own_event` binds the Event object into the thread rather than closing
        # over the name. The name is rebound at the top of the next attempt, and a
        # closure would follow it: the previous heartbeat would start polling the
        # NEW attempt's unset Event, never observe its own set(), and run for the
        # life of the process. Two stalls meant two threads printing two unrelated
        # elapsed counters into the same stream.
        def heartbeat(own_event=first_token_received):
            start_wait = time.time()
            while not own_event.wait(5):
                elapsed = int(time.time() - start_wait)
                print(f"      ↳ [Waiting for LLM... {elapsed}s]", flush=True)

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()

        try:
            full_response = []
            reasoning_response = []
            finish_reason = None
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
                            return (f"[ERROR: LLM returned status {resp.status_code}]",
                                    None, "".join(reasoning_response))
                        
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

                            token, reasoning, done, finish = _extract_delta(line, is_ollama)
                            if finish:
                                finish_reason = finish
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
                                         estimated_prompt_tokens, output_cap)
                    _report_truncation(label, finish_reason, output_cap,
                                       full_response, reasoning_response, cfg)
                    return ("".join(full_response), finish_reason,
                            "".join(reasoning_response))

                except httpx.ReadTimeout:
                    first_token_received.set()
                    salvaged = "".join(full_response)
                    if salvaged:
                        # The prompt and sampler settings are unchanged, so a retry
                        # reproduces the same stall and throws away this partial on
                        # the way. Keep what the model actually produced.
                        print(f"\n      ✗ [Stability Protocol] Stream stalled ({STALL_TIMEOUT:.0f}s idle). "
                              f"{_salvage_note(full_response, reasoning_response)} Not retrying - identical prompt.")
                        return salvaged, None, "".join(reasoning_response)
                    if attempt < max_retries - 1:
                        print(f"\n      ⚠ [Stability Protocol] Stalled with no output. Retrying part ({attempt+2}/{max_retries})...")
                        report_server_state(model_config)
                        continue
                    print(f"\n      ✗ [Stability Protocol] Stalled on FINAL ATTEMPT. {_salvage_note(full_response, reasoning_response)}")
                    report_server_state(model_config)
                    return "[ERROR: ReadTimeout]", None, "".join(reasoning_response)

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
            return f"[ERROR: {e}]", None, "".join(reasoning_response)
        except Exception as e:
            first_token_received.set()
            print(f"\n  ❌ Unexpected error: {e}")
            return f"[ERROR: {e}]", None, "".join(reasoning_response)
        finally:
            # Belt and braces for the paths that return or continue without
            # setting it, so no attempt can ever outlive itself and print over
            # the next one. Runs on the success return too, which is correct.
            first_token_received.set()
            heartbeat_thread.join(timeout=6)

    return "[ERROR: Max retries exceeded]", None, "".join(reasoning_response)


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


def select_relevant_kb(kb_dir: str, instruction: str, max_chars: int = KB_MAX_CHARS) -> str:
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


# What PROJECT_OVERVIEW may cost. The README was injected whole: on a live
# workspace that was 62KB - 20855 tokens, 17% of the entire window - to tell a
# design pass what the project is. Most of it is installation steps, badges,
# contribution guidance and changelog: prose written for a human arriving at the
# repository, not facts a pass can design against.
README_MAX_CHARS = 6000

# The digest is written back to the workspace so the selection is auditable
# after the run, and so a human can see exactly what the architect was told the
# project is. Same directory as every other pipeline intermediate.
README_DIGEST_PATH = ".cline_context/.readme_digest.md"

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)


def split_markdown_sections(text: str) -> list:
    """
    Split Markdown into (heading, block) pairs, preamble first.

    The preamble - everything above the first heading - comes back under an
    empty heading. It is where a README says what the project IS, so it is the
    one part the selector never scores away.
    """
    matches = list(_MD_HEADING_RE.finditer(text))
    if not matches:
        return [("", text.strip())]
    sections = []
    if matches[0].start() > 0:
        preamble = text[:matches[0].start()].strip()
        if preamble:
            sections.append(("", preamble))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((match.group(2).strip(), text[match.start():end].strip()))
    return sections


def distill_readme(content: str, instruction: str,
                   max_chars: int = README_MAX_CHARS,
                   store_to: str = None) -> str:
    """
    Reduce a README to the sections that bear on NEW_REQUEST.

    Located, extracted, stored: sections are scored against the request the same
    way select_relevant_kb scores files, the ones that earn their place are kept
    whole, and the result is written to README_DIGEST_PATH so the selection can
    be read back after the run.

    Two rules the scoring does not get to overrule. The preamble is always kept -
    a pass that does not know what the project is cannot design for it - and
    every dropped heading is still listed. A section named but not included is a
    fact the pass can block on and have answered; a section deleted without trace
    is one it will never know to ask about.
    """
    content = (content or "").strip()
    if not content or len(content) <= max_chars:
        return content

    raw_words = re.findall(r"[a-zA-Z0-9_]+", (instruction or "").lower())
    keywords = {w for w in raw_words if len(w) >= 3 and w not in STOP_WORDS}

    sections = split_markdown_sections(content)
    kept, omitted, used = [], [], 0

    # The trailer is reserved before anything is spent, not appended after. A
    # README with 52 headings produces ~2k characters of roll-up on its own, and
    # adding that to a budget already spent put the digest 35% over its cap - the
    # same unbounded-footer shape as the skeleton's bare-file list.
    # Floored as well as fractioned: a quarter of a small cap is less than the
    # opening text itself, which spent the whole trailer on punctuation and
    # listed none of the headings it exists to name.
    trailer_cap = min(max_chars // 2, max(200, max_chars // 4))
    body_budget = max_chars - trailer_cap

    # The top of the file first and unconditionally, trimmed only if it alone
    # overruns. It is what the project IS, and it is never scored away - whether
    # it sits above the first heading or under the H1 title, which is why this
    # takes section zero rather than only an empty-heading preamble. A README
    # opening with "# Project\n\nA thing that does things" has no preamble by
    # that stricter test, so the one section saying what the project is scored 0
    # against the request and was dropped.
    if sections:
        preamble = sections.pop(0)[1]
        if len(preamble) > body_budget // 2:
            preamble = preamble[:body_budget // 2].rsplit("\n", 1)[0]
        kept.append(preamble)
        used += len(preamble)

    scored = []
    for index, (heading, block) in enumerate(sections):
        lowered_heading, lowered_block = heading.lower(), block.lower()
        score = sum(10 for kw in keywords if kw in lowered_heading)
        score += sum(3 for kw in keywords if kw in lowered_block)
        # index keeps the sort stable and, among equals, keeps README order -
        # which is the author's own ordering by importance.
        scored.append((-score, index, heading, block))
    scored.sort()

    for negative_score, _index, heading, block in scored:
        if negative_score < 0 and used + len(block) + 2 <= body_budget:
            kept.append(block)
            used += len(block) + 2
        else:
            omitted.append(heading or "(preamble)")

    digest = "\n\n".join(kept)
    if omitted:
        # Terse deliberately. Every character of framing here is a heading the
        # trailer cannot name, and the heading is the part that is actionable.
        opening = "\n\n<!-- README sections omitted as not bearing on NEW_REQUEST: "
        listed, spent = [], len(opening) + len(" -->")
        for heading in omitted:
            if spent + len(heading) + 2 > trailer_cap:
                break
            listed.append(heading)
            spent += len(heading) + 2
        trailer = "; ".join(listed)
        if len(listed) < len(omitted):
            trailer += f"; +{len(omitted) - len(listed)} more"
        digest += opening + trailer + " -->"

    print(f"  📄 README digest: {len(content)} → {len(digest)} chars "
          f"(cap {max_chars}); kept {len(kept)} section(s), listed {len(omitted)} "
          f"by heading", flush=True)

    if store_to:
        try:
            os.makedirs(os.path.dirname(store_to), exist_ok=True)
            with open(store_to, "w", encoding="utf-8") as f:
                f.write(f"<!-- Extracted from README.md for: {instruction[:200]!r} -->\n\n")
                f.write(digest + "\n")
        except Exception as e:
            # Storing is for the human reading the run afterwards. Failing to
            # store must never cost the pass the digest it is about to be given.
            print(f"  ⚠ Could not store the README digest ({e}); continuing.", flush=True)

    return digest


# npm writes this into `scripts.test` when nothing is configured. Treating it as
# a real suite would make the gate fail every project that never set one up.
_NPM_TEST_PLACEHOLDER = "no test specified"


# How deep to look for a package that tests itself. One level only: a workspace
# keeps its packages as immediate children, and walking further turns every
# vendored example and fixture project into part of the completion gate.
SIBLING_TEST_MAX = 4


def _detect_sibling_test_commands(project_dir: str) -> list:
    """
    Test commands for immediate subdirectories that are their own package.

    Returns commands runnable from the project root, because the gate runs one
    shell line from there. `npm --prefix <dir> run test` is the portable way to
    say that without a `cd`.
    """
    cmds = []
    try:
        entries = sorted(os.listdir(project_dir))
    except OSError:
        return cmds
    for entry in entries:
        if entry.startswith(".") or entry in SKELETON_SKIP_DIRS:
            continue
        sub = os.path.join(project_dir, entry)
        if not os.path.isdir(sub):
            continue
        manifest_path = os.path.join(sub, "package.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            continue
        script = str(manifest.get("scripts", {}).get("test", "")).strip()
        if script and _NPM_TEST_PLACEHOLDER not in script.lower():
            cmds.append(f"npm --prefix {entry} run test --silent")
        if len(cmds) >= SIBLING_TEST_MAX:
            break
    return cmds


def detect_test_command(project_dir: str) -> str:
    """
    The command that decides whether this project's tests pass, or "" if none.

    Separate from detect_project_toolchain, which produces prose for a model to
    read ("Test Runner: jest/vitest"). This has to be runnable by a shell, so it
    resolves the ambiguity: a package.json test script is the project's own
    answer to how it is tested, and beats any guess made from a marker file.

    Returns "" freely. A project with no suite is not a project that should be
    blocked from completing - it is one whose completion gate degrades to what
    it was before, which the caller reports rather than hides.
    """
    def has(*names) -> bool:
        return any(os.path.exists(os.path.join(project_dir, n)) for n in names)

    pkg = os.path.join(project_dir, "package.json")
    if os.path.isfile(pkg):
        deps = {}
        try:
            with open(pkg, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            scripts = manifest.get("scripts", {})
            deps = {**manifest.get("dependencies", {}),
                    **manifest.get("devDependencies", {})}
            test_script = str(scripts.get("test", "")).strip()
            if test_script and _NPM_TEST_PLACEHOLDER not in test_script.lower():
                return "npm test --silent"
        except Exception:
            pass

        # No `test` script is not the same as no tests. The repo this was built
        # against ships vitest.config.ts, a tests/ directory and vitest in
        # devDependencies, and simply never wired up the npm alias - so a
        # manifest-only check found nothing to gate on the one project that most
        # needed gating. Fall through to the runner's own config and deps.
        #
        # Playwright is deliberately not used here even when present: it drives a
        # real browser against a running server, which is a slow and flaky signal
        # for a completion gate rather than a cheap and decisive one.
        root_cmd = ""
        if has("vitest.config.ts", "vitest.config.js", "vitest.config.mts") or "vitest" in deps:
            root_cmd = "npx --no-install vitest run --reporter=dot"
        elif has("jest.config.ts", "jest.config.js", "jest.config.mjs") or "jest" in deps:
            root_cmd = "npx --no-install jest --ci"

        # A root runner is not necessarily the whole suite. veriform-ui keeps its
        # API in Backend/ with its own package.json and vitest.config.ts, while
        # the root config includes only `src/**` - so the gate ran 168 frontend
        # tests, went green, and declared a build complete with all 188 backend
        # tests failing on a migration that had never been applied. It was not
        # lying; it could not see them.
        #
        # Only reached when the root has no `test` script. A project that names
        # its own test command has already answered this question, and second-
        # guessing it would run somebody's suite twice.
        sibling_cmds = _detect_sibling_test_commands(project_dir)
        if root_cmd and sibling_cmds:
            return " && ".join([root_cmd] + sibling_cmds)
        if sibling_cmds:
            return " && ".join(sibling_cmds)
        if root_cmd:
            return root_cmd

    if os.path.isfile(os.path.join(project_dir, "Cargo.toml")):
        return "cargo test --quiet"
    if os.path.isfile(os.path.join(project_dir, "go.mod")):
        return "go test ./..."
    if os.path.isfile(os.path.join(project_dir, "pom.xml")):
        return "mvn -q test"
    if any(os.path.isfile(os.path.join(project_dir, m))
           for m in ("build.gradle", "build.gradle.kts")):
        return "gradle test --quiet"

    # Python has no manifest key for this, so go by what is there to collect. A
    # manifest is not required: a directory of test_*.py files is a test suite
    # whether or not anyone wrote a pyproject.toml.
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in SKELETON_SKIP_DIRS]
        if any(f.startswith("test_") and f.endswith(".py") for f in files):
            return "python3 -m pytest -q"
    return ""


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
# `export const DEFAULT_VOICE_QUESTIONS: VoiceQuestion[] = [...]`. A data
# constant is not a function and not a keyword declaration, so neither pattern
# above saw it and the file scanned as having no exported surface at all - it
# fell into the bare roll-up, where the architect learned only that it existed.
#
# That is the file the measured run blocked on. Question banks, route manifests,
# default datasets, config tables and enum-like objects are all declared this
# way, and they are exactly the shapes architect.md R21 requires legal values
# for. A skeleton that cannot see them cannot answer VALUES for any of them.
#
# Anchored at column zero, deliberately: the module's own surface, not every
# `const x = 5` inside a function body.
CONST_RE = re.compile(
    rf"^{_SYM_MODIFIERS}(?:const|let|var)\s+(?P<name>[A-Za-z0-9_]+)\s*(?=[:=])",
    re.MULTILINE,
)
IMPORT_RE = re.compile(
    r"^\s*(?:import\s+.+|from\s+\S+\s+import\s+.+|#include\s+.+|require\(.+\))",
    re.MULTILINE,
)

# 30000 chars - 7.5k tokens - was set when PROJECT_HISTORY and the README were
# taking 101300 tokens between them and there was nothing left to give. With
# those capped the payload runs at ~17k tokens against a 110635-token facts
# budget, so the map the design pass navigates by is the right place to spend it.
#
# Measured on a 1597-file workspace, this cap buys 487 of 837 entries at the
# exported-names tier. It does NOT buy signatures: the tier that carries them
# needs 248693 chars, and every cap below that lands in the overflow path where
# a symbol is a bare name. R20 turns a bare name into a blocker, so raising this
# reduces how often the architect blocks without removing the cause. 250000 is
# the number that removes it, at 83k tokens of payload.
MAX_SKELETON_CHARS = 90000

# How far past a symbol's name to read looking for its parameters, and how much
# of what is found to keep. The scan has to outrun a wrapped declaration - four
# parameters one per line is ~150 characters before the return type - while the
# rendered cap keeps one baroque generic from crowding out a whole file.
_SIG_SCAN_CHARS = 400
_SIG_MAX_CHARS = 110
SKELETON_SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "__pycache__",
                      "dist", "build", "public", ".knowledge_base",
                      ".cline_context", ".cline_logs"}
SKELETON_EXTS = (".py", ".ts", ".js", ".tsx", ".jsx", ".go", ".rs", ".java",
                 ".c", ".cpp", ".h")

# Configuration and data files are listed by path and never parsed. A design
# pass cannot reason about a file it does not know exists - the run that asked
# for `config.yaml` could only name it because the directory tree happened to
# show it, and the survey, which sees the symbol index and not the tree, could
# not have named it at all. Listing costs 614 characters on a real workspace;
# indexing their contents would cost the window and teach nothing, because a
# config file has no exported surface to index.
CONFIG_EXTS = (".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf",
               ".xml", ".properties")

# Never listed, because a path a pass can see is a path it can request.
#
# Lockfiles are excluded for budget: package-lock.json is 200KB+ here, and a
# request for one spends EVIDENCE_MAX_FILE_CHARS to learn nothing a design uses.
# Anything .env is excluded for a different reason entirely - it holds secrets,
# and the evidence read puts whatever it is given into a payload that leaves the
# machine. Neither belongs in front of a model.
CONFIG_EXCLUDE_NAMES = {"package-lock.json", "bun.lock", "yarn.lock",
                        "pnpm-lock.yaml", "composer.lock", "poetry.lock",
                        "cargo.lock", "Cargo.lock"}


def _skip_dir(name: str) -> bool:
    """
    One definition of which directories are not part of the project.

    Shared by the symbol walk and the config listing. Two walks with two filters
    is two chances for them to disagree about what the project contains, and the
    hidden-directory rule is exactly the sort that gets applied to one and not
    the other - see scan_project_files for what that cost the last time.
    """
    return name in SKELETON_SKIP_DIRS or name.startswith(".")


def is_listable_config(name: str) -> bool:
    """True for a config file worth naming to a design pass."""
    if name.startswith(".env") or name in CONFIG_EXCLUDE_NAMES:
        return False
    return name.endswith(CONFIG_EXTS)


def list_config_files(project_dir: str) -> list:
    """Project-relative paths of configuration and data files. Names only."""
    found = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if not _skip_dir(d)]
        for name in sorted(files):
            if is_listable_config(name):
                found.append(os.path.relpath(os.path.join(root, name), project_dir))
    return sorted(found)

# Languages with no export keyword, where the public surface has to be read from
# indentation instead: a declaration at module level, not underscore-prefixed by
# convention. Without this a Python project has no exported symbols at all, so
# every signature lands in `internal` and none of them is ever rendered.
#
# Only these. In TypeScript a top-level `function` with no `export` is private on
# purpose, and promoting it would offer the architect a symbol the module does
# not export - `pub` and `public` already cover Rust and Java through _SYM_MODIFIERS.
IMPLICIT_EXPORT_EXTS = (".py",)


def _signature_tail(content: str, name_end: int, stop_at_assign: bool = False) -> str:
    """
    The declaration that follows a symbol's name: parameters and return type.

    Read from the source rather than captured by SIGNATURE_RE/ARROW_RE, so the
    patterns that decide *which* symbols exist keep matching exactly what they
    matched before - a skeleton that gained signatures by losing symbols would be
    a bad trade. Scanning also handles the two shapes a capture group cannot:
    a declaration wrapped across lines, where stopping at the newline yields a
    bare "(" and teaches nothing, and an arrow function, whose parameters sit
    inside the matched region rather than after it.

    Starts at the end of the name, so both patterns are read the same way, and
    stops at the first token that ends a declaration at depth zero - the body
    brace, the statement end, or the line. Braces and newlines inside the
    parameter list are depth-protected; a stray bracket inside a string literal
    is not, which is what the character cap is for.
    """
    depth, out = 0, []
    for ch in content[name_end:name_end + _SIG_SCAN_CHARS]:
        if ch in "([<":
            depth += 1
        elif ch in ")]>":
            depth = max(depth - 1, 0)
        elif depth == 0 and ch in "{;\n":
            break
        elif depth == 0 and stop_at_assign and ch == "=":
            # A constant's declared type is its interface; its value is data.
            # Without this the scan follows `= [` into the array literal and
            # spends _SIG_SCAN_CHARS transcribing a question bank into the
            # skeleton - the one place in the payload with no room for it.
            break
        out.append(" " if ch in "\t\r" else ch)
    # "=", ":" and "=>" are the joins between a name and its value; each is the
    # last thing left when the value itself was a brace we stopped at.
    sig = re.sub(r"\s+", " ", "".join(out)).strip().rstrip("=:,").strip()
    if sig.endswith("=>"):
        sig = sig[:-2].strip()
    # A wrapped parameter list arrives as "( a: string, b?: number, )" once its
    # newlines are spaces. Close it back up, trailing comma included.
    sig = re.sub(r"\(\s+", "(", sig)
    sig = re.sub(r",?\s+\)", ")", sig)
    if len(sig) > _SIG_MAX_CHARS:
        sig = sig[:_SIG_MAX_CHARS].rstrip() + "..."
    return sig


def _scan_symbols(content: str, ext: str = ""):
    """
    Split a file's declarations into (exported, internal), preserving order.

    Each entry is (name, signature). The signature is what makes the difference
    between the architect knowing a symbol exists and knowing whether it already
    does the job - a name alone supports "there is an upsertRole", never "it
    takes a role and returns nothing", so a design pass reading names has no way
    to reuse a symbol and reaches for a new one beside it.

    `ext` decides how the public surface is recognised; see IMPLICIT_EXPORT_EXTS.
    """
    exported, internal = [], []
    seen = set()
    implicit = ext in IMPLICIT_EXPORT_EXTS
    # CONST_RE runs last so a `const` that IS a function is claimed by ARROW_RE
    # first and keeps its parameter list; `seen` makes the precedence stick.
    for pattern, is_const in ((SIGNATURE_RE, False), (ARROW_RE, False), (CONST_RE, True)):
        for m in pattern.finditer(content):
            name = m.group("name")
            if name in seen:
                continue
            seen.add(name)
            mods = m.group("mods") or ""
            public = "export" in mods or "pub" in mods
            if implicit and not public:
                # `^\s*` can consume the newlines before a declaration, so read
                # the indentation off the last line of the match, not the first.
                indent = m.group(0).rpartition("\n")[2]
                public = not indent[:1].isspace() and not name.startswith("_")
            if public:
                exported.append(
                    (name, _signature_tail(content, m.end("name"), is_const))
                )
            else:
                # No tier renders an internal signature, so none is scanned for.
                internal.append((name, ""))
    return exported, internal


def _sym_text(sym, with_signature: bool) -> str:
    """Render one scanned symbol. Accepts a bare name for callers that have one."""
    if isinstance(sym, str):
        return sym
    name, sig = sym
    if not (with_signature and sig):
        return name
    # "f(a: string)" and "Card: React.FC" keep the name flush against what
    # follows; "Role = a | b" and "Foo extends Bar" need the space back that
    # normalising took off.
    return name + ("" if sig.startswith(("(", "<", "[", ":")) else " ") + sig


# Detail tiers, richest first. get_symbol_skeleton emits the first one that fits
# MAX_SKELETON_CHARS, so the order is a statement about what a design pass can
# least afford to lose. Signatures outrank imports and internal helpers: the
# reuse rules in architect.md (R18, R20) are answerable from an exported
# signature and unanswerable from a bare name, and a pass that cannot answer
# them either blocks - costing a full re-run of a 27B model - or guesses.
SkeletonDetail = collections.namedtuple(
    "SkeletonDetail", ("note", "imports", "signatures", "internal")
)
SKELETON_TIERS = (
    SkeletonDetail("", True, True, True),
    SkeletonDetail("(exported signatures only - imports and internal helpers "
                   "omitted for size)", False, True, False),
    SkeletonDetail("(symbol names only - signatures omitted for size)",
                   True, False, True),
    SkeletonDetail("(exported symbol names only - imports, signatures and "
                   "internal helpers omitted for size)", False, False, False),
)


def _skeleton_block(rel_path: str, line_count: int, imports: list,
                    exported: list, internal: list, detail: SkeletonDetail) -> str:
    """
    Render one file's entry at the given detail tier.

    The exported surface is the only part every tier keeps: it is what a design
    pass has to name to reuse anything.
    """
    block = [f"\n{rel_path} ({line_count} lines)"]
    if detail.imports and imports:
        block.append("  imports:")
        for imp in imports[:5]:
            block.append(f"    {imp.strip()}")
        if len(imports) > 5:
            block.append(f"    ... +{len(imports) - 5} more")
    if exported:
        block.append("  exports:")
        block.extend(f"    - {_sym_text(s, detail.signatures)}" for s in exported)
    if detail.internal and internal:
        block.append("  internal:")
        # Names only, at every tier. Internal helpers are navigation, not reuse
        # surface - nothing outside the file may call one - so their signatures
        # would spend the cap that the exported ones have to fit inside.
        block.extend(f"    - {_sym_text(s, False)}" for s in internal)
    return "\n".join(block)


BARE_FILES_HEADING = "\nfiles with no symbols at this detail level (present, listed for navigation):"


def _render_skeleton(files_data: list, detail: SkeletonDetail) -> tuple[list, str]:
    """
    Render every file, splitting out the ones that carry no detail.

    In the exports-only tier a file whose declarations are all internal renders
    as a bare `path (N lines)` header - measured at 110 of 255 entries, 23% of
    the skeleton, spent on three lines each to say a file exists. Those collapse
    into one comma-separated roll-up.

    They are listed rather than dropped: prune_tree_against_skeleton removes
    from the directory tree everything the skeleton covers, so a file that fell
    out of both would disappear from the payload entirely.
    """
    blocks, bare = [], []
    for rel_path, line_count, imports, exported, internal in files_data:
        block = _skeleton_block(rel_path, line_count, imports, exported, internal, detail)
        if "\n" in block.strip():
            blocks.append(block)
        else:
            bare.append(rel_path)
    footer = f"{BARE_FILES_HEADING}\n{', '.join(bare)}\n" if bare else ""
    return blocks, footer


# Most of the overflow cap the bare roll-up may take before detail starts losing.
# Navigation degrades gracefully - a partial file list still navigates, and the
# directory tree carries the rest - while detail does not: an exported signature
# the pass never sees is a fact it can only block on or invent.
BARE_FOOTER_CAP_FRACTION = 0.25


def _fit_bare_footer(footer: str, cap: int) -> str:
    """
    Trim the bare-file roll-up to `cap` characters, on a path boundary.

    The roll-up is one comma-separated line naming every file that rendered no
    symbols at the current tier, and it was unbounded. On a 1596-file workspace
    it measured 50355 characters - 1.7x the whole 30000-char skeleton cap - and
    get_symbol_skeleton's overflow path seeded its running total with it before
    fitting a single entry. `total + len(block) > MAX_SKELETON_CHARS` was
    therefore true on the FIRST block, the loop broke immediately, and the
    skeleton went out as "0 of 837 detailed entries shown": 837 bare filenames,
    not one exported symbol anywhere in the project.

    That is the input the architect was handed before it blocked on
    `src/features/voice-profile/questions.ts`. Under R20 it had no other move -
    the file was in the roll-up, so it knew the file existed and nothing else.
    A blocker that names a real file is the correct response to this skeleton,
    which is why the failure looked like a model problem and was not one.
    """
    if not footer or len(footer) <= cap:
        return footer
    heading = BARE_FILES_HEADING + "\n"
    if cap <= len(heading):
        return ""
    paths = [p.strip() for p in footer[len(heading):].split(",") if p.strip()]
    # Reserved against the full count so the reservation can only over-reserve,
    # the same defensive sizing read_evidence uses for its truncation marker.
    marker = "\n... [+{} more files not listed]\n".format(len(paths))
    kept, used = [], len(heading) + len(marker)
    for path in paths:
        if used + len(path) + 2 > cap:
            break
        kept.append(path)
        used += len(path) + 2
    if not kept:
        return ""
    out = heading + ", ".join(kept) + "\n"
    omitted = len(paths) - len(kept)
    if omitted:
        out += f"... [+{omitted} more files not listed]\n"
    return out


def skeleton_paths(skeleton: str) -> set:
    """
    Every project-relative path the skeleton accounts for.

    Covers both renderings: the per-file entries and the bare roll-up footer.
    """
    paths = set(re.findall(r"^(\S+) \(\d+ lines\)$", skeleton, re.MULTILINE))
    footer = skeleton.split(BARE_FILES_HEADING.strip())
    if len(footer) > 1:
        paths.update(p.strip() for p in footer[-1].split(",") if p.strip())
    return paths


# tree(1) pads with non-breaking spaces, not plain ones, so both are accepted
# here - matching only U+0020 silently reconstructs nothing and prunes nothing.
_TREE_LINE_RE = re.compile("^((?:[\u2502 \u00a0][ \u00a0]{3})*)(?:[\u251c\u2514]\u2500\u2500 )(.+)$")


def prune_tree_against_skeleton(tree_output: str, covered: set) -> str:
    """
    Drop from the directory tree every file the symbol skeleton already names.

    The two blocks were assembled independently and overlap almost completely -
    94% of the tree's code files also appear in the skeleton, with their full
    relative paths - so the tree was spending ~3k tokens restating them.

    Paths are reconstructed from tree's indentation rather than matched on
    basename, so `src/a/index.ts` is never dropped because `src/b/index.ts`
    happens to be in the skeleton. Directory lines always survive: the shape of
    the tree is the part the skeleton does not carry.
    """
    if not covered:
        return tree_output

    stack, kept, dropped = [], [], 0
    for line in tree_output.splitlines():
        m = _TREE_LINE_RE.match(line)
        if not m:
            kept.append(line)
            continue
        depth = len(m.group(1)) // 4
        del stack[depth:]
        stack.append(m.group(2).strip())
        if "/".join(stack) in covered:
            dropped += 1
            continue
        kept.append(line)

    if dropped:
        kept.append(f"\n[{dropped} source files omitted here - "
                    f"they appear in SYMBOL_INDEX with full paths]")
    return "\n".join(kept)


def scan_project_files(project_dir: str) -> list:
    """
    Walk the project once and return (path, lines, imports, exported, internal).

    Extracted so the three blocks built from it - the symbol index, the call
    graph and the skeleton - share one traversal and one definition of what
    counts as a project file. Three walks with three filters is three chances
    for them to disagree about which files exist.
    """
    files_data = []
    for root, dirs, files in os.walk(project_dir):
        # Hidden directories are skipped, which is not a tidiness rule.
        # DIRECTORY_STRUCTURE comes from tree(1) without -a, so it never shows
        # them; a skeleton that walks them names files the payload's own
        # structure block says do not exist, and prune_tree_against_skeleton can
        # never match one.
        #
        # What that cost, measured on a live workspace: `.claude/worktrees/` held
        # two full checkouts of the project, so 1142 of 1597 scanned files - 71%
        # of the skeleton - were second and third copies of the same code. Tier 1
        # cost 248693 chars and did not fit any affordable cap; the real project
        # costs 57636 and fits with room to spare.
        #
        # The duplication was not merely wasteful. architect.md R14 requires a
        # name that resolves twice to be path-qualified before it can be cited,
        # and every symbol in the project resolved three times. The design pass
        # was being asked to disambiguate its own workspace against itself.
        dirs[:] = [d for d in dirs if not _skip_dir(d)]
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
            exported, internal = _scan_symbols(content, os.path.splitext(file)[1])
            if imports or exported or internal:
                files_data.append((rel_path, content.count("\n") + 1,
                                   imports, exported, internal))
    return files_data


# --- The three blocks a design pass reads the codebase through --------------
#
# One general-purpose skeleton was carrying every rule at once and serving none
# of them well. The rules want different things, so they get different blocks:
#
#   R18 (no duplicate symbol) needs a NEGATIVE - "nothing already does this" -
#       which only an exhaustive list of exported names can support. Signatures
#       are irrelevant to a negative; coverage is everything.
#   R19 (all call sites) needs a reverse-dependency query. Similarity search
#       cannot answer it: a caller may import a file for reasons that share no
#       vocabulary with the request. It is computed, not retrieved.
#   R17 (prior art) and the design facts themselves need real source, but only
#       for the handful of files the request actually passes through - which is
#       what the survey identifies and verifies.
#
# The signature skeleton was a hedge against not knowing which files mattered.
# Once the survey knows, the hedge is replaced by the code itself.

# Module specifier inside an import line: the quoted part of `from "@/x/y"`.
_IMPORT_SPEC_RE = re.compile(r"""['"]([^'"\n]+)['"]""")

# Extensions stripped when matching a specifier to a file on disk. TypeScript
# and friends import `./questions`, never `./questions.ts`.
_MODULE_EXT_RE = re.compile(r"\.(ts|tsx|js|jsx|mjs|cjs|py|go|rs|java)$")


def build_symbol_index(files_data: list, config_files: list = None) -> str:
    """
    Every file, every exported name, no signatures. The R18 authority.

    Deliberately the leanest tier rather than the richest that fits: this block
    exists to make an exhaustive claim, and an exhaustive claim it cannot afford
    is worth less than a cheap one it can. Measured on a 458-file workspace at
    14775 tokens against 22768 for the same files with signatures - and the
    signatures are supplied, in full and from source, for the files the survey
    identifies.
    """
    blocks, footer = _render_skeleton(files_data, SKELETON_TIERS[-1])
    header = ("[EXPORTED SYMBOL INDEX]\nEvery file in the project and every name "
              "it exports. COMPLETE: a name absent here is exported nowhere. "
              "Names only - for a symbol's shape, read it in SURVEYED_SOURCE or "
              "ask for the file.")
    body = "\n".join([header] + blocks + ([footer] if footer else []))
    if config_files:
        # Listed, never parsed. Their contents are available on request like any
        # other file; what was missing was any way to know they were there.
        body += ("\n\nConfiguration and data files (contents NOT indexed - a "
                 "config file has no exported surface. Name one by path if its "
                 "format or values matter to the design):\n"
                 + ", ".join(config_files) + "\n")
    print(f"  🗂️  Symbol index: {len(files_data)} files, "
          f"{len(config_files or [])} config file(s) listed, {len(body)} chars",
          flush=True)
    return body


def resolve_import_target(spec: str, by_stem: dict) -> str:
    """Map one import specifier onto a project file, or None if it leaves the project."""
    candidate = _MODULE_EXT_RE.sub("", spec.strip())
    candidate = candidate.lstrip("@").lstrip("./").lstrip("/")
    if not candidate:
        return None
    for form in (candidate, f"src/{candidate}",
                 f"{candidate}/index", f"src/{candidate}/index"):
        if form in by_stem:
            return by_stem[form]
    return None


def build_call_graph(files_data: list) -> str:
    """
    Who imports each file. The R19 authority.

    R19 requires a CALLERS bullet for every [MODIFIED] file, naming its other
    consumers "from CONTEXT". The tier the skeleton actually shipped renders no
    imports at all, so that fact was not in CONTEXT and never had been: the pass
    could satisfy R19 only by asserting "sole call site" with nothing behind it.
    An uncounted consumer is, in R19's own words, the next defect.

    Computed from the import lines already collected, so it is exact rather than
    inferred, and exhaustive rather than ranked. Files nothing imports are listed
    together: "nothing imports this" is the answer R19 wants for a leaf, and it
    is not the same answer as "not mentioned".
    """
    by_stem = {}
    for rel_path, _lines, _imports, _exported, _internal in files_data:
        by_stem[_MODULE_EXT_RE.sub("", rel_path)] = rel_path

    callers = {}
    for rel_path, _lines, imports, _exported, _internal in files_data:
        for line in imports:
            match = _IMPORT_SPEC_RE.search(line)
            if not match:
                continue
            target = resolve_import_target(match.group(1), by_stem)
            # Self-imports say nothing and a file is not its own call site.
            if target and target != rel_path:
                callers.setdefault(target, set()).add(rel_path)

    lines = [f"{path} <- {', '.join(sorted(callers[path]))}"
             for path in sorted(callers)]
    uncalled = sorted(p for p, *_ in files_data if p not in callers)

    header = ("[CALL GRAPH]\nWho imports each file, computed from its import "
              "statements. COMPLETE for imports this project resolves; an "
              "external package or a dynamic import resolves to nothing and is "
              "absent. Read `a <- b, c` as: changing a changes b and c.")
    body = "\n".join([header] + lines)
    if uncalled:
        body += ("\n\nNo project file imports these (entry points, routes, "
                 "configs and leaves):\n" + ", ".join(uncalled) + "\n")
    print(f"  🔗 Call graph: {len(callers)} imported file(s), "
          f"{len(uncalled)} with no project importer, {len(body)} chars", flush=True)
    return body


def get_symbol_skeleton(project_dir: str) -> str:
    """
    Build a navigable map of the project's declarations.

    Tiered under the size cap: emit the richest of SKELETON_TIERS that fits, from
    the full map down to bare exported names. Truncating mid-walk - as this used
    to - drops whole files off the end of the directory walk, so the architect
    silently never learns that, say, engagement-card.tsx exists. Shedding detail
    before shedding files keeps every file represented.
    """
    files_data = scan_project_files(project_dir)

    for detail in SKELETON_TIERS:
        blocks, footer = _render_skeleton(files_data, detail)
        total = sum(len(b) for b in blocks) + len(footer)
        if total <= MAX_SKELETON_CHARS:
            header = "[PROJECT SYMBOL SKELETON]"
            if detail.note:
                header += f"\n{detail.note}"
            print(f"  🦴 Skeleton: {len(files_data)} files, {total} chars, "
                  f"signatures {'on' if detail.signatures else 'OFF'}"
                  f"{' — ' + detail.note if detail.note else ''}", flush=True)
            return "\n".join([header] + blocks + ([footer] if footer else []))

    # Even the leanest tier overflows: keep as many whole files as fit, and say
    # how many were dropped rather than trailing off mid-walk.
    blocks, footer = _render_skeleton(files_data, SKELETON_TIERS[-1])
    # Fitted, not seeded. An unbounded roll-up spent the entire cap before the
    # first entry was considered - see _fit_bare_footer.
    footer = _fit_bare_footer(footer, int(MAX_SKELETON_CHARS * BARE_FOOTER_CAP_FRACTION))
    skeleton, total, kept = ["[PROJECT SYMBOL SKELETON]"], len(footer), 0
    for block in blocks:
        if total + len(block) > MAX_SKELETON_CHARS:
            break
        skeleton.append(block)
        total += len(block)
        kept += 1
    skeleton.append(f"\n... [Skeleton truncated: {kept} of {len(blocks)} detailed entries shown]")
    if footer:
        skeleton.append(footer)
    print(f"  🦴 Skeleton: {len(files_data)} files, {total} chars, OVERFLOW tier — "
          f"{kept} of {len(blocks)} entries carry detail, roll-up trimmed to "
          f"{len(footer)} chars", flush=True)
    return "\n".join(skeleton)


# Chat commands that launch a pipeline run. Any of them can appear alone or with
# a real instruction attached; only the attached text is a design request.
TRIGGER_COMMANDS = ("!build", "!architect", "!bugfix", "!approve", "!review")
_TRIGGER_SYNTAX_RE = re.compile(
    r"!build|!architect|!bugfix|!approve|!review|--repo\s+\S+|--kb\s+\S+|--open",
    flags=re.IGNORECASE,
)


def _is_trigger_message(content: str) -> bool:
    lowered = (content or "").lower()
    return any(cmd in lowered for cmd in TRIGGER_COMMANDS)


def strip_trigger_syntax(content: str) -> str:
    """Return what the user actually said, with command tokens and flags removed."""
    return _TRIGGER_SYNTAX_RE.sub("", content or "").strip()


def run_distillation():
    """Execute the 4-pass distillation pipeline."""
    print("=" * 60, flush=True)
    print("🧠 Multi-Pass Context Distillation Engine", flush=True)
    print("=" * 60, flush=True)

    config = load_config()
    _resolve_context_window(config)
    _resolve_sampling(config)
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
    # Set in the rebuild branch only. The blocker-resolution retry maps blockers
    # onto files through it, so a fresh build - which has no code to read - leaves
    # it empty and skips the retry rather than raising.
    symbol_skeleton = ""
    
    if is_rebuild:
        status_text = "ALREADY PARTIALLY IMPLEMENTED" if not has_git else "EXISTING REPOSITORY DETECTED"
        print(f"\n🔄 {status_text} for {PROJECT_NAME}: Using structured context and latest instruction.", flush=True)
        try:
            tree_output = subprocess.check_output(
                ["tree", "/workspace", "-I", "node_modules|.git|venv|.venv|.cline_context|.cline_logs|__pycache__|dist|build|public|.knowledge_base"], 
                text=True, stderr=subprocess.DEVNULL
            )
        except Exception:
            tree_output = "(Could not generate directory tree)"
            
        # One walk, three blocks - see build_symbol_index / build_call_graph.
        # symbol_skeleton is the index: it is what the survey and the blocker
        # protocol map a request or a blocker onto files through, and names are
        # all either needs to do that.
        project_files = scan_project_files("/workspace")
        symbol_index = build_symbol_index(project_files,
                                          list_config_files("/workspace"))
        call_graph = build_call_graph(project_files)
        symbol_skeleton = symbol_index
        tree_output = prune_tree_against_skeleton(
            tree_output, skeleton_paths(symbol_index)
        )
        toolchain_info = detect_project_toolchain("/workspace")
        
        latest_instruction = ""
        user_directives = ""
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not _is_trigger_message(content):
                continue
            directives = strip_trigger_syntax(content)
            if not directives:
                # A bare trigger (`!approve`, `!build` with no text) carries no
                # design intent. Handing it over as NEW_REQUEST is how the
                # architect ends up blocking on "this is a build directive, not a
                # design request", so keep walking back to the real instruction.
                continue
            latest_instruction = content
            user_directives = f"\n  <USER_DIRECTIVES>\n{directives}\n  </USER_DIRECTIVES>\n"
            break

        if not latest_instruction:
            # No trigger message carried text: fall back to the most recent user
            # message that says something, skipping bare commands.
            for msg in reversed(messages):
                if msg.get("role") != "user":
                    continue
                content = msg.get("content", "")
                if strip_trigger_syntax(content):
                    latest_instruction = content
                    break

        # Located and extracted against the request, not injected whole - see
        # distill_readme. The digest is stored back into the workspace so the
        # selection is auditable next to the pass output that used it.
        readme_content = distill_readme(
            read_workspace_file("README.md"), latest_instruction,
            store_to=os.path.join("/workspace", README_DIGEST_PATH),
        )
        issues_content = read_workspace_file(".cline_context/.build_issues.md")

        # The eight directives above are written for a design request. A bug
        # report is not one: the same instruction to "fulfil the NEW_REQUEST"
        # reads as licence to improve whatever the symptom touches. One line
        # re-points them, rather than forking the whole block for two words.
        bugfix_directive = (
            "    9. [P0] DIAGNOSIS: NEW_REQUEST is a bug report, not a feature. "
            "Find the one defect causing it and change nothing else. Improvements "
            "you notice are out of scope by definition.\n"
            if DISTILL_DESIGN_PASS == "bugfix" else ""
        )
        
        # The KB is selected last, once everything it competes with has been
        # measured - see solve_kb_budget. A placeholder holds its position so the
        # block still lands between PROJECT_HISTORY and PROJECT_OVERVIEW.
        conversation_text = (
            f"<SITUATIONAL_AWARENESS>\n"
            f"  <MODE>ITERATIVE_REBUILD</MODE>\n"
            f"  <STATUS>This project is ALREADY PARTIALLY IMPLEMENTED. Use the provided DIRECTORY_STRUCTURE, SYMBOL_INDEX, CALL_GRAPH and SURVEYED_SOURCE to understand the current state.</STATUS>\n"
            f"  <DIRECTIVES>\n"
            f"    1. [P0] PRESERVATION: Prioritize building on top of existing code. Maintain the current file organization and design idioms. Rework is strictly prohibited.\n"
            f"    2. [P0] CONTINUITY: Read the 'PROJECT_HISTORY' to pick up exactly where the last agent left off.\n"
            f"    3. [P0] NAVIGATION: SYMBOL_INDEX is the complete list of exported names - a name absent there is exported nowhere. CALL_GRAPH is the complete list of importers. SURVEYED_SOURCE, when present, is real source for the files this request turns on.\n"
            f"    4. [P0] ANALYZE: Carefully examine the 'DIRECTORY_STRUCTURE', 'SYMBOL_INDEX', 'CALL_GRAPH', 'SURVEYED_SOURCE' and 'PROJECT_OVERVIEW' blocks below before planning any code changes.\n"
            f"    5. [P0] NON-REDUNDANT_PLANNING: DO NOT plan for or recreate files that already exist in the structure unless the 'NEW_REQUEST' explicitly requires a logic change in them.\n"
            f"    6. [P0] FILE_STATUS_AWARENESS: If the 'ARCHITECTURE' section (developed by the architect) mentions a file that is NOT present in the 'DIRECTORY_STRUCTURE', it is a NEW component. You MUST create it.\n"
            f"    7. [P0] CONTEXT_ALIGNMENT: Use the 'PROJECT_HISTORY' to understand the intent and reasoning behind the current request.\n"
            f"    8. [P1] SCOPE_FOCUS: Focus exclusively on fulfilling the 'NEW_REQUEST' and resolving the 'KNOWN_BUILD_ISSUES'.\n"
            f"{bugfix_directive}"
            f"  </DIRECTIVES>\n"
            f"{user_directives}"
            f"</SITUATIONAL_AWARENESS>\n\n"
            f"<PROJECT_DATA>\n"
            f"  <NAME>{PROJECT_NAME}</NAME>\n"
            f"  <PROJECT_HISTORY>\n"
            f"{conversation_to_text(messages[:-1], HISTORY_MAX_MESSAGES)}\n"
            f"  </PROJECT_HISTORY>\n\n"
        )
        
        conversation_text += KB_PLACEHOLDER

        if readme_content:
            conversation_text += f"  <PROJECT_OVERVIEW>\n```markdown\n{readme_content}\n```\n  </PROJECT_OVERVIEW>\n\n"
            
        if issues_content:
            conversation_text += f"  <KNOWN_BUILD_ISSUES>\n```markdown\n{issues_content}\n```\n  </KNOWN_BUILD_ISSUES>\n\n"

        conversation_text += (
            f"  <DIRECTORY_STRUCTURE>\n```\n{tree_output}\n```\n  </DIRECTORY_STRUCTURE>\n\n"
            f"  <SYMBOL_INDEX>\n{symbol_index}\n  </SYMBOL_INDEX>\n\n"
            f"  <CALL_GRAPH>\n{call_graph}\n  </CALL_GRAPH>\n"
            f"{SURVEY_PLACEHOLDER}\n"
        )
        if toolchain_info:
            conversation_text += f"  {toolchain_info}\n\n"
        conversation_text += (
            f"  <NEW_REQUEST>\n{latest_instruction}\n  </NEW_REQUEST>\n"
            f"</PROJECT_DATA>"
        )

        # Everything else is now assembled and measurable, so the KB can be given
        # exactly the room that is left rather than a fixed 100k characters.
        kb_dir = "/workspace/.knowledge_base"
        kb_block = ""
        if os.path.exists(kb_dir):
            payload_tokens = est_tokens(conversation_text.replace(KB_PLACEHOLDER, ""))
            system_tokens = max(
                (est_tokens(prompts.get(k, "")) for k in (DISTILL_DESIGN_PASS, "engineer")),
                default=0,
            )
            kb_budget = solve_kb_budget(CONTEXT_WINDOW, system_tokens, payload_tokens)
            print(f"  📖 KB budget: {kb_budget} chars "
                  f"(window {CONTEXT_WINDOW}, payload ~{payload_tokens} tok, "
                  f"system ~{system_tokens} tok)", flush=True)
            if kb_budget <= 0:
                print("  📖 KB: no room left in the window; skipping.", flush=True)
            else:
                kb_content = select_relevant_kb(kb_dir, latest_instruction, kb_budget)
                if kb_content:
                    kb_block = (f"\n<BEST_PRACTICES_KNOWLEDGE_BASE>\n{kb_content}\n"
                                f"</BEST_PRACTICES_KNOWLEDGE_BASE>\n\n")
        conversation_text = conversation_text.replace(KB_PLACEHOLDER, kb_block)
    elif DISTILL_DESIGN_PASS == "bugfix":
        # There is no bug in a project that does not exist. Reaching the fresh
        # branch with `!bugfix` means the workspace is empty, so the pass would be
        # asked to diagnose a symptom in code it has never been shown - which it
        # can only answer by inventing one. Say so here rather than spending a
        # design pass to be told the same thing.
        print(f"\n❌ !bugfix on an empty workspace for {PROJECT_NAME}.", flush=True)
        print("  ↳ There is no code to diagnose. Use !architect or !build to create "
              "the project first.", flush=True)
        update_status("Aborted: nothing to diagnose.")
        raise SystemExit(3)
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
            f"  <MODE>NEW_BUILD</MODE>\n"
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

    design_label = ("🩺  Pass 1/4: Diagnostic Engineer" if DISTILL_DESIGN_PASS == "bugfix"
                    else "🏗️  Pass 1/4: System Architect")
    all_passes = [
        (DISTILL_DESIGN_PASS, design_label),
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
                _resolve_model_config(models.get(keep_key, models.get("architect")),
                                      pass_key=keep_key)
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
            model_config = _resolve_model_config(model_entry, pass_key=pass_key)
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
            else:
                # Ollama was the only provider with no warm step, so its models
                # cold-loaded inside call_llm's stall budget. Symmetry with the
                # branch above, and the reason test_engineer could burn 3x45s
                # without ever receiving a token.
                preload_model(client, model_config)

            # R17 says survey before you design, and until this ran nothing
            # could: the design pass could see source only by blocking for it,
            # which costs a whole pass and only fires after the design has
            # already failed. One small call maps the request onto files, the
            # mapping is checked against the workspace, and the verified source
            # goes into the payload ahead of the pass that needs it.
            #
            # After the preload, not before it. The first attempt ran this ahead
            # of the loop and it failed three times with ECONNREFUSED: for a
            # llama.cpp model the server process does not exist until the
            # orchestrator is asked to spawn it, which is what the preload above
            # does. There was nothing listening on the base URL to survey with,
            # and no amount of retrying was going to start it.
            #
            # The placeholder is cleared on every pass that reaches this point,
            # survey or no survey, so a run whose design pass is skipped or
            # resumed can never leak the sentinel into a payload.
            if SURVEY_PLACEHOLDER in conversation_text:
                survey_block = ""
                if pass_key == DISTILL_DESIGN_PASS and symbol_skeleton and latest_instruction:
                    try:
                        survey_block = survey_codebase(
                            client, model_config, latest_instruction, symbol_skeleton,
                            solve_addendum_budget(
                                CONTEXT_WINDOW, est_tokens(prompt),
                                est_tokens(conversation_text.replace(
                                    SURVEY_PLACEHOLDER, "")),
                            ),
                        )
                    except Exception as e:
                        # The survey is an optimisation on the blocker protocol,
                        # not a prerequisite for it. Losing it costs a round trip
                        # later, never the run.
                        print(f"  ⚠ Survey errored ({e}); continuing without it.",
                              flush=True)
                conversation_text = conversation_text.replace(
                    SURVEY_PLACEHOLDER, survey_block)

            prior_context = ""
            if results:
                prior_context = "\n\n".join(
                    f"#### {k.upper()} ANALYSIS\n{v}" 
                    for k, v in results.items()
                )

            # DISTILL_DESIGN_PASS, not a literal "architect": bugfix occupies the
            # same Pass 1 slot (see DESIGN_PASSES and all_passes below) and needs
            # the same input. Hardcoding "architect" here sent bugfix down the
            # review branch, so it received the 157-char stub instead of the
            # conversation - and, being pass 1, an empty prior_context alongside
            # it. It answered the only way its B12 permits: BLOCKED, naming the
            # REPORTED_SYMPTOM and CONTEXT it was never given.
            if pass_key in (DISTILL_DESIGN_PASS, "engineer"):
                target_content = conversation_text
            else:
                # Passes 3 and 4 only read the previous plans. They do not get chunked!
                target_content = "Review the PREVIOUS ANALYSES provided above based strictly on your system role and required template format. Do not invent new features or write source code."

            try:
                result = call_llm(client, model_config, prompt, target_content, prior_context)
                if not result.strip():
                    # "✓ Complete (0 chars)" used to be a valid outcome here: the
                    # empty string was saved as this pass's intermediate and the
                    # remaining passes designed against it. _check_llm_result only
                    # ever matched "[ERROR:", so nothing between the model and the
                    # disk had an opinion about a document with nothing in it.
                    raise _empty_pass_failure(pass_key, "pass output")
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
                _abort_pass(pass_key, model_config, e)

            result = resolve_pass_blockers(
                client, pass_key, model_config, prompt,
                target_content, prior_context, symbol_skeleton, result,
            )

            if pass_key == "bugfix":
                limits = config.get("limits", {})
                result, verified = verify_bugfix_reproduction(
                    client, model_config, prompt, target_content, prior_context,
                    symbol_skeleton, result,
                    max_attempts=int(limits.get("bugfix_max_repro_attempts", 3)),
                    timeout_secs=float(limits.get("bugfix_repro_timeout_secs", 300)),
                )
                if not verified and not detect_blockers(result):
                    result = mark_unverified(
                        result, int(limits.get("bugfix_max_repro_attempts", 3))
                    )

            if not result.strip():
                # Re-checked after the blocker and reproduction rounds, which each
                # run their own model calls and hand back a replacement result.
                _abort_pass(pass_key, model_config,
                            _empty_pass_failure(pass_key, "post-processing"))

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

    still_blocked = {k: detect_blockers(v) for k, v in results.items()}
    still_blocked = {k: v for k, v in still_blocked.items() if v}

    # A partial run (the review gate) must not overwrite .clinerules with an
    # incomplete ruleset - the approve run assembles the real one.
    missing = [key for key, _ in all_passes if key not in results]
    if missing:
        update_status("Awaiting review.")
        print(f"\n⏸️  Partial distillation complete. Skipped: {', '.join(missing)}", flush=True)
        if still_blocked:
            # Surfaced, not fatal: the whole point of the review gate is to put
            # this in front of a human, who can answer the blocker directly in
            # chat and re-run. Exiting non-zero here would kill that loop.
            print(f"  ⚠ Still blocked: {', '.join(still_blocked)} — the review "
                  f"below explains what is missing.", flush=True)
        print(f"  ↳ .clinerules NOT written; review {_intermediate_path(passes[0][0])} then approve.", flush=True)
        print("=" * 60, flush=True)
        print("✅ Review gate reached", flush=True)
        print("=" * 60, flush=True)
        return

    # A blocked pass is a refusal, and implementing a refusal is the single most
    # expensive thing this pipeline can do: the last time it happened the build
    # loop spent four hours across five iterations against a .clinerules whose
    # architecture section read "# BLOCKED". Stop where BudgetInfeasible and
    # ExtractionFailed stop - before anything is overwritten.
    if still_blocked:
        update_status("Aborted: unresolved blockers.")
        print("\n  ❌ Distillation is blocked and could not be resolved from the "
              "workspace.", flush=True)
        for key, blockers in still_blocked.items():
            print(f"\n  [{key}]", flush=True)
            for b in blockers:
                print(f"    · {b}", flush=True)
        print("\n  ↳ Aborting before .clinerules is written; nothing was overwritten.",
              flush=True)
        print("  ↳ Supply the missing facts in chat and re-run !build, or use "
              "!architect to iterate on the design first.", flush=True)
        raise SystemExit(3)

    # Same stop, one reason further on. A blocked pass refuses to diagnose; an
    # unverified one diagnoses something nothing has been observed to do. Building
    # the second is worse than building the first, because it looks like a plan.
    # The review gate above returns before this, so the document is still shown in
    # chat - this only stops a full run turning it into code.
    if UNVERIFIED_MARKER in results.get("bugfix", ""):
        update_status("Aborted: the bug was never reproduced.")
        print("\n  ❌ The declared reproduction never failed as diagnosed.", flush=True)
        print(f"  ↳ Aborting before .clinerules is written; nothing was overwritten.",
              flush=True)
        print(f"  ↳ Review {_intermediate_path('bugfix')}. Fix section 2 and delete "
              f"the banner to approve it anyway, or re-run !bugfix.", flush=True)
        raise SystemExit(4)

    print(f"\n📝 Writing {OUTPUT_PATH}", flush=True)
    update_status("Assembling .clinerules...")
    clinerules = assemble_clinerules(results, config, messages)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(clinerules)

    # Baseline for the re-plan trigger: from here on, growth in .build_issues.md
    # is growth the current plan has not accounted for.
    write_replan_state(_issues_bytes(), 0)

    update_status("Distillation complete.")
    print(f"  ✓ Written ({len(clinerules)} chars)", flush=True)
    print("=" * 60, flush=True)
    print("✅ Distillation complete", flush=True)
    print("=" * 60, flush=True)


# --- Re-plan on evidence ------------------------------------------------------
#
# Distillation runs once, in Phase 1, and the build loop then re-runs the same
# .clinerules against the same objective up to max_build_iterations times. So a
# design flaw discovered on iteration 2 gets patched tactically three more times
# and is never redesigned: .build_issues.md accumulates the evidence, but the
# architect only ever sees it on the *next* !build.
#
# This closes that loop. When the issues file has grown materially since the plan
# was written, the design-bearing passes re-run against the current tree, the
# current skeleton and the accumulated issues, and .clinerules is rebuilt. One
# re-plan costs roughly one pass; the failure it replaces costs a whole build.

BUILD_ISSUES_PATH = "/workspace/.cline_context/.build_issues.md"
REPLAN_STATE_PATH = os.path.join(INTERMEDIATE_DIR, ".replan_state.json")

# Both design passes re-run, not just the architect. The roadmap is a file-level
# mapping *of* the architecture, so revising the architecture and keeping the old
# roadmap produces a .clinerules that disagrees with itself - the expensive half
# of the cost buys the only version that is coherent. Narrow this to
# ["architect"] in agent_config.json if the GPU time matters more.
DEFAULT_REPLAN_PASSES = ("architect", "engineer")

# Carried forward rather than re-run: these two never see the codebase, so the
# build has taught them nothing new.
REPLAN_CARRY_PASSES = ("test_engineer", "safety")


def _issues_bytes() -> int:
    try:
        return os.path.getsize(BUILD_ISSUES_PATH)
    except OSError:
        return 0


def read_replan_state() -> dict:
    try:
        with open(REPLAN_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        return {"issues_bytes": int(state.get("issues_bytes", 0)),
                "replans": int(state.get("replans", 0))}
    except Exception:
        return {"issues_bytes": 0, "replans": 0}


def write_replan_state(issues_bytes: int, replans: int) -> None:
    try:
        os.makedirs(os.path.dirname(REPLAN_STATE_PATH), exist_ok=True)
        with open(REPLAN_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"issues_bytes": issues_bytes, "replans": replans}, f)
    except Exception as e:
        print(f"  ⚠ Could not record re-plan state: {e}", flush=True)


def replan_due(growth_threshold: int, max_replans: int) -> tuple:
    """
    Decide whether the plan has fallen far enough behind reality to redo it.

    Returns (due, reason).

    The trigger is *growth* since the plan was last written, not absolute size. A
    large issues file that has stopped growing describes problems already being
    worked through; one that keeps growing describes a plan that is not matching
    what the code is doing.
    """
    state = read_replan_state()
    if max_replans <= 0:
        return False, "re-planning disabled (max_replans=0)"
    if state["replans"] >= max_replans:
        return False, f"re-plan budget spent ({state['replans']}/{max_replans})"

    growth = _issues_bytes() - state["issues_bytes"]
    if growth < growth_threshold:
        return False, (f"issues grew {growth}B since the plan "
                       f"(threshold {growth_threshold}B)")
    return True, (f"issues grew {growth}B since the plan "
                  f"(threshold {growth_threshold}B)")


ISSUES_PLACEHOLDER = "\x00BUILD_ISSUES\x00"


def build_replan_payload(previous: dict, new_request: str,
                         system_tokens: int = 0) -> str:
    """
    Assemble the evidence for a revision: what was planned, what happened, what
    the code looks like now.

    MODE stays ITERATIVE_REBUILD rather than becoming a new value. architect.md's
    R7 and R8 are mutually exclusive and it is told exactly one is active for the
    current MODE, so an unrecognised mode leaves both inactive and the output
    contract undefined. A re-plan of a partially-built project is a rebuild; the
    revision framing goes in its own block instead.
    """
    tree = "(Could not generate directory tree)"
    try:
        tree = subprocess.check_output(
            ["tree", "/workspace", "-I",
             "node_modules|.git|venv|.venv|.cline_context|.cline_logs|__pycache__|dist|build|public|.knowledge_base"],
            text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    skeleton = get_symbol_skeleton("/workspace")
    tree = prune_tree_against_skeleton(tree, skeleton_paths(skeleton))
    toolchain = detect_project_toolchain("/workspace")

    issues = ""
    if os.path.exists(BUILD_ISSUES_PATH):
        try:
            with open(BUILD_ISSUES_PATH, "r", encoding="utf-8") as f:
                issues = f.read().strip()
        except Exception:
            pass

    payload = (
        "<SITUATIONAL_AWARENESS>\n"
        "  <MODE>ITERATIVE_REBUILD</MODE>\n"
        "  <STATUS>A build against your PREVIOUS_PLAN is in progress and has run "
        "into the problems recorded in BUILD_ISSUES. You are revising that plan, "
        "not starting one.</STATUS>\n"
        "  <DIRECTIVES>\n"
        "    1. [P0] REVISE: Keep every part of PREVIOUS_PLAN that BUILD_ISSUES "
        "does not contradict. Change only what the evidence forces.\n"
        "    2. [P0] EVIDENCE: Treat BUILD_ISSUES as fact. It is what happened "
        "when the previous plan was executed, including harness test results.\n"
        "    3. [P0] CURRENT_STATE: DIRECTORY_STRUCTURE and SYMBOL_SKELETON are "
        "regenerated as of now and already include work completed so far.\n"
        "    4. [P0] NO_RESTART: Work already built and not implicated in "
        "BUILD_ISSUES stands. Do not plan to rewrite it.\n"
        "    5. [P1] SCOPE: NEW_REQUEST is unchanged. Deliver it, adjusted for "
        "what execution has shown to be wrong.\n"
        "  </DIRECTIVES>\n"
        "</SITUATIONAL_AWARENESS>\n\n"
        "<PROJECT_DATA>\n"
        f"  <NAME>{PROJECT_NAME}</NAME>\n"
    )
    for key in (DISTILL_DESIGN_PASS, "engineer"):
        if previous.get(key):
            payload += (f"  <PREVIOUS_PLAN source=\"{key}\">\n"
                        f"{previous[key]}\n  </PREVIOUS_PLAN>\n\n")
    # Always emitted, even when empty. The directives above tell the model to
    # treat BUILD_ISSUES as fact and to preserve whatever it does not contradict,
    # so omitting the block entirely would leave those instructions pointing at
    # nothing. An explicit "none recorded" is a fact it can act on.
    payload += ISSUES_PLACEHOLDER
    payload += (
        f"  <DIRECTORY_STRUCTURE>\n```\n{tree}\n```\n  </DIRECTORY_STRUCTURE>\n\n"
        f"  <SYMBOL_SKELETON>\n{skeleton}\n  </SYMBOL_SKELETON>\n\n"
    )
    if toolchain:
        payload += f"  {toolchain}\n\n"
    payload += f"  <NEW_REQUEST>\n{new_request}\n  </NEW_REQUEST>\n</PROJECT_DATA>"

    if not issues:
        return payload.replace(ISSUES_PLACEHOLDER,
                               "  <BUILD_ISSUES>none recorded</BUILD_ISSUES>\n\n")

    # .build_issues.md only ever grows, and every re-plan appends the gate output
    # that triggered it, so on a long build it is the one part of this payload
    # with no natural bound. Left uncapped it would eventually push the re-plan
    # off the single-pass path and into chunked extraction - the same silent
    # downgrade solve_kb_budget exists to prevent.
    #
    # The tail is kept rather than the head: the most recent failures are the
    # ones the revision has to answer.
    budget = solve_addendum_budget(
        CONTEXT_WINDOW, system_tokens,
        est_tokens(payload.replace(ISSUES_PLACEHOLDER, "")),
    )
    if len(issues) > budget > 0:
        dropped = len(issues) - budget
        issues = (f"[{dropped} characters of older issues elided; the most recent "
                  f"are below]\n...\n" + issues[-budget:])
        print(f"  ↳ Build issues clipped to the last {budget} chars "
              f"({dropped} elided).", flush=True)
    elif budget <= 0:
        issues = "[build issues omitted: no room left in the context window]"

    return payload.replace(ISSUES_PLACEHOLDER,
                           f"  <BUILD_ISSUES>\n{issues}\n  </BUILD_ISSUES>\n\n")


# Operator-authored facts about the environment the build runs IN, as opposed to
# the code it is building. Verbatim, project-local, and present in EVERY
# .clinerules: an LLM pass never sees it, so it cannot be paraphrased away.
#
# It exists because nothing else in the pipeline could carry it. `.knowledge_base/`
# is keyword-scored against the instruction, so "add job revisions" scores zero on
# a Postgres file and the agent goes into the build not knowing a database exists.
# `.build_issues.md` is a ledger the agent is told to cross items off - the wrong
# semantics for a standing fact. `<operational_constraints>` is compiled in and
# shared by every project. This is the per-project slot none of those provide.
#
# WHAT BELONGS HERE: how to reach services that are already running, the exact
# connection strings, which database is safe to write to, and what to do when a
# service is unreachable. WHAT DOES NOT: anything the agent can learn by reading
# the code, and anything that changes per build - that is the request, not the
# environment.
BUILDER_ENV_PATH = "/workspace/.builder_env.md"

# Small on purpose. This is a fact sheet competing with the plan for the agent's
# window; at 4000 chars it costs ~1000 tokens. A file that wants to be larger is
# documentation and belongs in `.knowledge_base/`, where it is budgeted against
# the window instead of charged to it unconditionally.
BUILDER_ENV_MAX_CHARS = 4000


def read_builder_env(path: str = None) -> str:
    """
    Return the project's environment fact sheet, or "" if there is none.

    Absence is the normal case and is not an error: most projects have no
    external services and the block is simply omitted. A read failure is also
    non-fatal - a build must not die because an optional file was unreadable.
    """
    path = path or BUILDER_ENV_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return ""

    content = content.strip()
    if not content:
        return ""

    if len(content) > BUILDER_ENV_MAX_CHARS:
        print(f"  ⚠️  {path} is {len(content)} chars, truncating to "
              f"{BUILDER_ENV_MAX_CHARS} - move the detail to .knowledge_base/",
              flush=True)
        content = content[:BUILDER_ENV_MAX_CHARS].rstrip() + "\n[TRUNCATED]"

    return content


def assemble_clinerules(results: dict, config: dict, messages: list) -> str:
    """Combine the 4-pass results into a structured .clinerules document."""
    limits = config.get("limits", {})

    # The target objective is the last thing the user actually asked for.
    #
    # This used to look only for "!build", which meant every gated route wrote the
    # fallback text instead: `!architect "add SSO"` followed by `!approve` has no
    # "!build" in it anywhere, so the document the agent reads opened with
    # "Complete the implementation roadmap as specified." Walk back over the same
    # trigger vocabulary run_distillation uses, skipping bare commands, so
    # !build, !architect and !bugfix all name their own objective.
    target_obj = "Complete the implementation roadmap as specified."
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        directives = strip_trigger_syntax(msg.get("content", ""))
        if directives:
            target_obj = directives
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
    ]

    # The plan comes first.
    #
    # This document used to open with the test and safety passes under a "CORE
    # DIRECTIVES (High Priority)" banner, followed by twenty operational rules,
    # and only then the architecture and the roadmap. That inverted the actual
    # priority twice over: the design the pipeline exists to produce was the last
    # thing the agent read, and the two passes promoted above it are the two that
    # never see the codebase - they receive a ~90-token instruction to review the
    # earlier analyses and nothing else. They are commentary on the plan, so they
    # now sit after it, as gates the plan has to satisfy.
    section_map = {
        "architect": ("Architecture & Directory Structure", "🏗️"),
        "bugfix": ("Diagnosis & Fix Plan", "🩺"),
        "engineer": ("Implementation Roadmap", "⚙️"),
    }
    for key, (title, icon) in section_map.items():
        if key in results:
            doc.extend([f"## {icon} {title}", "", results[key], ""])

    if "test_engineer" in results or "safety" in results:
        doc.extend([
            "## ⚠️ GATES ON THE PLAN ABOVE",
            "The plan is not complete until these are satisfied. They constrain the "
            "implementation; they do not replace it.",
            "",
        ])

    if "test_engineer" in results:
        doc.append("### 🧪 Critical Test & Quality Gates")
        doc.append(results["test_engineer"])
        doc.append("")

    if "safety" in results:
        doc.append("### 🛡️ Safety & Security Mitigations")
        doc.append(results["safety"])
        doc.append("")

    doc.extend([
        "## 🔧 Operating Rules",
        "",
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
        "- SYMBOL SKELETON FIRST: Your .clinerules contains a Symbol Skeleton with imports, function names and - where they fitted - their signatures. Use this to navigate, not readFile. If a signature is there, do not open the file to learn it.",
        "- DEBUG-FIRST: When you need to understand how code works, write a small probe script, run it, and read the output. This is faster and more accurate than reading 500 lines of source code.",
        "- MANDATORY TEST GATE: After editing ANY file, run the project's test suite. If tests fail after your edit, fix the regression BEFORE moving to the next task.",
        "</operational_constraints>",
        "",
    ])

    # After the rules, because it qualifies them: several of the constraints above
    # ("run the test suite", "verify each major component") are unachievable if the
    # agent cannot reach the services the tests need, and the agent's own guess at
    # what to do about that has been to try installing one.
    builder_env = read_builder_env()
    if builder_env:
        doc.extend([
            "## 🌐 Environment You Are Building In",
            "",
            "Operator-supplied and authoritative. It describes services that are "
            "ALREADY RUNNING and how to reach them. Do not install, provision or "
            "substitute anything described here, and do not infer the environment "
            "from the code - this section outranks both.",
            "",
            "<project_environment>",
            builder_env,
            "</project_environment>",
            "",
        ])

    return "\n".join(doc)


def run_replan(growth_threshold: int, max_replans: int) -> int:
    """
    Revise the plan against what the build has learned, then rewrite .clinerules.

    Returns a shell exit code. Non-fatal by design: every failure path leaves the
    existing .clinerules in place and returns 0, because a build that is making
    progress must not be killed by a re-plan that could not run. The only thing
    a bad re-plan is allowed to cost is the GPU time it used.
    """
    due, reason = replan_due(growth_threshold, max_replans)
    if not due:
        print(f"  ↻ Re-plan not triggered: {reason}", flush=True)
        return 0

    print("=" * 60, flush=True)
    print(f"🔄 Re-planning against build evidence — {reason}", flush=True)
    print("=" * 60, flush=True)
    update_status("Re-planning against build evidence...")

    config = load_config()
    _resolve_context_window(config)
    _resolve_sampling(config)
    models = config.get("models", {})
    prompts = load_prompts(config)
    # `replan_passes` is configured by name, and the name it carries is
    # "architect" because that is the design pass in every ordinary run. On a
    # bugfix run the plan on disk is the diagnosis, so the configured design pass
    # is substituted rather than requiring the operator to keep two lists in sync
    # - and a config that already names "bugfix" is left exactly as written.
    replan_passes = tuple(
        DISTILL_DESIGN_PASS if p in DESIGN_PASSES else p
        for p in config.get("limits", {}).get("replan_passes", DEFAULT_REPLAN_PASSES)
    )

    previous = {}
    for key in replan_passes + REPLAN_CARRY_PASSES:
        saved = load_saved_pass(key)
        if saved:
            previous[key] = saved
    if not any(previous.get(k) for k in replan_passes):
        print("  ⚠ No previous plan on disk to revise; keeping .clinerules as is.",
              flush=True)
        return 0

    try:
        messages = load_conversation()
    except Exception as e:
        print(f"  ⚠ Could not read the conversation ({e}); keeping .clinerules.",
              flush=True)
        return 0

    new_request = ""
    for msg in reversed(messages):
        if msg.get("role") == "user" and "!build" in msg.get("content", "").lower():
            new_request = msg.get("content", "")
            break
    if not new_request and messages:
        new_request = messages[-1].get("content", "")

    system_tokens = max((est_tokens(prompts.get(k, "")) for k in replan_passes),
                        default=0)
    payload = build_replan_payload(previous, new_request, system_tokens)
    print(f"📄 Re-plan payload: {len(payload)} chars (~{est_tokens(payload)} tok)",
          flush=True)

    replans_so_far = read_replan_state()["replans"]

    def abandon(message: str) -> int:
        """
        Give up on this revision without touching the plan the build is using.

        The baseline moves to the current issues size but the re-plan count does
        not: a failure should not spend the budget, or one bad attempt would cost
        a good one later. Moving the baseline stops the same evidence re-firing
        the trigger on the very next iteration, so the next attempt waits for
        genuinely new evidence rather than burning a pass per iteration.
        """
        print(f"  ⚠ {message}", flush=True)
        write_replan_state(_issues_bytes(), replans_so_far)
        return 0

    skeleton_for_blockers = get_symbol_skeleton("/workspace")
    results = dict(previous)
    previous_model_config = None
    revised = []

    with httpx.Client() as client:
        for pass_key in replan_passes:
            print(f"\n🔄 Re-planning: {pass_key}", flush=True)
            print("-" * 40, flush=True)
            model_config = _resolve_model_config(
                models.get(pass_key, models.get("architect")), pass_key=pass_key)
            model_name = model_config.get("model", "")
            prompt = prompts.get(pass_key, "Revise the plan.")

            prev_name = previous_model_config.get("model", "") if previous_model_config else None
            if prev_name and prev_name != model_name:
                print(f"  ↳ Switching model: {prev_name} → {model_name}", flush=True)
                unload_model(client, previous_model_config)
            preload_model(client, model_config)

            # Only passes revised earlier in *this* run become prior context, so
            # the engineer maps files against the architecture just revised rather
            # than the one it replaced.
            prior_context = "\n\n".join(
                f"#### {k.upper()} ANALYSIS\n{results[k]}" for k in revised
            )

            try:
                result = call_llm(client, model_config, prompt, payload, prior_context)
            except (BudgetInfeasible, ExtractionFailed) as e:
                return abandon(f"Re-plan of {pass_key} failed ({e}); keeping the "
                               f"existing plan.")

            result = resolve_pass_blockers(
                client, pass_key, model_config, prompt,
                payload, prior_context, skeleton_for_blockers, result,
            )
            if detect_blockers(result):
                # Unlike the initial distillation, this is not fatal: there is
                # already a working plan on disk and a build using it.
                return abandon(f"Re-planned {pass_key} is blocked; discarding the "
                               f"revision and keeping the existing plan.")
            if not result.strip():
                return abandon(f"Re-planned {pass_key} came back empty; discarding "
                               f"the revision and keeping the existing plan.")

            results[pass_key] = result
            revised.append(pass_key)
            previous_model_config = model_config
            print(f"  ✓ Revised ({len(result)} chars)", flush=True)

    # Nothing is persisted until every pass has succeeded. A revision written as
    # each pass finished would leave the architect's new design on disk while
    # .clinerules still held the old one if the engineer then blocked - and the
    # next re-plan reads those intermediates, so it would revise a design the
    # build was never given.
    clinerules = assemble_clinerules(results, config, messages)
    try:
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            f.write(clinerules)
        for pass_key in revised:
            path = _intermediate_path(pass_key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# Distillation Intermediate: {pass_key.title()}\n\n"
                        f"{results[pass_key]}")
    except Exception as e:
        return abandon(f"Could not persist the revision ({e}); the old plan stands.")

    write_replan_state(_issues_bytes(), replans_so_far + 1)
    update_status("Re-plan complete.")
    print(f"\n  ✓ .clinerules rewritten ({len(clinerules)} chars) from revised "
          f"{', '.join(replan_passes)}", flush=True)
    print("=" * 60, flush=True)
    return 0


if __name__ == "__main__":
    # `--test-command <dir>` prints the project's test command and exits. The
    # build loop calls this once per iteration rather than reading a value cached
    # at distillation time, because on a fresh build the suite does not exist yet
    # when Phase 1 runs - it is written during Phase 2, by the agent being gated.
    if len(sys.argv) > 2 and sys.argv[1] == "--test-command":
        print(detect_test_command(sys.argv[2]))
        raise SystemExit(0)
    # `--replan <growth_bytes> <max_replans>` is called by the build loop between
    # iterations. It decides for itself whether the plan has fallen behind, so
    # the shell does not have to duplicate the trigger logic.
    if sys.argv[1:2] == ["--replan"]:
        threshold = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
        budget = int(sys.argv[3]) if len(sys.argv) > 3 else 2
        raise SystemExit(run_replan(threshold, budget))
    run_distillation()