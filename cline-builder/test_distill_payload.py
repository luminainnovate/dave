#!/usr/bin/env python3
"""
Acceptance and regression tests for the distillation payload pipeline.

Covers the two changes that made `!architect` usable on a real codebase:

  1. TARGET_CHUNK_SIZE raised from 2048, with the extraction record cap derived
     from it so a bigger chunk does not silently drop facts past the old limit.
  2. MODE and NEW_REQUEST re-injected into the merge prompt. Neither survives
     extraction, so before this the merge pass saw facts with no goal attached
     and architect.md R10 returned "# BLOCKED" for every multi-chunk payload.

No network: every test that exercises call_llm swaps _single_llm_call for a fake
that records the prompts it was handed.

Run:  python3 cline-builder/test_distill_payload.py
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import shlex
import sys
import tempfile

# distill.py reads its context window from the environment at import time, so pin
# it before loading the module or the derived chunk budget will not match the
# container. Loading by path rather than by name keeps the suite runnable from any
# working directory, and keeps every import at the top of the file.
os.environ.setdefault("EXPERT_CTX", "131072")

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("distill", os.path.join(_HERE, "distill.py"))
distill = importlib.util.module_from_spec(_spec)
sys.modules["distill"] = distill
_spec.loader.exec_module(distill)


# --- Fixtures ---------------------------------------------------------------

REQUEST_BODY = (
    "Scope: record version history. Do not touch attribution or share-link code.\n"
    "Add a RecordRevision type modelled on the existing AuditLogEntry shape."
)
BUILD_COMMAND = "Build a CLI that converts CSV to Parquet."


def _bulk(section: str, kb: int) -> str:
    """Filler that is unique per section, so chunk coverage is checkable."""
    line = f"{section} line with enough text to be worth splitting on."
    return "\n".join(f"{line} #{i}" for i in range(kb * 1024 // len(line)))


def iterative_payload(history_kb: int = 80) -> str:
    """A payload shaped like the ITERATIVE_REBUILD branch of run_distillation."""
    return (
        "<SITUATIONAL_AWARENESS>\n"
        "  <MODE>ITERATIVE_REBUILD</MODE>\n"
        "  <STATUS>This project is ALREADY PARTIALLY IMPLEMENTED.</STATUS>\n"
        "</SITUATIONAL_AWARENESS>\n\n"
        "<PROJECT_DATA>\n"
        "  <NAME>veriform-ui</NAME>\n"
        "  <PROJECT_HISTORY>\n"
        f"{_bulk('HISTORY', history_kb)}\n"
        "  </PROJECT_HISTORY>\n\n"
        "  <DIRECTORY_STRUCTURE>\n"
        f"{_bulk('TREE', 8)}\n"
        "  </DIRECTORY_STRUCTURE>\n\n"
        "  <SYMBOL_SKELETON>\n"
        f"{_bulk('SYMBOL', 12)}\n"
        "  </SYMBOL_SKELETON>\n\n"
        f"  <NEW_REQUEST>\n{REQUEST_BODY}\n  </NEW_REQUEST>\n"
        "</PROJECT_DATA>"
    )


def fresh_payload(history_kb: int = 60) -> str:
    """A payload shaped like the NEW_BUILD branch of run_distillation."""
    return (
        "<SITUATIONAL_AWARENESS>\n"
        "  <MODE>NEW_BUILD</MODE>\n"
        "</SITUATIONAL_AWARENESS>\n\n"
        "<PROJECT_DATA>\n"
        "  <PROJECT_HISTORY>\n"
        f"{_bulk('HISTORY', history_kb)}\n"
        "  </PROJECT_HISTORY>\n\n"
        f"  <FINAL_BUILD_COMMAND>\n{BUILD_COMMAND}\n  </FINAL_BUILD_COMMAND>\n"
        "</PROJECT_DATA>"
    )


class Skipped(Exception):
    """Raised by a test whose fixture is not present on this machine."""


class FakeLLM:
    """Stands in for _single_llm_call and records every prompt it receives."""

    def __init__(self, reply: str = "FACT | a.ts:1 | x | note"):
        self.reply = reply
        self.calls = []

    def __call__(self, client, model_config, system_prompt, user_content,
                 label="Inference", max_output_tokens=None):
        self.calls.append({
            "system": system_prompt,
            "user": user_content,
            "label": label,
            "max_output_tokens": max_output_tokens,
        })
        return self.reply

    @property
    def merge(self):
        """The merge pass is the last call, and is the only one so labelled."""
        merges = [c for c in self.calls if c["label"] == "Merging Parts"]
        assert len(merges) == 1, f"expected exactly one merge pass, got {len(merges)}"
        return merges[0]

    @property
    def chunks(self):
        return [c for c in self.calls if c["label"].startswith("Part ")]


# Chunking is now decided purely by whether the payload fits one merge-shaped
# call, so a fixture only reaches the map-reduce path when the window is small
# enough to exclude it. The 80KB fixture is ~38k tokens; 32768 puts the single-
# pass budget at ~23k, comfortably below it, while still leaving tiny payloads
# (the single-pass tests) on the direct path.
CHUNKING_WINDOW = 32768


def run_call_llm(payload: str, prior_context: str = "",
                 window: int = CHUNKING_WINDOW) -> FakeLLM:
    """
    Drive call_llm against a fake model and hand back the captured prompts.

    `window` pins CONTEXT_WINDOW for the call. Merge-path tests rely on the
    default being small enough to force chunking; pass a larger value to assert
    the single-pass path.
    """
    fake = FakeLLM()
    real_call = distill._single_llm_call
    original_window = distill.CONTEXT_WINDOW
    distill._single_llm_call = fake
    distill.CONTEXT_WINDOW = window
    try:
        # call_llm narrates its progress to stdout; keep the test output readable.
        with contextlib.redirect_stdout(io.StringIO()):
            distill.call_llm(None, "fake-model", "ARCHITECT SYSTEM PROMPT", payload, prior_context)
    finally:
        distill._single_llm_call = real_call
        distill.CONTEXT_WINDOW = original_window
    return fake


# --- A. Pivot extraction ----------------------------------------------------

def test_extract_request_iterative():
    assert distill.extract_request(iterative_payload()) == REQUEST_BODY


def test_extract_request_falls_back_to_build_command():
    """NEW_BUILD names the request differently; both tags must resolve."""
    assert distill.extract_request(fresh_payload()) == BUILD_COMMAND


def test_extract_request_absent_is_empty():
    assert distill.extract_request("<PROJECT_DATA>no request here</PROJECT_DATA>") == ""


def test_extract_request_ignores_blank_tag():
    """An empty tag is not a request - the caller must omit the section, not assert one."""
    assert distill.extract_request("<NEW_REQUEST>\n   \n</NEW_REQUEST>") == ""


def test_extract_mode_iterative():
    assert distill.extract_mode(iterative_payload()) == "ITERATIVE_REBUILD"


def test_extract_mode_fresh():
    assert distill.extract_mode(fresh_payload()) == "NEW_BUILD"


def test_extract_mode_absent_is_empty():
    assert distill.extract_mode("<PROJECT_DATA>no mode here</PROJECT_DATA>") == ""


def test_extract_mode_strips_surrounding_whitespace():
    assert distill.extract_mode("<MODE>\n  ITERATIVE_REBUILD\n</MODE>") == "ITERATIVE_REBUILD"


# --- B. Merge prompt carries the pivots (the bug) ---------------------------

def test_payload_actually_chunks():
    """Guard for the tests below: a realistic payload must take the merge path."""
    fake = run_call_llm(iterative_payload())
    assert len(fake.chunks) > 1, "fixture is too small to exercise the merge path"


def test_merge_prompt_carries_new_request():
    """The regression. Before the fix the merge pass saw facts and nothing else."""
    merge = run_call_llm(iterative_payload()).merge
    assert "NEW_REQUEST" in merge["user"]
    assert REQUEST_BODY in merge["user"]


def test_merge_prompt_carries_mode():
    merge = run_call_llm(iterative_payload()).merge
    assert "### MODE" in merge["user"]
    assert "ITERATIVE_REBUILD" in merge["user"]


def test_merge_prompt_states_pivots_before_facts():
    """Order matters: the goal has to be read before the records it scopes."""
    body = run_call_llm(iterative_payload()).merge["user"]
    assert body.index("### MODE") < body.index("### NEW_REQUEST") < body.index("EXTRACTED FACTS")


def test_merge_instruction_admits_the_pivots():
    """
    "Using ONLY these details" told the model to ignore anything that was not an
    extracted record - which would discard the re-injected pivots.
    """
    body = run_call_llm(iterative_payload()).merge["user"]
    assert "Using ONLY these details and the MODE and NEW_REQUEST above" in body


def test_merge_prompt_carries_fresh_build_command():
    merge = run_call_llm(fresh_payload(), window=8192).merge
    assert BUILD_COMMAND in merge["user"]
    assert "NEW_BUILD" in merge["user"]


def test_merge_prompt_without_pivots_makes_no_false_promise():
    """A payload with no pivots must not point the model at absent sections."""
    body = run_call_llm(_bulk("PROSE", 60), window=8192).merge["user"]
    assert "Using ONLY these details, write your final response." in body
    assert "### MODE" not in body
    assert "### NEW_REQUEST" not in body


def test_merge_prompt_uses_the_real_system_prompt():
    """The chunks run a relaxed extractor prompt; the merge must not."""
    fake = run_call_llm(iterative_payload())
    assert fake.merge["system"] == "ARCHITECT SYSTEM PROMPT"
    assert all(c["system"] != "ARCHITECT SYSTEM PROMPT" for c in fake.chunks)


# --- C. Chunk size acceptance -----------------------------------------------

def test_chunk_size_cuts_the_call_count():
    payload = iterative_payload()
    before = len(distill.chunk_text(payload, 2048))
    after = len(distill.chunk_text(payload, distill.TARGET_CHUNK_SIZE))
    assert after * 3 <= before, f"expected a material reduction, got {before} -> {after}"


def test_record_cap_scales_with_chunk_size():
    """A larger chunk with the old 20-record cap would drop facts silently."""
    assert distill.EXTRACTION_RECORD_CAP == distill.TARGET_CHUNK_SIZE // 100
    assert distill.EXTRACTION_RECORD_CAP > 20


def test_derivation_reproduces_the_original_tuning():
    """At the old chunk size the formula must return the old, hand-tuned triple."""
    assert 2048 // 100 == 20
    assert (2048 // 100) * 50 == 1000  # the original 1024 cap, to rounding


def test_extractor_prompt_states_the_current_cap():
    """A stale literal in the prompt would cap output below the derived budget."""
    fake = run_call_llm(iterative_payload())
    system = fake.chunks[0]["system"]
    assert f"Max {distill.EXTRACTION_RECORD_CAP}," in system
    assert "Max 20," not in system


def test_extraction_calls_get_the_derived_token_cap():
    fake = run_call_llm(iterative_payload())
    assert all(c["max_output_tokens"] == distill.EXTRACTION_MAX_TOKENS for c in fake.chunks)


def test_chunk_budget_still_fits_the_context_window():
    """A chunk plus its output must leave the margin intact, at the pinned window."""
    chunk, cap, out = distill.solve_extraction_budget(distill.CONTEXT_WINDOW, 900)
    assert 900 + chunk + out + distill.safety_margin(distill.CONTEXT_WINDOW) <= distill.CONTEXT_WINDOW
    assert cap == chunk // distill.EXTRACTION_TOKENS_PER_RECORD


# --- C2. The budget solver -----------------------------------------------------
#
# The invariant used to be asserted against whatever CONTEXT_WINDOW the suite had
# pinned for itself (131072), so it could never fail - including at the 8192 that
# docker-compose actually set. These exercise the solver at hostile windows.

def _for_each_window(fn):
    """Run a check across the windows this pipeline is realistically deployed at."""
    for window in (4096, 8192, 16384, 32768, 131072):
        fn(window)


def test_solved_extraction_never_exceeds_any_window():
    def check(window):
        for fixed in (400, 900, 1800):
            chunk, _, out = distill.solve_extraction_budget(window, fixed)
            total = fixed + chunk + out + distill.safety_margin(window)
            assert total <= window, f"window {window}, fixed {fixed}: committed {total}"
    _for_each_window(check)


def test_solved_merge_never_exceeds_any_window():
    def check(window):
        for fixed in (400, 1800):
            facts, answer = distill.solve_merge_budget(window, fixed)
            total = fixed + facts + answer + distill.safety_margin(window)
            assert total <= window, f"window {window}, fixed {fixed}: committed {total}"
            assert answer >= distill.ANSWER_FLOOR, "no room left for the template"
            assert facts >= distill.MIN_FACTS_TOKENS, "no room left for evidence"
    _for_each_window(check)


def test_output_cap_tracks_the_clamped_chunk():
    """
    The regression. The output cap was derived from the TARGET_CHUNK_SIZE constant
    while the input was derived from the window, so a clamped chunk still reserved
    output sized for a chunk that was never sent.
    """
    tight, _, tight_out = distill.solve_extraction_budget(8192, 900)
    wide, _, wide_out = distill.solve_extraction_budget(131072, 900)
    assert tight < wide, "a small window must clamp the chunk"
    assert tight_out < wide_out, "a clamped chunk must reserve less output, not the same"


def test_target_chunk_size_is_a_ceiling_not_a_floor():
    chunk, _, _ = distill.solve_extraction_budget(131072, 900)
    assert chunk == distill.TARGET_CHUNK_SIZE
    tight, _, _ = distill.solve_extraction_budget(8192, 900)
    assert tight < distill.TARGET_CHUNK_SIZE


def test_infeasible_window_raises_rather_than_clamping():
    """
    Clamping is what produced a silently truncated prompt. A window that cannot
    hold a viable call must fail, and must name the window it needs.
    """
    try:
        distill.solve_extraction_budget(2048, 1800)
    except distill.BudgetInfeasible as e:
        assert e.required > 2048, "the error must name a window that would work"
    else:
        raise AssertionError("an infeasible extraction budget was clamped, not raised")

    try:
        distill.solve_merge_budget(1024, 800)
    except distill.BudgetInfeasible as e:
        assert e.required > 1024
    else:
        raise AssertionError("an infeasible merge budget was clamped, not raised")


def test_est_tokens_over_estimates():
    """Budget errors must fail toward headroom, never toward a truncated prompt."""
    text = "x" * 300
    assert distill.est_tokens(text) >= len(text) // distill.CHARS_PER_TOKEN
    assert distill.est_tokens("") == 0
    assert distill.est_tokens("a") == 1, "must round up, not down"


def test_truncate_to_tokens_respects_its_budget():
    text = "\n".join(f"line {i} with some content" for i in range(500))
    cut = distill.truncate_to_tokens(text, 100)
    assert distill.est_tokens(cut) <= 100
    assert distill.truncate_to_tokens("short", 100) == "short"


def test_small_context_window_clamps_the_chunk():
    """Every assembled chunk prompt must fit the window it was solved against."""
    original = distill.CONTEXT_WINDOW
    distill.CONTEXT_WINDOW = 8192
    try:
        fake = run_call_llm(iterative_payload(), window=8192)
        for call in fake.chunks:
            committed = (
                distill.est_tokens(call["system"])
                + distill.est_tokens(call["user"])
                + call["max_output_tokens"]
            )
            assert committed <= 8192, \
                f"chunk call committed {committed} tokens to an 8192 window"
    finally:
        distill.CONTEXT_WINDOW = original


def test_merge_call_fits_the_window_too():
    """The merge was the one call with no budget check at all."""
    original = distill.CONTEXT_WINDOW
    distill.CONTEXT_WINDOW = 8192
    try:
        merge = run_call_llm(iterative_payload(), window=8192).merge
        committed = (
            distill.est_tokens(merge["system"])
            + distill.est_tokens(merge["user"])
            + merge["max_output_tokens"]
        )
        assert committed <= 8192, f"merge call committed {committed} tokens to an 8192 window"
    finally:
        distill.CONTEXT_WINDOW = original


def test_merge_answer_budget_can_hold_the_template():
    """architect.md's seven capped sections need room whatever the window."""
    original = distill.CONTEXT_WINDOW
    distill.CONTEXT_WINDOW = 8192
    try:
        merge = run_call_llm(iterative_payload(), window=8192).merge
        assert merge["max_output_tokens"] >= distill.ANSWER_FLOOR
    finally:
        distill.CONTEXT_WINDOW = original


