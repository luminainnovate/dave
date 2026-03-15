# 🧠 Project Architecture: Agentic Local AI Orchestrator (Bob)

## 1. System Context & Goals
**Goal:** Build a fully localized, unified, and agent-driven AI workspace ("Bob") running on a single NVIDIA GPU (24GB+ VRAM recommended) via Windows Subsystem for Linux (WSL2).
**Scalability:** While optimized for 24GB VRAM (allowing for large expert models like Qwen 27B), the system can be scaled down to 8GB-12GB VRAM by selecting smaller expert models (e.g., Llama-3-8B).
**Capabilities:** Text generation, complex coding, vision (image analysis), voice transcription, document RAG, live web search, and image generation.
**Accessibility:** The workspace must be accessible from the local area network (LAN), allowing phones, tablets, and other laptops to utilize the host's GPU resources via a unified web interface.
**Key Constraint:** The system must use a **Tiered Orchestration** approach to provide instant responses for simple tasks while dynamically routing heavy tasks to expert models, managing VRAM aggressively to prevent OOM on a 24GB budget.

---

## 2. Hardware Guardrails & VRAM Budget
The system operates on a strict hardware limit. All agentic routing and tool execution must respect the following constraints:

* **GPU:** 1x NVIDIA RTX 4090 (24GB VRAM). 
* **OS:** Windows 11 Host running WSL2 (Ubuntu).
* **VRAM Baseline Allocation:**
  * Windows OS / Display: ~2.8GB 
  * Open WebUI (Embedding Model - `nomic-embed-text`): ~1.5GB
  * **Optimization:** To save 1.5GB of GPU VRAM, the embedding model should be configured to run on **CPU** (`ollama run nomic-embed-text` usually defaults to GPU, but can be forced to CPU in the service config or by using a smaller model).
  * **Total Usable VRAM:** ~19.7GB - 21.2GB (depending on Embedding location)
* **Dynamic VRAM Allocations:**
  * **Resident Orchestrator (`qwen2.5:1.5b`):** ~1.2GB (Pinned to VRAM for instant routing/chat).
  * **Expert LLM (`qwen3.5:27b` quant):** ~15GB (Loaded on-demand).
  * **Image Model (Flux 2):** ~12GB (Loaded on-demand).
* **Rule:** The Resident Orchestrator stays in memory. The Expert LLM and Visual/Image models **cannot** exist in VRAM simultaneously. 

---

## 3. Architecture Decision Records (ADRs)

### ADR 001: Time-Slicing VRAM over Static Loading
* **Context:** We lack the VRAM to keep both the primary reasoning LLM and the image generation model in memory.
* **Decision:** We will aggressively time-slice VRAM. Ollama will be configured with `OLLAMA_KEEP_ALIVE=0` (or `1m`), forcing immediate model unloading after inference. ComfyUI will run with `--lowvram` to offload weights immediately to system RAM after generating an image.

### ADR 002: GPU Mutex via Intelligent Proxy (The "Gatekeeper")
* **Context:** Independent services (Ollama, ComfyUI) cannot coordinate VRAM usage natively, and reloading large models causes lag.
* **Decision:** We will implement a **Python FastAPI Proxy** (The Gatekeeper) using a tiny resident LLM as a router.
*   **Context:** Independent services (Ollama, ComfyUI) cannot coordinate VRAM usage natively, and reloading large models causes lag.
*   **Decision:** We will implement a **Python FastAPI Proxy** (The Gatekeeper) using a tiny resident LLM as a router.
    *   All API calls pass through this proxy.
    *   The proxy uses the **Resident Orchestrator** to categorize requests (Fast Path vs. Expert Path).
    *   The proxy maintains a `threading.Lock` for the 4090's "Expert Zone" (the remaining ~20GB of VRAM).
    *   Fast Path requests (greetings, simple knowledge) are answered by the Orchestrator immediately.
    *   Expert Path requests (Coding, Vision, RAG) or Image Generations trigger the Mutex lock and model swap.

