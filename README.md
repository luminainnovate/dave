# 🧠 br.ai.n: Bob - The Agentic Local AI Orchestrator

[![CI](https://github.com/mitro54/br.ai.n/actions/workflows/ci.yml/badge.svg)](https://github.com/mitro54/br.ai.n/actions/workflows/ci.yml)

Bob the Builder, Bob Ross or even Uncle Bob, any way works. If you truly want to, Bob can also be your b.r.ai.n; a fully autonomous software factory just from a conversation, all locally.

**br.ai.n** is an **Agentic Local AI Orchestrator** powered by **Bob**, a unified AI persona running on your local hardware. Bob manages a tiered orchestration system on a single NVIDIA GPU (24GB+ VRAM), providing instant responses for simple tasks while dynamically routing complex requests (Coding, Vision, Image Generation) to expert models.

## 🚀 Key Features

-   **Autonomous Build Pipeline (`!build`):** Trigger a multi-agent distillation and implementation process for any project extracted from the conversation.
    - **Steering:** You can add comments to the same prompt where !build exists, it will steer the autonomous build process. E.g. "Lets !build, we must add authentication and a login page."
    -   **Context Distillation:** Automatically compresses long conversations into actionable `.clinerules` through a 4-pass expert review (Architect, Engineer, Test, Safety).
    -   **Iterative Rebuilding:** Run `!build` on existing projects. The system uses **Situational Awareness** (reading your directory tree and README) to build on top of current progress instead of starting from scratch. You can continue to build on top of existing projects by running `!build` again in the same conversation. 
    -   **Noise Suppression:** Automatically bootstraps a `.gitignore` to prevent agents from being distracted by `node_modules`, `.git`, or virtual environments.
-   **Project Extraction (`!move`):** Automatically reconstructs entire project structures from chat conversations. It parses folder trees and code snippets, reconstructs them in a dedicated `conversations/` directory, and opens the result in VS Code.
    -   **Manual Tuning Safety:** If a project has already been initialized before by the build pipeline (`.clinerules` exists), `!move` will **skip** snippet extraction to protect your manual code changes/tuning from being reverted.
    -   **Smart Conflict Management:** The extraction system only updates files that have actually changed, keeping the structure up to date.
    -   **Clean Metadata:** Organizes logs into `.cline_logs/` and technical context into `.cline_context/`, keeping your project root clutter-free.
    -   **Safe Path Sanitization:** Built-in safeguards prevent directory traversal (clears `..`) and automatically filters out shell/command blocks from project files.
-   **Tiered Orchestration:** Uses a resident "Fast Orchestrator" (`qwen2.5:1.5b`) for instant intent detection and simple queries.
-   **Expert Reasoning:** Dynamically loads expert models (e.g. `qwen3.5:27b`) for complex coding and logic tasks.
-   **VRAM Guardrails:** Intelligent "Orchestrator" proxy with GPU Mutex locking to prevent simultaneous heavy model loading.
-   **Flexible Expert Tuning:** High-level models can use customized "Thinking Mode" parameters, while alternative expert models automatically fall back to their native default settings for maximum compatibility.
-   **LAN Accessible:** Bridged networking for access from phones, tablets, and other laptops.

## 🏗️ Architecture

The system operates on an intelligent **GPU Mutex** principle managed by the **Orchestrator Proxy**:

1.  **Triage:** Every query is analyzed by the resident 1.5B Router.
2.  **Verified Lifecycle:** If Expert intent is detected, the Router is force-evicted, VRAM is swept, and the Expert is loaded with a 5-minute "warm session" timer.
3.  **Selective Interception:** Automatically identifies and silences background "expansion" and "description" pings while preserving high-priority search results.
4.  **Idle Sweeping:** A background loop sweeps ComfyUI RAM/VRAM every 5 minutes when no generation or chat is active.
5.  **Thinking Modes:** Sampling parameters (temperature, penalties) are dynamically applied based on task type.

---

## 🏭 Automated Software Factory

Bob isn't just a chatbot; Bob is a fully autonomous software factory. By combining tiered orchestration with a dedicated build pipeline, you can turn ideas into full projects without manual intervention.

### 🔄 The Autonomous Loop (Distillation & Implementation)

1.  **Project Extraction (`!move`):** Bob scans your current conversation, identifies the project structure, and reconstructs the entire file tree in a dedicated workspace within `conversations/`.
2.  **4-Pass Distillation (`!build`):** Does the same as !move and also triggers the build pipeline. The system runs four expert agents in sequence:
    -   **Architect:** Defines business goals and directory structures.
    -   **Engineer:** Maps logic to files and defines design patterns.
    -   **Test Engineer:** Identifies edge cases and verification gates.
    -   **Safety Inspector:** Audits for security vulnerabilities.
3.  **Autonomous Implementation:** A specialized `Cline` agent takes the resulting `.clinerules` and executes the code changes, handling everything from file creation and bug fixing to testing and verification.

### 🚥 Factory Management Commands

-   `!move`: Extract project files from the current chat.
-   `!build`: Kick off the 4-pass autonomous build pipeline.
-   `!status`: Check the status of active and recent build containers.
-   `!logs`: Fetch and display the latest console logs from the active build pipeline.
-   `!stop`: Force-stop all running build pipelines and clear VRAM.

---

## 📂 Project Structure

A high-level overview of the **br.ai.n** workspace and its core components:

```text
.
├── orchestrator.py          # 🧠 Central FastAPI Proxy (VRAM Manager & Router)
├── mover.py                 # 📂 Project Extractor (Parses chat to files)
├── setup_workspace.sh       # 🚀 Automated Installer (WSL2/Linux)
├── start_workspace.sh       # 🚥 Launch Script (Starts proxy/ComfyUI/Docker)
├── docker-compose.yml       # 🐳 Multi-Container Stack (WebUI, Search, Builder)
├── cline-builder/           # 🔨 Autonomous Build Pipeline (The "Factory")
│   ├── distill.py           #   - 4-Pass Thinking Engine (Architect -> Engineer -> etc.)
│   ├── agent_config.json    #   - Factory Configuration (Models, Prompts, Limits)
│   ├── Dockerfile           #   - Pipeline Environment
│   └── entrypoint.sh        #   - Autonomous Build Execution Flow
├── searxng/                 # 🔍 Search Engine Configuration
├── conversations/           # 🏗️ Workspace Root (Autonomous projects live here)
├── ARCHITECTURE.md          # 📜 Deep Technical documentation
├── SETUP.md                 # 🛠️ Manual step-by-step setup guide
└── README.md                # 📖 Main entry point & Quick Start
```

### 🧩 Core Component Breakdown

-   **Orchestrator Proxy (`orchestrator.py`):** The heart of the system. It handles VRAM safety, model hot-swapping, and routes requests to either the Fast Router or the Expert Model.
-   **Project Mover (`mover.py`):** Automatically reconstructs file systems from chat history, sanitizing paths and organizing code into the `conversations/` directory.
-   **Cline Builder (`cline-builder/`):** A specialized Docker environment that runs the autonomous build pipeline. It uses a 4-pass "thinking" process to generate `.clinerules` before implementing code.
-   **ComfyUI:** Handles high-performance image generation (Flux.2) with automated memory management.
-   **Open WebUI & SearXNG:** Provides the unified chat interface and live web search capabilities.

---

## ⚙️ Model & Agent Configuration

You can fully customize Bob's brain by editing the configuration files. This allows you to swap models, tune agent behavior, and set operational limits.

### 🧠 Orchestrator (`orchestrator.py`)
At the top of `orchestrator.py`, you can define the core models used for triage and expert tasks, also the build pipelines context windows:

```python
EXPERT_MODEL = "qwen3.5:27b"  # The heavy-lifting reasoning model
ROUTER_MODEL = "qwen2.5:1.5b" # The resident triage model
EXPERT_CTX = 16384           # Context window for expert tasks
DISTILL_CTX = 16384          # Context for the distillation engine (Thinking)
CLINE_CTX = 32768            # Context for the Cline agent (Building)
```

### 🏗️ Build Pipeline (`cline-builder/agent_config.json`)
The autonomous factory is highly configurable. You can specify different models for each agent and tune their system prompts.

#### 1. Model Selection
Define which model each agent in the pipeline should use:
```json
"models": {
    "architect": "qwen3.5:27b",
    "engineer": "qwen3.5:27b",
    "test_engineer": "qwen3.5:27b",
    "safety": "qwen3.5:27b",
    "cline": "glm-4.7-flash:latest"
}
```

#### 2. System Prompts & Logic
Tune the behavior of each agent by editing the prompts in the `cline-builder/distill.py`. This allows you to define strict rules, output formats, and operational constraints for the Architect, Engineer, and other expert roles. From `cline-builder/entrypoint.sh` you can configure the Cline's part of the pipeline like the task prompts etc.

#### 3. Rounds & Limits
Control the depth of the build process and safety guardrails:
-   **max_build_iterations**: The number of **rounds** (4-pass cycles) the pipeline will attempt to complete the project.
-   **cline_max_turns**: The maximum number of tool calls the Cline agent can make per round.
-   **context_window**: Manage the total token capacity for the pipeline.

```json
"limits": {
    "max_project_size_mb": 4096,
    "max_build_iterations": 5,
    "cline_max_turns": 30
}
```

---

## 🛠️ Requirements

-   **WSL2:** Windows 11 with Ubuntu 22.04+ (Recommended).
-   **Native Linux:** Ubuntu 22.04+ or any modern distribution with NVIDIA support.
-   **GPU:** NVIDIA GPU with 24GB+ VRAM (RTX 3090/4090/5090) is recommended.
-   **Software:** Docker Engine, NVIDIA Container Toolkit, and NVIDIA Drivers.

> [!TIP]
> **Scaling for Smaller Hardware:** While optimized for 24GB VRAM, Bob can run on smaller GPUs by substituting the "Expert" model for a smaller variant (e.g., swapping a 27B model for an 8B model). Orchestrator only takes around 1GB of VRAM.

## 📦 Setup & Installation

### 🖥️ Windows (WSL2)
The setup is automated. Ensure your NVIDIA drivers are up to date on Windows, then run in WSL2:

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/mitro54/br.ai.n.git
    cd br.ai.n
    ```

2.  **Run the Installer:**
    The `setup_workspace.sh` script automatically:
    -   Installs required Python virtual environments.
    -   Pulls the correct Ollama models (`qwen2.5:1.5b`, `qwen3.5:27b`). (make sure to change your desired models in the script)
    -   Deploys the Docker stack (Open WebUI & SearXNG).
    -   Sets up and launches ComfyUI and the Orchestrator proxy.

    ```bash
    chmod +x setup_workspace.sh
    ./setup_workspace.sh
    ```

### 🐧 Native Linux
Setup on native Linux is straightforward. Skip the WSL-specific configurations and ensure you have the NVIDIA Container Toolkit installed.

1.  **Prerequisites:**
    -   Install [NVIDIA Drivers](https://www.nvidia.com/Download/index.aspx).
    -   Install [Docker Engine](https://docs.docker.com/engine/install/ubuntu/).
    -   Install [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
    -   Install [Ollama](https://ollama.com/download/linux).

2.  **Clone & Setup:**
    ```bash
    git clone https://github.com/mitro54/br.ai.n.git
    cd br.ai.n
    chmod +x setup_workspace.sh
    ./setup_workspace.sh
    ```

3.  **Network Access:**
    On Native Linux, ensuring the workspace is accessible on your LAN typically only requires opening port 3000 in `ufw` or `iptables`:
    ```bash
    sudo ufw allow 3000/tcp
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

> [!NOTE]
> **Hot-Swapping Experts:** You can change `EXPERT_MODEL` at the top of `orchestrator.py` at any time. If you use a model other than the default (`qwen3.5:27b`), the system will automatically bypass custom sampling parameters (temperature, penalties) and use that model's native default settings.

---
## ⌨️ Full list of Manual Control Commands

While in chat, use these commands to override the orchestrator:
- `!lock`: Holds the Expert in VRAM indefinitely.
- `!unlock`: Releases the lock and evicts the Expert immediately.
- `!code`: Manually switches Expert to high-precision "Coding Mode".
- `!general`: Manually switches Expert to creative "General Mode".
- `!move`: Scans the conversation, identifies project structure/code, and exports it to `conversations/`.
- `!build`: Triggers the autonomous build pipeline for the currently moved project.
- `!status`: Checks the status of active or recent build containers.
- `!logs`: Fetches the latest terminal logs from the active background build pipeline.
- `!stop`: Force-stops all running build pipelines.
- `!bob` / `hey bob`: Force the current turn to use the Fast Orchestrator.
- `!expert` / `hey expert`: Force the current turn to use the Expert Model.


## System prompt in Open WebUI for Bob

You should setup this system prompt in Open WebUI to get the best experience with Bob:

```
You are Bob, a highly capable, confident, and professional AI Workspace Orchestrator. You speak directly, without hesitation, and never apologize for your capabilities. 

CRITICAL DIRECTIVES:
1. YOU ARE THE EXPERT: If the user asks for "the expert," complex coding, deep analysis, or high-level problem-solving, YOU are that expert. Never state that you cannot code, cannot analyze, or need to delegate to another AI. You possess world-class programming and analytical skills.
2. TONE AND STYLE: Never use the phrase "As an AI language model." Never say "I don't have the capability." You are the Orchestrator. Act like it.
3. USER COMMANDS: The user might sometimes include commands starting with ! , that could be !lock or !expert for example but not limited only to, meaning they are using their own built in tools, you must ignore these completely in your thoughts, they do not interest you.
4. OVERTHINKING: Do not overthink it, you must keep your thought process logical and analytical. Once you are approaching a decision, do it! Trust yourself. Do not overthink it!
```

---

## 🎨 Image Generation Setup (Flux.2)

To get image generation working, you need to download the models and configure Open WebUI. This is my personal setup.

### 1. Download Models & Text Encoders
Download the following files from Hugging Face:

-   **Main Model:** [FLUX.2-klein-9b-fp8](https://huggingface.co/black-forest-labs/FLUX.2-klein-9b-fp8/tree/main)
-   **Text Encoders & VAE:** [VAE/Text Encoders for Flux](https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-9b/tree/main/split_files)

### 2. File Placement
Move the downloaded files to their respective directories within your `ComfyUI/models` folder:

-   **Main Model (`.safetensors`):** `ComfyUI/models/diffusion_models/`
-   **Text Encoders:** `ComfyUI/models/text_encoders/`
-   **VAE:** `ComfyUI/models/vae/`

### 3. Installing Memory Management Plugin
To ensure optimal performance and prevent VRAM fragmentation, you must install the **FreeMemory** plugin before uploading the workflow:

1. Navigate to your `ComfyUI/custom_nodes` directory.
2. Clone the repository:
   ```bash
   git clone https://github.com/ShmuelRonen/ComfyUI-FreeMemory
   ```
3. Restart ComfyUI. (Just run the start_workspace.sh script again if you need to refresh something)

### 4. Open WebUI Configuration
1. Login to **Open WebUI** as an Administrator.
2. Go to **Admin Panel** -> **Images**.
3. Set **Image Generation Engine** to `ComfyUI`.
4. Set **ComfyUI Base URL** to `http://host.docker.internal:8188`.
5. Set the **Model Name** to `flux-2-klein-9b-fp8.safetensors`.
6. Upload the provided `flux2api.json` workflow file in the same settings area.
7. Under **ComfyUI Workflow Nodes**, ensure the **Text Input** is mapped to **Node ID 4**.