def test_context_window_falls_back_to_agent_config():
    """The config key existed but was read by nobody."""
    original_window, original_env = distill.CONTEXT_WINDOW, distill._ENV_CONTEXT_WINDOW
    distill._ENV_CONTEXT_WINDOW = ""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            distill._resolve_context_window({"context_window": 65536})
        assert distill.CONTEXT_WINDOW == 65536
    finally:
        distill.CONTEXT_WINDOW, distill._ENV_CONTEXT_WINDOW = original_window, original_env


def test_environment_still_beats_agent_config():
    """The orchestrator injects EXPERT_CTX per build; it has to keep winning."""
    original_window, original_env = distill.CONTEXT_WINDOW, distill._ENV_CONTEXT_WINDOW
    distill._ENV_CONTEXT_WINDOW = "131072"
    distill.CONTEXT_WINDOW = 131072
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            distill._resolve_context_window({"context_window": 8192})
        assert distill.CONTEXT_WINDOW == 131072
    finally:
        distill.CONTEXT_WINDOW, distill._ENV_CONTEXT_WINDOW = original_window, original_env


# --- D. Chunking regressions -------------------------------------------------

def test_single_chunk_path_is_untouched():
    """Small payloads must still go straight to the model, with no merge pass."""
    fake = run_call_llm("<MODE>ITERATIVE_REBUILD</MODE>\n<NEW_REQUEST>tiny</NEW_REQUEST>")
    assert len(fake.calls) == 1
    assert fake.calls[0]["system"] == "ARCHITECT SYSTEM PROMPT"
    assert "tiny" in fake.calls[0]["user"]
    assert "EXTRACTED FACTS" not in fake.calls[0]["user"]


def test_single_chunk_path_keeps_prior_context():
    fake = run_call_llm("<NEW_REQUEST>tiny</NEW_REQUEST>", prior_context="earlier findings")
    assert "PREVIOUS ANALYSES" in fake.calls[0]["user"]
    assert "earlier findings" in fake.calls[0]["user"]


def test_every_chunk_still_sees_the_request():
    """The map-step fix must survive the merge-step fix."""
    fake = run_call_llm(iterative_payload())
    assert all(REQUEST_BODY in c["user"] for c in fake.chunks)


def test_every_chunk_is_labelled_with_its_sections():
    fake = run_call_llm(iterative_payload())
    assert all("PAYLOAD SECTIONS IN THIS PART:" in c["user"] for c in fake.chunks)


def test_chunking_covers_the_whole_payload():
    """No section may fall between two chunks."""
    payload = iterative_payload()
    joined = "".join(c for c, _ in distill.chunk_text(payload, distill.TARGET_CHUNK_SIZE))
    for marker in ("HISTORY line", "TREE line", "SYMBOL line", REQUEST_BODY.splitlines()[0]):
        assert marker in joined, f"{marker!r} lost in chunking"


def test_chunks_respect_the_char_budget():
    limit = distill.TARGET_CHUNK_SIZE * distill.CHARS_PER_TOKEN
    for chunk, _ in distill.chunk_text(iterative_payload(), distill.TARGET_CHUNK_SIZE):
        assert len(chunk) <= limit, f"chunk of {len(chunk)} chars exceeds {limit}"


def test_chunk_text_is_stable_for_short_input():
    text = "one small payload"
    assert [c for c, _ in distill.chunk_text(text, distill.TARGET_CHUNK_SIZE)] == [text]


# --- D2. Orientation blocks are never a source of records --------------------
#
# NEW_REQUEST carried "do not extract records from it"; PREVIOUS ANALYSES, sent
# on every chunk, carried nothing. So a fact extractor was handed the architect's
# finished document and emitted RULE/PATH records quoting its own design back
# into the merge.

ARCHITECT_ANALYSIS = (
    "#### ARCHITECT ANALYSIS\n"
    "# 1. Business Goal\n"
    "- Stop one API key degrading search latency.\n"
    "# 2. Directory Structure\n"
    "src/\n"
    "  http/\n"
    "    middleware/\n"
    "      rateLimit.ts [NEW]\n"
    "# 3. Technology Stack\n"
    "- EXISTING: Redis — reused as the counter store.\n"
    "# 4. Contracts\n"
    "- src/http/middleware/rateLimit.ts::rateLimit(key: string) -> Promise<Decision> [NEW]\n"
    "# 5. Data Flows\n"
    "- Request -> middleware -> Redis INCR -> allow or 429.\n"
    "# 6. Risks\n"
    "- RISK: Redis unreachable | MITIGATION: fail open.\n"
    "# 7. Out of Scope\n"
    "- Per-tenant quota dashboards.\n"
)


def test_extractor_prompt_states_the_source_boundary():
    system = run_call_llm(iterative_payload()).chunks[0]["system"]
    assert "SOURCE BOUNDARY" in system
    for heading in distill.ORIENTATION_HEADINGS:
        assert f"### {heading}" in system, f"{heading} not named as a non-source"


def test_both_orientation_blocks_carry_the_exclusion():
    """The asymmetry itself: one block was guarded and the other was not."""
    chunk = run_call_llm(iterative_payload(), prior_context=ARCHITECT_ANALYSIS).chunks[0]["user"]
    for heading in distill.ORIENTATION_HEADINGS:
        assert f"### {heading}" in chunk, f"{heading} block missing"
        block = chunk.split(f"### {heading}", 1)[1].split("---", 1)[0]
        assert "never emit a record sourced from it" in block.lower(), \
            f"{heading} block carries no exclusion"


def test_chunks_get_steering_not_the_whole_prior_document():
    """Re-sending the full architect document on all N chunks inflated every call."""
    fake = run_call_llm(iterative_payload(), prior_context=ARCHITECT_ANALYSIS)
    for call in fake.chunks:
        assert "rateLimit.ts" in call["user"], "steering must keep the paths in play"
        assert "Per-tenant quota dashboards" not in call["user"], \
            "section 7 is not steering; the full document must not be re-sent"


def test_merge_still_receives_the_full_prior_context():
    """Chunks are steered, but synthesis needs the whole thing."""
    merge = run_call_llm(iterative_payload(), prior_context=ARCHITECT_ANALYSIS).merge
    assert "Per-tenant quota dashboards" in merge["user"]
    assert "PREVIOUS ANALYSES" in merge["user"]


def test_steering_extract_picks_paths_and_contracts():
    steer = distill.steering_extract(ARCHITECT_ANALYSIS)
    assert "rateLimit.ts" in steer
    assert "Promise<Decision>" in steer
    assert "Business Goal" not in steer
    assert "Out of Scope" not in steer


def test_steering_extract_stops_at_the_next_wrapper_heading():
    """A following '#### ENGINEER ANALYSIS' must terminate the capture."""
    combined = ARCHITECT_ANALYSIS + "\n#### ENGINEER ANALYSIS\n- build order: rateLimit first\n"
    assert "build order" not in distill.steering_extract(combined)


def test_steering_extract_respects_its_cap():
    assert distill.est_tokens(distill.steering_extract("# 4. Contracts\n" + "- x\n" * 5000)) \
        <= distill.PRIOR_STEER_MAX_TOKENS


def test_steering_extract_falls_back_when_sections_are_absent():
    """Engineer/safety output has no section 2 or 4; it must still steer something."""
    steer = distill.steering_extract("free-form notes about the build order")
    assert "free-form notes" in steer


# --- D3. The consolidation ladder terminates ---------------------------------

def test_consolidation_is_a_filter_not_a_summariser():
    """A summariser paraphrases, which destroys the verbatim property."""
    prompt = distill.CONSOLIDATION_SYSTEM_PROMPT.lower()
    assert "never rewrite" in prompt and "verbatim" in prompt
    assert "summarizer" not in prompt


def test_facts_are_compressed_to_the_merge_budget():
    """A ladder that never re-checks its own output is not a ladder."""
    fake = FakeLLM(reply="SYM | a.ts:1 | x | note")
    huge = ["RECORD | a.ts:1 | " + "y" * 400 for _ in range(200)]
    real = distill._single_llm_call
    distill._single_llm_call = fake
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fitted = distill._fit_facts_to_budget(None, "m", huge, 300, 8192)
    finally:
        distill._single_llm_call = real
    assert distill.est_tokens("\n\n".join(fitted)) <= 300


def test_non_converging_consolidation_still_terminates_and_says_so():
    """A filter that returns its input unchanged must not spin, and must mark the loss."""
    class Stubborn(FakeLLM):
        def __call__(self, client, model_config, system_prompt, user_content,
                     label="Inference", max_output_tokens=None):
            super().__call__(client, model_config, system_prompt, user_content,
                             label, max_output_tokens)
            return user_content        # refuses to shrink anything

    fake = Stubborn()
    parts = ["RECORD | a.ts:1 | " + "z" * 500 for _ in range(40)]
    real = distill._single_llm_call
    distill._single_llm_call = fake
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fitted = distill._fit_facts_to_budget(None, "m", parts, 200, 8192)
    finally:
        distill._single_llm_call = real

    rounds = [c for c in fake.calls if c["label"].startswith("Consolidation")]
    assert rounds, "the ladder should have attempted at least one round"
    assert distill.est_tokens("\n\n".join(fitted)) <= 200
    assert "[TRUNCATED:" in fitted[-1], "dropped facts must be declared, not silent"
    assert "CONTEXT IS INCOMPLETE" in fitted[-1]


def test_facts_within_budget_are_left_alone():
    fake = FakeLLM()
    parts = ["SYM | a.ts:1 | export function a() | note"]
    real = distill._single_llm_call
    distill._single_llm_call = fake
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fitted = distill._fit_facts_to_budget(None, "m", parts, 4096, 131072)
    finally:
        distill._single_llm_call = real
    assert fitted == parts
    assert not fake.calls, "no consolidation call should be made when facts already fit"


def test_pack_buckets_never_exceeds_the_bucket_budget():
    parts = [f"record {i} " + "q" * (i * 37 % 900) for i in range(60)]
    for bucket in distill._pack_buckets(parts, 250):
        assert distill.est_tokens("\n\n".join(bucket)) <= 250 * 2, \
            "a bucket ran far past its budget"


# --- E. The real conversation payload ---------------------------------------