### ADR 003: Hybrid Native/Docker Orchestration
*   **Context:** Running Ollama in Docker within WSL2 adds overhead and complications for GPU passthrough.
*   **Decision:** Ollama will run natively in WSL2 for maximum performance. Open WebUI and SearXNG will run on a custom Docker bridge network (`ai-workspace-net`). Open WebUI will communicate with the host-bound Gatekeeper and native Ollama via `host.docker.internal`.

### ADR 004: ComfyUI-to-OpenAI Bridge (Prompt-to-Graph)
*   **Context:** Open WebUI expects DALL-E (OpenAI) schema, while ComfyUI requires a workflow graph JSON.
*   **Decision:** The Gatekeeper Proxy will perform **Prompt-to-Graph injection**. 
    *   A template `workflow_api.json` (exported from ComfyUI in API mode) will be stored on disk.
    *   The proxy will read this JSON, inject the user's prompt into the correct node (usually `CLIPTextEncode`), post it to ComfyUI's `/prompt` endpoint, and poll the `/history` endpoint for the resulting image URL.

---

## 4. Core Component Definitions

### A. The LLM Engine: Ollama
* **Role:** Hosts text, vision, and audio models.
* **Resident Orchestrator:** `qwen2.5:1.5b` (Always-on for intent detection).
* **Expert Model:** `qwen3.5:27b` (Dense logic, coding).
* **Deployment:** Native WSL2 binary (for optimal GPU/VRAM performance).
* **Configuration:** 
    *   VRAM managed dynamically by the Gatekeeper proxy.

### B. The Orchestrator & Frontend: Open WebUI
* **Role:** Unified UI, RAG document processing, voice transcription (Whisper), embedding generation, and agentic tool routing.
* **Deployment:** Docker container on `ai-workspace-net`.
* **Agentic Routing:** Utilizes Open WebUI's "Tools" feature to detect intent and route requests to SearXNG or ComfyUI.

### C. Live Web Search & RAG: SearXNG + Open WebUI
* **Role:** 
  * **SearXNG:** Live web search for real-time grounding.
  * **Open WebUI:** Handles document RAG, PDF parsing, and image/video preprocessing before sending context to the Gatekeeper. 
* **Deployment:** Docker containers on `ai-workspace-net`.

### D. Image Generation Engine: ComfyUI
* **Role:** Node-based backend for text-to-image workflows.
* **Deployment:** Python `venv` natively in WSL2.
* **Configuration:** 
  * Must be launched with `--lowvram`.
  * Integration: Accessed via the **Gatekeeper Proxy** to ensure VRAM safety.

---

## 5. Data Flow & VRAM Orchestration Logic
This flow illustrates the VRAM safety logic when the LLM triggers a heavy tool (Image Generation).

1. **User Request:** User asks a question or requests an image.
2. **Intent Detection:** The **Gatekeeper** queries the **Resident Orchestrator** (~1.2GB VRAM). This is instant.
3. **Routing Decision:**
   * **Fast Path:** If it's a simple query, the Orchestrator answers. VRAM usage remains flat.
   * **Expert Path:** If it needs the 27B model, the Gatekeeper acquires the `gpu_lock`.
   * **Media Path:** If it's an image, the Gatekeeper acquires the `gpu_lock`.
4. **Model Loading:** The Expert model or ComfyUI loads into the remaining ~20GB VRAM.
5. **Auto-Unload:** Once finished, the expert model drops (`KEEP_ALIVE=0`). 
6. **Release:** Gatekeeper releases the lock. The system returns to the Resident Orchestrator baseline.

---

## 6. Network & Port Mapping Matrix

| Component | Internal Network DNS | Host Exposed Port | Protocol |
| :--- | :--- | :--- | :--- |
| **Open WebUI** | `open-webui:8080` | `3000` | HTTP |
| **Ollama** | `ollama:11434` | `None` (Internal only) | REST/HTTP |
| **SearXNG** | `searxng:8080` | `None` (Internal only) | JSON/HTTP |
| **ComfyUI** | `127.0.0.1:8188` (Host routed) | `8188` | REST/WS |

*Note: ComfyUI is run outside the core Docker network for easier access to local model `.safetensors` directories, and is accessed via the host IP.*
