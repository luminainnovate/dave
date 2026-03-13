import asyncio
import httpx
import os
import json
import logging
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("Gatekeeper")

app = FastAPI(title="AI Workspace Gatekeeper Proxy")
gpu_lock = asyncio.Lock()
# Global client for connection pooling
http_client = httpx.AsyncClient(timeout=None)

# Model Configs
ROUTER_MODEL = "qwen2.5:1.5b"
GLM_MODEL = "glm-4.7-flash:latest"  # Tier 2: Utility/Reasoning
EXPERT_MODEL = "qwen3.5:27b"        # Tier 3: Extreme/Coding
OLLAMA_URL = "http://localhost:11434"
COMFYUI_URL = "http://localhost:8188"
WORKFLOW_PATH = "workflow_api.json"

# Common keywords that trigger FAST path instantly without LLM classification
FAST_KEYWORDS = {"hi", "hello", "who are you", "what model", "what version", "identify yourself", "hey", "test"}

async def force_unload(model_name: str):
    """Sends an explicit unload command to Ollama for the given model."""
    logger.info(f"Forcing unload of model: {model_name}")
    try:
        # Loading a model with keep_alive=0 tells Ollama to drop it from VRAM
        await http_client.post(
            f"{OLLAMA_URL}/api/generate", 
            json={"model": model_name, "keep_alive": 0},
            timeout=5.0
        )
    except Exception as e:
        logger.warning(f"Failed to force unload {model_name}: {e}")

import re

async def get_intent(prompt: str) -> str:
    """Uses the fast router model to categorize the intent into: FAST, GLM, EXPERT, or IMAGE."""
    clean_p = prompt.lower().strip().strip("?!.")
    
    # Instant bypass for common phrases
    if clean_p in FAST_KEYWORDS or len(clean_p) < 10:
        logger.info(f"Keyword/Length bypass triggered for: {clean_p}")
        return "FAST"

    # Classification logic
    system_msg = (
        "TASK: Classify the user prompt into exactly ONE of these categories: "
        "FAST, GLM, EXPERT, or IMAGE.\n"
        "- FAST: Greetings, simple conversation, status checks.\n"
        "- GLM: Medium reasoning, creative writing, summaries.\n"
        "- EXPERT: Complex coding, math, logical puzzles, high-precision analysis.\n"
        "- IMAGE: Requests to generate or create a visual/image.\n"
        "RULES: Output ONLY the category name. NO EXPLANATION. NO JSON."
    )
    payload = {
        "model": ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_msg}, 
            {"role": "user", "content": prompt}
        ],
        "stream": False,
        "keep_alive": -1 # Keep router pinned
    }
    try:
        resp = await http_client.post(f"{OLLAMA_URL}/v1/chat/completions", json=payload, timeout=10.0)
        resp.raise_for_status()
        raw_content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").upper()
        
        # Use regex to find the first valid category in the response
        match = re.search(r"(FAST|GLM|EXPERT|IMAGE)", raw_content)
        if match:
            intent = match.group(1)
            logger.info(f"Intent classified as: {intent} (Extracted from: {raw_content.replace('\n', ' ')})")
            return intent
        
        logger.warning(f"No valid intent found in response: {raw_content}. Defaulting to FAST.")
        return "FAST"
    except Exception as e:
        logger.warning(f"Routing failed: {e}. Defaulting to FAST.")
        return "FAST"

async def stream_proxy(url: str, body: dict, req_lock: asyncio.Lock = None):
    """Proxies the request, conditionally locking the GPU and enforcing VRAM cleanup."""
    target_model = body.get("model")
    if req_lock:
        try:
            # Acquisition timeout to prevent deadlock
            await asyncio.wait_for(req_lock.acquire(), timeout=30.0)
            logger.info(f"GPU Lock Acquired for {target_model}.")
            
            # Heavy models MUST have keep_alive: 0
            body["keep_alive"] = 0
            
            async with http_client.stream("POST", url, json=body) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk
        except asyncio.TimeoutError:
            logger.error("GPU Lock Acquisition Timed Out.")
            yield b"data: " + json.dumps({"error": "Heavy resource busy. Please retry."}).encode() + b"\n\n"
        finally:
            if req_lock.locked():
                # Release lock BEFORE calling unload to allow next request to start loading if ready
                req_lock.release()
                logger.info("GPU Lock Released.")
            # Explicit cleanup call
            await force_unload(target_model)
    else:
        logger.info(f"Fast Path execution ({target_model}).")
        async with http_client.stream("POST", url, json=body) as resp:
            async for chunk in resp.aiter_bytes():
                yield chunk

@app.get("/v1/models")
async def list_models():
    """Proxies model list, filtering for valid tiers."""
    try:
        resp = await http_client.get(f"{OLLAMA_URL}/v1/models")
        return JSONResponse(content=resp.json())
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/v1/chat/completions")
async def proxy_ollama(request: Request):
    try:
        body = await request.json()
        messages = body.get("messages", [])
        user_prompt = messages[-1].get("content", "") if messages else ""
        
        has_images = any("image_url" in str(msg) for msg in messages)
        intent = await get_intent(user_prompt)
        
        # Tiered Decision
        is_expert = has_images or "EXPERT" in intent or len(user_prompt) > 4000
        is_glm = "GLM" in intent and not is_expert
        
        if is_expert:
            target_model = EXPERT_MODEL
            use_lock = True
        elif is_glm:
            target_model = GLM_MODEL
            use_lock = True
        else:
            target_model = ROUTER_MODEL
            use_lock = False
            
        # If we are in FAST path, we don't need a lock and can stream immediately
        # This reduces overhead for Tier 1.
            
        body["model"] = target_model
        logger.info(f"Routing to: {target_model}")
        
        return StreamingResponse(
            stream_proxy(f"{OLLAMA_URL}/v1/chat/completions", body, req_lock=gpu_lock if use_lock else None),
            media_type="text/event-stream"
        )
    except Exception as e:
        logger.error(f"Chat Proxy Error: {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/v1/images/generations")
async def proxy_comfyui(request: Request):
    try:
        await asyncio.wait_for(gpu_lock.acquire(), timeout=30.0)
        logger.info("ComfyUI Lock Acquired.")
        try:
            body = await request.json()
            prompt_text = body.get("prompt", "")
            with open(WORKFLOW_PATH, 'r') as f: workflow = json.load(f)
            # Find and update text prompt
            for node_id in workflow:
                if workflow[node_id].get("class_type") == "CLIPTextEncode":
                    workflow[node_id]["inputs"]["text"] = prompt_text
            p_resp = await http_client.post(f"{COMFYUI_URL}/prompt", json={"prompt": workflow})
            p_id = p_resp.json().get("prompt_id")
            for _ in range(60):
                h_resp = await http_client.get(f"{COMFYUI_URL}/history/{p_id}")
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
                await asyncio.sleep(1)
            return JSONResponse(status_code=500, content={"error": "Generation failed."})
        finally:
            if gpu_lock.locked(): gpu_lock.release()
    except asyncio.TimeoutError:
        return JSONResponse(status_code=503, content={"error": "Resources busy."})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("gatekeeper.proxy:app" if __name__ != "__main__" else app, host="0.0.0.0", port=8000, reload=False, log_level="info", access_log=False)