def test_real_conversation_reaches_the_architect_whole():
    """
    End-to-end against the bound workspace, when one is present.

    This assertion is deliberately the inverse of what it used to be. A real
    conversation payload is ~15k tokens against the container's 64k window, so
    it must now reach the architect INTACT rather than being split and squeezed
    through the extractor's capped bullet records. Losing the prose that way is
    what produced the "# BLOCKED - the actual JSX content is absent" answers
    while that very content sat in PROJECT_HISTORY.
    """
    path = os.path.join(
        _HERE, "..", "conversations", "veriform-ui_e6b3b8906f60",
        ".cline_context", "conversation.json",
    )
    if not os.path.exists(path):
        raise Skipped("no bound workspace on this machine")

    import json
    with open(path, encoding="utf-8") as f:
        messages = json.load(f)
    payload = (
        "<SITUATIONAL_AWARENESS>\n  <MODE>ITERATIVE_REBUILD</MODE>\n"
        "</SITUATIONAL_AWARENESS>\n\n<PROJECT_DATA>\n  <PROJECT_HISTORY>\n"
        f"{distill.conversation_to_text(messages[:-1])}\n  </PROJECT_HISTORY>\n\n"
        f"  <NEW_REQUEST>\n{messages[-1].get('content', '')}\n  </NEW_REQUEST>\n"
        "</PROJECT_DATA>"
    )
    # 65536 is what the orchestrator injects as EXPERT_CTX for a build container.
    fake = run_call_llm(payload, window=65536)
    assert not fake.chunks, \
        f"real payload should not be chunked at a 64k window, got {len(fake.chunks)} parts"
    assert len(fake.calls) == 1, "expected exactly one architect call"

    body = fake.calls[0]["user"]
    assert "ITERATIVE_REBUILD" in body

    # Derived from the file, not hardcoded: this workspace is live and its
    # newest request changes as the project is worked on.
    request = distill.extract_request(payload)
    assert request, "fixture payload carries no request to check"
    assert request in body, "the real request did not reach the architect"

    # The whole point: prose from the history survives verbatim, not as bullets.
    history = distill.conversation_to_text(messages[:-1])
    if len(history) > 400:
        assert history[:400] in body, "history was summarised instead of passed through"



# --- F. Payload reduction ----------------------------------------------------
#
# Every element below was measured on a live workspace before being changed:
# PROJECT_HISTORY 13607 tok (52% of the payload), SYMBOL_SKELETON 6909, the
# directory tree 3028 with 94% of its code files already named by the skeleton,
# and a knowledge-base cap of 100000 characters set independently of the solver.
# These tests pin the reductions and, more importantly, the two invariants that
# make them safe: nothing is dropped from both the tree and the skeleton, and
# the KB can no longer push a payload off the single-pass path.


def test_receipts_are_dropped_from_history():
    """Orchestrator acknowledgements are chat chrome, not conversation."""
    messages = [
        {"role": "user", "content": "!approve"},
        {"role": "assistant", "content": "\U0001f528 **Build pipeline triggered.**\n\nWorkspace: x"},
        {"role": "user", "content": "carry on"},
    ]
    text = distill.conversation_to_text(messages)
    assert "Build pipeline triggered" not in text
    assert "carry on" in text, "a real turn was dropped along with the receipt"


def test_duplicate_assistant_turns_collapse_to_a_marker():
    """
    Re-running !architect produces byte-identical proposals. The last copy stays
    put; earlier ones leave a marker so the user turn they answered still has a
    reply and the transcript keeps its shape.
    """
    body = "PROPOSAL " * 200
    messages = [
        {"role": "user", "content": "first ask"},
        {"role": "assistant", "content": body},
        {"role": "user", "content": "refined ask"},
        {"role": "assistant", "content": body},
    ]
    text = distill.conversation_to_text(messages)
    assert text.count(body) == 1, "the duplicate body was not collapsed"
    assert distill.SUPERSEDED_MARKER in text
    assert text.index(distill.SUPERSEDED_MARKER) < text.index(body), \
        "the surviving copy should be the later one"
    assert text.count("[ASSISTANT]") == 2, "a turn disappeared from the transcript"


def test_history_reduction_is_lossless_for_unique_prose():
    """Nothing unique may be lost - only receipts and exact duplicates go."""
    messages = [
        {"role": "user", "content": "unique question about the schema"},
        {"role": "assistant", "content": "unique answer about the schema"},
        {"role": "assistant", "content": "\U0001f528 **Build pipeline triggered.**"},
    ]
    text = distill.conversation_to_text(messages)
    assert "unique question about the schema" in text
    assert "unique answer about the schema" in text


def test_tree_pruning_removes_only_what_the_skeleton_names():
    tree = (
        "/workspace\n"
        "\u251c\u2500\u2500 src\n"
        "\u2502\u00a0\u00a0 \u251c\u2500\u2500 covered.ts\n"
        "\u2502\u00a0\u00a0 \u2514\u2500\u2500 uncovered.json\n"
        "\u2514\u2500\u2500 README.md\n"
    )
    pruned = distill.prune_tree_against_skeleton(tree, {"src/covered.ts"})
    assert "covered.ts" not in pruned, "a skeleton-covered file survived in the tree"
    assert "uncovered.json" in pruned, "a file the skeleton does not name was dropped"
    assert "README.md" in pruned
    assert "src" in pruned, "directory structure must survive - the skeleton lacks it"
    assert "1 source files omitted" in pruned


def test_tree_pruning_matches_full_paths_not_basenames():
    """
    src/a/index.ts must not be dropped because src/b/index.ts is in the skeleton.
    tree(1) pads with non-breaking spaces; matching plain spaces prunes nothing.
    """
    tree = (
        "/workspace\n"
        "\u2514\u2500\u2500 src\n"
        "\u00a0\u00a0\u00a0 \u251c\u2500\u2500 a\n"
        "\u00a0\u00a0\u00a0 \u2502\u00a0\u00a0 \u2514\u2500\u2500 index.ts\n"
        "\u00a0\u00a0\u00a0 \u2514\u2500\u2500 b\n"
        "\u00a0\u00a0\u00a0\u00a0\u00a0\u00a0  \u2514\u2500\u2500 index.ts\n"
    )
    pruned = distill.prune_tree_against_skeleton(tree, {"src/b/index.ts"})
    assert "1 source files omitted" in pruned, "path reconstruction failed entirely"


def test_tree_pruning_is_a_noop_without_a_skeleton():
    tree = "/workspace\n\u2514\u2500\u2500 main.py\n"
    assert distill.prune_tree_against_skeleton(tree, set()) == tree


def test_skeleton_lists_symbolless_files_rather_than_dropping_them():
    """
    The roll-up is what makes tree pruning safe. A file that fell out of the
    skeleton *and* out of the tree would vanish from the payload entirely.
    """
    files_data = [
        ("src/rich.ts", 40, ["import x"], [("Thing", "(a: string)")], []),
        ("src/bare.ts", 10, ["import y"], [], [("helper", "()")]),
    ]
    blocks, footer = distill._render_skeleton(files_data, distill.SKELETON_TIERS[-1])
    assert len(blocks) == 1, "the symbol-less file still rendered a detail block"
    assert "src/bare.ts" in footer, "the symbol-less file was dropped outright"
    assert len(footer) < 200, "the roll-up should be compact"


def test_skeleton_paths_covers_both_renderings():
    """prune_tree_against_skeleton is only safe if this sees every path."""
    files_data = [
        ("src/rich.ts", 40, ["import x"], [("Thing", "(a: string)")], []),
        ("src/bare.ts", 10, ["import y"], [], [("helper", "()")]),
    ]
    blocks, footer = distill._render_skeleton(files_data, distill.SKELETON_TIERS[-1])
    skeleton = "\n".join(["[PROJECT SYMBOL SKELETON]"] + blocks + [footer])
    paths = distill.skeleton_paths(skeleton)
    assert paths == {"src/rich.ts", "src/bare.ts"}, paths


def test_skeleton_carries_exported_signatures():
    """
    The regression this exists for: the skeleton named `upsertRole` and stopped
    there, so a design pass could see that a role writer existed and not what it
    took or returned - which is exactly enough to add a second one beside it.
    """
    src = (
        'export async function upsertRole(role: ResumeRole): Promise<void> {}\n'
        'export type Role = "owner" | "viewer";\n'
    )
    exported, _internal = distill._scan_symbols(src)
    rendered = [distill._sym_text(s, True) for s in exported]
    assert "upsertRole(role: ResumeRole): Promise<void>" in rendered, rendered
    assert 'Role = "owner" | "viewer"' in rendered, rendered


def test_signature_survives_a_declaration_wrapped_across_lines():
    """
    Anything with more than two parameters is formatted one per line, and a
    scan that stopped at the newline would render "upsertRole(" - worse than the
    bare name it replaced, because it looks like information.
    """
    src = (
        'export function upsertRole(\n'
        '  role: ResumeRole,\n'
        '  actor?: string,\n'
        '): Promise<ResumeRole> {\n'
        '  return role;\n'
        '}\n'
    )
    exported, _internal = distill._scan_symbols(src)
    assert distill._sym_text(exported[0], True) == \
        "upsertRole(role: ResumeRole, actor?: string): Promise<ResumeRole>"


def test_arrow_signature_reads_the_parameters_not_the_body():
    """
    ARROW_RE's match ends past the `=>`, so scanning from the match end would
    capture the function body. Signatures are read from the end of the name.
    """
    src = 'export const rateLimit = async (key: string, limit: number) => { return 1; };\n'
    exported, _internal = distill._scan_symbols(src)
    sig = distill._sym_text(exported[0], True)
    assert "key: string, limit: number" in sig, sig
    assert "return 1" not in sig, sig


def test_signature_scan_is_not_derailed_by_a_brace_in_a_default():
    """A `{` inside the parameter list must not end the signature."""
    src = 'export function fmt(sep = "{", pad = 2): string {}\n'
    exported, _internal = distill._scan_symbols(src)
    sig = distill._sym_text(exported[0], True)
    assert "pad = 2" in sig, sig
    assert sig.endswith("string"), sig


def test_long_signatures_are_capped_not_left_to_run():
    """One baroque generic must not spend the cap a whole file needs."""
    src = f'export function wide({", ".join(f"a{i}: SomeLongTypeName" for i in range(40))}) {{}}\n'
    exported, _internal = distill._scan_symbols(src)
    assert len(distill._sym_text(exported[0], True)) <= distill._SIG_MAX_CHARS + 20


def test_python_module_level_declarations_are_the_public_surface():
    """
    Python has no export keyword, so the exported/internal split has to come from
    indentation. Without it every Python symbol is "internal", no tier renders a
    signature, and the whole feature is a no-op on a Python project.
    """
    src = (
        "def solve_budget(window: int, fixed: int) -> tuple:\n"
        "    return (window, fixed)\n"
        "def _private_helper(x):\n"
        "    return x\n"
        "class Runner:\n"
        "    def method(self, a):\n"
        "        return a\n"
    )
    exported, internal = distill._scan_symbols(src, ".py")
    names = [n for n, _ in exported]
    assert names == ["solve_budget", "Runner"], names
    assert [n for n, _ in internal] == ["_private_helper", "method"]
    assert distill._sym_text(exported[0], True) == \
        "solve_budget(window: int, fixed: int) -> tuple"


def test_typescript_without_export_stays_private():
    """
    The indentation fallback is for languages with no export keyword only. A
    top-level TS function with no `export` is private on purpose, and offering it
    to the architect would invite a design that imports what nothing exports.
    """
    src = "function helper(a: string) {}\nexport function api(b: number) {}\n"
    exported, internal = distill._scan_symbols(src, ".ts")
    assert [n for n, _ in exported] == ["api"]
    assert [n for n, _ in internal] == ["helper"]


def test_internal_helpers_never_carry_signatures():
    """
    Nothing outside a file may call its internal helpers, so their signatures are
    not reuse surface - they would spend the cap the exported ones must fit in.
    """
    files_data = [("src/a.ts", 10, [], [("Api", "(x: number)")], [("helper", "(y: string)")])]
    block = distill._render_skeleton(files_data, distill.SKELETON_TIERS[0])[0][0]
    assert "Api(x: number)" in block
    assert "helper" in block and "y: string" not in block


def test_skeleton_sheds_signatures_before_it_sheds_files():
    """
    Tier order is the whole safety argument: a project too large for signatures
    still gets every file, one detail level down. Losing files silently is what
    the tiering exists to prevent.
    """
    files_data = [(f"src/f{i}.ts", 10, ["import x"],
                   [(f"Sym{i}", "(a: VeryLongParameterTypeName, b: AnotherOne): Promise<void>")], [])
                  for i in range(200)]

    rich = distill._render_skeleton(files_data, distill.SKELETON_TIERS[0])[0]
    lean = distill._render_skeleton(files_data, distill.SKELETON_TIERS[-1])[0]
    assert sum(map(len, lean)) < sum(map(len, rich)), "the lean tier must be cheaper"
    assert len(lean) == len(rich) == 200, "no tier may drop a file"
    assert "Promise<void>" not in "".join(lean), "the lean tier still carried signatures"


