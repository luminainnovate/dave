"""
Behaviour tests for `!jira` - one Story's subtasks, delivered in order.

The runner and the ADF conversion are the real code. Jira is an in-memory fake
that records every write; the Engine is a fake whose ship marker moves through
a scripted sequence of CI-gate states, standing in for containers and GitHub.
"""
import asyncio
import importlib.util
import os
import sys
import tempfile

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_here, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


jira_client = _load("jira_client")
jira_runner = _load("jira_runner")
jira_runner.POLL_SECS = 0

# A fake clock: each engine.wait() is one 15-second poll. The real grace
# periods stay in force, so the tests exercise the timing the runner uses.
CLOCK = [0.0]
jira_runner._now = lambda: CLOCK[0]

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def issue(key, status="To Do", labels=None, summary=None, subtask=True, parent="KAN-1",
          itype=None, hierarchy=None, category=None, description=""):
    cat = category or {"to do": "new", "mvp+": "new", "done": "done",
                       "cancelled": "done"}.get(status.lower(), "indeterminate")
    return {"key": key, "summary": summary or f"summary of {key}", "status": status,
            "status_category": cat, "labels": list(labels or []),
            "type": itype or ("Subtask" if subtask else "Story"), "is_subtask": subtask,
            "hierarchy": hierarchy if hierarchy is not None else (-1 if subtask else 0),
            "parent": parent if subtask else "", "description": description or f"do {key}",
            "subtask_keys": []}


class FakeJira:
    def __init__(self, story, subtasks, fail_writes=False):
        self.issues = {story["key"]: story, **{s["key"]: s for s in subtasks}}
        self.order = [s["key"] for s in subtasks]
        self.story_key = story["key"]
        self.log = []
        self.fail_writes = fail_writes

    def browse_url(self, key):
        return f"https://jira.example/browse/{key}"

    def issue(self, key):
        return dict(self.issues[key])

    def children(self, key):
        return [dict(self.issues[k]) for k in self.order] if key == self.story_key else []

    def _write(self, *entry):
        self.log.append(entry)
        if self.fail_writes:
            raise jira_client.JiraError("Jira is down")

    def transition(self, key, status):
        self._write("transition", key, status)
        self.issues[key]["status"] = status

    def comment(self, key, text):
        self._write("comment", key, text)

    def add_label(self, key, label):
        self._write("add_label", key, label)
        if label not in self.issues[key]["labels"]:
            self.issues[key]["labels"].append(label)

    def remove_label(self, key, label):
        self._write("remove_label", key, label)
        if label in self.issues[key]["labels"]:
            self.issues[key]["labels"].remove(label)

    def statuses(self, key):
        return [e[2] for e in self.log if e[0] == "transition" and e[1] == key]

    def comments(self, key):
        return [e[2] for e in self.log if e[0] == "comment" and e[1] == key]


INTEGRATED = [{"state": "awaiting_checks", "pr_url": "https://gh/pr/1"},
              {"state": "awaiting_base_ci", "pr_url": "https://gh/pr/1", "merge_sha": "abc1234"},
              {"state": "integrated", "pr_url": "https://gh/pr/1", "merge_sha": "abc1234"}]


