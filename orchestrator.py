import asyncio
import httpx
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

import mover


# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("Bob-Orchestrator")

# --- STRICT MODEL CONFIG ---
DEFAULT_EXPERT_MODEL = "qwen3.5:27b"
ROUTER_MODEL = "qwen2.5:1.5b"
EXPERT_MODEL = "qwen3.5:27b"  # Switch this to any model
OLLAMA_URL = "http://localhost:11434"
COMFYUI_URL = "http://localhost:8188"

# --- STATE MANAGEMENT ---
gpu_lock = asyncio.Lock()
http_client: httpx.AsyncClient = None
vram_locked = False
expert_warm_until = 0
expert_mode = "general"  # "general" or "coding"
last_comfy_history_count = 0  # Track ComfyUI history count for automated pings

# --- EXPERT PARAMETERS (Thinking Modes) ---
PARAMS_GENERAL = {
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
    "repeat_penalty": 1.0,
}

PARAMS_CODING = {
    "temperature": 0.6,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 0.0,
    "repeat_penalty": 1.0,
}

# --- LIFESPAN ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manages the application lifecycle, initializing and closing the HTTP client."""
    global http_client, last_comfy_history_count
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
    logger.info("HTTP client initialized.")

    # Synchronize initial ComfyUI history state
    last_comfy_history_count = await get_comfy_history_count()

    # Start periodic background cleanup
    cleanup_task = asyncio.create_task(periodic_cleanup())

    yield
    cleanup_task.cancel()
    await http_client.aclose()
    logger.info("HTTP client closed.")


app = FastAPI(title="Bob: AI Workspace Orchestrator", lifespan=lifespan)

# =============================================================================
# VRAM MANAGEMENT
# =============================================================================

async def get_loaded_models() -> list[str]:
    """Queries the Ollama API to identify models currently residing in VRAM."""
    try:
        resp = await http_client.get(f"{OLLAMA_URL}/api/ps", timeout=2.0)
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            return [m.get("name", "") for m in models]
    except Exception:
        pass
    return []

async def force_unload(model_name: str):
    """
    Directly requests Ollama to unload the specified model from VRAM.
    Uses a minimal request payload to ensure high reliability.
    """
    try:
        logger.info(f"Unloading model: {model_name}")
        await http_client.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model_name, "keep_alive": 0},
            timeout=5.0
        )
    except Exception as e:
        logger.warning(f"Failed to unload {model_name}: {e}")

async def verified_unload(model_name: str, max_wait: float = 3.0):
    """
    Attempts to unload a model and verifies its removal through short polling.
    """
    loaded = await get_loaded_models()
    if not any(model_name in m for m in loaded):
        return True

    await force_unload(model_name)

    deadline = time.time() + max_wait
    while time.time() < deadline:
        await asyncio.sleep(0.4)
        loaded = await get_loaded_models()
        if not any(model_name in m for m in loaded):
            return True

    logger.warning(f"{model_name} persistent in VRAM after {max_wait}s; continuing.")
    return False


async def sweep_vram_for_expert():
    """
    Ensures the Router model is removed before loading the Expert model.
    This prevents VRAM fragmentation and avoids offloading to slower system RAM.
    """
    loaded = await get_loaded_models()
    if any(ROUTER_MODEL in m for m in loaded):
        logger.info("Sweeping VRAM for Expert model load.")
        await verified_unload(ROUTER_MODEL)

async def free_comfyui():
    """Immediately signals ComfyUI to clear models and release system/video memory."""
    try:
        await http_client.post(
            f"{COMFYUI_URL}/free",
            json={"unload_models": True, "free_memory": True},
            timeout=3.0
        )
    except Exception:
        pass


async def periodic_cleanup():
    """
    Background loop that periodically sweeps memory if the system is idle.
    Runs every 5 minutes.
    """
    while True:
        try:
            await asyncio.sleep(300)  # 5 minute interval
            if not await is_comfy_active() and not gpu_lock.locked():
                logger.info("Periodic idle cleanup triggered.")
                await free_comfyui()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cleanup Task Error: {e}")
            await asyncio.sleep(60)  # Wait before retry on error