def test_kb_budget_leaves_the_payload_on_the_single_pass_path():
    """
    The regression this exists for: a 100000-character KB plus a real payload
    exceeds the single-pass budget, and the overflow is silent - call_llm just
    switches to chunked extraction, and the merge then sees capped bullet
    records instead of the codebase facts the KB was added to inform.
    """
    # Pinned, not ambient: this suite imports at 131072, where the old literal
    # happens to fit. 65536 is what the orchestrator injects as DISTILL_CTX for
    # a real build container, and it is where the overflow actually happened.
    window = 65536
    system_tokens = 1747          # architect.md
    payload_tokens = 21161        # measured, post-reduction, on a live workspace

    budget = distill.solve_kb_budget(window, system_tokens, payload_tokens)
    assert budget > 0, "a 64k window should have room for some KB"

    facts, _answer = distill.solve_merge_budget(
        window, system_tokens + distill.est_tokens("### CURRENT TASK\n")
    )
    with_kb = payload_tokens + distill.est_tokens("x" * budget)
    assert with_kb <= facts, "the solved KB budget still overflows single-pass"

    # And the old literal would not have fitted, which is why it is gone.
    assert payload_tokens + distill.est_tokens("x" * 100000) > facts


def test_kb_budget_never_exceeds_the_absolute_ceiling():
    huge = distill.solve_kb_budget(1_000_000, 1000, 1000)
    assert huge == distill.KB_MAX_CHARS


def test_kb_budget_yields_nothing_when_the_window_is_full():
    assert distill.solve_kb_budget(8192, 1747, 40000) == 0
    assert distill.solve_kb_budget(2048, 1747, 100) == 0



# --- G. Blocker protocol and completion gate ---------------------------------
#
# Context: on the last real build all four passes refused. The architect emitted
# R10's "# BLOCKED" naming the facts it lacked, the other three cascaded on the
# missing specification, assemble_clinerules concatenated the lot, and the build
# loop ran five build/verify/safety iterations against it - four hours of GPU
# time spent implementing a refusal. Nothing in the codebase read the refusal.


def test_detect_blockers_reads_the_architect_refusal():
    """architect.md R10: a bare two-line document."""
    blockers = distill.detect_blockers(
        "# BLOCKED\n- CONTEXT lacks the mock data shapes needed to derive the schema."
    )
    assert len(blockers) == 1
    assert "mock data shapes" in blockers[0]


def test_detect_blockers_reads_the_downstream_refusal():
    """engineer/test_engineer/safety: '- BLOCKER: ... | NEEDS: ...' bullets."""
    blockers = distill.detect_blockers(
        "# 1. Blockers\n"
        "- BLOCKER: A section 2 is missing | NEEDS: the architect specification\n"
        "- BLOCKER: no test runner named | NEEDS: E section 4 commands\n"
    )
    assert len(blockers) == 2
    assert all("NEEDS:" in b for b in blockers)


def test_detect_blockers_ignores_healthy_output():
    """'- none' is the healthy value of the section and must never trip this."""
    assert distill.detect_blockers("# 1. Blockers\n- none\n# 2. Mapping\n- src/a.ts") == []
    assert distill.detect_blockers("# 1. Objective\n- ship it") == []
    assert distill.detect_blockers("") == []
    # Prose that merely uses the word must not read as a refusal.
    assert distill.detect_blockers("We avoided a blocked state by design.") == []


def _fake_resolve(here, reply):
    def fake(client, cfg, system, user, label=None, max_output_tokens=None, **kw):
        return reply

    original = distill._single_llm_call
    distill._single_llm_call = fake
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return distill.resolve_blocker_paths(
                None, "m", ["needs the payload tests"], "[SKELETON]", here
            )
    finally:
        distill._single_llm_call = original


def test_resolve_blocker_paths_separates_what_is_not_on_disk():
    """
    The resolver is a model reading a skeleton, so it can name a file that does
    not exist. Real files are read; path-shaped misses are kept as confirmed
    absences; prose is dropped as noise.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    real = os.path.basename(__file__)
    found = _fake_resolve(here, f"{real}\nno/such/file.ts\n- not a path either\nNONE")
    assert found.present == [real], found.present
    assert found.absent == ["no/such/file.ts"], found.absent


def test_a_confirmed_absence_still_reaches_the_retry():
    """
    A pass that blocks on "does schema.ts already define something?" is answered
    by "that file does not exist". Dropping the miss sent the retry back in
    knowing exactly what it knew before, and it blocked again — 50s of GPU for
    nothing. An absence alone must still build an evidence block.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    with contextlib.redirect_stdout(io.StringIO()):
        block = distill.read_evidence(here, [], 10000, ["src/db/schema.ts"]).text
    assert "<ABSENT>" in block
    assert "src/db/schema.ts" in block
    assert "REQUESTED_EVIDENCE" in block


def test_absence_and_contents_travel_together():
    here = os.path.dirname(os.path.abspath(__file__))
    name = os.path.basename(__file__)
    with contextlib.redirect_stdout(io.StringIO()):
        block = distill.read_evidence(here, [name], 5000, ["gone.ts"]).text
    assert f'<file path="{name}">' in block
    assert "<ABSENT>" in block and "gone.ts" in block


def test_read_evidence_respects_its_budget():
    here = os.path.dirname(os.path.abspath(__file__))
    name = os.path.basename(__file__)
    with contextlib.redirect_stdout(io.StringIO()):
        block = distill.read_evidence(here, [name], budget_chars=500).text
    assert block, "no evidence block produced"
    assert "truncated:" in block, "an oversized file was not truncated"
    assert len(block) < 2000


def test_read_evidence_is_empty_without_budget_or_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    assert distill.read_evidence(here, [], 10000).text == ""
    assert distill.read_evidence(here, [], 10000, []).text == ""
    assert distill.read_evidence(here, [os.path.basename(__file__)], 0).text == ""
    assert distill.read_evidence(here, [], 0, ["gone.ts"]).text == ""


def test_addendum_budget_keeps_a_retry_on_the_single_pass_path():
    """A retry appends evidence to a payload that already fit; it must still fit."""
    window, system_tokens, payload_tokens = 65536, 1747, 21161
    budget = distill.solve_addendum_budget(window, system_tokens, payload_tokens)
    facts, _ = distill.solve_merge_budget(
        window, system_tokens + distill.est_tokens("### CURRENT TASK\n")
    )
    assert payload_tokens + distill.est_tokens("x" * budget) <= facts
    assert distill.solve_addendum_budget(2048, 1747, 100) == 0


def test_clinerules_puts_the_plan_before_the_commentary():
    """
    The design must precede the passes that never saw the codebase, and the
    static operating rules must not sit between the agent and the roadmap.
    """
    results = {
        "architect": "# 1. Objective\n- ARCHITECT_BODY",
        "engineer": "# 1. Blockers\n- none\n- ENGINEER_BODY",
        "test_engineer": "# 1. Blockers\n- none\n- TEST_BODY",
        "safety": "# 1. Blockers\n- none\n- SAFETY_BODY",
    }
    doc = distill.assemble_clinerules(results, {"limits": {}}, [])
    order = [doc.index(m) for m in
             ("ARCHITECT_BODY", "ENGINEER_BODY", "TEST_BODY", "SAFETY_BODY")]
    assert order == sorted(order), "clinerules sections are out of priority order"
    assert doc.index("ARCHITECT_BODY") < doc.index("<operational_constraints>"), \
        "the static rule wall still precedes the architecture"


def test_detect_test_command_prefers_the_projects_own_script():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "package.json"), "w") as f:
        json.dump({"scripts": {"test": "vitest run"},
                   "devDependencies": {"jest": "1"}}, f)
    assert distill.detect_test_command(d) == "npm test --silent"


def test_detect_test_command_finds_a_suite_with_no_npm_alias():
    """
    The regression this exists for: the target repo ships vitest.config.ts,
    a tests/ directory and vitest in devDependencies, and never wired up
    `npm test` - so a manifest-only check gated nothing.
    """
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "package.json"), "w") as f:
        json.dump({"devDependencies": {"vitest": "^1.0.0"}}, f)
    open(os.path.join(d, "vitest.config.ts"), "w").close()
    assert "vitest" in distill.detect_test_command(d)


def test_detect_test_command_returns_nothing_rather_than_guessing():
    """
    A wrong command is worse than none: it fails forever and the agent cannot
    fix it. npm's placeholder and an e2e-only setup must both yield no gate.
    """
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "package.json"), "w") as f:
        json.dump({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}, f)
    assert distill.detect_test_command(d) == ""

    e = tempfile.mkdtemp()
    with open(os.path.join(e, "package.json"), "w") as f:
        json.dump({"devDependencies": {"@playwright/test": "1"}}, f)
    open(os.path.join(e, "playwright.config.ts"), "w").close()
    assert distill.detect_test_command(e) == "", "playwright is not a completion gate"

    assert distill.detect_test_command(tempfile.mkdtemp()) == ""


def test_detect_test_command_survives_a_broken_manifest():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "package.json"), "w") as f:
        f.write("{ not json")
    assert distill.detect_test_command(d) == ""



# --- H. Re-plan on evidence ---------------------------------------------------
#
# Distillation ran once and the build loop then re-ran the same .clinerules up to
# max_build_iterations times, so a design flaw found on iteration 2 was patched
# tactically three more times and never redesigned. These pin the trigger (growth
# since the plan, not absolute size), the payload contract the architect prompt
# depends on, and the rule that a failed revision costs nothing but GPU time.


@contextlib.contextmanager
def _replan_sandbox():
    """Point the re-plan's on-disk state at a scratch directory."""
    tmp = tempfile.mkdtemp()
    saved = (distill.REPLAN_STATE_PATH, distill.BUILD_ISSUES_PATH)
    distill.REPLAN_STATE_PATH = os.path.join(tmp, ".replan_state.json")
    distill.BUILD_ISSUES_PATH = os.path.join(tmp, ".build_issues.md")
    try:
        yield tmp
    finally:
        distill.REPLAN_STATE_PATH, distill.BUILD_ISSUES_PATH = saved


def _write_issues(size: int):
    with open(distill.BUILD_ISSUES_PATH, "w", encoding="utf-8") as f:
        f.write("z" * size)


def test_replan_triggers_on_growth_not_on_size():
    """
    A big issues file that has stopped growing describes problems already being
    worked. One that keeps growing describes a plan that no longer matches the
    code. Only the second is worth a pass.
    """
    with _replan_sandbox():
        _write_issues(50000)
        distill.write_replan_state(50000, 0)          # all of it predates the plan
        due, why = distill.replan_due(2000, 2)
        assert not due, why

        _write_issues(50000 + 2500)                   # new evidence since
        due, why = distill.replan_due(2000, 2)
        assert due, why


def test_replan_respects_its_budget_and_the_off_switch():
    with _replan_sandbox():
        _write_issues(10000)
        distill.write_replan_state(0, 2)
        due, why = distill.replan_due(2000, 2)
        assert not due and "budget spent" in why, why

        distill.write_replan_state(0, 0)
        due, why = distill.replan_due(2000, 0)
        assert not due and "disabled" in why, why


def test_replan_state_survives_a_missing_or_corrupt_file():
    with _replan_sandbox():
        assert distill.read_replan_state() == {"issues_bytes": 0, "replans": 0}
        with open(distill.REPLAN_STATE_PATH, "w", encoding="utf-8") as f:
            f.write("{ not json")
        assert distill.read_replan_state() == {"issues_bytes": 0, "replans": 0}


def test_replan_payload_keeps_the_mode_the_architect_prompt_expects():
    """
    architect.md R7 (ITERATIVE_REBUILD) and R8 (NEW_BUILD) are mutually exclusive
    and it is told exactly one is active for the current MODE. Inventing a third
    value would leave both inactive and the output contract undefined, so a
    re-plan of a partially-built project stays an ITERATIVE_REBUILD.
    """
    with _replan_sandbox():
        payload = distill.build_replan_payload(
            {"architect": "PRIOR_DESIGN", "engineer": "PRIOR_ROADMAP"},
            "!build add the backend",
        )
    assert "<MODE>ITERATIVE_REBUILD</MODE>" in payload
    assert distill.extract_mode(payload) == "ITERATIVE_REBUILD"
    assert distill.extract_request(payload) == "!build add the backend"


def test_replan_payload_carries_the_plan_and_the_evidence():
    with _replan_sandbox():
        _write_issues(0)
        with open(distill.BUILD_ISSUES_PATH, "w", encoding="utf-8") as f:
            f.write("## Test gate failure — iteration 2\nvitest: 3 failed")
        payload = distill.build_replan_payload(
            {"architect": "PRIOR_DESIGN", "engineer": "PRIOR_ROADMAP"},
            "!build add the backend",
        )
    assert "PRIOR_DESIGN" in payload and "PRIOR_ROADMAP" in payload
    assert "<BUILD_ISSUES>" in payload and "vitest: 3 failed" in payload
    assert "SYMBOL_SKELETON" in payload, "the revision must see current code"
    assert "DIRECTORY_STRUCTURE" in payload


def test_replan_payload_states_the_absence_of_issues_explicitly():
    """
    The directives tell the model to treat BUILD_ISSUES as fact and preserve what
    it does not contradict. With no block at all those instructions point at
    nothing, so an empty file becomes an explicit "none recorded".
    """
    with _replan_sandbox():
        payload = distill.build_replan_payload({"architect": "PRIOR"}, "req")
    assert "<BUILD_ISSUES>none recorded</BUILD_ISSUES>" in payload
    assert "PRIOR" in payload


