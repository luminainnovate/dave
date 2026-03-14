import asyncio
import httpx
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("Bob-Orchestrator")

# --- STRICT MODEL CONFIG ---
ROUTER_MODEL = "qwen2.5:1.5b"
EXPERT_MODEL = "qwen3.5:27b"
OLLAMA_URL = "http://localhost:11434"
COMFYUI_URL = "http://localhost:8188"
WORKFLOW_PATH = "workflow_api.json"

# --- STATE MANAGEMENT ---
gpu_lock = asyncio.Lock()
http_client: httpx.AsyncClient = None
vram_locked = False
expert_warm_until = 0

# --- LIFESPAN ---
@asynccontextmanager
async def lifespan(app):
    global http_client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    logger.info("[STARTUP] HTTP client initialized.")
    yield
    await http_client.aclose()
    logger.info("[SHUTDOWN] HTTP client closed.")

app = FastAPI(title="Bob: AI Workspace Orchestrator", lifespan=lifespan)

# =============================================================================
# VRAM MANAGEMENT
# =============================================================================

async def get_loaded_models() -> list[str]:
    """Queries Ollama /api/ps to see what's actually in VRAM."""
    try:
        resp = await http_client.get(f"{OLLAMA_URL}/api/ps", timeout=2.0)
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            return [m.get("name", "") for m in models]
    except Exception:
        pass
    return []

async def force_unload(model_name: str):
    """Signals Ollama to unload a model. Minimal payload for reliability."""
    try:
        logger.info(f"[VRAM] Evicting {model_name}...")
        # Strictly correct Ollama unload: model + keep_alive 0
        await http_client.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model_name, "keep_alive": 0},
            timeout=5.0
        )
    except Exception as e:
        logger.warning(f"[VRAM ERROR] Failed to unload {model_name}: {e}")

async def verified_unload(model_name: str, max_wait: float = 3.0):
    """Unloads and confirms. Uses short polling to avoid blocking the lock too long."""
    loaded = await get_loaded_models()
    if not any(model_name in m for m in loaded):
        return True
    
    await force_unload(model_name)
    
    deadline = time.time() + max_wait
    while time.time() < deadline:
        await asyncio.sleep(0.4)
        loaded = await get_loaded_models()
        if not any(model_name in m for m in loaded):
            logger.info(f"[VRAM] Confirmed {model_name} is unloaded.")
            return True
    
    logger.warning(f"[VRAM] {model_name} still loaded after {max_wait}s — proceeding anyway.")
    return False

async def sweep_vram_for_expert():
    """CRITICAL: Ensures the tiny Router is GONE before the 27B Expert loads.
    Even 1GB of Router can push the 27B + KV Cache into slow System RAM (minutes of lag)."""
    loaded = await get_loaded_models()
    if any(ROUTER_MODEL in m for m in loaded):
        logger.info("[VRAM SWEEP] Router found in VRAM — evicting to prevent swapping")
        await verified_unload(ROUTER_MODEL)

# =============================================================================
# TRIAGE
# =============================================================================

async def analyze_request(messages: list) -> dict:
    """Smart Triage. Uses the Router model. Only reads recent context."""
    recent_msgs = [m for m in messages if m.get("role") == "user"][-2:]
    context_text = "\n".join([m.get("content", "") for m in recent_msgs])
    
    system_msg = (
        "You are the Triage AI for a workspace. Read the user's prompt and evaluate it. "
        "The 'Expert' AI is incredibly busy and expensive. You must only call the Expert if "
        "the complexity is above 6 (e.g., coding, deep logic, advanced math). "
        "Small talk, greetings, or simple facts are complexity 1-4. "
        "Guess if the user will need follow-up questions to solve this task (true/false). "
        "CRITICAL: Determine if the request requires using an external tool like Web Search to get real-time info or news (true/false). "
        "Respond ONLY in pure JSON format: {\"complexity\": <int>, \"expect_followups\": <bool>, \"requires_tool\": <bool>}"
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
        "options": {
            "temperature": 0.0,
            "num_ctx": 2048
        } 
    }
    
    try:
        resp = await http_client.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=10.0)
        if resp.status_code == 200:
            content = resp.json().get("message", {}).get("content", "{}")
            data = json.loads(content)
            return {
                "complexity": int(data.get("complexity", 1)),
                "followups": bool(data.get("expect_followups", False)),
                "requires_tool": bool(data.get("requires_tool", False))
            }
    except Exception as e:
        logger.warning(f"Router network/parsing error: {e}")

    return {"complexity": 1, "followups": False, "requires_tool": False}

# =============================================================================
# STREAMING
# =============================================================================

