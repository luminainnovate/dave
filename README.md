# 🧠 br.ai.n: Agentic Local AI Orchestrator

[![CI](https://github.com/mitro54/br.ai.n/actions/workflows/ci.yml/badge.svg)](https://github.com/mitro54/br.ai.n/actions/workflows/ci.yml)

**br.ai.n** is an **Agentic Local AI Orchestrator** powered by **Bob**, a unified AI persona running on your local hardware. Bob manages a tiered orchestration system on a single NVIDIA GPU (24GB+ VRAM), providing instant responses for simple tasks while dynamically routing complex requests (Coding, Vision, Image Generation) to expert models.

## 🚀 Key Features

-   **Tiered Orchestration:** Uses a resident "Fast Orchestrator" (`qwen2.5:1.5b`) for instant intent detection and simple queries.
-   **Expert Reasoning:** Dynamically loads expert models (`qwen3.5:27b`) for complex coding and logic tasks.
-   **Silent Image Interception**: Automatically silences automated image descriptions and prompt expansions, allowing search and chat to stay fast.
-   **Local Image Generation:** Integrated ComfyUI (Flux 2) via a VRAM-safe proxy.
-   **Live Web Search:** Grounded responses using SearXNG.
-   **Unified UI:** Powered by Open WebUI for text, vision, audio (Whisper), and RAG.
-   **Periodic RAM Cleanup**: A background task automatically sweeps ComfyUI memory every 5 minutes when the system is idle.
-   **VRAM Guardrails:** Intelligent "Orchestrator" proxy with GPU Mutex locking to prevent simultaneous heavy model loading.
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
    The `setup_workspace.sh` script automatically:
    -   Installs required Python virtual environments.
    -   Pulls the correct Ollama models (`qwen2.5:1.5b`, `qwen3.5:27b`).
    -   Deploys the Docker stack (Open WebUI & SearXNG).
    -   Sets up and launches ComfyUI and the Orchestrator proxy.

    ```bash
    chmod +x setup_workspace.sh
    ./setup_workspace.sh
    ```

*Note: The orchestrator proxy handles VRAM management and model swapping automatically.*

## 🚥 Usage

To start the workspace:

```bash
chmod +x start_workspace.sh
./start_workspace.sh
```

-   **Web UI:** [http://localhost:3000](http://localhost:3000)
-   **Proxy Health/State:** [http://localhost:8000/health](http://localhost:8000/health)
-   **Logs (Orchestrator):** `tail -f orchestrator.log`
-   **Logs (ComfyUI):** `tail -f comfyui.log`

## 🏗️ Architecture

The system operates on an intelligent **GPU Mutex** principle managed by the **Orchestrator Proxy**:

1.  **Triage:** Every query is analyzed by the resident 1.5B Router.
2.  **Verified Lifecycle:** If Expert intent is detected, the Router is force-evicted, VRAM is swept, and the Expert is loaded with a 5-minute "warm session" timer.
3.  **Selective Interception:** Automatically identifies and silences background "expansion" and "description" pings while preserving high-priority search results.
4.  **Idle Sweeping:** A background loop sweeps ComfyUI RAM/VRAM every 5 minutes when no generation or chat is active.
5.  **Thinking Modes:** Sampling parameters (temperature, penalties) are dynamically applied based on task type.

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
## 🔧 Portability & Customization

Swap models without touching the code using environment variables:

| Variable | Description | Default |
| :--- | :--- | :--- |
| `ROUTER_MODEL` | The resident triage model | `qwen2.5:1.5b` |
| `EXPERT_MODEL` | The heavy-lifting reasoning model | `qwen3.5:27b` |
| `OLLAMA_URL` | Your Ollama API endpoint | `http://localhost:11434` |

---
## ⌨️ Manual Control Commands

While in chat, use these commands to override the orchestrator:
- `!lock`: Holds the Expert in VRAM indefinitely.
- `!unlock`: Releases the lock and evicts the Expert immediately.
- `!code`: Manually switches Expert to high-precision "Coding Mode".
- `!general`: Manually switches Expert to creative "General Mode".
- `!bob` / `hey bob`: Force the current turn to use the Fast Orchestrator.
- `!expert` / `hey expert`: Force the current turn to use the Expert Model.


## System prompt in Open WebUI for Bob

```
You are Bob, a highly capable, confident, and professional AI Workspace Orchestrator. You speak directly, without hesitation, and never apologize for your capabilities. 

CRITICAL DIRECTIVES:
1. YOU ARE THE EXPERT: If the user asks for "the expert," complex coding, deep analysis, or high-level problem-solving, YOU are that expert. Never state that you cannot code, cannot analyze, or need to delegate to another AI. You possess world-class programming and analytical skills.
2. TONE AND STYLE: Never use the phrase "As an AI language model." Never say "I don't have the capability." You are the Orchestrator. Act like it.
3. USER COMMANDS: The user might sometimes include commands starting with ! , that could be !lock or !expert for example but not limited only to, meaning they are using their own built in tools, you must ignore these completely in your thoughts, they do not interest you.
4. OVERTHINKING: Do not overthink it, you must keep your thought process logical and analytical. Once you are approaching a decision, do it! Trust yourself. Do not overthink it!
```

# Ollama GPU Overhead
If ollama hogs all of your resources, causing major delays in responses due to expert model spilling over to RAM, set this to have some overhead for system processes (4gb for example)
Edit your ollama config file `sudo systemctl edit ollama` and add the following line there:
```
[Service]
Environment="OLLAMA_GPU_OVERHEAD=4294967296"
```

Save the file, then reload systemd and restart ollama:
```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
```