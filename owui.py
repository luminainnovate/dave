"""
Undo the parts of an Open WebUI request that the rest of br.ai.n cannot read.

Shared by orchestrator.py and router.py, so it imports nothing but the standard
library: the router runs on the Pi without the orchestrator's dependencies.

Open WebUI 0.13 changed two things about the message list it sends.

1. The typed message is no longer last. With native function calling on, it
   recalls memory (and activated skills) itself, before the model runs, and
   records that as ordinary tool history appended AFTER the user's message:

       user       "!approve"
       assistant  tool_calls=[recall_memories]          (hidden in the chat)
       tool       "<memory_context>...</memory_context>"

   Everything here that reads `messages[-1]` - every `!` command, the
   background-ping detectors, the router's classifier - read the memory instead
   of what the user typed. `!approve` fell through to an ordinary Expert turn.

2. Tool calls arrive in Ollama's shape on `/api/chat`: `arguments` is an object
   and there is no `"type": "function"`. vLLM's OpenAI endpoint validates both
   and rejects the request with a 400, which the agentic loop reports as
   "Agentic inference failed." Earlier versions never replayed tool calls in
   the history, so no request carried one.
"""
import json

# Tool calls Open WebUI makes on its own, before the model is asked anything,
# to put context in front of it (utils/middleware.py: preload_memory and
# preload_skills). Their results are context for the turn, not the turn.
PRELOAD_TOOLS = frozenset({"recall_memories", "view_skill"})


def _tool_name(call: dict) -> str:
    return str((call.get("function") or {}).get("name") or "")


def _is_preload_call(message: dict) -> bool:
    calls = message.get("tool_calls") or []
    return (message.get("role") == "assistant"
            and bool(calls)
            and not str(message.get("content") or "").strip()
            and all(_tool_name(c) in PRELOAD_TOOLS for c in calls))


def put_typed_turn_last(messages: list) -> list:
    """
    The message list with Open WebUI's preloaded context moved ahead of the
    user message it was recalled for, so that message is last again.

    Moved rather than dropped: the memory is still what the user asked Open
    WebUI to supply, and the model still sees it, as the tool result it is.
    Only a tail made entirely of preload calls and their results moves; a tail
    holding anything else - a tool the model chose to call, an assistant reply -
    is a real continuation and is left exactly as it arrived.
    """
    if not messages:
        return messages
    last_user = next((i for i in range(len(messages) - 1, -1, -1)
                      if messages[i].get("role") == "user"), None)
    if last_user is None or last_user == len(messages) - 1:
        return messages

    tail = messages[last_user + 1:]
    seen_call = False
    for message in tail:
        if _is_preload_call(message):
            seen_call = True
        elif message.get("role") == "tool" and seen_call:
            continue
        else:
            return messages
    return messages[:last_user] + tail + [messages[last_user]]


def openai_tool_calls(messages: list) -> list:
    """
    The message list with every assistant tool call in OpenAI's shape:
    `"type": "function"`, an `id`, and `arguments` as a JSON string.

    For OpenAI-compatible backends only; Ollama wants the object form Open WebUI
    already sends. Messages without tool calls are passed through as they are.
    """
    out = []
    for message in messages:
        calls = message.get("tool_calls")
        if message.get("role") != "assistant" or not calls:
            out.append(message)
            continue
        fixed = []
        for n, call in enumerate(calls):
            function = dict(call.get("function") or {})
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                function["arguments"] = json.dumps(arguments if arguments is not None else {})
            fixed.append({**call,
                          "id": call.get("id") or f"call_{len(out)}_{n}",
                          "type": call.get("type") or "function",
                          "function": function})
        out.append({**message, "tool_calls": fixed})
    return out