async def stream_proxy(url: str, body: dict, lock_to_release: asyncio.Lock, is_native_ollama: bool = False):
    """Streams data. Guarantees lock release even on client disconnect."""
    target_model = body.get("model")
    lock_released = False
    
    def _release_lock():
        nonlocal lock_released
        if not lock_released:
            lock_released = True
            try:
                lock_to_release.release()
            except RuntimeError:
                pass
    
    try:
        async with http_client.stream("POST", url, json=body, timeout=120.0) as resp:
            if resp.status_code != 200:
                error_body = await resp.aread()
                yield b"ERROR: " + error_body
                return
            
            async for line in resp.aiter_lines():
                if not line:
                    continue
                
                if is_native_ollama:
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
                            rewritten = re.sub(r'("model"\s*:\s*")[^"]+(")' , r'\1Bob\2', line)
                            yield f"{rewritten}\n\n".encode('utf-8')
    except (GeneratorExit, asyncio.CancelledError):
        logger.info(f"[STREAM] Cancelled during {target_model} stream")
    except Exception as e:
        logger.error(f"[STREAM ERROR] {e}")
    finally:
        logger.info(f"[STREAM] Finished {target_model} — releasing lock")
        _release_lock()

# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.get("/api/tags")
@app.get("/v1/models")
async def list_models(request: Request):
    """Forces Open-WebUI to only see Bob."""
    bob_model = {
        "name": "Bob", "model": "Bob", "modified_at": "2024-01-01T00:00:00.000000Z", "size": 0,
        "digest": "bob-identity", "details": {"family": "llama", "parameter_size": "Expert", "quantization_level": "Q8_0"}
    } if "tags" in str(request.url) else {
        "id": "Bob", "object": "model", "created": int(time.time()), "owned_by": "System"
    }
    return JSONResponse(content={"models": [bob_model]} if "models" not in str(request.url) else {"object": "list", "data": [bob_model]})

@app.get("/health")
async def health_check():
    """Debug state."""
    loaded = await get_loaded_models()
    return JSONResponse(content={
        "loaded_models": loaded,
        "expert_warm": time.time() < expert_warm_until,
        "vram_locked": vram_locked,
        "gpu_lock_held": gpu_lock.locked()
    })