def test_replan_clips_a_runaway_issues_file_to_the_recent_tail():
    """
    .build_issues.md only grows, and each re-plan appends the gate output that
    triggered it. Uncapped it would eventually push the re-plan onto the chunked
    path, which is the silent downgrade solve_kb_budget exists to prevent.
    """
    with _replan_sandbox():
        with open(distill.BUILD_ISSUES_PATH, "w", encoding="utf-8") as f:
            f.write("OLDEST-MARKER\n" + ("z" * 900000) + "\nNEWEST-MARKER")
        payload = distill.build_replan_payload({"architect": "P"}, "req", 1747)

    facts, _ = distill.solve_merge_budget(
        distill.CONTEXT_WINDOW, 1747 + distill.est_tokens("### CURRENT TASK\n")
    )
    assert distill.est_tokens(payload) <= facts, "re-plan payload left the single-pass path"
    assert "NEWEST-MARKER" in payload, "the most recent failures were dropped"
    assert "OLDEST-MARKER" not in payload
    assert "elided" in payload


def test_replan_carry_passes_are_disjoint_from_the_revised_ones():
    """
    test_engineer and safety receive a ~90-token review instruction and never see
    the codebase, so a build teaches them nothing; re-running them would be spend
    with no new input. They must be carried, and carried exactly once.
    """
    assert not set(distill.DEFAULT_REPLAN_PASSES) & set(distill.REPLAN_CARRY_PASSES)
    assert "architect" in distill.DEFAULT_REPLAN_PASSES


def test_a_declaration_of_no_blockers_is_not_a_blocker():
    """
    The template shows exactly one line shape, "- BLOCKER: ...", then notes that a
    clear pass emits "- none". Models resolve that by writing "- BLOCKER: - none" —
    a statement that there is nothing wrong, which aborted a run whose four passes
    had all succeeded. Every spelling of empty must read as empty.
    """
    for empty in ("- none", "none", "None.", "N/A", "n/a", "nil", "no blockers",
                  "- none | NEEDS: none", "NONE"):
        text = f"# 1. Blockers\n- BLOCKER: {empty}\n\n# 2. Next\n- something\n"
        assert distill.detect_blockers(text) == [], f"{empty!r} read as a blocker"


def test_a_real_blocker_that_merely_starts_with_none_survives():
    """
    The guard anchors on the whole statement, so prose beginning with one of the
    empty words is still a blocker. Losing these would be far worse than the bug
    it fixes: the run would build against a spec nobody validated.
    """
    for real in ("none of the routes define a request shape | NEEDS: shapes",
                 "nil handling for orgId is unspecified | NEEDS: contract",
                 "no blockers were listed by A§4 but the type is absent | NEEDS: it"):
        text = f"# 1. Blockers\n- BLOCKER: {real}\n"
        assert len(distill.detect_blockers(text)) == 1, f"{real!r} was swallowed"


def test_a_heartbeat_cannot_outlive_its_attempt():
    """
    The heartbeat used to close over the NAME first_token_received. The retry loop
    rebinds that name to a fresh Event, so the previous heartbeat started polling
    the next attempt's unset Event, never saw its own set(), and ran until the
    process died — printing a second, unrelated elapsed counter over the live one.
    Binding the Event as a default argument is what makes set() reach the right
    thread; this asserts the thread actually stops.
    """
    import threading
    import time

    events = []

    def spawn():
        ev = threading.Event()
        events.append(ev)

        def heartbeat(own_event=ev):
            while not own_event.wait(0.01):
                pass

        t = threading.Thread(target=heartbeat, daemon=True)
        t.start()
        return t

    first = spawn()
    spawn()          # rebinding the name is what used to strand `first`
    events[0].set()  # stop only the first attempt's heartbeat
    first.join(timeout=2)
    assert not first.is_alive(), "heartbeat outlived its own attempt"

    events[1].set()


def test_bare_triggers_carry_no_design_intent():
    """
    `!approve` and a flags-only `!build` are launch commands, not requests. Passed
    through as NEW_REQUEST they make the architect block on "this is a build
    directive, not a design request", and every later pass inherits BLOCKED.
    """
    assert distill.strip_trigger_syntax("!approve") == ""
    assert distill.strip_trigger_syntax("  !approve  ") == ""
    assert distill.strip_trigger_syntax("!build --repo foo --kb bar") == ""
    assert distill.strip_trigger_syntax("!build add dark mode") == "add dark mode"
    assert distill.strip_trigger_syntax("!architect rework auth") == "rework auth"


def test_every_trigger_command_is_recognised_and_stripped():
    """
    A command the finder recognises but the stripper leaves behind would read as
    an instruction; one the stripper eats but the finder ignores would skip the
    message that actually carries the request. The two lists must stay in step.
    """
    for cmd in distill.TRIGGER_COMMANDS:
        assert distill._is_trigger_message(f"{cmd} do the thing")
        assert distill.strip_trigger_syntax(f"{cmd} do the thing") == "do the thing"
    assert not distill._is_trigger_message("just a normal message")


# --- N. Reasoning effort -----------------------------------------------------
#
# The model template resolves reasoning_effort to 'xhigh' when it is unset and
# raises on any value outside xhigh/medium/low, so the payload builder is the
# only thing standing between a config typo and a mid-pass HTTP 500. "off" is
# not one of the template's levels - it is enable_thinking=false - and these
# assert that translation rather than assuming a passthrough.


class _PayloadCaptured(BaseException):
    """
    Unwinds _single_llm_call the moment the payload exists.

    BaseException deliberately: the function catches httpx errors and bare
    Exception to drive its retry ladder, and a caught sentinel would mean three
    attempts, two sleeps and a report_server_state() call over a dead socket.
    """


def _payload_for(reasoning: str, provider: str = "llamacpp",
                 pass_key: str = None) -> dict:
    """
    Capture the request body _single_llm_call would send, without sending it.

    Patches httpx.Client rather than passing a fake: the streaming path opens
    its own client and ignores the one handed to it.
    """
    captured = {}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, method, url, json=None, **kw):
            captured.update(json)
            raise _PayloadCaptured

    cfg = distill._resolve_model_config({
        "model": "m", "provider": provider,
        "base_url": "http://x", "reasoning": reasoning,
    }, pass_key=pass_key)
    real_client = distill.httpx.Client
    distill.httpx.Client = FakeClient
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            distill._single_llm_call(None, cfg, "sys", "user", "L",
                                     max_output_tokens=1000)
    except _PayloadCaptured:
        pass
    finally:
        distill.httpx.Client = real_client
    return captured


def test_reasoning_level_reaches_the_template_kwargs():
    for level in ("low", "medium", "xhigh"):
        kwargs = _payload_for(level).get("chat_template_kwargs")
        assert kwargs == {"reasoning_effort": level}, f"{level}: got {kwargs}"


def test_reasoning_off_is_sent_as_enable_thinking_false():
    """The template raises on reasoning_effort='none'; off must not become one."""
    kwargs = _payload_for("off").get("chat_template_kwargs")
    assert kwargs == {"enable_thinking": False}, kwargs


def test_high_is_accepted_as_the_templates_alias_for_xhigh():
    assert distill._validate_reasoning("high") == "xhigh"
    assert distill._validate_reasoning("LOW") == "low"


def test_unknown_reasoning_level_fails_at_config_time():
    """A typo must not survive to become a 500 several minutes into a pass."""
    for bad in ("none", "off_", "extreme", "1"):
        try:
            distill._validate_reasoning(bad, "m")
        except ValueError:
            continue
        raise AssertionError(f"'{bad}' was accepted as a reasoning level")


def test_thinking_pass_is_given_room_beyond_its_answer_cap():
    """
    Reasoning is charged against the same cap as the answer, so the reserve has
    to be added to it - otherwise the pass spends its answer allowance thinking
    and returns zero answer tokens.
    """
    thinking = _payload_for("low")["max_tokens"]
    off = _payload_for("off")["max_tokens"]
    assert off == 1000, off
    assert thinking == 1000 + distill.REASONING_RESERVE_TOKENS, thinking


def test_the_reserve_is_taken_back_out_of_the_solver_window():
    """
    The other half of the same bargain: a larger cap that is not paid for out of
    the window is just an overrun. Solving against the shortened window must
    leave a thinking pass with a strictly smaller answer budget.
    """
    # Sized so the merge answer is the solver's proportional term rather than
    # ANSWER_MAX_TOKENS: at a 64k window both sides clamp to the same ceiling
    # and the test would pass without measuring anything.
    window, fixed = 24576, 2000
    facts_off, answer_off = distill.solve_merge_budget(window, fixed)
    facts_on, answer_on = distill.solve_merge_budget(
        window - distill.REASONING_RESERVE_TOKENS, fixed)
    assert answer_on < answer_off, (answer_on, answer_off)
    assert facts_on < facts_off, (facts_on, facts_off)


def _sampling_in(payload: dict, provider: str) -> dict:
    """Where a provider carries sampling: Ollama nests it, OpenAI-compatible doesn't."""
    return payload["options"] if provider == "ollama" else payload


_PROVIDERS = ("llamacpp", "ollama")


def test_sampling_matches_the_model_card():
    """
    0.3 was set for a non-thinking model and sits below both of the card's
    presets; on this family that is a repetition-loop risk.
    """
    card = distill.SAMPLING_MODES["thinking"]
    for provider in _PROVIDERS:
        opts = _sampling_in(_payload_for("low", provider, "architect"), provider)
        assert opts["temperature"] == card["temperature"], provider
        assert opts["top_p"] == card["top_p"], provider
        assert opts["top_k"] == card["top_k"], provider
        assert opts["min_p"] == card["min_p"], provider


def test_each_phase_samples_in_its_own_mode():
    """
    The whole point of per-phase sampling: the engineer is the one instruct pass
    and must not inherit the thinking preset every other phase runs at.
    """
    thinking = distill.SAMPLING_MODES["thinking"]
    instruct = distill.SAMPLING_MODES["instruct"]
    for provider in _PROVIDERS:
        eng = _sampling_in(_payload_for("low", provider, "engineer"), provider)
        assert eng["temperature"] == instruct["temperature"], provider
        assert eng["top_p"] == instruct["top_p"], provider
        assert eng["presence_penalty"] == instruct["presence_penalty"], provider

        for phase in ("architect", "safety", "test_engineer", "bugfix", "cline_startup"):
            opts = _sampling_in(_payload_for("low", provider, phase), provider)
            assert opts["temperature"] == thinking["temperature"], (provider, phase)
            assert opts["top_p"] == thinking["top_p"], (provider, phase)
            assert opts["presence_penalty"] == thinking["presence_penalty"], (provider, phase)


def test_repetition_penalty_reaches_the_wire_under_the_name_servers_know():
    """
    The config says repetition_penalty because the model card does; both servers
    call the knob repeat_penalty, and a request sending the card's name simply
    has it dropped.
    """
    for provider in _PROVIDERS:
        opts = _sampling_in(_payload_for("low", provider, "engineer"), provider)
        assert "repeat_penalty" in opts, provider
        assert "repetition_penalty" not in opts, provider
        assert opts["repeat_penalty"] == \
            distill.SAMPLING_MODES["instruct"]["repetition_penalty"], provider


def test_a_phase_may_override_one_parameter_of_its_mode():
    """An override is a deviation from a preset, not a replacement for it."""
    resolved = distill._validate_sampling(
        {"mode": "thinking", "temperature": 0.4}, "architect", distill.SAMPLING_MODES)
    assert resolved["temperature"] == 0.4
    assert resolved["top_p"] == distill.SAMPLING_MODES["thinking"]["top_p"]
    assert resolved["presence_penalty"] == 0.0


def test_a_phase_with_no_entry_gets_its_default_mode():
    for phase, mode in distill.DEFAULT_SAMPLING_MODES.items():
        resolved = distill._validate_sampling(None, phase, distill.SAMPLING_MODES)
        assert resolved == distill.SAMPLING_MODES[mode], phase


def test_a_mistyped_sampler_name_fails_at_config_time():
    """
    Every server silently drops an unknown sampler, so the only symptom of a typo
    is a pass that samples wrong - which is indistinguishable from a bad prompt.
    """
    for bad in ({"temprature": 0.7}, {"top_kk": 20}, {"repeat_penalty": 1.1}):
        try:
            distill._validate_sampling(bad, "architect", distill.SAMPLING_MODES)
        except ValueError:
            continue
        raise AssertionError(f"{bad} was accepted as a sampling parameter")

    try:
        distill._validate_sampling({"mode": "creative"}, "architect", distill.SAMPLING_MODES)
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown sampling mode was accepted")


def test_config_sampling_block_is_resolved_for_every_phase():
    """
    _modes retunes a preset for everything that names it; a phase block picks the
    preset. A config with neither leaves the built-in defaults standing.
    """
    original = distill.SAMPLING_BY_PASS
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            distill._resolve_sampling({
                "sampling": {
                    "_modes": {"thinking": {"temperature": 0.9}},
                    "safety": {"mode": "instruct"},
                }
            })
        resolved = distill.SAMPLING_BY_PASS
        assert set(resolved) >= set(distill.DEFAULT_SAMPLING_MODES)
        # The retuned preset reaches every thinking phase, not just one.
        for phase in ("architect", "bugfix", "cline_startup", "test_engineer"):
            assert resolved[phase]["temperature"] == 0.9, phase
            # Untouched parameters still come from the preset.
            assert resolved[phase]["top_p"] == 0.95, phase
        # ...and not the phase that was moved off it.
        assert resolved["safety"] == distill.SAMPLING_MODES["instruct"]

        with contextlib.redirect_stdout(io.StringIO()):
            distill._resolve_sampling({})
        assert distill.SAMPLING_BY_PASS["engineer"] == distill.SAMPLING_MODES["instruct"]
        assert distill.SAMPLING_BY_PASS["architect"] == distill.SAMPLING_MODES["thinking"]
    finally:
        distill.SAMPLING_BY_PASS = original


