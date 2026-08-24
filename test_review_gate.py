"""
Behaviour tests for the design-review gate's wait.

Loads orchestrator.py by path and stubs only the two things that reach outside
the process: the container launch and the liveness probe. Everything else - the
polling loop, the progress emission, the death race, the SSE framing - is the
real code.
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time

os.environ.setdefault("LLAMACPP_BINARY", "/bin/true")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "orch", os.path.join(os.path.dirname(os.path.abspath(__file__)), "orchestrator.py"))
orch = importlib.util.module_from_spec(_spec)
sys.modules["orch"] = orch
_spec.loader.exec_module(orch)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


class Project:
    """A bound project directory with a .cline_context, as the gate expects."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctx = os.path.join(self.tmp.name, ".cline_context")
        os.makedirs(self.ctx)

    def write_doc(self, pass_key="bugfix", body="# 4. Root Cause\n- it broke"):
        with open(os.path.join(self.ctx, f"distill_{pass_key}.md"), "w") as f:
            f.write(body)

    def write_status(self, text):
        with open(os.path.join(self.ctx, "distill_status"), "w") as f:
            f.write(text)


def install(project, *, alive, budget_streaming=6, budget_blocking=6):
    """Point the gate at a temp project and stub what leaves the process."""
    orch._get_bound_project_dir = lambda messages: project.tmp.name
    orch._write_design_pass = lambda messages, pass_key: None

    async def fake_trigger(messages, extra_env=None, mode_label="", skip_cooldown=False):
        return "cline-builder-test"
    orch._trigger_build_pipeline_safe = fake_trigger

    async def fake_alive(name):
        return alive()
    orch._container_is_running = fake_alive

    orch.DESIGN_REVIEW_TIMEOUT = budget_blocking
    orch.DESIGN_REVIEW_TIMEOUT_STREAMING = {"architect": budget_streaming,
                                            "bugfix": budget_streaming}
    orch.DESIGN_REVIEW_POLL_SECS = 0.05
    orch.DESIGN_REVIEW_LIVENESS_SECS = 0.1
    orch.DESIGN_REVIEW_HEARTBEAT_SECS = 0.1
    orch.DESIGN_REVIEW_DEATH_GRACE_SECS = 0.5


async def collect(pass_key="bugfix", streaming=True, messages=None):
    out = []
    async for item in orch._run_design_review(messages or [], pass_key, streaming=streaming):
        out.append(item)
    return out


def finals(items):
    return [t for is_final, t in items if is_final]


# --------------------------------------------------------------------------


def test_a_finished_pass_returns_the_document():
    p = Project()
    install(p, alive=lambda: True)

    async def run():
        task = asyncio.create_task(collect())
        await asyncio.sleep(0.5)
        p.write_doc()
        return await task

    items = asyncio.run(run())
    f = finals(items)
    check("exactly one final item", len(f) == 1, f"got {len(f)}")
    check("the document is returned", "Root Cause" in f[0], repr(f[0][:80]))
    check("nothing follows the final item", items[-1][0] is True)


def test_a_dead_container_stops_the_wait_immediately():
    """
    The old loop polled for the file only, so a container that died in the first
    30s still held the turn for the full 680s and then said "still running".
    """
    p = Project()
    install(p, alive=lambda: False, budget_streaming=60, budget_blocking=60)

    started = time.time()
    items = asyncio.run(collect())
    elapsed = time.time() - started

    f = finals(items)
    check("gives up quickly rather than waiting out the budget", elapsed < 5,
          f"took {elapsed:.1f}s")
    check("reports the failure honestly", "stopped without producing" in f[0],
          repr(f[0][:80]))
    check("does not claim it is still running", "still running" not in f[0])


def test_a_container_that_dies_just_after_writing_still_wins():
    """
    The race the grace period exists for: exit and document-write are not
    ordered, and losing it would report a successful pass as a failure.

    The document cannot be pre-written - the gate clears stale design documents
    on entry, deliberately, so a sibling left by the other gate can never be
    built. So it lands mid-grace, which is the real shape of the race anyway.
    """
    p = Project()
    install(p, alive=lambda: False, budget_streaming=60)

    async def run():
        task = asyncio.create_task(collect())
        await asyncio.sleep(0.3)          # inside DEATH_GRACE_SECS
        p.write_doc(body="# 4. Root Cause\n- raced but written")
        return await task

    f = finals(asyncio.run(run()))
    check("the document wins over the dead container", "raced but written" in f[0],
          repr(f[0][:80]))


