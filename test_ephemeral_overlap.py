"""
Behaviour tests for overlapping builds sharing the ephemeral build services.

Observed on KAN-142 (2026-11-20): `!jira` launched the build four seconds
before the design container finished exiting. The design container's monitor
then removed `build-postgres`, whose network namespace the new build had just
joined, and every model call from the build failed with "[Errno 101] Network
is unreachable". It died 33 seconds in.

Loads orchestrator.py by path; only `_reset_ephemeral_services` (which shells
out to docker compose) is stubbed.
"""
import asyncio
import importlib.util
import os
import sys
import tempfile

os.environ.setdefault("LLAMACPP_BINARY", "/bin/true")

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
_spec = importlib.util.spec_from_file_location("orch", os.path.join(_here, "orchestrator.py"))
orch = importlib.util.module_from_spec(_spec)
sys.modules["orch"] = orch
_spec.loader.exec_module(orch)

FAILURES = []
FILES = ["-f", "docker-compose.yml", "-f", "docker-compose.ephemeral-veriform.yml"]


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILURES.append(name)


def _stub_reset():
    calls = []
    orch._reset_ephemeral_services = lambda files: calls.append(list(files))
    return calls


def test_finished_build_keeps_services_for_a_live_one():
    calls = _stub_reset()
    orch._live_builds.clear()
    orch._live_builds.add("cline-builder-2")  # the build that just started
    asyncio.run(orch._release_ephemeral_services("cline-builder-1", FILES))
    check("services left up while another build is live", calls == [], calls)
    orch._live_builds.clear()


def test_last_build_removes_services():
    calls = _stub_reset()
    orch._live_builds.clear()
    asyncio.run(orch._release_ephemeral_services("cline-builder-2", FILES))
    check("services removed once no build is live", calls == [FILES], calls)


def test_jira_waits_for_the_design_container():
    orch._live_builds.clear()
    orch._live_builds.add("cline-builder-design")

    async def scenario():
        async def exit_later():
            await asyncio.sleep(0.1)
            orch._live_builds.discard("cline-builder-design")
        task = asyncio.create_task(exit_later())
        ok = await orch._wait_for_builds_to_finish(30)
        await task
        return ok
    check("returns once the design container is gone", asyncio.run(scenario()) is True)

    orch._live_builds.add("cline-builder-stuck")
    check("gives up after the limit", asyncio.run(orch._wait_for_builds_to_finish(0)) is False)
    orch._live_builds.clear()


def test_jira_design_waits_out_a_slow_pass_instead_of_retrying():
    """A Jira architect pass ran past the chat gate's 1800s wait, the runner
    read "still running" as "no design" and started a second architect
    container beside the first."""
    alive = {"n": 3}

    async def running(name):
        alive["n"] -= 1
        return alive["n"] > 0

    async def gate(messages, pass_key, streaming=False):
        yield True, ("⏳ **Architect pass is still running** (`cline-builder-42`).\n\n"
                     "It exceeded the 1800s wait.")

    saved = (orch._run_design_review, orch._container_is_running, orch.JIRA_DESIGN_POLL_SECS,
             orch._design_review_path, orch._wait_for_builds_to_finish)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
    tmp.write("# Design\n\nDo it.")
    tmp.close()

    async def no_wait(limit):
        return True

    orch._run_design_review = gate
    orch._container_is_running = running
    orch.JIRA_DESIGN_POLL_SECS = 0
    orch._design_review_path = lambda messages, pass_key: tmp.name
    orch._wait_for_builds_to_finish = no_wait
    try:
        doc, _ = asyncio.run(orch._OrchestratorEngine("/tmp").design([], "architect"))
    finally:
        (orch._run_design_review, orch._container_is_running, orch.JIRA_DESIGN_POLL_SECS,
         orch._design_review_path, orch._wait_for_builds_to_finish) = saved
        os.unlink(tmp.name)
    check("waited for the container, then read its design", doc == "# Design\n\nDo it.", doc)
    check("polled until it exited", alive["n"] == 0, alive)


def test_container_exit_wait_gives_up():
    async def running(name):
        return True
    saved = (orch._container_is_running, orch.JIRA_DESIGN_POLL_SECS)
    orch._container_is_running, orch.JIRA_DESIGN_POLL_SECS = running, 0
    try:
        ok = asyncio.run(orch._wait_for_container_exit("cline-builder-1", 0))
    finally:
        orch._container_is_running, orch.JIRA_DESIGN_POLL_SECS = saved
    check("false while it is still running", ok is False)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()
    print("\n" + ("FAILED: " + ", ".join(FAILURES) if FAILURES else "ALL PASSED"))
    sys.exit(1 if FAILURES else 0)
