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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()
    print("\n" + ("FAILED: " + ", ".join(FAILURES) if FAILURES else "ALL PASSED"))
    sys.exit(1 if FAILURES else 0)
