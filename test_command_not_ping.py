"""
A `!` command the user typed must never be classed as an Open WebUI background
ping, whatever its argument contains.

The ping detectors are substring matches ("tags", "summarize", "describe", ...).
An `!architect` brief with a `| tags |` row in a table was read as the
tag-generation task, and Rule E silenced it as a follow-up to the previous
`!architect`: an empty reply and no pass.

Loads orchestrator.py by path and drives the real _orchestrate, stubbing only
ComfyUI, the pipeline probe and the design pass itself.
"""
import asyncio
import importlib.util
import os
import sys
from unittest import mock

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


class Req:
    def __init__(self, body):
        self._body = body
        self.url = mock.Mock(path="/v1/chat/completions")

    async def json(self):
        return self._body


PREVIOUS = [
    {"role": "user", "content": "!architect\nFirst draft of the brief."},
    {"role": "assistant", "content": "🏗️ **Architect pass running**"},
]

BRIEF = (
    "!architect\n**Business objective.** Configure the guardrail.\n\n"
    "| Setting | Value |\n|---|---|\n"
    "| tags | `app=veriform`, `feature=voice-dna` |\n"
    "Summarize the IAM policy and describe the logging setup.\n"
)


def passes_started(messages):
    started = []

    async def fake_review(msgs, pass_key, streaming=False):
        started.append(pass_key)
        yield True, "RAN"

    orch.expert_warm_until = 0
    orch.vram_locked = False
    with mock.patch.multiple(
            orch,
            is_comfy_active=mock.AsyncMock(return_value=False),
            get_comfy_history_count=mock.AsyncMock(return_value=0),
            is_pipeline_active=mock.AsyncMock(return_value=False),
            free_comfyui=mock.AsyncMock(),
            _run_design_review=fake_review):
        asyncio.run(orch._orchestrate(Req({"stream": False, "messages": messages})))
    return started


def test_a_brief_mentioning_tags_still_runs_the_pass():
    started = passes_started(PREVIOUS + [{"role": "user", "content": BRIEF}])
    check("the architect pass is started", started == ["architect"], repr(started))


def test_a_real_tags_ping_after_a_command_is_still_silenced():
    ping = ("### Task:\nGenerate 1-3 broad tags categorizing the main themes "
            "of the chat history.\n### Chat History:\n<chat_history>\n"
            "USER: !architect ...\n</chat_history>")
    started = passes_started(PREVIOUS + [{"role": "user", "content": ping}])
    check("no pass is started for Open WebUI's own ping", started == [], repr(started))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print("\n" + "-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL PASSED")