def test_progress_is_emitted_as_the_status_changes():
    p = Project()
    install(p, alive=lambda: True)

    async def run():
        task = asyncio.create_task(collect())
        for phase in ("Distilling: bugfix", "Resolving blockers: bugfix (round 1)",
                      "Verifying reproduction (2/3)"):
            p.write_status(phase)
            await asyncio.sleep(0.35)
        p.write_doc()
        return await task

    items = asyncio.run(run())
    progress = "".join(t for is_final, t in items if not is_final)
    for phase in ("Distilling: bugfix", "Resolving blockers", "Verifying reproduction (2/3)"):
        check(f"progress mentions {phase!r}", phase in progress)
    check("each phase is reported once", progress.count("Verifying reproduction (2/3)") == 1,
          progress)


def test_a_silent_pass_still_emits_keepalives():
    """
    The router forwards with httpx.Timeout(700.0); for a stream that is the gap
    BETWEEN chunks. A silent wait dies there however large the budget is.
    """
    p = Project()
    install(p, alive=lambda: True)

    async def run():
        task = asyncio.create_task(collect())
        await asyncio.sleep(1.0)          # never writes a status
        p.write_doc()
        return await task

    items = asyncio.run(run())
    keepalives = [t for is_final, t in items if not is_final and t == ""]
    check("emits keepalives while silent", len(keepalives) >= 2, f"got {len(keepalives)}")


def test_the_blocking_path_stays_silent_and_bounded():
    """
    A non-streaming client has no heartbeat to offer, so it must stay under the
    router's 700s and must not receive progress chunks.
    """
    p = Project()
    install(p, alive=lambda: True, budget_blocking=2, budget_streaming=60)

    items = asyncio.run(collect(streaming=False))
    check("no progress on the blocking path",
          all(is_final for is_final, _ in items), items)
    f = finals(items)
    check("times out with the fallback advice", "!review" in f[0], repr(f[0][:80]))
    check("the budget it reports is the blocking one", " 2s wait" in f[0], repr(f[0][:120]))


def test_the_streaming_budget_is_bigger_and_pass_aware():
    real = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(real)
    check("bugfix waits longer than architect",
          real.DESIGN_REVIEW_TIMEOUT_STREAMING["bugfix"]
          > real.DESIGN_REVIEW_TIMEOUT_STREAMING["architect"])
    check("both exceed the old fixed wait",
          min(real.DESIGN_REVIEW_TIMEOUT_STREAMING.values()) > real.DESIGN_REVIEW_TIMEOUT)
    check("the blocking wait stays under the router's 700s",
          real.DESIGN_REVIEW_TIMEOUT < 700)


def test_sse_framing_is_valid_and_terminated():
    p = Project()
    install(p, alive=lambda: True)

    async def run():
        resp = orch._progress_command_response(
            orch._run_design_review([], "bugfix", streaming=True),
            is_streaming=True, is_native=False)
        chunks = []

        async def feed():
            await asyncio.sleep(0.3)
            p.write_status("Verifying reproduction (1/3)")
            await asyncio.sleep(0.3)
            p.write_doc()

        task = asyncio.create_task(feed())
        async for c in resp.body_iterator:
            chunks.append(c)
        await task
        return b"".join(chunks)

    body = asyncio.run(run())
    check("terminates the stream", body.endswith(b"data: [DONE]\n\n"))
    payloads = [ln[6:] for ln in body.split(b"\n\n") if ln.startswith(b"data: ")
                and ln[6:] != b"[DONE]"]
    check("every chunk is valid JSON",
          all(json.loads(p_)["choices"][0]["delta"] is not None for p_ in payloads))
    joined = "".join(json.loads(p_)["choices"][0]["delta"]["content"] for p_ in payloads)
    check("the document reaches the client", "Root Cause" in joined, repr(joined[:120]))


def test_native_framing_is_valid_and_terminated():
    p = Project()
    install(p, alive=lambda: True)

    async def run():
        resp = orch._progress_command_response(
            orch._run_design_review([], "bugfix", streaming=True),
            is_streaming=True, is_native=True)
        chunks = []

        async def feed():
            await asyncio.sleep(0.3)
            p.write_doc()

        task = asyncio.create_task(feed())
        async for c in resp.body_iterator:
            chunks.append(c)
        await task
        return b"".join(chunks)

    body = asyncio.run(run())
    lines = [json.loads(ln) for ln in body.decode().strip().split("\n")]
    check("last line marks done", lines[-1].get("done") is True, lines[-1])
    joined = "".join(l.get("message", {}).get("content", "") for l in lines)
    check("the document reaches the native client", "Root Cause" in joined)


def test_a_failed_launch_short_circuits():
    p = Project()
    install(p, alive=lambda: True)

    async def fake_trigger(messages, extra_env=None, mode_label="", skip_cooldown=False):
        return "⚠️ **Build aborted.** No code snippets found."
    orch._trigger_build_pipeline_safe = fake_trigger

    items = asyncio.run(collect())
    check("the launch error is the only item", len(items) == 1, items)
    check("and it is final", items[0][0] is True)
    check("and it is the launcher's message", "Build aborted" in items[0][1])


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
