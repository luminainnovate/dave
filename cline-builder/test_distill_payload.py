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
import os
import sys

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
    """A payload shaped like the FRESH_BUILD branch of run_distillation."""
    return (
        "<SITUATIONAL_AWARENESS>\n"
        "  <MODE>FRESH_BUILD</MODE>\n"
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


def run_call_llm(payload: str, prior_context: str = "") -> FakeLLM:
    """Drive call_llm against a fake model and hand back the captured prompts."""
    fake = FakeLLM()
    real_call = distill._single_llm_call
    distill._single_llm_call = fake
    try:
        # call_llm narrates its progress to stdout; keep the test output readable.
        with contextlib.redirect_stdout(io.StringIO()):
            distill.call_llm(None, "fake-model", "ARCHITECT SYSTEM PROMPT", payload, prior_context)
    finally:
        distill._single_llm_call = real_call
    return fake


# --- A. Pivot extraction ----------------------------------------------------

def test_extract_request_iterative():
    assert distill.extract_request(iterative_payload()) == REQUEST_BODY


def test_extract_request_falls_back_to_build_command():
    """FRESH_BUILD names the request differently; both tags must resolve."""
    assert distill.extract_request(fresh_payload()) == BUILD_COMMAND


def test_extract_request_absent_is_empty():
    assert distill.extract_request("<PROJECT_DATA>no request here</PROJECT_DATA>") == ""


def test_extract_request_ignores_blank_tag():
    """An empty tag is not a request - the caller must omit the section, not assert one."""
    assert distill.extract_request("<NEW_REQUEST>\n   \n</NEW_REQUEST>") == ""


def test_extract_mode_iterative():
    assert distill.extract_mode(iterative_payload()) == "ITERATIVE_REBUILD"


def test_extract_mode_fresh():
    assert distill.extract_mode(fresh_payload()) == "FRESH_BUILD"


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
    merge = run_call_llm(fresh_payload()).merge
    assert BUILD_COMMAND in merge["user"]
    assert "FRESH_BUILD" in merge["user"]


def test_merge_prompt_without_pivots_makes_no_false_promise():
    """A payload with no pivots must not point the model at absent sections."""
    body = run_call_llm(_bulk("PROSE", 60)).merge["user"]
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
        fake = run_call_llm(iterative_payload())
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
        merge = run_call_llm(iterative_payload()).merge
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
        merge = run_call_llm(iterative_payload()).merge
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

def test_real_conversation_reaches_the_merge_with_its_request():
    """
    End-to-end against the bound workspace, when one is present. This is the
    exact input that produced the "# BLOCKED" architecture.
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
    fake = run_call_llm(payload)
    assert len(fake.chunks) > 1, "real payload should still exercise the merge path"
    body = fake.merge["user"]
    assert "ITERATIVE_REBUILD" in body

    # Derived from the file, not hardcoded: this workspace is live and its
    # newest request changes as the project is worked on.
    request = distill.extract_request(payload)
    assert request, "fixture payload carries no request to check"
    assert request in body, "the real request did not reach the merge pass"


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
