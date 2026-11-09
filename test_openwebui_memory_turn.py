"""
Open WebUI 0.13 requests: the typed message is not last, and tool calls are in
Ollama's shape. See owui.py.

`!approve` typed with memory on reached the Expert as a plain chat turn - the
orchestrator read the recalled memory as the prompt - and vLLM rejected the
replayed recall_memories call with a 400: "Agentic inference failed."

Loads orchestrator.py by path and drives the real _orchestrate, stubbing only
ComfyUI, the pipeline probe and the project binding.
"""
import asyncio
import importlib.util
import json
import os
import sys
from unittest import mock

os.environ.setdefault("LLAMACPP_BINARY", "/bin/true")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("orch", os.path.join(HERE, "orchestrator.py"))
orch = importlib.util.module_from_spec(_spec)
sys.modules["orch"] = orch
_spec.loader.exec_module(orch)

import owui  # noqa: E402

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
        self.url = mock.Mock(path="/api/chat")

    async def json(self):
        return self._body


MEMORY = ("<memory_context>\n[User Memory]\n- if working on veriform, use the "
          "veriform-postgres docker\n</memory_context>")


def recalled(typed, call_name="recall_memories"):
    """What Open WebUI 0.13 sends on /api/chat for one typed turn, memory on."""
    return [
        {"role": "system", "content": "You are Bob."},
        {"role": "user", "content": "!architect\nAdd a widget cache."},
        {"role": "assistant", "content": "Architect pass running"},
        {"role": "user", "content": typed},
        # Ollama shape: index, no type, arguments as an object.
        {"role": "assistant", "content": "", "tool_calls": [
            {"index": 0, "id": "fc_9f2", "function": {"name": call_name, "arguments": {}}}]},
        {"role": "tool", "tool_call_id": "fc_9f2", "content": MEMORY},
    ]


def reply_text(response):
    return json.loads(bytes(response.body))["message"]["content"]


def orchestrate(messages):
    orch.expert_warm_until = 0
    orch.vram_locked = False
    with mock.patch.multiple(
            orch,
            is_comfy_active=mock.AsyncMock(return_value=False),
            get_comfy_history_count=mock.AsyncMock(return_value=0),
            is_pipeline_active=mock.AsyncMock(return_value=False),
            free_comfyui=mock.AsyncMock(),
            _get_bound_project_dir=mock.Mock(return_value=None)):
        return asyncio.run(orch._orchestrate(Req({"stream": False, "messages": messages})))


def test_approve_with_memory_on_is_handled_as_the_command():
    response = orchestrate(recalled("!approve"))
    text = reply_text(response)
    check("the orchestrator answers it as !approve, no model call",
          "Nothing to approve" in text, text[:200])


def test_the_typed_message_is_put_back_last_and_the_memory_kept():
    out = owui.put_typed_turn_last(recalled("!approve"))
    check("the typed message is last", out[-1] == {"role": "user", "content": "!approve"}, out[-1])
    check("the recall and its result are kept, just before it",
          out[-3].get("tool_calls") and out[-2].get("content") == MEMORY, out[-3:])
    check("nothing is lost", len(out) == len(recalled("!approve")))
    check("and doing it twice changes nothing", owui.put_typed_turn_last(out) == out)


def test_a_tool_the_model_chose_is_a_continuation_and_stays_put():
    messages = recalled("what's in app.ts?", call_name="read_file")
    check("left exactly as it arrived", owui.put_typed_turn_last(messages) == messages)


def test_vllm_gets_tool_calls_in_openai_shape():
    body = {"model": "qwen", "messages": recalled("hello"), "options": {}}
    adapted = orch._adapt_body(body, {"provider": "vllm"})
    call = adapted["messages"][4]["tool_calls"][0]
    check("arguments is a JSON string", call["function"]["arguments"] == "{}", call)
    check("type is function", call["type"] == "function", call)
    check("the id the tool result answers is kept", call["id"] == "fc_9f2", call)
    original = body["messages"][4]["tool_calls"][0]
    check("the caller's body is not mutated", original["function"]["arguments"] == {}, original)


def test_ollama_keeps_the_object_form():
    body = {"model": "qwen", "messages": recalled("hello")}
    check("passed through untouched", orch._adapt_body(body, {"provider": "ollama"}) is body)


def test_the_router_classifies_the_typed_command():
    _rspec = importlib.util.spec_from_file_location("router_mod", os.path.join(HERE, "router.py"))
    router = importlib.util.module_from_spec(_rspec)
    _rspec.loader.exec_module(router)
    messages = owui.put_typed_turn_last(recalled("!approve"))
    got = router._classify_request(messages)
    check("!approve is a desktop command", got == "desktop", got)


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
