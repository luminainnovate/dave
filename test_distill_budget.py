"""
Behaviour tests for the two payload reductions that feed the design pass.

The failure these exist for: an architect pass blocked for the signature of
`Backend/src/routes/feedback.ts::serializeFeedback`, a 9268-character file
sitting on the mounted volume, and stayed blocked through both evidence rounds.
Nothing was broken. The payload was 98296 tokens against a facts budget of
110819, 48312 tokens of it an append-only build-issues log that was 99%
crossed-off work - and what the log displaced was the budget for reading source.
The survey got 12993 characters for up to 12 files, and had no floor to fall
back on the way the evidence rounds do.

So: reduce_build_issues drops what the agent already crossed off, and
solve_survey_budget gives the survey the floor evidence already had.

Run: python3 test_distill_budget.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DISTILL = os.path.join(HERE, "cline-builder", "distill.py")
ARCHITECT = os.path.join(HERE, "cline-builder", "prompts", "architect.md")

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def _distill():
    spec = importlib.util.spec_from_file_location("distill_under_test", DISTILL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["distill_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _entry(mark, label, body_lines=6):
    body = "".join(f"  ({i}) gate {i} passed\n" for i in range(1, body_lines + 1))
    return f"- [{mark}] **{label}**\n{body}\n"


# ---------------------------------------------------------------------------
# reduce_build_issues
# ---------------------------------------------------------------------------

def test_crossed_off_entries_are_dropped_and_open_ones_kept():
    d = _distill()
    # Shaped like the measured file: overwhelmingly crossed-off work, with the
    # two entries that matter buried in it.
    log = ("# Build Issues Log\n\n"
           + "".join(_entry("x", f"VERIFIED 2026-09-{16 - i} iteration {i}")
                     for i in range(10))
           + _entry("~", "SUPERSEDED - DO NOT REPEAT THIS FIX")
           + _entry(" ", "OPEN: migration 0101 not applied"))
    out = d.reduce_build_issues(log)

    check("the superseded warning survives", "DO NOT REPEAT THIS FIX" in out, out)
    check("the open item survives", "migration 0101 not applied" in out, out)
    check("crossed-off entries are gone", "VERIFIED 2026-09-16" not in out, out)
    check("and it says how many it dropped", "10 resolved entries" in out, out)
    check("the reduction is real", len(out) < len(log) / 3, f"{len(out)} of {len(log)}")


def test_a_fully_resolved_log_reduces_to_nothing():
    d = _distill()
    log = "# Build Issues Log\n\n" + _entry("x", "VERIFIED") * 20
    check("nothing open means no block at all", d.reduce_build_issues(log) == "",
          d.reduce_build_issues(log)[:120])


def test_open_entries_past_the_cap_are_dropped_whole():
    d = _distill()
    log = "".join(_entry(" ", f"OPEN {i}", body_lines=20) for i in range(10))
    out = d.reduce_build_issues(log, max_chars=1200)

    check("within the cap, plus the note", len(out) <= 1200 + 200, len(out))
    check("the last entry is kept", "OPEN 9" in out, out[-200:])
    check("the oldest are dropped", "OPEN 0" not in out, out[:200])
    check("no entry is cut in half",
          out.count("- [") == out.count("(1) gate 1 passed"), out[:200])
    check("and it says so", "omitted to fit the window" in out, out[:200])


def test_a_log_with_no_entry_markers_is_capped_not_emptied():
    d = _distill()
    log = "free-form notes about the build\n" * 200
    out = d.reduce_build_issues(log, max_chars=500)
    check("capped", len(out) <= 500, len(out))
    check("not emptied", "free-form notes" in out, out[:80])
    check("truncation is declared", "earlier characters omitted" in out, out[:80])


def test_a_small_log_is_left_alone():
    d = _distill()
    log = "- [ ] **OPEN** one thing to fix\n"
    check("unchanged", d.reduce_build_issues(log) == log.strip(),
          d.reduce_build_issues(log))


# ---------------------------------------------------------------------------
# solve_survey_budget
# ---------------------------------------------------------------------------

def test_the_survey_floor_matches_the_evidence_floor():
    d = _distill()
    window = 131072
    system = d.est_tokens(open(ARCHITECT, encoding="utf-8").read())
    facts, _answer = d.solve_merge_budget(
        window, system + d.est_tokens("### CURRENT TASK\n"))

    # The measured payload from the run this was written for.
    payload = 98296
    solved = d.solve_addendum_budget(window, system, payload)
    budget = d.solve_survey_budget(window, system, payload)

    check("the unfloored budget is the starved one", solved < 24000, solved)
    check("the floor is taken", budget == d.EVIDENCE_MIN_BUDGET_CHARS, budget)
    check("which fits feedback.ts (9268 chars) whole", budget >= 9268, budget)


def test_taking_the_floor_cannot_cost_the_pass_its_single_pass_call():
    """
    The invariant solve_survey_budget's docstring rests on: the floor is smaller
    than the ANSWER_MAX_TOKENS that solve_addendum_budget holds back, so spending
    it eats the reserve rather than the facts budget.
    """
    d = _distill()
    check("floor < the held-back reserve",
          d.EVIDENCE_MIN_BUDGET_CHARS / d.CHARS_PER_TOKEN_DENSE
          <= d.ANSWER_MAX_TOKENS,
          f"{d.EVIDENCE_MIN_BUDGET_CHARS} chars vs {d.ANSWER_MAX_TOKENS} tok")

    window = 131072
    system = 5502
    for payload in (10000, 60000, 98296, 102000):
        solved = d.solve_addendum_budget(window, system, payload)
        if solved >= d.EVIDENCE_MIN_BUDGET_CHARS:
            continue
        budget = d.solve_survey_budget(window, system, payload)
        facts, _ = d.solve_merge_budget(
            window, system + d.est_tokens("### CURRENT TASK\n"))
        total = payload + budget // d.CHARS_PER_TOKEN_DENSE
        check(f"payload {payload} + floor still fits the facts budget",
              total <= facts, f"{total} vs {facts}")


def test_a_zero_budget_survey_says_so_instead_of_returning_silently():
    d = _distill()
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = d.survey_codebase(None, None, "do a thing", "skeleton", 0)
    check("still returns nothing", out == "", out)
    check("but is audible now", "Survey skipped" in buf.getvalue(), buf.getvalue())


# ---------------------------------------------------------------------------
# blocker_targets / round accounting
# ---------------------------------------------------------------------------

def test_blocker_targets_reads_what_the_pass_asked_for():
    d = _distill()
    check("path::symbol form",
          d.blocker_targets(["`Backend/src/routes/feedback.ts::serializeFeedback` "
                             "— need its signature"])
          == frozenset({"Backend/src/routes/feedback.ts::serializeFeedback"}))
    check("prose form pairs the symbol with the path",
          d.blocker_targets(["`src/a.ts` — need the `Foo` interface"])
          == frozenset({"src/a.ts::Foo"}))
    check("a blocker naming no file falls back to its own text",
          d.blocker_targets(["the request never said what the behaviour was"])
          == frozenset({"the request never said what the behaviour was"}))
    check("wording changes around the same target do not move it",
          d.blocker_targets(["`src/a.ts::Foo` — need the shape"])
          == d.blocker_targets(["need `src/a.ts::Foo`, its fields specifically"]))


class _ScriptedPass:
    """A pass that emits a scripted sequence of results, one per call."""

    def __init__(self, results, resolved):
        self.results = list(results)
        self.resolved = list(resolved)   # paths the resolver returns, per round
        self.calls = 0

    def call_llm(self, client, model_config, prompt, target, prior):
        self.calls += 1
        return self.results[min(self.calls, len(self.results) - 1)]

    def resolve_blocker_paths(self, client, model_config, blockers, skeleton, project_dir):
        idx = min(self.calls, len(self.resolved) - 1)
        return d_mod.BlockerPaths(self.resolved[idx], [])

    def read_evidence(self, project_dir, paths, budget, absent=None, **kw):
        return d_mod.Evidence(f"\n<E>{','.join(paths)}</E>\n", list(paths))


def _run(scripted, pass_key="architect", max_rounds=None):
    global d_mod
    d_mod.call_llm = scripted.call_llm
    d_mod.resolve_blocker_paths = scripted.resolve_blocker_paths
    d_mod.read_evidence = scripted.read_evidence
    return d_mod.resolve_pass_blockers(
        None, pass_key, {}, "PROMPT", "PAYLOAD", "", "SKELETON",
        scripted.results[0], max_rounds=max_rounds,
    )


def test_a_narrowing_trail_is_followed_past_the_old_two_round_ceiling():
    """
    The measured failure: blocked for A, shown A, asked for B, shown B, asked for
    `feedback.ts::serializeFeedback` - and the ceiling ended it with that question
    never asked. Each step names something new, so none of them is billed.
    """
    global d_mod
    d_mod = _distill()
    scripted = _ScriptedPass(
        results=[
            "# BLOCKED\n- `src/a.ts::Alpha` — need its shape",
            "# BLOCKED\n- `src/b.ts::Beta` — need its shape",
            "# BLOCKED\n- `Backend/src/routes/feedback.ts::serializeFeedback` — need it",
            "# 1. Blockers\n- BLOCKER: - none\n\n# 2. The design\n- it is designed",
        ],
        resolved=[["src/a.ts"], ["src/b.ts"], ["Backend/src/routes/feedback.ts"]],
    )
    out = _run(scripted)

    check("the third question was actually asked", scripted.calls >= 3, scripted.calls)
    check("and the pass got there", "it is designed" in out, out[:120])
    check("no blockers survive", d_mod.detect_blockers(out) == [], out[:120])
    check("under the old accounting this needed more than EVIDENCE_ROUNDS",
          scripted.calls > d_mod.EVIDENCE_ROUNDS["architect"], scripted.calls)


def test_restating_one_question_is_billed_even_when_the_resolver_wanders():
    """
    The case the 'paths already supplied' stop cannot see: the pass asks for the
    same thing every round, and the resolver maps it somewhere new each time, so
    every round looks like fresh ground.
    """
    global d_mod
    d_mod = _distill()
    same = "# BLOCKED\n- `src/a.ts::Alpha` — need its shape"
    scripted = _ScriptedPass(
        results=[same] * 6,
        resolved=[["src/one.ts"], ["src/two.ts"], ["src/three.ts"],
                  ["src/four.ts"], ["src/five.ts"]],
    )
    out = _run(scripted)

    check("it stops on the restatement budget, not the ceiling",
          scripted.calls <= d_mod.EVIDENCE_ROUNDS["architect"] + 1, scripted.calls)
    check("well short of the ceiling",
          scripted.calls < d_mod.EVIDENCE_MAX_ROUNDS["architect"], scripted.calls)
    check("and hands back the blocked result", d_mod.detect_blockers(out) != [], out[:80])


def test_the_ceiling_still_bounds_an_endless_trail():
    global d_mod
    d_mod = _distill()
    scripted = _ScriptedPass(
        results=[f"# BLOCKED\n- `src/f{i}.ts::Sym{i}` — need it" for i in range(12)],
        resolved=[[f"src/f{i}.ts"] for i in range(12)],
    )
    out = _run(scripted)
    check("never more rounds than the ceiling",
          scripted.calls <= d_mod.EVIDENCE_MAX_ROUNDS["architect"], scripted.calls)
    check("still blocked", d_mod.detect_blockers(out) != [], out[:80])


def test_an_explicit_max_rounds_still_caps_both_meters():
    """The reproduction loop passes 1 and must get exactly one round."""
    global d_mod
    d_mod = _distill()
    scripted = _ScriptedPass(
        results=[f"# BLOCKED\n- `src/f{i}.ts::Sym{i}` — need it" for i in range(6)],
        resolved=[[f"src/f{i}.ts"] for i in range(6)],
    )
    _run(scripted, pass_key="bugfix", max_rounds=1)
    check("exactly one retry", scripted.calls == 1, scripted.calls)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n{t.__name__}")
        try:
            t()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAILURES.append(f"{t.__name__}: {e}")
    print("\n" + ("-" * 60))
    print("FAILED:" if FAILURES else "ALL PASSED", len(FAILURES) or "")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1 if FAILURES else 0)