class FakeEngine(jira_runner.Engine):
    def __init__(self, ci_scripts=None, document="# Design\n\nDo it.", gates=True,
                 ship_ok=True, build_ok=True, marker=None, fix_ok=True):
        self.workspace = tempfile.mkdtemp(prefix="jira_runner_test_")
        self.ci_scripts = list(ci_scripts if ci_scripts is not None else [])
        self.document, self.gates, self.ship_ok = document, gates, ship_ok
        self.build_ok, self.fix_ok = build_ok, fix_ok
        self.marker, self.script = marker, []
        self.calls, self.notes = [], []
        # Liveness answers for the build container, one per check; False once spent.
        self.running = []

    async def design(self, messages, pass_key):
        self.calls.append(("design", pass_key, messages))
        return self.document, "design text"

    async def build(self, messages, pass_key):
        self.calls.append(("build", pass_key, messages))
        if not self.build_ok:
            return None, "❌ Build Trigger Failed: docker"
        n = len([c for c in self.calls if c[0] == "build"])
        self.marker = {"state": "ready", "container": f"cline-builder-{n}"}
        return f"cline-builder-{n}", "🔨 Build pipeline triggered."

    async def container_running(self, name):
        return self.running.pop(0) if self.running else False

    def read_marker(self):
        return self.marker

    async def wait(self):
        CLOCK[0] += 15
        # Time passing is what moves the CI gate along.
        if self.script:
            self.marker = self.script.pop(0)

    def build_passed_gates(self):
        return self.gates

    def ship(self, title):
        self.calls.append(("ship", title))
        if not self.ship_ok:
            return False, "🛑 **Not on a build branch.**"
        self.script = list(self.ci_scripts.pop(0)) if self.ci_scripts else list(INTEGRATED)
        self.marker = self.script.pop(0)
        return True, "🚦 Pull request open"

    def watch_ci(self):
        pass

    async def fix_ci(self, messages):
        self.calls.append(("fix_ci",))
        return (self.fix_ok, "🔨 Build pipeline triggered." if self.fix_ok
                else "🟠 Nothing here is this change's to fix.")

    def note(self, text):
        self.notes.append(text)


def run(jira, engine, key="KAN-1"):
    r = jira_runner.StoryRun(jira, engine, key, "!clone https://github.com/x/y")
    return r, asyncio.run(r.run())


def story(status="To Do"):
    return issue("KAN-1", status=status, subtask=False, summary="implement fluentd",
                 description="Ship logs to fluentd.")


# --------------------------------------------------------------------------- #

def test_plan_order_skips_and_stops():
    subs = [issue("KAN-2", "Done"), issue("KAN-3", "Cancelled"), issue("KAN-4", "MVP+"),
            issue("KAN-5", "To Do"), issue("KAN-6", "In Progress", labels=["agent-dave"]),
            issue("KAN-7", "In Review"), issue("KAN-8", "To Do")]
    plan = {i["key"]: a for i, a, _ in jira_runner.unit_plan(story(), subs)}
    check("Done skipped", plan["KAN-2"] == "skip")
    check("Cancelled skipped", plan["KAN-3"] == "skip")
    check("MVP+ deferred", plan["KAN-4"] == "skip")
    check("To Do worked", plan["KAN-5"] == "work")
    check("Dave's own In Progress resumed", plan["KAN-6"] == "resume")
    check("someone else's In Review stops the run", plan["KAN-7"] == "stop")
    check("nothing after the stop is touched", plan["KAN-8"] == "skip", plan)
    only = jira_runner.unit_plan(story(), [])
    check("Story with no subtasks is the unit", only[0][0]["key"] == "KAN-1" and only[0][1] == "work")
    text = jira_runner.plan_text(story(), jira_runner.unit_plan(story(), subs), lambda k: k)
    check("plan text names where it stops", "stop at `KAN-7`" in text, text)