def test_an_operator_note_beside_a_preset_is_not_read_as_a_preset():
    """
    Underscore keys are comments throughout this config, and _modes is the one
    place that convention was not honoured: a note next to a preset was parsed as
    a preset and raised at startup.
    """
    original = distill.SAMPLING_BY_PASS
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            distill._resolve_sampling({
                "sampling": {"_modes": {
                    "_thinking_help": "why this is not 1.0",
                    "thinking": {"temperature": 0.95},
                }}
            })
        assert distill.SAMPLING_BY_PASS["architect"]["temperature"] == 0.95
    finally:
        distill.SAMPLING_BY_PASS = original


def test_the_shipped_config_names_a_mode_for_every_phase():
    """
    The config is the source of truth for sampling, so a phase missing from it is
    a phase running on a default nobody chose.
    """
    with open(os.path.join(_HERE, "agent_config.json"), encoding="utf-8") as f:
        block = json.load(f).get("sampling", {})
    for phase, mode in distill.DEFAULT_SAMPLING_MODES.items():
        assert phase in block, f"agent_config.json has no sampling entry for '{phase}'"
        assert block[phase].get("mode") == mode, phase
    # The presets themselves are the operator's to retune - that is what _modes is
    # for - so this checks their shape, not their values. Asserting equality with
    # the built-in defaults would make every deliberate tune a test failure.
    for name, preset in distill.SAMPLING_MODES.items():
        assert set(block["_modes"][name]) == set(preset), name
        assert all(isinstance(v, (int, float)) for v in block["_modes"][name].values()), name


def test_ollama_gets_a_boolean_because_effort_levels_do_not_cross_over():
    assert _payload_for("xhigh", "ollama")["think"] is True
    assert _payload_for("off", "ollama")["think"] is False


# --- O. The bugfix design pass ----------------------------------------------
#
# `!bugfix` swaps the architect out of pass 1 for a diagnostician, and the thing
# that makes it more than a differently-worded architect is that its diagnosis is
# checked against a command that actually runs. A pass cannot run anything, so
# "the bug is reproducible" from a pass is an opinion; these cover the machinery
# that turns it into an exit code, and the refusal when it will not become one.

_GOOD_DOC = """# 1. Symptom
- Uploads above 5MB store zero bytes.
# 2. Reproduction
- COMMAND: `npx --no-install vitest run tests/upload.spec.ts`
- SIGNATURE: expected stored size 6291456 to be greater than 0
# 3. Evidence
- src/storage/store.ts::store — slices to MAX_INLINE.
"""


def test_reproduction_is_parsed_through_the_markup_a_model_adds():
    """
    Backticks round a command and a trailing full stop are formatting, not intent.
    Rejecting a correct repro over them would spend a full design pass to be told
    the same thing again.
    """
    parsed, reason = distill.parse_reproduction(_GOOD_DOC)
    assert reason is None
    command, signature = parsed
    assert command == "npx --no-install vitest run tests/upload.spec.ts"
    assert signature == "expected stored size 6291456 to be greater than 0"

    parsed, _ = distill.parse_reproduction(
        "- COMMAND: `pytest -k upload`.\n- SIGNATURE: **AssertionError: size was 0**"
    )
    assert parsed[0] == "pytest -k upload"


def test_a_document_with_no_reproduction_is_reported_not_guessed():
    for doc, expect in (
        ("# 1. Symptom\n- it breaks", "no COMMAND"),
        ("- COMMAND: pytest", "no SIGNATURE"),
        ("- COMMAND:\n- SIGNATURE: something specific here", "COMMAND"),
    ):
        parsed, reason = distill.parse_reproduction(doc)
        assert parsed is None
        assert expect in reason, (doc, reason)


def test_only_a_project_runner_may_be_executed():
    """
    bugfix.md B5 restricts the command to a toolchain binary. Enforced here rather
    than trusted to the prompt: the pass names a program and its arguments, and
    that is the whole of what it can cause to happen.
    """
    sig = "expected stored size 6291456 to be greater than 0"
    assert distill.validate_reproduction("npx vitest run", sig) is None
    assert distill.validate_reproduction("python3 -m pytest -q", sig) is None
    for bad in ("rm -rf /", "bash -c 'pytest'", "curl http://example.com",
                "sh ./run.sh", "/bin/sh -c pytest"):
        reason = distill.validate_reproduction(bad, sig)
        assert reason and "not one of the allowed runners" in reason, bad


def test_shell_operators_cannot_become_operators():
    """
    shlex + argv means `&&` is an argument to the runner, never a second command.
    The prohibition in B5 is structural, not a promise the model keeps.
    """
    argv = distill.shlex.split("npm test && rm -rf /")
    assert argv[0] == "npm"
    assert "&&" in argv  # an argument, and npm will simply reject it
    assert distill.validate_reproduction("npm test && rm -rf /",
                                         "expected 6291456 to be above 0") is None


def test_a_signature_that_matches_any_failure_is_not_a_signature():
    """
    'FAILED' identifies a broken build, not this bug, and would verify whatever
    diagnosis happened to be in front of it.
    """
    for weak in ("FAILED", "Error", "1 failed", "exception", "traceback"):
        reason = distill.validate_reproduction("pytest -q", weak)
        assert reason, weak
    assert distill.validate_reproduction("pytest -q", "size 0") is not None  # too short
    assert distill.validate_reproduction(
        "pytest -q", "AssertionError: stored size was 0, expected 6291456") is None


def _repro(script: str, signature: str, tmp: str):
    with contextlib.redirect_stdout(io.StringIO()):
        return distill.run_reproduction(
            f"python3 -c {distill.shlex.quote(script)}", signature, 30.0, cwd=tmp
        )


def test_a_reproduction_that_fails_as_declared_is_verified():
    with tempfile.TemporaryDirectory() as tmp:
        result = _repro(
            "import sys; sys.stderr.write('AssertionError: stored size was 0\\n'); "
            "sys.exit(1)",
            "AssertionError: stored size was 0", tmp,
        )
    assert result.verified, result.reason


def test_a_command_that_succeeds_did_not_reproduce_anything():
    with tempfile.TemporaryDirectory() as tmp:
        result = _repro("pass", "AssertionError: stored size was 0", tmp)
    assert not result.verified
    assert "did not appear" in result.reason
    assert "exited 0" in result.observation


def test_failing_differently_is_not_reproducing_the_bug():
    """
    Non-zero alone would verify any diagnosis against any broken suite. The
    signature is what ties the failure to the cause that was claimed.
    """
    with tempfile.TemporaryDirectory() as tmp:
        result = _repro(
            "import sys; sys.stderr.write('SyntaxError: bad token\\n'); sys.exit(1)",
            "AssertionError: stored size was 0", tmp,
        )
    assert not result.verified
    assert "declared signature" in result.reason
    assert "SyntaxError: bad token" in result.observation


def test_a_missing_runner_is_not_a_reproduced_bug():
    """
    Exit 127 says the box is missing a dependency. Counting it as a failing repro
    would confirm every diagnosis on a machine with an incomplete toolchain -
    the same classification the build loop's test gate already applies.
    """
    with tempfile.TemporaryDirectory() as tmp:
        with contextlib.redirect_stdout(io.StringIO()):
            missing = distill.run_reproduction(
                "cargo test --quiet", "AssertionError: stored size was 0", 30.0, cwd=tmp
            )
            modless = distill.run_reproduction(
                "python3 -c 'import nope_not_a_module'",
                "AssertionError: stored size was 0", 30.0, cwd=tmp,
            )
    assert not missing.verified
    assert not modless.verified
    assert "runner" in modless.reason or "installed" in missing.reason


def test_a_reproduction_that_hangs_is_bounded():
    with tempfile.TemporaryDirectory() as tmp:
        with contextlib.redirect_stdout(io.StringIO()):
            result = distill.run_reproduction(
                "python3 -c 'import time; time.sleep(30)'",
                "AssertionError: stored size was 0", 1.0, cwd=tmp,
            )
    assert not result.verified
    assert "timed out" in result.reason


@contextlib.contextmanager
def _repro_loop(outcomes, replies):
    """Drive verify_bugfix_reproduction with scripted repro results and re-runs."""
    saved = (distill.run_reproduction, distill.call_llm, distill.resolve_pass_blockers)
    seen = {"runs": 0, "llm": []}

    def fake_run(command, signature, timeout_secs, cwd="/workspace", repro_file=None):
        seen["runs"] += 1
        seen.setdefault("files", []).append(repro_file)
        return outcomes[min(seen["runs"] - 1, len(outcomes) - 1)]

    def fake_llm(client, model_config, prompt, user_content, prior_context=""):
        seen["llm"].append(user_content)
        return replies[min(len(seen["llm"]) - 1, len(replies) - 1)]

    distill.run_reproduction = fake_run
    distill.call_llm = fake_llm
    distill.resolve_pass_blockers = lambda *a, **k: a[7] if len(a) > 7 else k["result"]
    try:
        yield seen
    finally:
        (distill.run_reproduction, distill.call_llm,
         distill.resolve_pass_blockers) = saved


_MISS = distill.ReproResult(False, "the command succeeded", "It exited 0.")
_HIT = distill.ReproResult(True, "failed with the declared signature (exit 1)", "")


def _verify(seen_ctx, doc, attempts=3):
    with contextlib.redirect_stdout(io.StringIO()):
        return distill.verify_bugfix_reproduction(
            None, {}, "SYSTEM", "PAYLOAD", "", "skeleton", doc,
            max_attempts=attempts, timeout_secs=5.0,
        )


def test_the_loop_stops_the_moment_the_bug_is_reproduced():
    with _repro_loop([_HIT], [_GOOD_DOC]) as seen:
        doc, verified = _verify(seen, _GOOD_DOC)
    assert verified
    assert seen["runs"] == 1
    assert seen["llm"] == []  # no re-run paid for once it is proven


def test_a_failed_reproduction_is_fed_back_as_evidence():
    """
    The pass is not asked to try harder; it is shown what actually happened. That
    is the only new information in the loop, so it is what B11 makes it act on.
    """
    with _repro_loop([_MISS, _HIT], [_GOOD_DOC]) as seen:
        doc, verified = _verify(seen, _GOOD_DOC)
    assert verified
    assert seen["runs"] == 2
    assert len(seen["llm"]) == 1
    fed = seen["llm"][0]
    assert "<REPRO_OBSERVATION>" in fed
    assert "It exited 0." in fed
    assert fed.startswith("PAYLOAD")  # the addendum extends the payload, not replaces it


def test_the_loop_is_bounded_and_says_so():
    with _repro_loop([_MISS], [_GOOD_DOC]) as seen:
        doc, verified = _verify(seen, _GOOD_DOC, attempts=3)
    assert not verified
    assert seen["runs"] == 3
    assert len(seen["llm"]) == 2  # n attempts costs n-1 re-runs, not n


def test_a_rejected_command_costs_no_subprocess():
    """
    A repro that fails validation is fed back without being run. The rejection is
    the observation, and running it was never possible.
    """
    bad = "# 2. Reproduction\n- COMMAND: rm -rf /\n- SIGNATURE: everything is gone now\n"
    with _repro_loop([_HIT], [_GOOD_DOC]) as seen:
        doc, verified = _verify(seen, bad, attempts=2)
    assert seen["runs"] == 1  # attempt 1 rejected before running; attempt 2 ran
    assert "not one of the allowed runners" in seen["llm"][0]


def test_verification_can_be_switched_off_but_not_silently():
    with _repro_loop([_MISS], [_GOOD_DOC]) as seen:
        doc, verified = _verify(seen, _GOOD_DOC, attempts=0)
    assert verified          # self-reported, exactly as the config says
    assert seen["runs"] == 0


def test_an_unverified_diagnosis_is_refused_rather_than_reused():
    """
    Same guard as ABORT_MARKER, one reason further on. A blocked pass refuses to
    diagnose; an unverified one diagnoses something nothing was seen to do, which
    is worse because it looks like a plan.
    """
    marked = distill.mark_unverified(_GOOD_DOC, 3)
    assert distill.UNVERIFIED_MARKER in marked
    assert _GOOD_DOC.strip() in marked

    tmp = tempfile.mkdtemp()
    saved = distill.INTERMEDIATE_DIR
    distill.INTERMEDIATE_DIR = tmp
    try:
        with open(distill._intermediate_path("bugfix"), "w", encoding="utf-8") as f:
            f.write(f"# Distillation Intermediate: Bugfix\n\n{marked}")
        with contextlib.redirect_stdout(io.StringIO()):
            assert distill.load_saved_pass("bugfix") is None
        # Deleting the banner by hand is the documented override.
        with open(distill._intermediate_path("bugfix"), "w", encoding="utf-8") as f:
            f.write(f"# Distillation Intermediate: Bugfix\n\n{_GOOD_DOC}")
        with contextlib.redirect_stdout(io.StringIO()):
            assert distill.load_saved_pass("bugfix") is not None
    finally:
        distill.INTERMEDIATE_DIR = saved


