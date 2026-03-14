import asyncio
import httpx
import json
import logging
import re
import time
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("Bob-Orchestrator")

app = FastAPI(title="Bob: AI Workspace Orchestrator")
gpu_lock = asyncio.Lock()
http_client = httpx.AsyncClient(timeout=None)

# --- STRICT MODEL CONFIG ---
ROUTER_MODEL = "qwen2.5:1.5b"
EXPERT_MODEL = "qwen3.5:27b"
OLLAMA_URL = "http://localhost:11434"
COMFYUI_URL = "http://localhost:8188"
WORKFLOW_PATH = "workflow_api.json"

# --- STATE MANAGEMENT ---
vram_locked = False

async def force_unload(model_name: str):
    """Frees VRAM manually (Only used when switching to ComfyUI now)"""
    try:
        logger.info(f"[VRAM] Sending eviction notice for {model_name}...")
        asyncio.create_task(http_client.post(
            f"{OLLAMA_URL}/api/generate", 
            json={"model": model_name, "keep_alive": 0},
            timeout=2.0
        ))
    except Exception as e:
        logger.warning(f"[VRAM ERROR] Failed to signal unload for {model_name}: {e}")

async def analyze_request(prompt: str) -> dict:
    """Smart Triage. Evaluates complexity, follow-ups, and tool necessity."""
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
            {"role": "user", "content": prompt[:500]} 
        ],
        "stream": False,
        "keep_alive": "10m", 
        "options": {
            "temperature": 0.0, 
            "num_ctx": 2048,
            "num_gpu": 0 
        } 
    }
    try:
        resp = await http_client.post(f"{OLLAMA_URL}/v1/chat/completions", json=payload, timeout=5.0)
        if resp.status_code == 200:
            content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "{}")
            data = json.loads(content)
            return {
                "complexity": int(data.get("complexity", 1)),
                "followups": bool(data.get("expect_followups", False)),
                "requires_tool": bool(data.get("requires_tool", False))
            }
    except Exception as e:
        logger.warning(f"Router network/parsing error: {e}")
    
    # Failsafe default
    return {"complexity": 1, "followups": False, "requires_tool": False}

async def stream_proxy(url: str, body: dict, lock_to_release: asyncio.Lock, is_native_ollama: bool = False):
    """Streams data and cleanly releases the lock."""
    target_model = body.get("model")
    
    async def _stream_with_rewriting(resp):
        try:
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
                        except Exception:
                            rewritten = re.sub(r'("model"\s*:\s*")[^"]+(")', r'\1Bob\2', line)
                            yield f"{rewritten}\n\n".encode('utf-8')
        except Exception as e:
            logger.error(f"[STREAM CONTENT ERROR] {e}")
            msg = {"error": str(e)}
            yield (json.dumps(msg) + "\n").encode() if is_native_ollama else f"data: {json.dumps(msg)}\n\n".encode()
        finally:
            logger.info(f"[STREAM] Finished stream for {target_model}")
            if lock_to_release.locked(): 
                lock_to_release.release()

    try:
        async with http_client.stream("POST", url, json=body, timeout=120.0) as resp:
            if resp.status_code != 200:
                error_body = await resp.aread()
                yield b"ERROR: " + error_body
                if lock_to_release.locked():
                    lock_to_release.release()
                return
            async for line_bytes in _stream_with_rewriting(resp):
                yield line_bytes
    except Exception:
        if lock_to_release.locked():
            lock_to_release.release()

@app.get("/api/tags")
@app.get("/v1/models")
async def list_models(request: Request):
    """Forces Open-WebUI to only see Bob."""
    try:
        is_ollama = "tags" in str(request.url)
        bob_model = {
            "name": "Bob",
            "model": "Bob",
            "modified_at": "2024-01-01T00:00:00.000000Z",
            "size": 0,
            "digest": "bob-identity",
            "details": {"family": "llama", "parameter_size": "Expert", "quantization_level": "Q8_0"}
        } if is_ollama else {
            "id": "Bob", "object": "model", "created": int(time.time()), "owned_by": "System"
        }
        return JSONResponse(content={"models": [bob_model]} if is_ollama else {"object": "list", "data": [bob_model]})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/chat")