def test_human_subtask_pauses_the_run():
    subs = [issue("KAN-2"), issue("KAN-3", labels=["Human"], summary="run the eval"),
            issue("KAN-4")]
    plan = {i["key"]: a for i, a, _ in jira_runner.unit_plan(story(), subs)}
    check("work before the human subtask", plan["KAN-2"] == "work", plan)
    check("human subtask in To Do stops the run", plan["KAN-3"] == "stop", plan)
    check("nothing after it is touched", plan["KAN-4"] == "skip", plan)
    text = jira_runner.plan_text(story(), jira_runner.unit_plan(story(), subs), lambda k: k)
    check("plan text says it needs a person", "pause at `KAN-3`" in text and "needs a person" in text, text)

    jira = FakeJira(story(), subs)
    engine = FakeEngine()
    _, result = run(jira, engine)
    check("only the first subtask built", [c[0] for c in engine.calls] == ["design", "build", "ship"],
          engine.calls)
    check("result says it is waiting on a person", "Waiting on a person" in result and "KAN-3" in result,
          result)
    check("human subtask left alone", jira.statuses("KAN-3") == []
          and "agent-dave" not in jira.issues["KAN-3"]["labels"], jira.log)
    check("hand-over commented on the human subtask",
          any("needs a person" in c and "!jira KAN-1" in c for c in jira.comments("KAN-3")),
          jira.comments("KAN-3"))
    check("story not closed", jira.issues["KAN-1"]["status"] == "In Progress", jira.log)
    state = jira_runner.read_run_state(engine.workspace)
    check("run saved as paused, waiting on KAN-3",
          state["status"] == "paused" and state.get("waiting_on") == "KAN-3", state)
    check("chat note names the subtask", any("waiting on a person" in n and "KAN-3" in n
                                              for n in engine.notes), engine.notes)

    # The person finishes it; the rerun carries on past it.
    jira.issues["KAN-3"]["status"] = "Done"
    jira.issues["KAN-3"]["status_category"] = "done"
    engine2 = FakeEngine()
    _, result2 = run(jira, engine2)
    check("rerun builds only the subtask after it", len([c for c in engine2.calls if c[0] == "design"]) == 1
          and "KAN-4" in engine2.calls[0][2][1]["content"], engine2.calls)
    check("story completes", "complete" in result2 and jira.issues["KAN-1"]["status"] == "Done", result2)


def test_human_subtask_first_does_nothing():
    jira = FakeJira(story(), [issue("KAN-2", labels=["human"]), issue("KAN-3")])
    engine = FakeEngine()
    _, result = run(jira, engine)
    check("nothing built", engine.calls == [], engine.calls)
    check("story not moved", jira.statuses("KAN-1") == [], jira.log)
    check("still reports waiting", "Waiting on a person" in result, result)


def test_happy_path_delivers_in_order_and_closes_story():
    jira = FakeJira(story(), [issue("KAN-2"), issue("KAN-3", "Done"), issue("KAN-4")])
    engine = FakeEngine()
    _, result = run(jira, engine)
    check("run reports complete", "complete" in result, result)
    order = [c[0] for c in engine.calls]
    check("design, build, ship per unit, two units",
          order == ["design", "build", "ship", "design", "build", "ship"], order)
    check("Done subtask not rebuilt", all("KAN-3" not in str(c) for c in engine.calls))
    check("subtask walked To Do -> In Progress -> In Review -> Done",
          jira.statuses("KAN-2") == ["In Progress", "In Review", "Done"], jira.statuses("KAN-2"))
    check("story moved In Progress then Done", jira.statuses("KAN-1") == ["In Progress", "Done"],
          jira.statuses("KAN-1"))
    i2 = jira.log.index(("transition", "KAN-2", "Done"))
    i4 = jira.log.index(("transition", "KAN-4", "In Progress"))
    check("second subtask starts only after the first is Done", i2 < i4)
    check("subtask labelled agent-dave", "agent-dave" in jira.issues["KAN-2"]["labels"])
    check("PR link commented", any("https://gh/pr/1" in c for c in jira.comments("KAN-2")))
    check("design excerpt commented", any("Design approved" in c for c in jira.comments("KAN-2")))
    design_msgs = engine.calls[0][2]
    check("messages anchored to the chat's first message",
          design_msgs[0]["content"] == "!clone https://github.com/x/y")
    check("design pass asked for", design_msgs[-1]["content"] == "!architect")
    check("prompt carries subtask and Story background",
          "KAN-2" in design_msgs[1]["content"] and "Ship logs to fluentd." in design_msgs[1]["content"]
          and "ONLY this subtask" in design_msgs[1]["content"])
    check("build approves", engine.calls[1][2][-1]["content"] == "!approve")
    check("PR title carries the key", engine.calls[2][1].startswith("KAN-2: "), engine.calls[2])
    check("run state saved", jira_runner.read_run_state(engine.workspace)["status"] == "complete")


def test_story_without_subtasks_is_delivered_itself():
    jira = FakeJira(story(), [])
    engine = FakeEngine()
    _, _result = run(jira, engine)
    check("story itself built", [c[0] for c in engine.calls] == ["design", "build", "ship"])
    check("story prompt has no subtask framing",
          "ONLY this subtask" not in engine.calls[0][2][1]["content"])
    check("story ends Done", jira.issues["KAN-1"]["status"] == "Done", jira.log)


