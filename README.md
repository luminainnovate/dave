# 🧠 br.ai.n: Agentic Local AI Orchestrator

[![CI](https://github.com/mitro54/br.ai.n/actions/workflows/ci.yml/badge.svg)](https://github.com/mitro54/br.ai.n/actions/workflows/ci.yml)

**br.ai.n** is an **Agentic Local AI Orchestrator** powered by **Bob**, a unified AI persona running on your local hardware. Bob manages a tiered orchestration system on a single NVIDIA GPU (24GB+ VRAM), providing instant responses for simple tasks while dynamically routing complex requests (Coding, Vision, Image Generation) to expert models.

## 🚀 Key Features

-   **Tiered Orchestration:** Uses a resident "Fast Orchestrator" (`qwen2.5:1.5b`) for instant intent detection and simple queries.
-   **Expert Reasoning:** Dynamically loads expert models (`qwen3.5:27b`) for complex coding and logic tasks.
-   **Local Image Generation:** Integrated ComfyUI (Flux.1 Schnell / SDXL) via a VRAM-safe proxy.
-   **Live Web Search:** Grounded responses using SearXNG.
-   **Unified UI:** Powered by Open WebUI for text, vision, audio (Whisper), and RAG.
-   **VRAM Guardrails:** Intelligent "Gatekeeper" proxy with GPU Mutex locking to prevent simultaneous heavy model loading.
-   **LAN Accessible:** Bridged networking for access from phones, tablets, and other laptops.

## 🛠️ Requirements

-   **OS:** Windows 11 with WSL2 (Ubuntu 22.04+ recommended).
-   **GPU:** NVIDIA GPU with 24GB+ VRAM (RTX 3090/4090/5090) is recommended for the default tiered setup, but smaller cards can be used with adjusted model selection.
-   **Software:** Docker Engine (inside WSL2) and NVIDIA Container Toolkit.

> [!TIP]
> **Scaling for Smaller Hardware:** While optimized for 24GB VRAM, Bob can run on smaller GPUs by substituting the "Expert" model for a smaller variant (e.g., swapping a 27B model for an 8B model). Orchestrator only takes around 1GB of VRAM.

## 📦 Setup & Installation

The setup is automated. Ensure your NVIDIA drivers are up to date on Windows, then run in WSL2:

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/mitro54/br.ai.n.git
    cd br.ai.n
    ```

2.  **Run the Installer:**
    The `setup_workspace.sh` script installs dependencies, pulls models, and configures the environment.
    ```bash
    chmod +x setup_workspace.sh
    ./setup_workspace.sh
    ```

*Note: VRAM management is handled automatically by the Orchestrator proxy. No manual service configuration is required.*

## 🚥 Usage

To start the workspace:

```bash
chmod +x start_workspace.sh
./start_workspace.sh
```

-   **Web UI:** [http://localhost:3000](http://localhost:3000)
-   **Logs:** `tail -f gatekeeper.log` or `tail -f comfyui.log`

## 🏗️ Architecture

The system operates on an intelligent **GPU Mutex** principle managed by the **Gatekeeper Proxy**:

1.  **Intent Detection:** Every request is analyzed by the Resident Orchestrator (CPU-bound or tiny VRAM footprint).
2.  **VRAM Locking:** If "Expert" or "Image" intent is detected, the Gatekeeper acquires a global lock.
3.  **Model Swapping:** The Expert LLM or ComfyUI is loaded into the primary VRAM slice.
4.  **Auto-Eviction:** Models are unloaded immediately after inference (`OLLAMA_KEEP_ALIVE=0`) to free space for the next task.

For details, see [ARCHITECTURE.md](ARCHITECTURE.md) and [SETUP.md](SETUP.md).

## 🌐 Network Access (Multi-Device)

To access your workspace from other devices on your LAN:

1.  Enable **Mirrored Networking** in your Windows `%USERPROFILE%\.wslconfig`:
    ```ini
    [wsl2]
    networkingMode=mirrored
    firewall=true
    ```
2.  Open port 3000 in your **Windows Firewall** (Admin PowerShell):
    ```powershell
    New-NetFirewallRule -DisplayName "AI Workspace - Open WebUI" -Direction Inbound -LocalPort 3000 -Protocol TCP -Action Allow
    ```

## ⚖️ License

MIT License. See [LICENSE](LICENSE) for details.