@app.post("/api/chat")
@app.post("/v1/chat/completions")
async def proxy_ollama(request: Request):
    global vram_locked, expert_warm_until
    body = await request.json()
    is_streaming = body.get("stream", True)
    messages = body.get("messages", [])
    messages_str = str(messages).lower()
    
    # 1. Identify Background Tasks
    is_background_task = False
    if not is_streaming:
        if any(kw in messages_str for kw in ["title", "summarize", "short label", "generate a title", "tags"]):
            is_background_task = True

    # 2. BROAD Fast Exit: ALL non-streaming requests during Expert sessions are skipped.
    # Open WebUI user messages are ALWAYS streaming. Non-streaming = housekeeping
    # (titles, tags, summaries, follow-up suggestions, search queries, embeddings).
    # These were silently queuing up on the Expert, eating 30-60s before your follow-up.
    if not is_streaming and (time.time() < expert_warm_until or vram_locked):
        logger.info(f"[FAST-EXIT] Non-streaming request skipped (Expert session active)")
        if "/api/chat" in str(request.url.path):
            return JSONResponse(content={"model": "Bob", "message": {"role": "assistant", "content": "Analyzing..."}, "done": True})
        return JSONResponse(content={"id": "chatcmpl-Bob", "object": "chat.completion", "created": int(time.time()), "model": "Bob", "choices": [{"index": 0, "message": {"role": "assistant", "content": "Analyzing..."}, "finish_reason": "stop"}]})

    # 3. Lock Acquisition
    try:
        await asyncio.wait_for(gpu_lock.acquire(), timeout=120.0)
    except asyncio.TimeoutError:
        return JSONResponse(status_code=503, content={"error": "System busy."})

    lock_held = True
    try:
        current_time = time.time()
        is_expert_warm = current_time < expert_warm_until
        user_prompt_lower = (messages[-1].get("content", "") if messages else "").lower()

        # --- Manual Overrides ---
        if "!lock" in user_prompt_lower:
            vram_locked = True
            _ = JSONResponse(content={"id": "chatcmpl-Bob", "object": "chat.completion", "created": int(time.time()), "model": "Bob", "choices": [{"index": 0, "message": {"role": "assistant", "content": "🔒 VRAM Locked."}, "finish_reason": "stop"}]})
            gpu_lock.release()
            return _
        elif "!unlock" in user_prompt_lower:
            vram_locked = False
            expert_warm_until = 0
            await verified_unload(EXPERT_MODEL)
            _ = JSONResponse(content={"id": "chatcmpl-Bob", "object": "chat.completion", "created": int(time.time()), "model": "Bob", "choices": [{"index": 0, "message": {"role": "assistant", "content": "🔓 VRAM Unlocked."}, "finish_reason": "stop"}]})
            gpu_lock.release()
            return _

        # --- Routing ---
        target_model = ROUTER_MODEL
        dynamic_keep_alive = 0
        is_cold_expert = False

        if is_background_task:
            target_model = ROUTER_MODEL
            dynamic_keep_alive = 0
            logger.info("[ROUTING] Background Task -> FAST")
        elif "@bob" in user_prompt_lower or "hey bob" in user_prompt_lower:
            target_model = ROUTER_MODEL
            expert_warm_until = 0
            logger.info("[ROUTING] Force FAST")
        elif "@expert" in user_prompt_lower or "hey expert" in user_prompt_lower:
            target_model = EXPERT_MODEL
            dynamic_keep_alive = "3m"
            expert_warm_until = current_time + 180
            if not is_expert_warm: is_cold_expert = True
            logger.info(f"[ROUTING] Force EXPERT ({'cold' if is_cold_expert else 'warm'})")
        elif is_expert_warm or vram_locked:
            target_model = EXPERT_MODEL
            dynamic_keep_alive = "3m" if not vram_locked else "-1"
            if not vram_locked: expert_warm_until = current_time + 180
            logger.info("[ROUTING] Expert warm")
        else:
            # Triage Path
            await sweep_vram_for_expert() # Clear Expert if it was lingering
            analysis = await analyze_request(messages)
            await verified_unload(ROUTER_MODEL) # Clear Router after triage
            
            if analysis.get("complexity", 1) > 6 or analysis.get("requires_tool", False):
                target_model = EXPERT_MODEL
                dynamic_keep_alive = "3m" if analysis.get("followups") else 0
                expert_warm_until = current_time + 180
                is_cold_expert = True
            else:
                target_model = ROUTER_MODEL
            logger.info(f"[ROUTING] Triage -> {target_model}")

        # --- VRAM SAFETY (Cold loads only) ---
        # With the broad fast-exit, no background task can load the Router
        # during an Expert session, so warm paths don't need sweeping.
        if target_model == EXPERT_MODEL and is_cold_expert:
            await sweep_vram_for_expert()

        # --- Payload Config ---
        options = body.get("options", {})
        if is_background_task:
            options.update({"temperature": 0.0, "num_ctx": 2048})
        else:
            # REDUCE default context to 8192 for the 27B model on 24GB GPU.
            # 16-32K context with a 27B model (15-18GB weights) pushes VRAM over 24GB.
            options.update({"temperature": options.get("temperature", 0.7), "num_ctx": 8192})
        
        body.update({"model": target_model, "keep_alive": dynamic_keep_alive, "options": options})
        logger.info(f"[EXECUTE] {target_model} | ctx: {options['num_ctx']} | warm: {not is_cold_expert}")

        path = str(request.url.path)
        is_native = "/api/chat" in path
        target_path = "/api/chat" if is_native else "/v1/chat/completions"

        if not is_streaming:
            try:
                resp = await http_client.post(f"{OLLAMA_URL}{target_path}", json=body, timeout=120.0)
                if resp.status_code != 200:
                    return JSONResponse(status_code=resp.status_code, content={"error": resp.text})
                data = resp.json()
                data["model"] = "Bob"
                if "id" in data: data["id"] = "chatcmpl-Bob"
                return JSONResponse(content=data)
            finally:
                if lock_held: gpu_lock.release(); lock_held = False
        
        lock_held = False
        return StreamingResponse(
            stream_proxy(f"{OLLAMA_URL}{target_path}", body, gpu_lock, is_native_ollama=is_native),
            media_type="application/x-ndjson" if is_native else "text/event-stream"
        )
    except Exception as e:
        logger.error(f"Global Proxy Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        if lock_held: gpu_lock.release()

@app.post("/v1/images/generations")
async def proxy_comfyui(request: Request):
    global expert_warm_until
    await gpu_lock.acquire()
    try:
        expert_warm_until = 0
        await verified_unload(EXPERT_MODEL)
        await verified_unload(ROUTER_MODEL)
        
        body = await request.json()
        prompt_text = body.get("prompt", "")
        with open(WORKFLOW_PATH, 'r') as f:
            workflow = json.load(f)
        for node_id in workflow:
            if workflow[node_id].get("class_type") == "CLIPTextEncode":
                workflow[node_id]["inputs"]["text"] = prompt_text
        
        p_resp = await http_client.post(f"{COMFYUI_URL}/prompt", json={"prompt": workflow})
        if p_resp.status_code != 200: return JSONResponse(status_code=500, content={"error": "ComfyUI Error"})
        p_id = p_resp.json().get("prompt_id")
        
        for _ in range(30):
            h_resp = await http_client.get(f"{COMFYUI_URL}/history/{p_id}")
            if h_resp.status_code == 200:
                hist = h_resp.json()
                if p_id in hist:
                    out = hist[p_id].get("outputs", {})
                    if out:
                        nid = list(out.keys())[0]
                        fn = out[nid].get("images", [{}])[0].get("filename")
                        return JSONResponse(content={"data": [{"url": f"{COMFYUI_URL}/view?filename={fn}"}]})
            await asyncio.sleep(1.0)
        return JSONResponse(status_code=500, content={"error": "Timeout"})
    finally:
        gpu_lock.release()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False, log_level="info")