def test_bug_label_uses_bugfix_pass():
    jira = FakeJira(story(), [issue("KAN-2", labels=["bug"])])
    engine = FakeEngine()
    run(jira, engine)
    check("bugfix pass", engine.calls[0][1] == "bugfix" and engine.calls[0][2][-1]["content"] == "!bugfix")


def test_unapprovable_design_blocks_the_story():
    jira = FakeJira(story(), [issue("KAN-2"), issue("KAN-3")])
    engine = FakeEngine(document=None)
    r, result = run(jira, engine)
    check("reported as stopped", "Stopped on `KAN-2`" in result, result)
    check("no build after a failed design", [c[0] for c in engine.calls] == ["design", "design"])
    check("blocked label", "dave-blocked" in jira.issues["KAN-2"]["labels"])
    check("reason commented", any("did not produce a design" in c for c in jira.comments("KAN-2")))
    check("next subtask untouched", not [e for e in jira.log if e[1] == "KAN-3"])
    check("subtask left In Progress, not Done", jira.issues["KAN-2"]["status"] == "In Progress")
    check("state blocked", r.state["status"] == "blocked")


def test_aborted_design_is_rerun_not_approved():
    """KAN-142, 2026-11-20: the architect pass timed out and its abort report
    was approved as the design."""
    jira = FakeJira(story(), [issue("KAN-2")])
    engine = FakeEngine()
    answers = [None, "# Design\n\nDo it."]

    async def flaky_design(messages, pass_key):
        engine.calls.append(("design", pass_key, messages))
        return answers.pop(0), "## ❌ ABORTED — the pass could not run"
    engine.design = flaky_design
    run(jira, engine)
    check("design ran twice", len([c for c in engine.calls if c[0] == "design"]) == 2)
    approved = [c for c in jira.comments("KAN-2") if "Design approved" in c]
    check("only the real design approved", approved and "ABORTED" not in approved[0], approved)
    check("subtask done", jira.issues["KAN-2"]["status"] == "Done")


def test_design_that_keeps_aborting_blocks():
    jira = FakeJira(story(), [issue("KAN-2")])
    engine = FakeEngine(document=None)
    _, result = run(jira, engine)
    check("design tried twice", len([c for c in engine.calls if c[0] == "design"]) == 2)
    check("no build", "build" not in [c[0] for c in engine.calls])
    check("blocked", "did not produce a design" in result, result)


def test_build_that_fails_its_gates_is_not_shipped():
    jira = FakeJira(story(), [issue("KAN-2")])
    engine = FakeEngine(gates=False)
    _, result = run(jira, engine)
    check("not shipped", "ship" not in [c[0] for c in engine.calls])
    check("reason names VERIFIED and SAFE", "VERIFIED and SAFE" in result, result)


def test_build_that_leaves_nothing_blocks():
    jira = FakeJira(story(), [issue("KAN-2")])
    engine = FakeEngine()
    real_build = engine.build

    async def build_without_marker(messages, pass_key):
        name, text = await real_build(messages, pass_key)
        engine.marker = None  # stopped on a blocker: no ship marker written
        engine.running = [True, True]
        return name, text
    engine.build = build_without_marker
    _, result = run(jira, engine)
    check("stopped for nothing to ship", "without leaving anything to ship" in result, result)


def test_design_blocker_is_reported_with_its_question():
    """KAN-152, 2026-11-21: the safety pass stopped on an unmapped enum value and
    Jira was told only that the build "ended without leaving anything to ship"."""
    jira = FakeJira(story(), [issue("KAN-2")])
    engine = FakeEngine()
    real_build = engine.build

    async def blocked_build(messages, pass_key):
        name, text = await real_build(messages, pass_key)
        engine.marker = None
        engine.running = [True]
        return name, text
    engine.build = blocked_build
    engine.stop_reason = lambda container: "[safety]\n- which kind does 'analyse' persist as?"
    _, result = run(jira, engine)
    check("the question is in the stop message", "which kind does 'analyse'" in result, result)
    check("says where to answer", "description" in result, result)
    check("question commented on the subtask",
          any("which kind does 'analyse'" in c for c in jira.comments("KAN-2")))