async def is_comfy_active() -> bool:
    """Checks the ComfyUI queue for active or pending generation jobs."""
    try:
        resp = await http_client.get(f"{COMFYUI_URL}/queue", timeout=2.0)
        if resp.status_code == 200:
            data = resp.json()
            return len(data.get("queue_running", [])) > 0 or len(data.get("queue_pending", [])) > 0
    except Exception:
        pass
    return False

async def get_comfy_history_count() -> int:
    """Retrieves the total number of completed prompts from the ComfyUI history."""
    try:
        resp = await http_client.get(f"{COMFYUI_URL}/history", timeout=2.0)
        if resp.status_code == 200:
            return len(resp.json())
    except Exception:
        pass
    return 0

# =============================================================================
# TRIAGE
# =============================================================================

async def analyze_request(messages: list) -> dict:
    """
    Performs request triage using the Router model to determine task complexity.
    Routes complex logic or coding tasks to the Expert model.
    """
    recent_msgs = [m for m in messages if m.get("role") == "user"][-2:]
    context_text = "\n".join([m.get("content", "") for m in recent_msgs])

    system_msg = (
        "You are the Triage AI for a workspace. Read the user's prompt and evaluate it. "
        "The 'Expert' AI is incredibly busy and expensive. You must only call the Expert if "
        "the complexity is above 6 (e.g., coding, deep logic, advanced math). "
        "Complexity 1-4: Small talk, greetings, simple facts. "
        "Complexity 5-10: Deep logic, advanced math, structural coding. "
        "Guess if the user will need follow-up questions to solve this task (true/false). "
        "Also determine if this is a 'coding' task (True if involves writing code, debugging, or technical WebDev logic). "
        "and if it requires a tool (True if it needs web search). "
        "Respond ONLY in pure JSON format: {\"complexity\": <int>, \"expect_followups\": <bool>, \"requires_tool\": <bool>, \"is_coding\": <bool>}"
    )

    payload = {
        "model": ROUTER_MODEL,
        "format": "json",
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": context_text[:1000]}
        ],
        "stream": False,
        "keep_alive": 0,
        "options": {"temperature": 0.0, "num_ctx": 2048}
    }

    try:
        resp = await http_client.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=10.0)
        if resp.status_code == 200:
            content = resp.json().get("message", {}).get("content", "{}")
            data = json.loads(content)
            return {
                "complexity": int(data.get("complexity", 1)),
                "followups": bool(data.get("expect_followups", False)),
                "requires_tool": bool(data.get("requires_tool", False)),
                "is_coding": bool(data.get("is_coding", False))
            }
    except Exception as e:
        logger.warning(f"Router network/parsing error: {e}")

    return {"complexity": 1, "followups": False, "requires_tool": False, "is_coding": False}

# =============================================================================
# STREAMING
# =============================================================================

async def stream_proxy(url: str, body: dict, lock: asyncio.Lock, is_native: bool = False):
    """
    Proxies a streaming response from the backend AI model while managing VRAM locks.
    """
    lock_released = False

    def _release():
        nonlocal lock_released
        if not lock_released:
            lock_released = True
            try:
                lock.release()
            except RuntimeError:
                pass

    try:
        async with http_client.stream("POST", url, json=body, timeout=600.0) as resp:
            if resp.status_code != 200:
                yield b"ERROR: Backend unavailable"
                return

            async for line in resp.aiter_lines():
                if not line:
                    continue

                if is_native:
                    try:
                        data = json.loads(line)
                        data["model"] = "Bob"
                        yield f"{json.dumps(data)}\n".encode('utf-8')
                    except Exception:
                        yield f"{line}\n".encode('utf-8')
                else:
                    if line.startswith("data: "):
                        if line == "data: [DONE]":
                            yield b"data: [DONE]\n\n"
                            continue
                        try:
                            data = json.loads(line[6:])
                            data["model"] = "Bob"
                            if "id" in data:
                                data["id"] = "chatcmpl-Bob"
                            yield f"data: {json.dumps(data)}\n\n".encode('utf-8')
                        except (Exception, json.JSONDecodeError):
                            rewritten = re.sub(r'("model"\s*:\s*")[^"]+(")', r'\1Bob\2', line)
                            yield f"{rewritten}\n\n".encode('utf-8')
    except (GeneratorExit, asyncio.CancelledError):
        pass
    except Exception as e:
        logger.error(f"Stream Error: {e}")
    finally:
        _release()

# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.get("/api/tags")
@app.get("/v1/models")
async def list_models(request: Request):
    """Returns a unified model list to satisfy both Ollama and OpenAI API clients."""
    is_tags = "tags" in str(request.url)
    bob_model = {
        "name": "Bob", "model": "Bob", "modified_at": "2026-03-16T00:00:00Z", "size": 0,
        "digest": "bob-identity",
        "details": {"family": "llama", "parameter_size": "Expert", "quantization_level": "Q8_0"}
    } if is_tags else {
        "id": "Bob", "object": "model", "created": int(time.time()), "owned_by": "System"
    }

    if is_tags:
        return JSONResponse(content={"models": [bob_model]})
    return JSONResponse(content={"object": "list", "data": [bob_model]})

@app.get("/health")
async def health_check():
    """Debug state."""
    loaded = await get_loaded_models()
    return JSONResponse(content={
        "loaded_models": loaded,
        "expert_warm": time.time() < expert_warm_until,
        "expert_mode": expert_mode,
        "vram_locked": vram_locked,
        "gpu_lock_held": gpu_lock.locked()
    })

@app.post("/api/chat")
@app.post("/v1/chat/completions")
async def proxy_ollama(request: Request):
    """
    Main entry point for AI chat requests.
    Handles orchestration, triage, interception, and VRAM management.
    """
    global vram_locked, expert_warm_until, expert_mode, last_comfy_history_count

    body = await request.json()
    is_streaming = body.get("stream", True)
    messages = body.get("messages", [])
    path = str(request.url.path)
    is_native = "/api/chat" in path

    is_background_task = False
    if messages:
        last_msg = messages[-1]
        last_content = str(last_msg.get("content", "")).lower()
        role = last_msg.get("role", "")

        # 1. Generation State Tracking
        is_busy = await is_comfy_active()
        current_history_count = await get_comfy_history_count()
        has_finished_gen = current_history_count > last_comfy_history_count
        has_gen_signal = is_busy or has_finished_gen

        # 2. Categorization
        # Detect UI-injected context tags
        system_content = "".join([m.get("content", "") for m in messages if m.get("role") == "system"]).lower()
        has_injected_context = "the requested image has been created" in system_content or "<context>" in system_content

        # Background tasks (Follow-ups, titles, etc.)
        is_suggestion_ping = any(kw in last_content for kw in ["suggest 3-5", "generate a title", "generate a short title", "summarize", "short label", "tags"])
        # Prompt Expansion (Inhibited by policy)
        is_expansion_ping = "generate a detailed prompt" in last_content or "### task:\ngenerate a detailed prompt" in last_content

        # Image Descriptions
        description_keywords = ["describe", "analyze", "summarize", "tell me about", "what is in this", "what do you see", "explain the image"]
        is_description_ping = has_injected_context or (
            any(kw in last_content for kw in description_keywords) and ("### task:" in last_content or len(last_content) < 400)
        )

        # Tool results
        is_tool_result = (role == "tool")
        is_image_tool = is_tool_result and any(kw in last_content for kw in ["![", "comfy", "image_url", "image/"])
        is_search_tool = is_tool_result and not is_image_tool
        
        # Search Intent (In user prompt)
        search_triggers = [
            "google", "find on the web", "look up", "latest news", "recent news", "current weather", 
            "what is the price of", "what is the status of", "current events", "today",
            "recently", "latest version of"
        ]
        is_search_query = (role == "user") and any(kw in last_content for kw in search_triggers)
        
        # Image Intent (In user prompt)
        image_triggers = ["generate an image", "create an image", "create a picture", "draw a", "make an image", "flux", "comfyui"]
        is_image_query = (role == "user") and any(kw in last_content for kw in image_triggers)

        # History-based Search Detection (Check only last 2 messages)
        has_search_history = any(
            (m.get("role") == "tool" or "retrieved" in str(m.get("content", "")).lower() or "sources" in str(m.get("content", "")).lower()) 
            and not any(kw in str(m.get("content", "")).lower() for kw in ["![", "comfy", "image_url", "image/"])
            for m in messages[-2:]
        )
        
        # 3. Interception Rules
        # Rule A: Silence image tool outputs
        if is_image_tool:
            last_comfy_history_count = current_history_count
            asyncio.create_task(free_comfyui())
            return _silent_response(is_native)

        # Rule B: Silence expansions and fresh-image descriptions
        should_kill_comment = (is_description_ping and has_gen_signal)
        if should_kill_comment or is_expansion_ping:
            last_comfy_history_count = current_history_count
            asyncio.create_task(free_comfyui())
            return _silent_response(is_native)

        # Rule C: Route background pings to small model
        is_background_task = (is_suggestion_ping or is_description_ping or is_expansion_ping) and not (is_search_tool or is_search_query or has_search_history or is_image_query)

        # Rule D: Signal Reset (Manual user turns clear the state)
        if role == "user" and not (is_suggestion_ping or is_description_ping or is_expansion_ping or "### task:" in last_content):
            if current_history_count != last_comfy_history_count:
                last_comfy_history_count = current_history_count
                asyncio.create_task(free_comfyui())

    # =============================================================================

    # 2. Fast Exit for Background Traffic
    if not is_streaming and (time.time() < expert_warm_until or vram_locked):
        return _silent_response(is_native, "Analyzing...")

    # 3. Request Orchestration
    try:
        await asyncio.wait_for(gpu_lock.acquire(), timeout=120.0)
    except asyncio.TimeoutError:
        return JSONResponse(status_code=503, content={"error": "The orchestrator is busy."})

    lock_held = True
    try:
        current_time = time.time()
        is_expert_warm = current_time < expert_warm_until
        prompt_lower = (messages[-1].get("content", "") if messages else "").lower()

        # Handle explicit user commands
        if not is_background_task:
            if "!lock" in prompt_lower:
                vram_locked = True
                return _command_response("🔒 **VRAM Locked.** Expert model is persistent.", is_streaming, is_native)
            elif "!unlock" in prompt_lower:
                vram_locked = False
                expert_warm_until = 0
                await verified_unload(EXPERT_MODEL)
                return _command_response("🔓 **VRAM Unlocked.** Memory cleared.", is_streaming, is_native)
            elif "!code" in prompt_lower:
                expert_mode = "coding"
                logger.info("Command: Switched to Coding Mode.")
            elif "!general" in prompt_lower:
                expert_mode = "general"
                logger.info("Command: Switched to General Mode.")
            elif "!move" in prompt_lower:
                logger.info("Command: Move initiated.")
                success = mover.handle_move(messages)
                msg = "Files moved!" if "Moved" in success else "Files failed to move!"
                return _command_response(msg, is_streaming, is_native)

        # Determine target model and load strategy
        target_model = ROUTER_MODEL
        keep_alive = 0
        is_cold_expert = False

        if is_background_task or is_image_query or is_search_query or is_search_tool or has_search_history:
            target_model = ROUTER_MODEL
            if is_image_query:
                logger.info("Image generation intent detected: Routing to Router model.")
            elif is_search_query or is_search_tool or has_search_history:
                source = "intent" if is_search_query else "result" if is_search_tool else "history"
                logger.info(f"Search {source} detected: Routing to Router model.")
            else:
                logger.info("Routing background task to Router model.")
        elif any(kw in prompt_lower for kw in ["!bob", "hey bob"]):
            target_model = ROUTER_MODEL
            expert_warm_until = 0
            logger.info("Direct request for Router model.")
        elif any(kw in prompt_lower for kw in ["!expert", "hey expert", "!code", "!general"]):
            target_model = EXPERT_MODEL
            keep_alive = "10m"
            expert_warm_until = current_time + 600
            is_cold_expert = not is_expert_warm
            logger.info(f"Direct request for Expert model ({expert_mode}).")
        elif is_expert_warm or vram_locked:
            target_model = EXPERT_MODEL
            keep_alive = "10m" if not vram_locked else "-1"
            if not vram_locked:
                expert_warm_until = current_time + 600
        else:
            # Complexity Triage
            await sweep_vram_for_expert()
            analysis = await analyze_request(messages)
            await verified_unload(ROUTER_MODEL)

            if analysis.get("complexity", 1) > 6 or analysis.get("requires_tool", False):
                target_model = EXPERT_MODEL
                keep_alive = "3m" if analysis.get("followups") else 0
                if analysis.get("followups"):
                    expert_warm_until = current_time + 300
                is_cold_expert = True
                expert_mode = "coding" if analysis.get("is_coding") else "general"
                logger.info(f"Triage: Expert model required ({expert_mode}).")
            else:
                target_model = ROUTER_MODEL
                logger.info("Triage: Router model sufficient.")

        if target_model == EXPERT_MODEL and is_cold_expert:
            await sweep_vram_for_expert()

        # --- Payload Config ---
        options = body.get("options", {})
        if is_background_task:
            options.update({"temperature": 0.0, "num_ctx": 2048})
        elif target_model == EXPERT_MODEL:
            # Apply Thinking Mode parameters for Expert only if using the default model
            if EXPERT_MODEL == DEFAULT_EXPERT_MODEL:
                params = PARAMS_CODING if expert_mode == "coding" else PARAMS_GENERAL
                options.update(params)
                logger.debug(f"Applied expert parameters for {EXPERT_MODEL}")
            else:
                logger.info(f"Non-default expert {EXPERT_MODEL} detected; using default model settings.")
            
            options["num_ctx"] = 8192
        else:
            options.update({"temperature": options.get("temperature", 0.7), "num_ctx": 8192})

        body.update({"model": target_model, "keep_alive": keep_alive, "options": options})
        logger.info(f"Executing {target_model} (ctx: {options['num_ctx']}, warm: {not is_cold_expert})")

        target_path = "/api/chat" if is_native else "/v1/chat/completions"

        if not is_streaming:
            try:
                resp = await http_client.post(f"{OLLAMA_URL}{target_path}", json=body, timeout=600.0)
                if resp.status_code != 200:
                    return JSONResponse(status_code=resp.status_code, content={"error": "Inference failed."})
                data = resp.json()
                data["model"] = "Bob"
                if "id" in data:
                    data["id"] = "chatcmpl-Bob"
                return JSONResponse(content=data)
            finally:
                if lock_held:
                    gpu_lock.release()
                    lock_held = False

        lock_held = False
        return StreamingResponse(
            stream_proxy(f"{OLLAMA_URL}{target_path}", body, gpu_lock, is_native=is_native),
            media_type="application/x-ndjson" if is_native else "text/event-stream"
        )
    except Exception as e:
        logger.error(f"Orchestration Error: {e}")
        return JSONResponse(status_code=500, content={"error": "Internal orchestration error."})
    finally:
        if lock_held:
            gpu_lock.release()