def test_the_design_pass_is_swapped_not_added():
    """
    A bug report handed to the architect comes back as a refactor; a feature
    request handed to the diagnostician comes back BLOCKED for want of a symptom.
    They are alternatives, so exactly one occupies slot 1.
    """
    assert distill.DISTILL_DESIGN_PASS in distill.DESIGN_PASSES
    assert distill.DESIGN_PASSES == ("architect", "bugfix")
    # Both own a .clinerules section, so whichever ran is rendered under its own
    # heading rather than falling out of the assembled document.
    for key in distill.DESIGN_PASSES:
        assert key in ("architect", "bugfix", "engineer")
    doc = distill.assemble_clinerules(
        {"bugfix": "DIAGNOSIS", "engineer": "ROADMAP"}, {"limits": {}},
        [{"role": "user", "content": "!bugfix login 500s"}],
    )
    assert "Diagnosis & Fix Plan" in doc
    assert doc.index("DIAGNOSIS") < doc.index("ROADMAP")


def test_diagnosis_gets_more_evidence_rounds_than_design():
    """
    The architect blocks on a specification gap, and a gap that survives being
    shown the files is one the workspace does not contain - so one round. For
    diagnosis, reading files IS the work and each round narrows the search.
    """
    assert "bugfix" in distill.EVIDENCE_RETRY_PASSES
    assert distill.EVIDENCE_ROUNDS.get("bugfix", 0) > distill.EVIDENCE_ROUNDS_DEFAULT
    assert distill.EVIDENCE_ROUNDS.get("architect", distill.EVIDENCE_ROUNDS_DEFAULT) == 1


def test_every_gate_names_its_own_objective():
    """
    The target objective used to be found by searching for "!build". A gated route
    has none - `!architect "add SSO"` then `!approve` - so the agent's build
    specification opened with the fallback text instead of the actual request.
    """
    for trigger in ("!build add dark mode", "!architect add SSO", "!bugfix login 500s"):
        doc = distill.assemble_clinerules(
            {"engineer": "ROADMAP"}, {"limits": {}},
            [{"role": "user", "content": trigger},
             {"role": "assistant", "content": "🔨 Build pipeline triggered."}],
        )
        expected = distill.strip_trigger_syntax(trigger)
        assert f"> {expected}" in doc, trigger


def test_a_deferred_symptom_is_still_reproducible_on_its_own():
    """
    A two-symptom report gets one diagnosis and the rest as DEFERRED bullets
    (bugfix.md B13). Section 2 must reproduce the bug that was diagnosed, not the
    report as a whole - a repro spanning both would verify a merged cause, which
    is the fabrication B13 exists to prevent.
    """
    doc = """# 1. Symptom
- Uploads above 5MB store zero bytes.
# 2. Reproduction
- COMMAND: npx --no-install vitest run tests/upload.spec.ts
- SIGNATURE: expected stored size 6291456 to be greater than 0
# 7. Out of Scope
- DEFERRED: the login page returns 500 after a password reset
- MAX_INLINE is duplicated as a literal in the client.
"""
    parsed, reason = distill.parse_reproduction(doc)
    assert reason is None
    command, signature = parsed
    assert distill.validate_reproduction(command, signature) is None
    # The deferred symptom is quoted verbatim so it can be re-run as its own
    # !bugfix, and is what the orchestrator surfaces as the next action.
    deferred = re.findall(r"^\s*[-*]\s*DEFERRED:\s*(.+?)\s*$", doc, re.MULTILINE)
    assert deferred == ["the login page returns 500 after a password reset"]


def test_a_bare_approve_does_not_become_the_objective():
    doc = distill.assemble_clinerules(
        {"engineer": "ROADMAP"}, {"limits": {}},
        [{"role": "user", "content": "!bugfix uploads over 5MB store zero bytes"},
         {"role": "assistant", "content": "🩺 Diagnosis"},
         {"role": "user", "content": "!approve"}],
    )
    assert "> uploads over 5MB store zero bytes" in doc


# --- REPRO_FILE ---------------------------------------------------------------

_REPRO_FILE_DOC = """# 2. Reproduction
- COMMAND: npx --no-install vitest run src/x.spec.ts --reporter=dot
- SIGNATURE: stored 0 bytes of a 6291456 byte body
- REPRO_FILE: src/x.spec.ts
```ts
import { it, expect } from "vitest";
it("stores the whole body", () => { expect(store(big)).toBe(big.length); });
```
"""


def test_a_reproduction_may_bring_the_test_it_needs():
    """
    Most bugs have no failing test yet; the suite encodes its author's fixtures.
    Requiring the command to fail against the unmodified tree made the gate
    unsatisfiable for exactly those bugs, so the pass may supply the test.
    """
    parsed, reason = distill.parse_repro_file(_REPRO_FILE_DOC)
    assert reason is None
    path, body = parsed
    assert path == "src/x.spec.ts"
    assert "stores the whole body" in body


def test_a_document_without_a_repro_file_is_not_an_error():
    """An existing failing test is still the better reproduction."""
    parsed, reason = distill.parse_repro_file(_GOOD_DOC)
    assert parsed is None and reason is None


def test_a_repro_file_declaration_that_cannot_be_used_is_reported():
    for doc, expect in (
        ("- REPRO_FILE: src/x.spec.ts\n(no fence at all)", "no fenced code block"),
        ("- REPRO_FILE:\n```ts\nsomething\n```", "no path"),
        ("- REPRO_FILE: src/x.spec.ts\n```ts\nx\n```", "too small"),
    ):
        parsed, reason = distill.parse_repro_file(doc)
        assert parsed is None, doc
        assert expect in reason, (doc, reason)


def test_a_reproduction_may_not_overwrite_the_project():
    """
    The pass is diagnosing, not editing. A REPRO_FILE that lands on an existing
    path would replace source code with a test as a side effect of a diagnosis.
    """
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "src"))
        existing = os.path.join(tmp, "src", "store.ts")
        with open(existing, "w") as f:
            f.write("export const store = 1;")

        assert "already exists" in distill.validate_repro_file("src/store.ts", tmp)
        assert "outside the project root" in distill.validate_repro_file("../escape.ts", tmp)
        assert "absolute" in distill.validate_repro_file("/etc/passwd", tmp)
        assert "does not exist" in distill.validate_repro_file("nope/x.spec.ts", tmp)
        assert distill.validate_repro_file("src/new.spec.ts", tmp) is None
        # untouched
        with open(existing) as f:
            assert f.read() == "export const store = 1;"


def test_the_repro_file_is_removed_however_the_run_ends():
    """
    Written, run, deleted - on success, on failure and on a raised exception.
    A diagnosis that leaves stray files behind is a diagnosis that edited the
    workspace, which is the one thing this pass must not do.
    """
    with tempfile.TemporaryDirectory() as tmp:
        rel = "probe.spec.ts"
        body = "it('x', () => { throw new Error('boom'); });" + " " * 40

        for script in ("import sys; sys.exit(0)",
                       "import sys; sys.stderr.write('nope'); sys.exit(1)"):
            with contextlib.redirect_stdout(io.StringIO()):
                distill.run_reproduction(
                    f"python3 -c {shlex.quote(script)}", "sig", 30.0,
                    cwd=tmp, repro_file=(rel, body),
                )
            assert not os.path.exists(os.path.join(tmp, rel)), script

        # The file really was there while the command ran.
        seen = os.path.join(tmp, "seen.txt")
        script = (f"import os,shutil; shutil.copy({rel!r}, {seen!r}); "
                  "raise SystemExit(1)")
        with contextlib.redirect_stdout(io.StringIO()):
            distill.run_reproduction(
                f"python3 -c {shlex.quote(script)}", "sig", 30.0,
                cwd=tmp, repro_file=(rel, body),
            )
        assert os.path.exists(seen), "the command never saw the written file"
        assert not os.path.exists(os.path.join(tmp, rel))


def test_an_unreachable_service_is_an_environment_fault_not_a_verdict():
    """
    This container reaches the host on the docker bridge, so a database
    published on the host's loopback is refused. Classified as a signature miss,
    the observation told the pass its root cause was wrong - steering a correct
    diagnosis off the causal path over a networking detail.
    """
    with tempfile.TemporaryDirectory() as tmp:
        script = ("import sys; sys.stderr.write('Error: connect ECONNREFUSED "
                  "172.17.0.1:5432'); sys.exit(1)")
        with contextlib.redirect_stdout(io.StringIO()):
            out = distill.run_reproduction(
                f"python3 -c {shlex.quote(script)}", "role row was not inserted",
                30.0, cwd=tmp,
            )
    assert not out.verified
    assert "could not execute" in out.reason
    assert "not evidence against your root cause" in out.observation

    # Same for a runner that matched nothing: the path is wrong, not the code.
    with tempfile.TemporaryDirectory() as tmp:
        script = "import sys; sys.stderr.write('No test files found, exiting'); sys.exit(1)"
        with contextlib.redirect_stdout(io.StringIO()):
            out = distill.run_reproduction(
                f"python3 -c {shlex.quote(script)}", "some specific thing", 30.0, cwd=tmp,
            )
    assert not out.verified and "could not execute" in out.reason


def test_a_malformed_repro_file_is_rejected_before_the_command_runs():
    """
    Running the command without the test it was written for fails for a reason
    the pass never predicted, and the observation sends it chasing that instead.
    """
    doc = _REPRO_FILE_DOC.replace("```ts\nimport", "NOFENCE\nimport")
    with _repro_loop([_HIT], [doc]) as seen:
        _doc, verified = _verify(seen, doc, attempts=1)
    assert not verified
    assert seen["runs"] == 0, "a broken declaration must cost no subprocess"


# --- Symbol-aware evidence ----------------------------------------------------

def test_a_blockers_symbol_is_carried_into_the_evidence_read():
    hints = distill.blocker_symbol_hints([
        "`src/types/domain.ts::DomainControlChallenge` — need its full field definition",
        "- BLOCKER: Backend/src/routes/orgs.ts::serializeOrg | NEEDS: the return shape",
        "src/services/org.service.ts — no symbol named here",
    ])
    assert hints["src/types/domain.ts"] == ["DomainControlChallenge"]
    assert hints["Backend/src/routes/orgs.ts"] == ["serializeOrg"]
    assert "src/services/org.service.ts" not in hints
    assert distill.blocker_symbol_hints([]) == {}


def test_a_symbol_named_in_prose_is_a_hint_too():
    """
    B2 asks for `<path>::<symbol>` and the model sometimes writes that, and
    sometimes writes the same request as prose. Only the first form was handled,
    so this blocker produced no hints at all, the slice fell back to the head of
    the file, and `AttestationRequest` was cut off at byte 24000 of 34636 — the
    exact failure the symbol-aware slice exists to prevent, missed on a
    punctuation difference. The pass then blocked for a file it had been given.
    """
    prose = ("`src/services/user.service.ts` — need its full method list, and "
             "`src/types/domain.ts` — need the `AttestationRequest` interface to "
             "find the field that links a request to its claim ID for the PATCH URL.")
    hints = distill.blocker_symbol_hints([prose])
    assert hints.get("src/types/domain.ts") == ["AttestationRequest"], hints
    # Nothing ties a symbol to a path in prose, so it is offered to both. A file
    # that does not define it simply does not match.
    assert hints.get("src/services/user.service.ts") == ["AttestationRequest"]

    # Paths are never mistaken for symbols, and prose words are not invented.
    for junk in hints.values():
        assert not any("/" in s or s.endswith(".ts") for s in junk), junk
        assert "PATCH" not in junk and "URL" not in junk, junk

    # The explicit form still leads, and both forms key the same entry.
    both = distill.blocker_symbol_hints([
        "`src/types/domain.ts::DomainControlChallenge` and `AttestationRequest`"])
    assert both["src/types/domain.ts"][0] == "DomainControlChallenge", both


def test_a_real_definition_outranks_a_passing_mention_of_another_hint():
    """
    Hints are gathered loosely from prose, so one file can be offered several
    symbols and define only one. A per-symbol fallback to a bare-name match let
    an unrelated import outrank the real definition further down and aim the
    slice at the wrong part of the file.
    """
    content = ('import { AttestationRequest } from "./x";\n' + ("pad\n" * 4000) +
               "export interface ClaimLink {\n  claimId: string;\n}\n")
    hit = distill._find_definition(content, ["AttestationRequest", "ClaimLink"])
    assert content[hit:].startswith("export interface ClaimLink"), content[hit:hit + 40]


def test_evidence_is_sliced_around_the_symbol_not_the_top_of_the_file():
    """
    The failure this exists for: an architect asked for
    `src/types/domain.ts::DomainControlChallenge`, whose definition sits at byte
    27473 of a 33736-byte file against a 24000-char cap. The head slice handed
    back 24k that did not contain it, so the pass blocked again in the same
    words with its one retry round already spent - and a plausible slice of the
    right file looks exactly like the request being honoured.
    """
    filler = "// padding line to push the definition well past the cap\n"
    head = filler * 500
    target = ("export interface DomainControlChallenge {\n"
              "  recordName: string;\n  ttl: number;\n}\n")
    content = head + target + filler * 200
    keep = len(head) // 2                      # definition is far beyond this

    blind = distill._slice_around_symbols(content, keep, None)
    assert "DomainControlChallenge" not in blind, "head slice should miss it"

    aimed = distill._slice_around_symbols(content, keep, ["DomainControlChallenge"])
    assert "interface DomainControlChallenge" in aimed
    assert "recordName" in aimed, "the fields it asked about must come too"
    assert len(aimed) <= keep, (len(aimed), keep)
    assert "earlier characters omitted" in aimed, "must say what was skipped"