def test_slow_starting_build_is_waited_for():
    """KAN-142: the container was not visible yet on the first checks, and the
    runner gave up a minute in while the build went on for two hours."""
    jira = FakeJira(story(), [issue("KAN-2")])
    engine = FakeEngine()
    real_build = engine.build

    async def slow_build(messages, pass_key):
        name, text = await real_build(messages, pass_key)
        ready, engine.marker = engine.marker, None
        # Not created for 2 minutes, then runs for an hour, then exits.
        engine.running = [False] * 8 + [True] * 240
        engine.script = [None] * 248 + [ready]
        return name, text
    engine.build = slow_build
    _, result = run(jira, engine)
    check("shipped after the long build", "ship" in [c[0] for c in engine.calls], result)
    check("subtask done", jira.issues["KAN-2"]["status"] == "Done", jira.issues["KAN-2"])


def test_build_that_never_appears_blocks():
    jira = FakeJira(story(), [issue("KAN-2")])
    engine = FakeEngine()
    real_build = engine.build

    async def phantom_build(messages, pass_key):
        name, text = await real_build(messages, pass_key)
        engine.marker = None
        return name, text
    engine.build = phantom_build
    _, result = run(jira, engine)
    check("stopped: never started", "never started" in result, result)


def test_refused_ship_blocks():
    jira = FakeJira(story(), [issue("KAN-2")])
    _, result = run(jira, FakeEngine(ship_ok=False))
    check("ship refusal stops", "Shipping was refused" in result and "build branch" in result, result)


def test_ci_failure_gets_fix_rounds_then_merges():
    script = [{"state": "awaiting_checks"},
              {"state": "ci_failed", "fix_attempts": 0},
              {"state": "fixing", "fix_attempts": 1},
              {"state": "awaiting_checks", "fix_attempts": 1},
              *INTEGRATED[1:]]
    jira = FakeJira(story(), [issue("KAN-2")])
    engine = FakeEngine(ci_scripts=[script])
    _, result = run(jira, engine)
    check("one fix round", [c[0] for c in engine.calls].count("fix_ci") == 1, engine.calls)
    check("delivered after the fix", jira.issues["KAN-2"]["status"] == "Done", result)
    check("fix round commented", any("fix round 1" in c for c in jira.comments("KAN-2")))


def test_ci_still_failing_after_max_rounds_blocks():
    script = [{"state": "awaiting_checks"},
              {"state": "ci_failed", "fix_attempts": jira_runner.MAX_FIX_ROUNDS,
               "notice": "🔴 **CI failed**"}]
    jira = FakeJira(story(), [issue("KAN-2")])
    engine = FakeEngine(ci_scripts=[script])
    _, result = run(jira, engine)
    check("no further fix", "fix_ci" not in [c[0] for c in engine.calls])
    check("stopped with the round count", "after 2 fix round" in result, result)
    check("subtask stays In Review", jira.issues["KAN-2"]["status"] == "In Review")


def test_rerun_of_a_story_stopped_on_fix_rounds_gets_fresh_rounds():
    """KAN-163, 2026-11-22: the run stopped after 2 fix rounds; `!jira KAN-141`
    resumed at the CI gate and stopped again within a second, with no build."""
    spent = jira_runner.MAX_FIX_ROUNDS
    jira = FakeJira(story("In Progress"),
                    [issue("KAN-2", "In Review", labels=["agent-dave"])])
    engine = FakeEngine(marker={"state": "ci_failed", "fix_attempts": spent,
                                "pr_url": "https://gh/pr/9"})
    engine.script = [{"state": "fixing", "fix_attempts": spent + 1},
                     {"state": "awaiting_checks", "fix_attempts": spent + 1},
                     *INTEGRATED[1:]]
    _, result = run(jira, engine)
    check("a fresh fix round ran", [c[0] for c in engine.calls].count("fix_ci") == 1,
          engine.calls)
    check("delivered", jira.issues["KAN-2"]["status"] == "Done", result)
    check("numbered from the re-run", any("fix round 1 of" in c for c in jira.comments("KAN-2")),
          jira.comments("KAN-2"))