@app.post("/v1/chat/completions")
async def proxy_ollama(request: Request):
    global vram_locked
    body = await request.json()
    is_streaming = body.get("stream", True)
    messages = body.get("messages", [])
    messages_str = str(messages).lower()
    
    # 1. IDENTIFY BACKGROUND TASKS (Titles, Tags, Summaries)
    is_background_task = False
    if not is_streaming:
        if any(kw in messages_str for kw in ["title", "summarize", "short label", "generate a title", "tags"]):
            is_background_task = True

    try:
        await asyncio.wait_for(gpu_lock.acquire(), timeout=120.0)
    except asyncio.TimeoutError:
        return JSONResponse(status_code=503, content={"error": "System busy."})

    try:
        user_prompt = messages[-1].get("content", "") if messages else ""
        user_prompt_lower = user_prompt.lower()

        # --- MANUAL VRAM OVERRIDES ---
        if "!lock" in user_prompt_lower:
            vram_locked = True
            gpu_lock.release()
            return JSONResponse(content={"id": "chatcmpl-Bob", "object": "chat.completion", "created": int(time.time()), "model": "Bob", "choices": [{"index": 0, "message": {"role": "assistant", "content": "🔒 **VRAM Locked.** Expert is held in memory."}, "finish_reason": "stop"}]})
        elif "!unlock" in user_prompt_lower:
            vram_locked = False
            await force_unload(EXPERT_MODEL)
            gpu_lock.release()
            return JSONResponse(content={"id": "chatcmpl-Bob", "object": "chat.completion", "created": int(time.time()), "model": "Bob", "choices": [{"index": 0, "message": {"role": "assistant", "content": "🔓 **VRAM Unlocked.** Memory cleared."}, "finish_reason": "stop"}]})

        # --- SMART ROUTING & TRIAGE ---
        target_model = ROUTER_MODEL
        dynamic_keep_alive = 0 
        
        if is_background_task:
            target_model = ROUTER_MODEL
            dynamic_keep_alive = 0
            logger.info("[ROUTING] Background Task (Title/Tags) -> Forcing FAST model")
            
        elif "@expert" in user_prompt_lower or "hey expert" in user_prompt_lower:
            target_model = EXPERT_MODEL
            dynamic_keep_alive = "3m"
            logger.info("[ROUTING] Explicit keyword -> Forcing EXPERT")
            
        elif "@bob" in user_prompt_lower or "hey bob" in user_prompt_lower:
            target_model = ROUTER_MODEL
            dynamic_keep_alive = 0
            logger.info("[ROUTING] Explicit keyword -> Forcing FAST")
            
        else:
            # Let the Triage AI Decide
            analysis = await analyze_request(user_prompt)
            
            # Using .get() ensures it never crashes, even if the dictionary is malformed
            complexity = analysis.get("complexity", 1)
            requires_tool = analysis.get("requires_tool", False)
            followups = analysis.get("followups", False)
            
            logger.info(f"[TRIAGE] Complexity: {complexity}/10 | Needs Tool: {requires_tool} | Follow-ups: {followups}")
            
            if complexity > 6 or requires_tool:
                target_model = EXPERT_MODEL
                dynamic_keep_alive = "3m" if followups else 0
            else:
                target_model = ROUTER_MODEL

        # Apply global lock if active
        if vram_locked:
            dynamic_keep_alive = "-1"

        # 3. Final Payload Prep
        body["options"] = {
            "num_ctx": 24576,
            "temperature": body.get("options", {}).get("temperature", 0.7) if is_background_task else 0.7
        }
        body["keep_alive"] = dynamic_keep_alive
        body["model"] = target_model
        
        logger.info(f"[EXECUTE] Target: {target_model} | Keep-Alive: {dynamic_keep_alive}")

        path = str(request.url.path)
        is_native = "/api/chat" in path
        target_path = "/api/chat" if is_native else "/v1/chat/completions"
        
        # Handle Synchronous Requests
        if not is_streaming:
            try:
                resp = await http_client.post(f"{OLLAMA_URL}{target_path}", json=body, timeout=120.0)
                if resp.status_code != 200:
                    return JSONResponse(status_code=resp.status_code, content={"error": resp.text})
                
                data = resp.json()
                data["model"] = "Bob"
                if "id" in data:
                    data["id"] = "chatcmpl-Bob"
                return JSONResponse(content=data)
            except Exception as sync_e:
                logger.error(f"[SYNC POST ERROR] {sync_e}")
                return JSONResponse(status_code=500, content={"error": str(sync_e)})
            finally:
                if gpu_lock.locked():
                    gpu_lock.release()

        return StreamingResponse(
            stream_proxy(f"{OLLAMA_URL}{target_path}", body, gpu_lock, is_native_ollama=is_native),
            media_type="application/x-ndjson" if is_native else "text/event-stream"
        )

    except Exception as e:
        logger.error(f"Proxy Error: {e}")
        if gpu_lock.locked():
            gpu_lock.release()
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/v1/images/generations")
async def proxy_comfyui(request: Request):
    await gpu_lock.acquire()
    try:
        # Extra safety check to clear VRAM for ComfyUI
        await force_unload(EXPERT_MODEL)
        await asyncio.sleep(1.0) 
        
        body = await request.json()
        prompt_text = body.get("prompt", "")
        with open(WORKFLOW_PATH, 'r') as f:
            workflow = json.load(f)
        for node_id in workflow:
            if workflow[node_id].get("class_type") == "CLIPTextEncode":
                workflow[node_id]["inputs"]["text"] = prompt_text
        p_resp = await http_client.post(f"{COMFYUI_URL}/prompt", json={"prompt": workflow})
        if p_resp.status_code != 200:
            return JSONResponse(status_code=500, content={"error": f"ComfyUI Error: {p_resp.status_code}"})
        
        try:
            p_id = p_resp.json().get("prompt_id")
        except Exception:
            return JSONResponse(status_code=500, content={"error": "Invalid JSON"})
        
        for _ in range(30):
            h_resp = await http_client.get(f"{COMFYUI_URL}/history/{p_id}")
            if h_resp.status_code == 200:
                try:
                    hist = h_resp.json()
                    if p_id in hist:
                        out = hist[p_id].get("outputs", {})
                        if out:
                            nid = list(out.keys())[0]
                            imgs = out[nid].get("images", [])
                            if imgs:
                                fn = imgs[0].get("filename")
                                return JSONResponse(content={"created": 1, "data": [{"url": f"{COMFYUI_URL}/view?filename={fn}"}]})
                        break
                except Exception:
                    pass
            await asyncio.sleep(0.5)
            
        return JSONResponse(status_code=500, content={"error": "Generation failed."})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        if gpu_lock.locked():
            gpu_lock.release()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False, log_level="info")