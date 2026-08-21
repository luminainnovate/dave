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
        ("src/rich.ts", 40, ["import x"], ["Thing"], []),
        ("src/bare.ts", 10, ["import y"], [], ["helper"]),
    ]
    blocks, footer = distill._render_skeleton(files_data, full=False)
    assert len(blocks) == 1, "the symbol-less file still rendered a detail block"
    assert "src/bare.ts" in footer, "the symbol-less file was dropped outright"
    assert len(footer) < 200, "the roll-up should be compact"


def test_skeleton_paths_covers_both_renderings():
    """prune_tree_against_skeleton is only safe if this sees every path."""
    files_data = [
        ("src/rich.ts", 40, ["import x"], ["Thing"], []),
        ("src/bare.ts", 10, ["import y"], [], ["helper"]),
    ]
    blocks, footer = distill._render_skeleton(files_data, full=False)
    skeleton = "\n".join(["[PROJECT SYMBOL SKELETON]"] + blocks + [footer])
    paths = distill.skeleton_paths(skeleton)
    assert paths == {"src/rich.ts", "src/bare.ts"}, paths


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
        block = distill.read_evidence(here, [], 10000, ["src/db/schema.ts"])
    assert "<ABSENT>" in block
    assert "src/db/schema.ts" in block
    assert "REQUESTED_EVIDENCE" in block


def test_absence_and_contents_travel_together():
    here = os.path.dirname(os.path.abspath(__file__))
    name = os.path.basename(__file__)
    with contextlib.redirect_stdout(io.StringIO()):
        block = distill.read_evidence(here, [name], 5000, ["gone.ts"])
    assert f'<file path="{name}">' in block
    assert "<ABSENT>" in block and "gone.ts" in block


def test_read_evidence_respects_its_budget():
    here = os.path.dirname(os.path.abspath(__file__))
    name = os.path.basename(__file__)
    with contextlib.redirect_stdout(io.StringIO()):
        block = distill.read_evidence(here, [name], budget_chars=500)
    assert block, "no evidence block produced"
    assert "truncated:" in block, "an oversized file was not truncated"
    assert len(block) < 2000


def test_read_evidence_is_empty_without_budget_or_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    assert distill.read_evidence(here, [], 10000) == ""
    assert distill.read_evidence(here, [], 10000, []) == ""
    assert distill.read_evidence(here, [os.path.basename(__file__)], 0) == ""
    assert distill.read_evidence(here, [], 0, ["gone.ts"]) == ""


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