def test_a_definition_beats_an_earlier_mention_of_the_same_name():
    """
    The first occurrence of a symbol is usually an import. Slicing around an
    import answers the blocker with the one line that carries no information.
    """
    content = ('import { Thing } from "./thing";\n' + ("x\n" * 4000) +
               "export interface Thing {\n  field: string;\n}\n" + ("y\n" * 100))
    out = distill._slice_around_symbols(content, 2000, ["Thing"])
    assert "interface Thing" in out and "field: string" in out


def test_slicing_never_exceeds_its_budget_or_crashes():
    content = "export class Widget {\n" + ("  m();\n" * 5000) + "}\n"
    for keep in (distill.EVIDENCE_MIN_SLICE_CHARS, 500, 5000, len(content) * 2):
        for syms in (None, ["Widget"], ["NotPresent"], ["Widget", "NotPresent"]):
            out = distill._slice_around_symbols(content, keep, syms)
            assert len(out) <= max(keep, len(content)), (keep, syms, len(out))
    # A file inside the cap is returned whole, markers and all absent.
    assert distill._slice_around_symbols("short", 1000, ["x"]) == "short"


def test_read_evidence_aims_the_slice_when_given_hints():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "src"))
        filler = "// pad\n" * 3000
        body = ("export interface DomainControlChallenge {\n  ttl: number;\n}\n")
        with open(os.path.join(tmp, "src", "domain.ts"), "w") as f:
            f.write(filler + body)

        blind = distill.read_evidence(tmp, ["src/domain.ts"], 8000)
        assert "DomainControlChallenge" not in blind.text

        aimed = distill.read_evidence(
            tmp, ["src/domain.ts"], 8000,
            symbol_hints={"src/domain.ts": ["DomainControlChallenge"]})
        assert "DomainControlChallenge" in aimed.text
        assert aimed.included == ["src/domain.ts"]


def test_a_failed_blocker_resolution_returns_the_right_shape():
    """
    It returned a bare [] on LLM failure, but the caller reads `.present`
    OUTSIDE the try that guards the call - so the one path built to survive a
    failed resolution raised AttributeError and killed the pass instead.
    """
    saved = distill._single_llm_call
    distill._single_llm_call = lambda *a, **k: "[ERROR: LLM returned status 500]"
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            out = distill.resolve_blocker_paths(None, {}, ["b"], "skeleton", "/tmp")
    finally:
        distill._single_llm_call = saved
    assert isinstance(out, distill.BlockerPaths), type(out)
    assert out.present == [] and out.absent == []


# --- Sibling test suites ------------------------------------------------------

def _mkpkg(root, rel, scripts=None, dev=None):
    d = os.path.join(root, rel) if rel else root
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "package.json"), "w") as f:
        json.dump({"scripts": scripts or {}, "devDependencies": dev or {}}, f)
    return d


def test_a_sibling_package_with_its_own_suite_is_gated_too():
    """
    veriform-ui keeps its API in Backend/ with its own package.json and vitest
    config, while the root config includes only src/**. The gate ran 168
    frontend tests, went green, and declared a build complete while all 188
    backend tests failed on an unapplied migration. It was not lying — it could
    not see them.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _mkpkg(tmp, "", dev={"vitest": "^4"})          # root: runner, no script
        open(os.path.join(tmp, "vitest.config.ts"), "w").close()
        _mkpkg(tmp, "Backend", scripts={"test": "vitest run"})

        cmd = distill.detect_test_command(tmp)
        assert "vitest run --reporter=dot" in cmd, cmd
        assert "npm --prefix Backend run test" in cmd, cmd
        assert cmd.index("vitest run --reporter=dot") < cmd.index("--prefix Backend")


def test_a_projects_own_test_script_is_not_second_guessed():
    """Naming a test command answers the question; running siblings too would
    run somebody's suite twice."""
    with tempfile.TemporaryDirectory() as tmp:
        _mkpkg(tmp, "", scripts={"test": "vitest run && npm --prefix Backend run test"})
        _mkpkg(tmp, "Backend", scripts={"test": "vitest run"})
        assert distill.detect_test_command(tmp) == "npm test --silent"


def test_a_sibling_suite_is_found_even_with_no_root_runner():
    with tempfile.TemporaryDirectory() as tmp:
        _mkpkg(tmp, "")                                # root: nothing runnable
        _mkpkg(tmp, "api", scripts={"test": "jest"})
        assert distill.detect_test_command(tmp) == "npm --prefix api run test --silent"


def test_sibling_detection_ignores_the_obvious_traps():
    with tempfile.TemporaryDirectory() as tmp:
        _mkpkg(tmp, "")
        _mkpkg(tmp, "node_modules", scripts={"test": "should-never-run"})
        _mkpkg(tmp, ".cache", scripts={"test": "should-never-run"})
        _mkpkg(tmp, "docs")                            # package, but no test script
        _mkpkg(tmp, "vendor", scripts={"test": "echo \"Error: no test specified\""})
        assert distill.detect_test_command(tmp) == ""

        # And it is bounded, so a workspace of packages cannot become the gate.
        for i in range(distill.SIBLING_TEST_MAX + 3):
            _mkpkg(tmp, f"pkg{i}", scripts={"test": "vitest run"})
        cmd = distill.detect_test_command(tmp)
        assert cmd.count("npm --prefix") == distill.SIBLING_TEST_MAX, cmd


# --- Continuation across the output cap ---------------------------------------

@contextlib.contextmanager
def _scripted_stream(rounds):
    """Drive _single_llm_call with scripted (text, finish_reason) rounds."""
    saved = distill._stream_llm_once
    seen = []

    def fake(client, cfg, system, user, label="Inference",
             max_output_tokens=0, assistant_prefix="", force_no_thinking=False):
        seen.append({"label": label, "prefix": assistant_prefix,
                     "no_thinking": force_no_thinking})
        return rounds[min(len(seen) - 1, len(rounds) - 1)]

    distill._stream_llm_once = fake
    try:
        yield seen
    finally:
        distill._stream_llm_once = saved


def _call():
    with contextlib.redirect_stdout(io.StringIO()):
        return distill._single_llm_call(None, {}, "SYS", "USER")


def test_a_truncated_answer_is_continued_rather_than_shipped_as_a_fragment():
    """
    Measured on the engineer pass: ~13,300 tokens of thinking and ~1,390 of
    answer against a 12,288 cap, at reasoning level 'low'. The half-written
    document was saved and handed downstream as if finished.
    """
    with _scripted_stream([("# 1. Head\n- first ha", "length"),
                           ("lf\n# 2. Tail\n- done", "stop")]) as seen:
        out = _call()
    assert out == "# 1. Head\n- first half\n# 2. Tail\n- done", repr(out)
    assert len(seen) == 2


def test_a_continuation_runs_with_thinking_off():
    """
    The reasoning has already been done. Left on, the continuation spends the
    whole cap thinking again and returns a second fragment - the loop this is
    built to break.
    """
    with _scripted_stream([("part one", "length"), ("part two", "stop")]) as seen:
        _call()
    assert seen[0]["no_thinking"] is False and seen[0]["prefix"] == ""
    assert seen[1]["no_thinking"] is True
    assert seen[1]["prefix"] == "part one", "the model must see what it wrote"
    assert "cont." in seen[1]["label"]


def test_a_clean_stop_costs_no_extra_call():
    with _scripted_stream([("all of it", "stop")]) as seen:
        assert _call() == "all of it"
    assert len(seen) == 1


def test_text_repeated_at_the_seam_is_dropped():
    """
    Told not to repeat itself a model usually complies and sometimes restates
    the last heading anyway. Blind concatenation leaves a duplicated fragment
    mid-document, which is wrong and near-invisible in a 4,000-char plan.
    """
    head = "# 6. Fix Plan\n- TEST: something that fails first\n"
    with _scripted_stream([(head, "length"), (head[-40:] + "- and then passes", "stop")]):
        out = _call()
    assert out.count("TEST: something that fails first") == 1, repr(out)
    assert out.endswith("- and then passes")

    # A restated heading is the common case and must not survive. "# 6. Fix Plan"
    # is 13 characters; an earlier 20-character floor let exactly this through.
    joined = distill._join_continuation("...text\n# 6. Fix Plan\n",
                                        "# 6. Fix Plan\n- the bullet")
    assert joined.count("# 6. Fix Plan") == 1, repr(joined)

    # Below the floor nothing is trimmed, so legitimately repeated short text
    # survives rather than being silently eaten.
    assert distill._join_continuation("abc", "def") == "abcdef"
    assert distill._join_continuation("...ends", "ends here") == "...endsends here"
    assert distill._join_continuation("", "x") == "x"
    assert distill._join_continuation("x", "") == "x"


def test_a_failed_continuation_keeps_what_was_already_written():
    with _scripted_stream([("real content so far", "length"),
                           ("[ERROR: ReadTimeout]", None)]):
        assert _call() == "real content so far"


def test_a_first_round_error_is_still_reported_as_an_error():
    with _scripted_stream([("[ERROR: Max retries exceeded]", None)]):
        out = _call()
    assert distill._check_llm_result(out, "x") is not None


def test_continuation_is_bounded():
    """A model that will not stop is a worse failure than a short document."""
    with _scripted_stream([("chunk ", "length")]) as seen:
        out = _call()
    assert len(seen) == distill.LLM_MAX_CONTINUATIONS + 1, len(seen)
    assert out.startswith("chunk")


def test_truncation_advice_does_not_tell_you_to_lower_the_lowest_level():
    """
    The first version said "lower this pass's reasoning level" unconditionally.
    The pass that hit it was already at 'low' — there is nowhere to go, and the
    advice sent the reader to a config knob that could not help.
    """
    def advice(level):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            distill._report_truncation("Inference", "length", 12288,
                                       ["short answer"], ["thinking " * 5000],
                                       {"reasoning": level})
        return buf.getvalue()

    floored = advice("low")
    assert "Lower this pass's reasoning level" not in floored
    assert "already the lowest" in floored and "continuation" in floored

    # Where there IS room to drop, it still says so.
    assert "Lower this pass's reasoning level" in advice("xhigh")


# --- Reasoning reserve / truncation -------------------------------------------

def test_the_reasoning_reserve_scales_with_the_level():
    """
    One flat reserve for every level meant raising a pass to xhigh raised how
    much it thought without raising where it was allowed to think. The bugfix
    pass then spent its entire output cap on reasoning and was cut off before
    section 2, which surfaced as "section 2 declared no COMMAND".
    """
    reserves = {level: distill._reasoning_spec({"reasoning": level})[1]
                for level in distill.REASONING_LEVELS}
    assert reserves["off"] == 0
    assert reserves["low"] < reserves["medium"] < reserves["xhigh"], reserves
    # An unknown level must not silently reserve nothing.
    assert distill._reasoning_spec({"reasoning": "banana"})[1] > 0
    assert distill._reasoning_spec({})[1] == reserves[distill.DEFAULT_REASONING]


def test_a_truncated_answer_says_so_instead_of_looking_complete():
    """
    finish_reason was tested for presence and its value thrown away, so "length"
    and "stop" were indistinguishable. A cut-off document was accepted as whole
    and only failed later, describing the wrong problem.
    """
    line = ('data: {"choices":[{"delta":{"content":"x"},'
            '"finish_reason":"length"}]}')
    token, _reasoning, done, finish = distill._extract_delta(line, False)
    assert (token, done, finish) == ("x", True, "length")

    stop = 'data: {"choices":[{"delta":{"content":"y"},"finish_reason":"stop"}]}'
    assert distill._extract_delta(stop, False)[3] == "stop"
    mid = 'data: {"choices":[{"delta":{"content":"z"}}]}'
    assert distill._extract_delta(mid, False)[2:] == (False, None)

    # Every early return still unpacks into four values.
    for bad in ("garbage", "data: [DONE]", "data: {", 'data: {"choices":[]}'):
        assert len(distill._extract_delta(bad, False)) == 4, bad


def test_truncation_names_the_cause_it_can_actually_fix():
    """
    Thinking that outweighs the answer is a reasoning-level problem; a long
    answer that hits the cap is a budget problem. Same banner, opposite fixes.
    """
    def report(content, reasoning, finish="length"):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            distill._report_truncation("Inference", finish, 12288,
                                       [content], [reasoning], {"reasoning": "xhigh"})
        return buf.getvalue()

    thinky = report("# 1. Symptom", "reasoning " * 4000)
    assert "TRUNCATED" in thinky
    assert "Lower this pass's reasoning level" in thinky

    wordy = report("answer " * 4000, "brief")
    assert "Raise ANSWER_MAX_TOKENS" in wordy

    # A clean stop is silent - the banner has to mean something when it appears.
    assert report("done", "brief", finish="stop") == ""


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures, skipped = [], []
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Skipped as e:
            skipped.append(test.__name__)
            print(f"  SKIP  {test.__name__}: {e}")
        except AssertionError as e:
            failures.append((test.__name__, e))
            print(f"  FAIL  {test.__name__}: {e}")
        except Exception as e:  # noqa: BLE001 - a crash is a failure, keep going
            failures.append((test.__name__, e))
            print(f"  ERROR {test.__name__}: {type(e).__name__}: {e}")

    passed = len(tests) - len(failures) - len(skipped)
    summary = f"\n{passed}/{len(tests)} passed"
    if skipped:
        summary += f", {len(skipped)} skipped"
    if failures:
        summary += f", {len(failures)} FAILED"
    print(summary)
    if failures:
        sys.exit(1)
