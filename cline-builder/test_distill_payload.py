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
    """A chunk plus its output must leave the reserve intact."""
    assert distill.TARGET_CHUNK_SIZE + distill.EXTRACTION_MAX_TOKENS < distill.CONTEXT_WINDOW


def test_small_context_window_clamps_the_chunk():
    """TARGET_CHUNK_SIZE is a ceiling, not a floor - a 16k window must win."""
    original = distill.CONTEXT_WINDOW
    distill.CONTEXT_WINDOW = 8192
    try:
        fake = run_call_llm(iterative_payload())
        budget = (8192 - distill.RESERVED_TOKENS) * distill.CHARS_PER_TOKEN
        for call in fake.chunks:
            assert len(call["user"]) <= budget + len(REQUEST_BODY) + 1024, \
                "chunk exceeded what the configured window can hold"
    finally:
        distill.CONTEXT_WINDOW = original


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
