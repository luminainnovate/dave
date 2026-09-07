"""
Behaviour tests for how the finished-build offer reaches the user.

Loads orchestrator.py by path, as test_review_gate.py does, and drives the two
functions that wrap every chat turn: the turn classifier and the response
prefixer. Nothing here talks to a model - the point of the design is that the
offer is orchestrator text, not something a 3B router is asked to remember to
mention.
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile

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


class FakeURL:
    def __init__(self, path):
        self.path = path


class FakeRequest:
    def __init__(self, messages, native=False):
        self._body = {"messages": messages}
        self.url = FakeURL("/api/chat" if native else "/v1/chat/completions")

    async def json(self):
        return self._body


def openai_reply(text="hello"):
    return orch.JSONResponse(content={
        "id": "chatcmpl-Bob",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
    })


def native_reply(text="hello"):
    return orch.JSONResponse(content={
        "model": "Bob", "message": {"role": "assistant", "content": text}, "done": True})


def stream_reply(text="hello"):
    async def _gen():
        yield f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n".encode()
        yield b"data: [DONE]\n\n"
    return orch.StreamingResponse(_gen(), media_type="text/event-stream")


def drain(response):
    async def _collect():
        return b"".join([c async for c in response.body_iterator])
    return asyncio.run(_collect()).decode()


def body_of(response):
    return json.loads(response.body)


def with_offer(workspace, offer="🌿 **The build finished on `agent/build-1`.**"):
    """Point the orchestrator at a workspace with exactly one offer waiting."""
    pending = {"n": 0}

    def fake_pending(path):
        pending["n"] += 1
        return offer if pending["n"] == 1 else None

    orch._get_bound_project_dir = lambda messages: workspace
    orch.ship.pending_offer = fake_pending
    return pending


USER = [{"role": "user", "content": "how did the build go?"}]


# =============================================================================


def test_background_pings_are_not_real_turns():
    check("a title ping is not a turn",
          not orch._is_plain_user_turn([{"role": "user", "content": "Generate a title for this"}]))
    check("an Open WebUI task is not a turn",
          not orch._is_plain_user_turn([{"role": "user", "content": "### Task:\nsummarise"}]))
    check("an assistant turn is not a turn",
          not orch._is_plain_user_turn([{"role": "assistant", "content": "hi"}]))
    check("the agent's own traffic is not a turn",
          not orch._is_plain_user_turn([
              {"role": "system", "content": "You are Cline, a software engineer."},
              {"role": "user", "content": "read the file"}]))
    check("a person asking a question is", orch._is_plain_user_turn(USER))


def test_the_offer_is_prepended_to_a_one_shot_reply():
    with tempfile.TemporaryDirectory() as tmp:
        with_offer(tmp)
        out = asyncio.run(orch._attach_build_offer(FakeRequest(USER), openai_reply()))
        content = body_of(out)["choices"][0]["message"]["content"]
    check("the offer comes first", content.startswith("🌿"), content[:60])
    check("and the model's reply survives", content.endswith("hello"), content[-30:])


def test_the_offer_is_prepended_to_a_native_reply():
    with tempfile.TemporaryDirectory() as tmp:
        with_offer(tmp)
        out = asyncio.run(orch._attach_build_offer(FakeRequest(USER, native=True), native_reply()))
        content = body_of(out)["message"]["content"]
    check("the native body is rewritten too", content.startswith("🌿"), content[:60])


def test_the_offer_leads_a_stream():
    with tempfile.TemporaryDirectory() as tmp:
        with_offer(tmp)
        out = asyncio.run(orch._attach_build_offer(FakeRequest(USER), stream_reply()))
        text = drain(out)
    first = text.splitlines()[0]
    check("the first chunk carries the offer", "agent/build-1" in first, first[:80])
    check("the stream still terminates", text.endswith("data: [DONE]\n\n"))
    check("and the reply is still in it", "hello" in text)


def test_a_silenced_ping_does_not_spend_the_offer():
    with tempfile.TemporaryDirectory() as tmp:
        pending = with_offer(tmp)
        silent = orch._silent_response(False, "Analyzing...")
        out = asyncio.run(orch._attach_build_offer(FakeRequest(USER), silent))
    check("the placeholder is untouched", out is silent)
    check("and the offer was never read", pending["n"] == 0)


def test_ship_does_not_get_the_offer_prepended():
    with tempfile.TemporaryDirectory() as tmp:
        pending = with_offer(tmp)
        req = FakeRequest([{"role": "user", "content": "!ship"}])
        out = asyncio.run(orch._attach_build_offer(req, openai_reply("proposal")))
    check("the reply is unchanged", out is not None and body_of(out)["choices"][0]["message"]["content"] == "proposal")
    check("and the offer is still waiting", pending["n"] == 0)


def test_an_unbound_conversation_is_left_alone():
    orch._get_bound_project_dir = lambda messages: None
    reply = openai_reply()
    out = asyncio.run(orch._attach_build_offer(FakeRequest(USER), reply))
    check("nothing is prepended", out is reply)


def test_a_broken_offer_never_costs_the_user_their_reply():
    def explode(path):
        raise RuntimeError("marker on fire")
    orch._get_bound_project_dir = lambda messages: "/nowhere"
    orch.ship.pending_offer = explode
    reply = openai_reply()
    out = asyncio.run(orch._attach_build_offer(FakeRequest(USER), reply))
    check("the reply passes through", out is reply)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n{t.__name__}")
        try:
            t()
        except Exception:
            import traceback
            traceback.print_exc()
            FAILURES.append(t.__name__)
    print("\n" + ("-" * 60))
    print("FAILED:" if FAILURES else "ALL PASSED", len(FAILURES) or "")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1 if FAILURES else 0)
