"""
Behaviour tests for the two ways the shared vLLM expert got slept mid-build.

Loads orchestrator.py by path and stubs only what reaches outside the process:
the sleep/wake HTTP calls and ComfyUI. The janitor loop, the shutdown endpoint,
the live-build registry and the idle arithmetic are the real code.

The bugs these pin down, both observed on a live build:

  1. The janitor measured idleness against expert_warm_until, which is 0 on a
     fresh process and is reset to 0 all over the file to mean "not warm". So
     `now > 0 + VLLM_SLEEP_AFTER_IDLE` was true for every clock since 1970 and
     the first five-minute tick slept a working engine, logging ~29,821,919
     minutes of idleness.

  2. The builder's EXIT trap POSTs /v1/shutdown_expert, which fires once per
     CONTAINER. With two builds overlapping, the first to finish slept the
     shared engine out from under the second AND cleared vram_locked, re-arming
     the janitor to do it again every five minutes for the rest of that build.
"""
import asyncio
import importlib.util
import json
import os
import sys
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


class Engine:
    """A resident vLLM expert that records whether anyone slept it."""

    def __init__(self, asleep=False):
        self.asleep = asleep
        self.sleeps = 0
        self.wakes = 0

    def install(self):
        async def is_sleeping(base_url):
            return self.asleep

        async def sleep(config):
            self.sleeps += 1
            self.asleep = True
            return True

        async def wake(config):
            self.wakes += 1
            self.asleep = False
            return True

        orch._vllm_is_sleeping = is_sleeping
        orch._vllm_sleep = sleep
        orch._vllm_wake = wake
        return self


def reset(engine_asleep=False):
    """A fresh process, one tick away from the janitor's first pass."""
    orch.vram_locked = False
    orch.expert_warm_until = 0
    orch.expert_last_used = time.time()
    orch._live_builds.clear()
    orch._managed_processes.clear()

    async def no_comfy():
        return False

    async def no_free():
        return None
    orch.is_comfy_active = no_comfy
    orch.free_comfyui = no_free
    return Engine(asleep=engine_asleep).install()


async def tick():
    """One janitor pass, without waiting out its 300s sleep."""
    real_sleep = asyncio.sleep
    calls = {"n": 0}

    async def fast_sleep(seconds, *a, **kw):
        # Count only the loop's own 300s pause, so an incidental sleep inside
        # the body cannot end the pass early and fake a passing test.
        if seconds and seconds >= 300:
            calls["n"] += 1
            if calls["n"] > 1:
                raise asyncio.CancelledError()
        return await real_sleep(0)

    asyncio.sleep = fast_sleep
    try:
        await orch.periodic_cleanup()
    finally:
        asyncio.sleep = real_sleep


def test_a_fresh_process_does_not_sleep_a_working_engine():
    engine = reset()
    asyncio.run(tick())
    check("an engine used just now is left alone", engine.sleeps == 0,
          f"slept {engine.sleeps}x with expert_last_used = now")


def test_a_genuinely_idle_engine_is_still_slept():
    engine = reset()
    orch.expert_last_used = time.time() - orch.VLLM_SLEEP_AFTER_IDLE - 60
    asyncio.run(tick())
    check("past the grace, the VRAM is released", engine.sleeps == 1,
          f"slept {engine.sleeps}x")


def test_a_live_build_holds_the_janitor_off():
    engine = reset()
    # Idle by every local timer - the builder's traffic never reaches here.
    orch.expert_last_used = time.time() - orch.VLLM_SLEEP_AFTER_IDLE - 6000
    orch._live_builds.add("cline-builder-1")
    asyncio.run(tick())
    check("a live build is not idleness", engine.sleeps == 0,
          f"slept {engine.sleeps}x with a build running")


def test_an_exiting_sibling_cannot_sleep_a_live_build_s_engine():
    engine = reset()
    orch.vram_locked = True
    orch._live_builds.update({"cline-builder-1", "cline-builder-2"})

    # Builder 1 exits and its EXIT trap hits the endpoint.
    orch._live_builds.discard("cline-builder-1")
    resp = asyncio.run(orch.shutdown_expert())
    body = json.loads(resp.body)

    check("the endpoint declines", body.get("status") == "declined", body)
    check("and names who is still running",
          body.get("live_builds") == ["cline-builder-2"], body)
    check("the engine stays awake", engine.sleeps == 0, f"slept {engine.sleeps}x")
    check("and the surviving build keeps its VRAM lock", orch.vram_locked is True)

    # ...and the janitor it would have re-armed still holds off.
    orch.expert_last_used = time.time() - orch.VLLM_SLEEP_AFTER_IDLE - 6000
    asyncio.run(tick())
    check("so the next tick does not finish the job", engine.sleeps == 0,
          f"slept {engine.sleeps}x on the tick after the sibling exited")


def test_the_last_build_out_still_releases_the_vram():
    engine = reset()
    orch.vram_locked = True
    orch._live_builds.add("cline-builder-2")

    orch._live_builds.discard("cline-builder-2")   # the monitor deregisters first
    resp = asyncio.run(orch.shutdown_expert())
    body = json.loads(resp.body)

    check("the endpoint acts", body.get("status") == "ok", body)
    check("the engine is slept", engine.sleeps == 1, f"slept {engine.sleeps}x")
    check("and the lock is cleared", orch.vram_locked is False)


def test_a_dead_monitor_does_not_pin_the_engine_awake():
    engine = reset()
    orch._live_builds.add("cline-builder-3")

    async def explode(*a, **kw):
        raise RuntimeError("docker inspect blew up")
    orch.asyncio.create_subprocess_exec = explode

    try:
        asyncio.run(orch._docker_safety_monitor(
            "cline-builder-3", workspace="/tmp", base_branch="main"))
    finally:
        importlib.reload  # keep the stub scoped to this test's process exit

    check("the crashed monitor deregisters its build",
          "cline-builder-3" not in orch._live_builds, orch._live_builds)


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