def test_rerun_still_stops_after_its_own_rounds():
    spent = jira_runner.MAX_FIX_ROUNDS
    jira = FakeJira(story("In Progress"),
                    [issue("KAN-2", "In Review", labels=["agent-dave"])])
    engine = FakeEngine(marker={"state": "ci_failed", "fix_attempts": spent,
                                "pr_url": "https://gh/pr/9"})
    engine.script = []
    for n in range(1, spent + 1):
        engine.script += [{"state": "fixing", "fix_attempts": spent + n},
                          {"state": "ci_failed", "fix_attempts": spent + n,
                           "notice": "🔴 **CI failed**"}]
    _, result = run(jira, engine)
    check("exactly this run's allowance",
          [c[0] for c in engine.calls].count("fix_ci") == spent, engine.calls)
    check("stopped with this run's count", f"after {spent} fix round" in result, result)


def test_unfixable_ci_failure_blocks():
    script = [{"state": "awaiting_checks"}, {"state": "ci_failed", "fix_attempts": 0}]
    jira = FakeJira(story(), [issue("KAN-2")])
    _, result = run(jira, FakeEngine(ci_scripts=[script], fix_ok=False))
    check("unfixable failure stops", "no fix build could be started" in result
          and "Nothing here" in result, result)


def test_stalled_gate_blocks():
    script = [{"state": "awaiting_checks"},
              {"state": "stalled", "stall_reason": "CI did not finish in time."}]
    jira = FakeJira(story(), [issue("KAN-2")])
    _, result = run(jira, FakeEngine(ci_scripts=[script]))
    check("stall stops with its reason", "CI did not finish in time" in result, result)


def test_resume_waits_on_open_pr_without_redesigning():
    jira = FakeJira(story("In Progress"),
                    [issue("KAN-2", "In Review", labels=["agent-dave"]), issue("KAN-3")])
    engine = FakeEngine(marker={"state": "awaiting_checks", "pr_url": "https://gh/pr/9"})
    engine.script = list(INTEGRATED[1:])
    _, _result = run(jira, engine)
    first = [c for c in engine.calls]
    check("no design for the resumed subtask", first[0][0] == "design"
          and "KAN-3" in first[0][2][1]["content"], first[:1])
    check("resumed subtask finished", jira.issues["KAN-2"]["status"] == "Done")
    check("then the next one", jira.issues["KAN-3"]["status"] == "Done", jira.log)


def test_rerun_after_a_merge_while_stopped_delivers_without_rebuilding():
    """KAN-163, 2026-11-22: `!jira stop` landed after the fix was pushed; the CI
    gate merged it with no run watching. A re-run must mark it Done, not
    design and build it again."""
    jira = FakeJira(story("In Progress"),
                    [issue("KAN-2", "In Review", labels=["agent-dave"]), issue("KAN-3")])
    engine = FakeEngine(marker={"state": "integrated", "title": "KAN-2: Subtask",
                                "pr_url": "https://gh/pr/9", "merge_sha": "c8be2ad0"})
    _, _result = run(jira, engine)
    check("not redesigned", not any(c[0] == "design" and "KAN-2" in str(c) for c in engine.calls),
          engine.calls)
    check("marked Done", jira.issues["KAN-2"]["status"] == "Done", jira.log)
    check("then the next one", jira.issues["KAN-3"]["status"] == "Done", jira.log)


def test_an_integrated_record_for_another_subtask_is_not_taken_as_this_one():
    jira = FakeJira(story("In Progress"), [issue("KAN-2", "In Review", labels=["agent-dave"])])
    engine = FakeEngine(marker={"state": "integrated", "title": "KAN-1: Earlier",
                                "pr_url": "https://gh/pr/8"})
    _, _result = run(jira, engine)
    check("designed afresh", any(c[0] == "design" for c in engine.calls), engine.calls)