def _silent_response(is_native: bool, text: str = ""):
    """Returns an empty assistant response to silently terminate an interaction."""
    if is_native:
        return JSONResponse(content={"model": "Bob", "message": {"role": "assistant", "content": text}, "done": True})
    return JSONResponse(content={
        "id": "chatcmpl-Bob", "object": "chat.completion", "created": int(time.time()), "model": "Bob",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]
    })


def _command_response(text: str, is_streaming: bool = False, is_native: bool = False):
    """Returns a JSON response (streaming or one-shot) for internal orchestrator commands."""
    if not is_streaming:
        if is_native:
            return JSONResponse(content={"model": "Bob", "message": {"role": "assistant", "content": text}, "done": True})
        return JSONResponse(content={
            "id": "chatcmpl-Bob", "object": "chat.completion", "created": int(time.time()), "model": "Bob",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        })

    async def _generator():
        if is_native:
            yield f"{json.dumps({'model': 'Bob', 'message': {'role': 'assistant', 'content': text}, 'done': False})}\n".encode('utf-8')
            yield f"{json.dumps({'model': 'Bob', 'done': True})}\n".encode('utf-8')
        else:
            chunk = {
                "id": "chatcmpl-Bob", "object": "chat.completion.chunk", "created": int(time.time()), "model": "Bob",
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
            }
            yield f"data: {json.dumps(chunk)}\n\n".encode('utf-8')
            yield b"data: [DONE]\n\n"

    return StreamingResponse(_generator(), media_type="application/x-ndjson" if is_native else "text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False, log_level="warning")