def test_unrelated_pr_in_flight_is_waited_for():
    """KAN-143, 2026-11-20: a hand-shipped PR was in CI, so the run stopped,
    and nothing restarted it when that PR merged minutes later."""
    jira = FakeJira(story(), [issue("KAN-2")])
    other = "https://gh/pr/other"
    engine = FakeEngine(marker={"state": "awaiting_checks", "pr_url": other})
    engine.script = [{"state": "awaiting_checks", "pr_url": other},
                     {"state": "awaiting_base_ci", "pr_url": other},
                     {"state": "integrated", "pr_url": other}]
    _, result = run(jira, engine)
    check("designed after the other PR merged", engine.calls and engine.calls[0][0] == "design",
          engine.calls[:1])
    check("subtask delivered", jira.issues["KAN-2"]["status"] == "Done", result)


def test_unrelated_pr_stuck_in_ci_blocks_new_work():
    jira = FakeJira(story(), [issue("KAN-2")])
    engine = FakeEngine(marker={"state": "ci_failed", "pr_url": "https://gh/pr/other"})
    _, result = run(jira, engine)
    check("refuses to start beside a failed PR", "stuck in the CI gate" in result, result)
    check("nothing designed", not engine.calls)


def test_refusals():
    for st, needle in ((issue("KAN-9", subtask=True, parent="KAN-1"), "is a subtask"),
                       (issue("KAN-9", subtask=False, itype="Epic", hierarchy=1), "is an Epic"),
                       (issue("KAN-9", "Cancelled", subtask=False), "is Cancelled")):
        jira = FakeJira(st, [])
        engine = FakeEngine()
        _, result = run(jira, engine, "KAN-9")
        check(f"refused: {needle}", needle in result and not engine.calls, result)


def test_jira_outage_does_not_stop_delivery():
    jira = FakeJira(story(), [issue("KAN-2")], fail_writes=True)
    engine = FakeEngine()
    _, result = run(jira, engine)
    check("work still shipped", "ship" in [c[0] for c in engine.calls], result)
    check("failure surfaced as a note", any("Jira update failed" in n for n in engine.notes))


def test_adf_to_markdown():
    doc = {"type": "doc", "version": 1, "content": [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Context"}]},
        {"type": "paragraph", "content": [
            {"type": "text", "text": "see "},
            {"type": "text", "text": "docs", "marks": [{"type": "link", "attrs": {"href": "https://d.io"}}]},
            {"type": "hardBreak"},
            {"type": "text", "text": "src/app.ts", "marks": [{"type": "code"}]}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "two"}]}]}]},
        {"type": "codeBlock", "attrs": {"language": "python"},
         "content": [{"type": "text", "text": "print('x')\n```danger```"}]},
    ]}
    md = jira_client.adf_to_markdown(doc)
    check("heading", "## Context" in md, md)
    check("link", "[docs](https://d.io)" in md, md)
    check("inline code kept", "`src/app.ts`" in md, md)
    check("bullets", "- one\n- two" in md, md)
    check("code block indented", "    print('x')" in md, md)
    check("no fenced block survives (mover would write it as a file)", "```" not in md, md)


def test_text_to_adf_links_urls():
    adf = jira_client.text_to_adf("PR open: https://github.com/x/y/pull/3.\n\nNext para")
    paras = adf["content"]
    link = [n for n in paras[0]["content"] if n.get("marks")]
    check("two paragraphs", len(paras) == 2, adf)
    check("url linked without trailing dot",
          link and link[0]["marks"][0]["attrs"]["href"] == "https://github.com/x/y/pull/3", link)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n{t.__name__}")
        try:
            t()
        except Exception:  # noqa: BLE001 - report every test, not just the first to crash
            import traceback
            traceback.print_exc()
            FAILURES.append(t.__name__)
    print("\n" + ("-" * 60))
    print("FAILED:" if FAILURES else "ALL PASSED", len(FAILURES) or "")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1 if FAILURES else 0)
