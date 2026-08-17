```
    ___                    __     ____  ___ _    ________
   /   | ____ ____  ____  / /_   / __ \/   | |  / / ____/
  / /| |/ __ `/ _ \/ __ \/ __/  / / / / /| | | / / __/   
 / ___ / /_/ /  __/ / / / /_   / /_/ / ___ | |/ / /___   
/_/  |_\__, /\___/_/ /_/\__/  /_____/_/  |_|___/_____/   
      /____/                                             
```                                                                                             
                                        
# 🧠 DAVE - The Agentic Local AI Orchestrator


[![CI](https://github.com/mitro54/br.ai.n/actions/workflows/ci.yml/badge.svg)](https://github.com/mitro54/br.ai.n/actions/workflows/ci.yml)

Agent DAVE is a fully autonomous software factory starting from a conversation, all locally.

**br.ai.n** is an **Agentic Local AI Orchestrator** powered by **Agent DAVE**, a unified AI persona running on your local hardware. Agent DAVE manages a tiered orchestration system on a single NVIDIA GPU (24GB+ VRAM), providing instant responses for simple tasks while dynamically routing complex requests (Coding, Vision, Image Generation) to expert models.

## Chat Commands:

### 🧠 VRAM & Model Control

| Command | Effect | Notes |
|---|---|---|
| `!lock` | Pins the Expert in VRAM indefinitely (`keep_alive: -1`). | Returns immediately. Persists until `!unlock`. |
| `!unlock` | Releases the lock, unloads Expert + Router, frees ComfyUI. | Returns immediately. |
| `!code` | Switches the Expert to Coding Mode parameters (temp 0.6, repeat_penalty 1.15). | **Falls through** — the rest of your message is still answered. Also forces routing to the Expert. |
| `!general` | Switches the Expert to General Mode parameters (temp 1.0, presence_penalty 1.5). | **Falls through**, same as above. |
| `!dave` / `hey dave` | Forces the turn onto the small Router model and clears the Expert warm timer. | The "stay fast, stay local" escape hatch. Note that `hey dave` fires on any message containing that phrase. |
| `!expert` / `hey expert` | Forces the turn onto the Expert model, warm for 10 minutes. | |

### 🏭 Project Binding & Build Pipeline

| Command | Effect | Notes |
|---|---|---|
| `!move` | Scans the conversation for code blocks and file trees and reconstructs them into `conversations/<name>_<conv_id>/`. | Binds the project to the conversation. Skips extraction if `.clinerules` already exists, to protect manual edits. |
| `!clone <url>` | Clones a Git repo into the conversation's workspace and binds it. Add `--kb <url>` to attach a second repo as `.knowledge_base/`. | **Must be at the start of the message.** Re-running on a bound project just reports the existing binding. |
| `!build` | Kicks off the full 4-pass autonomous pipeline (Architect → Engineer → Test → Safety) plus implementation, in the `cline-builder` container. | Extra text in the same message steers it, e.g. *"Lets !build, we must add authentication."* |
| `!architect` | Runs **Pass 1 only** and stops at a review gate. | The safe way in — no code is written. |
| `!review` | Re-displays the architecture document from the last `!architect`. | Read-only. |
| `!approve` | Accepts the reviewed architecture and resumes the remaining passes plus implementation. | Requires a prior `!architect`, otherwise it refuses. |
| `!status` | Reports the status of active and recent build containers. | |
| `!logs` | Fetches the last 200 lines from the active `cline-builder` container. | Also exposed to the Expert as a tool. |
| `!stop` | Force-stops all running build pipelines and clears VRAM. | |

### ✏️ Repository Editing & Pull Requests

| Command | Effect | Notes |
|---|---|---|
| `!write` | Enables write mode for this conversation, granting the Expert edit / write / delete / list-changes tools. | Requires a bound project. Off by default and resets to off when the orchestrator restarts. |
| `!readonly` | Revokes write mode. | Changes already on disk are left untouched. |
| `!diff` | Shows the full diff of everything the Expert has changed in this conversation. | Review this before `!pr`. |
| `!undo` | Restores every touched file to its exact pre-session bytes. | Untracked files restore correctly; your pre-existing uncommitted work is unaffected. |
| `!pr <title>` | Commits the session's changes to `brain/<conv_id>`, pushes, and opens a pull request against the bound repo's `origin`. | **Must be at the start of the message.** Only files this conversation touched are staged. |

### ⚙️ How Command Matching Works

| Rule | Detail |
|---|---|
| **Substring match, first wins** | Every command except `!clone` and `!pr` matches anywhere in the message, in a single `if/elif` chain. *"Should I run !build or !status?"* triggers `!build`. |
| **Order is fixed** | `!lock` → `!unlock` → `!code` → `!general` → `!move` → `!architect` → `!approve` → `!review` → `!build` → `!clone` → `!write` / `!readonly` → `!undo` → `!diff` → `!pr` → `!stop` → `!status` → `!logs`. Because `!code` sits third, *"!code let's !build this"* runs `!code` only. |
| **Two don't return** | `!code` and `!general` set a mode and let your message continue to inference. Every other command replies and stops the turn. |
| **Position-sensitive pair** | `!clone` and `!pr` use prefix matching, so they must open the message. The rest can appear anywhere. |
| **Background turns are exempt** | Title/tag/summary pings from Open WebUI never trigger commands. |

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
-   **Expert Reasoning:** Dynamically loads expert models (e.g. `qwen3.8:27b`) for complex coding and logic tasks.
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

### 🚀 Universal Scale: The 256k Context Milestone

For users with an **NVIDIA RTX 4090 (24GB VRAM)**, br.ai.n has been optimized to support massive context windows that allow for the ingestion of entire repositories.

#### How it works:
1. **Model:** We utilize MoE architectures like `Qwen 3.6 35B A3B` (GGUF).
2. **KV Quantization:** By forcing `--cache-type-k q8_0` and `--cache-type-v q8_0`, we halve the memory footprint of the conversation history.
3. **Flash Attention:** Mandatory for stability and speed at scales above 64k.
4. **Managed Slots:** The orchestrator forces `-np 1` to ensure all 24GB is dedicated to a single, deep reasoning process.

**Result:** You can fit a **fully offloaded 35B model** with a **256,144 token context window** in ~21.1GB of VRAM, leaving room for the resident router and OS overhead.

> [!NOTE]
> **This is not the shipped default.** Out of the box `EXPERT_CTX` is `131072` (128k) with `qwen3.8:27b` on Ollama. To reach the figures above you need to switch `EXPERT_CONFIG` to the `llamacpp` provider with a MoE GGUF and raise `EXPERT_CTX` yourself. KV quantisation (`--cache-type-k/v q8_0`) is already on by default via `LLAMACPP_DEFAULT_ARGS`, and Flash Attention is forced on at spawn.

---

## 🏭 Automated Software Factory

Agent DAVE isn't just a chatbot; Agent DAVE is a fully autonomous software factory. By combining tiered orchestration with a dedicated build pipeline, you can turn ideas into full projects without manual intervention.

### 🔄 The Autonomous Loop (Distillation & Implementation)

1.  **Project Extraction (`!move`):** Agent DAVE scans your current conversation, identifies the project structure, and reconstructs the entire file tree in a dedicated workspace within `conversations/`.
2.  **4-Pass Distillation (`!build`):** Does the same as !move and also triggers the build pipeline. The system runs four expert agents in sequence:
    -   **Architect:** Defines business goals and directory structures.
    -   **Engineer:** Maps logic to files and defines design patterns.
    -   **Test Engineer:** Identifies edge cases and verification gates.
    -   **Safety Inspector:** Audits for security vulnerabilities.
3.  **Autonomous Implementation:** A specialized `Cline` agent takes the resulting `.clinerules` and executes the code changes, handling everything from file creation and bug fixing to testing and verification.

### 🚥 Factory Management Commands

-   `!move`: Extract project files from the current chat.
-   `!build`: Kick off the 4-pass autonomous build pipeline.
-   `!architect`: Run **only** Pass 1 (Architect) and stop at a review gate, so you can read the proposed architecture before any code is written.
-   `!review`: Re-display the architecture document produced by `!architect`.
-   `!approve`: Accept the reviewed architecture and resume the remaining passes plus implementation. Requires a prior `!architect`.
-   `!clone`: Clone a Git repository into the conversation's workspace, e.g. `!clone https://github.com/user/repo`. Add `--kb <url>` to attach a second repo as a knowledge base.
-   `!status`: Check the status of active and recent build containers.
-   `!logs`: Fetch and display the latest console logs from the active build pipeline.
-   `!stop`: Force-stop all running build pipelines and clear VRAM.

> [!TIP]
> **Review-then-build workflow:** `!architect` → read the output → `!approve`. This is usually preferable to a bare `!build`, which commits to all four passes and implementation in one shot.

---

### ✏️ Repository Editing & Pull Requests

Once a project is bound to a conversation (via `!clone`, or by symlinking an existing checkout into `conversations/`), the Expert can read it. Run `!write` and it can change it too.

-   `!write`: Enable write mode for this conversation. Grants the Expert `orchestrator_edit_file`, `orchestrator_write_file`, `orchestrator_delete_file` and `orchestrator_list_changes`.
-   `!readonly`: Revoke write mode. Existing changes are left on disk.
-   `!diff`: Show the full diff of everything the Expert has changed in this conversation.
-   `!undo`: Revert every file the Expert touched back to its exact pre-session bytes.
-   `!pr <title>`: Commit this conversation's changes to a `brain/<conversation-id>` branch and open a pull request against the bound repo's own `origin`.

**How it stays safe.** Write mode is off by default, so ordinary discussion turns cannot modify anything. The Expert must read a file before it can edit or delete it — it cannot act on files it has only seen in the symbol skeleton. Edits are anchor-based and must match exactly once, so a wrong anchor fails loudly instead of corrupting code. Paths are realpath-resolved and containment-checked, which blocks both `../` traversal and symlinks inside the project pointing out of it. `.git/`, `.env*`, keys and certificates are never writable.

**How your existing work stays safe.** Like Claude Code, the Expert edits files in place rather than branching first — so a repo that is already dirty stays usable. The first time any file is touched, its exact bytes are snapshotted outside the repo, which is what `!undo` restores from. Because those snapshots are byte copies rather than a git stash, untracked files are restored correctly instead of being deleted. Branching happens only at `!pr`, and only the files this conversation actually changed are staged — anything else you had uncommitted is deliberately left alone.

> [!IMPORTANT]
> The Expert has no tool for committing or opening pull requests. That is deliberate: raising a PR is an outward-facing action against a real remote, so it happens only when *you* run `!pr`. Review with `!diff` first.

---

## 📂 Project Structure

A high-level overview of the **br.ai.n** workspace and its core components:

```text
.
├── orchestrator.py          # 🧠 Central FastAPI Proxy (VRAM Manager & Router)
├── router.py                # 🛡️ Pi Router Node (2-Device: triage + Wake-on-LAN)
├── mover.py                 # 📂 Project Extractor (Parses chat to files)
├── setup_workspace.sh       # 🚀 Automated Installer (Standalone/Desktop)
├── setup_pi.sh              # 🍓 Automated Installer for Raspberry Pi
├── start_standalone.sh      # 🚥 Launch Script (1-Device: Full Stack)
├── start_workspace.sh       # 🚥 Launch Script (1-Device: full stack incl. ComfyUI)
├── start_desktop.sh         # 🚥 Launch Script (2-Device: Desktop Worker Node)
├── start_router.sh          # 🚥 Launch Script (2-Device: Pi Router Node)
├── docker-compose.yml       # 🐳 Multi-Container Stack (1-Device)
├── docker-compose.pi.yml    # 🐳 Lightweight Container Stack (2-Device Pi)
├── flux2api.json            # 🎨 ComfyUI workflow for Flux.2 image generation
├── test_intent.py           # 🧪 Intent/routing tests
├── test_advanced_mover.py   # 🧪 Project-extraction tests
├── cline-builder/           # 🔨 Autonomous Build Pipeline (The "Factory")
│   ├── distill.py           #   - 4-Pass Thinking Engine (Architect -> Engineer -> etc.)
│   ├── agent_config.json    #   - Factory Configuration (Models, Prompts, Limits)
│   ├── prompts/             #   - Agent system prompts (architect/engineer/test/safety .md)
│   ├── Dockerfile           #   - Pipeline Environment
│   └── entrypoint.sh        #   - Autonomous Build Execution Flow
├── searxng/                 # 🔍 Search Engine Configuration (git-ignored)
├── conversations/           # 🏗️ Workspace Root (Autonomous projects live here, git-ignored)
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

Agent DAVE supports **three LLM backends** — you can mix and match them per model role. This means your Expert can run on Ollama while a stubborn model runs on llama.cpp, all sharing one GPU lock.

| Provider | How It Works | Best For |
|----------|-------------|----------|
| **Ollama** | Native model management, auto-loads on request | Daily driver, widest model library |
| **LM Studio** | Managed via `lms` CLI, OpenAI-compatible API | GUI users, easy model browsing |
| **llama.cpp** | Orchestrator spawns `llama-server` on demand | HuggingFace models, raw GGUF files, stubborn models |

---

### 🔧 Provider Setup

Before configuring models, ensure the provider you want to use is installed:

<details>
<summary><b>Ollama</b> (Default — already installed if you ran setup)</summary>

```bash
# Install (if not already)
curl -fsSL https://ollama.com/install.sh | sh

# Pull a model
ollama pull gemma4:26b

# Verify it's running
curl http://localhost:11434/api/tags
```
- **Default port:** `11434`
- **API format:** Ollama native + OpenAI-compatible (`/v1/chat/completions`)
- **Model management:** Automatic (loads on first request, unloads via `keep_alive`)
</details>

<details>
<summary><b>LM Studio</b></summary>

```bash
# Install LM Studio from https://lmstudio.ai
# Then bootstrap the CLI:
~/.lmstudio/bin/lms bootstrap

# Verify CLI is working
lms status

# Start the local server (or use the GUI)
lms server start
```
- **Default port:** `1234`
- **API format:** OpenAI-compatible (`/v1/chat/completions`)
- **Model management:** Via `lms load <model>` / `lms unload <model>` (orchestrator handles this automatically)

> [!NOTE]
> The model name in your config must match the identifier shown in `lms ls`. LM Studio uses its own naming convention (e.g., `lmstudio-community/qwen2.5-32b-GGUF`).
</details>

<details>
<summary><b>llama.cpp</b></summary>

```bash
# Option 1: Install from package manager
# Ubuntu/Debian:
sudo apt install llama.cpp

# Option 2: Build from source (recommended for GPU support)
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j$(nproc)
sudo cp build/bin/llama-server /usr/local/bin/

# Verify installation
llama-server --help
```
- **Default port:** `8080` (configurable per model)
- **API format:** OpenAI-compatible (`/v1/chat/completions`)
- **Model management:** Orchestrator spawns/kills `llama-server` processes automatically
- **HuggingFace support:** Use `-hf` flag syntax in model names (e.g., `unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q3_K_XL`)

> [!TIP]
> You do NOT need to start `llama-server` manually. The orchestrator manages the process lifecycle — it starts the server when the model is needed and stops it when switching to another model.
</details>

---

### 🧠 Orchestrator Models (`orchestrator.py`)

At the top of `orchestrator.py`, configure which model and provider to use for the **Expert** (complex tasks) and **Router** (triage):

```python
# --- STRICT MODEL CONFIG ---
EXPERT_CONFIG = {
    "model": "qwen3.8:27b",       # Model name (Ollama tag, LMS identifier, or HF repo)
    "provider": "ollama",          # "ollama", "lmstudio", or "llamacpp"
    "base_url": "http://localhost:11434",  # API endpoint
}
ROUTER_CONFIG = {
    "model": "qwen2.5:1.5b",
    "provider": "ollama",
    "base_url": "http://localhost:11434",
}
```

These are the shipped defaults. `DEFAULT_EXPERT_MODEL` is also set to `qwen3.8:27b` and is what the custom sampling parameters are tuned for — point `EXPERT_CONFIG` at anything else and the orchestrator falls back to that model's native defaults.

#### Example: Expert on llama.cpp with a HuggingFace model

```python
EXPERT_CONFIG = {
    "model": "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q3_K_XL",
    "provider": "llamacpp",
    "base_url": "http://localhost:8080",
}
```

The orchestrator will automatically:
1. Start `llama-server` with the `-hf` flag pointing to the HuggingFace repo
2. Wait for the server to download and load the model (up to 2 minutes)
3. Route all Expert requests to `http://localhost:8080/v1/chat/completions`
4. Kill the process when switching to another model

#### Example: Expert on LM Studio

```python
EXPERT_CONFIG = {
    "model": "lmstudio-community/qwen2.5-32b-GGUF",
    "provider": "lmstudio",
    "base_url": "http://localhost:1234",
}
```

The orchestrator will call `lms load <model>` before inference and `lms unload` when switching.

#### llama.cpp Advanced Settings

```python
# Path to the llama.cpp binary. This is the unified `llama` binary, which the
# orchestrator invokes via its `serve` subcommand — not a bare `llama-server`.
# Change this to wherever your binary actually lives.
LLAMACPP_BINARY = "/home/jonathan/.local/bin/llama"

# Extra CLI args appended to every spawn. KV-cache quantisation is on by
# default, which roughly halves the memory cost of the context window.
LLAMACPP_DEFAULT_ARGS = ["--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]
```

> [!IMPORTANT]
> `LLAMACPP_BINARY` ships with an absolute path from the author's machine. Set it to your own path before using the `llamacpp` provider.

Flash Attention is forced on by the orchestrator via the `LLAMA_ARG_FLASH_ATTN=on` environment variable when it spawns the process — you do not need to pass `-fa` yourself.

You can also add per-model args in the config dict:

```python
EXPERT_CONFIG = {
    "model": "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q3_K_XL",
    "provider": "llamacpp",
    "base_url": "http://localhost:8080",
    "args": ["-ngl", "99", "-fa"],  # Extra CLI flags
}
```

#### Context Windows

```python
EXPERT_CTX = 131072   # Context for the expert model (128k)
DISTILL_CTX = 131072  # Context for the distillation engine (128k)
CLINE_CTX = 131072    # Context for the Cline agent (128k)
```

---

### 🏗️ Build Pipeline (`cline-builder/agent_config.json`)

The autonomous factory supports the same multi-provider system. Each agent in the pipeline can use a different backend:

#### Per-Model Provider Config (Recommended)

```json
{
    "models": {
        "architect": {
            "model": "gemma4:26b",
            "provider": "ollama",
            "base_url": "http://host.docker.internal:11434"
        },
        "engineer": {
            "model": "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q3_K_XL",
            "provider": "llamacpp",
            "base_url": "http://host.docker.internal:8080"
        },
        "test_engineer": {
            "model": "gemma4:26b",
            "provider": "ollama",
            "base_url": "http://host.docker.internal:11434"
        },
        "safety": {
            "model": "gemma4:26b",
            "provider": "ollama",
            "base_url": "http://host.docker.internal:11434"
        },
        "cline": {
            "model": "Qwen3.6-35B-Q3-unsloth:latest",
            "provider": "ollama",
            "base_url": "http://host.docker.internal:11434"
        }
    },
    "ollama_host": "http://host.docker.internal:11434"
}
```

> [!IMPORTANT]
> The build pipeline runs inside Docker. Use `host.docker.internal` instead of `localhost` for all `base_url` values so the container can reach the host machine's LLM backends.

The example above shows a deliberately mixed setup. **The shipped `agent_config.json` is simpler** — all five roles (`architect`, `engineer`, `test_engineer`, `safety`, `cline`) point at `qwen3.8:27b` on `http://host.docker.internal:11434`.

#### Legacy String Format (Still Works)

For backward compatibility, simple strings still work and default to Ollama:

```json
"models": {
    "architect": "gemma4:26b",
    "engineer": "Qwen3.6-35B-Q3-unsloth:latest"
}
```

#### System Prompts & Logic

Each distillation agent's system prompt lives in its own Markdown file under `cline-builder/prompts/`, referenced from `agent_config.json` by a path relative to the config file:

```json
"prompts": {
    "architect":     "prompts/architect.md",
    "engineer":      "prompts/engineer.md",
    "test_engineer": "prompts/test_engineer.md",
    "safety":        "prompts/safety.md"
}
```

Edit those `.md` files to define strict rules, output formats, and operational constraints for each expert role. An inline prompt string is still accepted in place of a path for backwards compatibility.

The Cline agent's opening instructions are the `cline_startup_message` key in `agent_config.json` — `cline-builder/entrypoint.sh` only reads that value, so edit the config rather than the shell script.

> [!NOTE]
> The prompt files are mounted read-only into the container at `/app/prompts`. If you add a new prompt file, make sure the volume mount in `docker-compose.yml` still covers it.

#### Rounds & Limits
Control the depth of the build process and safety guardrails:
-   **max_build_iterations**: The number of **rounds** (4-pass cycles) the pipeline will attempt to complete the project.
-   **cline_max_retries**: The consecutive-mistake budget passed to the Cline CLI's `--retries` flag.
-   **max_project_size_mb**: Upper bound on the workspace size the pipeline will operate on.

```json
"limits": {
    "max_project_size_mb": 8192,
    "max_build_iterations": 5,
    "cline_max_retries": 6
}
```

`context_window` is a **top-level** key in `agent_config.json`, not part of `limits`:

```json
"context_window": 131072
```

---

### 🔀 Quick Reference: Switching Providers

Here's a cheat sheet for common scenarios:

| Scenario | Where to Edit | What to Change |
|----------|---------------|----------------|
| Change Expert model (same provider) | `orchestrator.py` | `EXPERT_CONFIG["model"]` |
| Move Expert to llama.cpp | `orchestrator.py` | Set `provider: "llamacpp"`, update `base_url` |
| Move Expert to LM Studio | `orchestrator.py` | Set `provider: "lmstudio"`, update `base_url` |
| Change a build agent's model | `agent_config.json` | Update the agent's object entry |
| Run build agent on llama.cpp | `agent_config.json` | Set `provider: "llamacpp"` + `base_url` |
| Use a HuggingFace model directly | Any config | Set `model` to `org/repo:quantization` with `provider: "llamacpp"` |

> [!NOTE]
> **Hot-Swapping Experts:** You can change `EXPERT_CONFIG` at the top of `orchestrator.py` at any time. If you use a model other than the default (`qwen3.8:27b`), the system will automatically bypass custom sampling parameters (temperature, penalties) and use that model's native default settings.

---

### 🐛 Troubleshooting Providers

| Symptom | Cause | Fix |
|---------|-------|-----|
| `llama-server: command not found` | Binary not installed or not in PATH | Set `LLAMACPP_BINARY` to the full path |
| `lms: command not found` | LM Studio CLI not bootstrapped | Run `~/.lmstudio/bin/lms bootstrap` |
| `Connection refused` on non-Ollama port | Server not started | For llama.cpp: orchestrator starts it automatically. For LM Studio: run `lms server start` |
| Model loads but inference is garbled | Wrong model format for provider | Ollama needs Ollama-format models. llama.cpp needs GGUF files. |
| Docker container can't reach backend | Using `localhost` in `agent_config.json` | Use `host.docker.internal` instead |
| VRAM conflict between providers | Two models loaded simultaneously | Check that only one model is active — the shared GPU lock should prevent this |

---

## 🛠️ Requirements

-   **WSL2:** Windows 11 with Ubuntu 22.04+ (Recommended).
-   **Native Linux:** Ubuntu 22.04+ or any modern distribution with NVIDIA support.
-   **GPU:** NVIDIA GPU with 24GB+ VRAM (RTX 3090/4090/5090) is recommended.
-   **Software:** Docker Engine, NVIDIA Container Toolkit, and NVIDIA Drivers.

> [!TIP]
> **Scaling for Smaller Hardware:** While optimized for 24GB VRAM, Agent DAVE can run on smaller GPUs by substituting the "Expert" model for a smaller variant (e.g., swapping a 27B model for an 8B model). Orchestrator only takes around 1GB of VRAM.

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
    -   Pulls the correct Ollama models (`qwen2.5:1.5b`, `qwen3.8:27b`). (make sure to change your desired models in the script)
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

The project dynamically supports both **1-Device (Standalone)** and **2-Device (Distributed)** setups natively.

### 1-Device Setup (Standalone)
If you are running everything on a single powerful machine:
```bash
chmod +x start_standalone.sh
./start_standalone.sh
```

### 2-Device Setup (Distributed)
If you want to offload the frontend routing to a Raspberry Pi and keep heavy LLM lifting on your Desktop:
1. **On the Desktop (Worker Node):**
   ```bash
   chmod +x start_desktop.sh
   ./start_desktop.sh
   ```
2. **On the Raspberry Pi (Router Node):**
   ```bash
   chmod +x start_router.sh
   ./start_router.sh
   ```

-   **Web UI:** [http://localhost:3000](http://localhost:3000) (Or your Pi's IP address)
-   **Proxy Health/State:** [http://localhost:8000/health](http://localhost:8000/health) (Or your Pi's IP on port 8001)
-   **Logs:** `tail -f orchestrator.log` (Desktop) or `tail -f router.log` (Pi)

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

**Orchestrator models are configured in code, not via environment variables.** Edit `EXPERT_CONFIG` and `ROUTER_CONFIG` at the top of `orchestrator.py` — see [Orchestrator Models](#-orchestrator-models-orchestratorpy) above. `EXPERT_MODEL` and `ROUTER_MODEL` are derived from those dicts:

```python
EXPERT_MODEL = EXPERT_CONFIG["model"]
ROUTER_MODEL = ROUTER_CONFIG["model"]
```

The environment variables the stack does honour:

| Variable | Read by | Description | Default |
| :--- | :--- | :--- | :--- |
| `AGENT_CONFIG_PATH` | `orchestrator.py`, `distill.py` | Path to the build pipeline config | `cline-builder/agent_config.json` |
| `EXPERT_CTX` | `distill.py` | Context window for distillation passes | set to `8192` in `docker-compose.yml` |
| `OLLAMA_HOST` | `distill.py` | Ollama endpoint used inside the build container | `http://host.docker.internal:11434` |
| `ROUTER_MODEL` | `router.py` (2-device Pi node only) | Triage model on the Pi | `qwen2.5:1.5b` |
| `ROUTER_OLLAMA_URL` | `router.py` (Pi node only) | Ollama endpoint on the Pi | `http://localhost:11434` |
| `ROUTER_PORT` | `router.py` (Pi node only) | Port the Pi router listens on | `8001` |
| `DESKTOP_IP` / `DESKTOP_PORT` | `router.py` (Pi node only) | Where the Pi forwards heavy requests | — |

`router.py` reads several more for Wake-on-LAN and SSH control of the desktop node (`WAKER_URL`, `WOL_BOOT_WAIT`, `DESKTOP_SSH_HOST`, …); see the top of that file for the full set.

> [!NOTE]
> **Hot-Swapping Experts:** You can change `EXPERT_CONFIG` at the top of `orchestrator.py` at any time. If you use a model other than the default (`qwen3.8:27b`), the system automatically bypasses the custom sampling parameters (`PARAMS_GENERAL` / `PARAMS_CODING`) and uses that model's native defaults.

---
## ⌨️ Full list of Manual Control Commands

While in chat, use these commands to override the orchestrator:
- `!lock`: Holds the Expert in VRAM indefinitely.
- `!unlock`: Releases the lock and evicts the Expert immediately.
- `!code`: Manually switches Expert to high-precision "Coding Mode".
- `!general`: Manually switches Expert to creative "General Mode".
- `!move`: Scans the conversation, identifies project structure/code, and exports it to `conversations/`.
- `!build`: Triggers the autonomous build pipeline for the currently moved project.
- `!architect`: Runs Pass 1 only and stops at a review gate with the proposed architecture.
- `!review`: Re-displays the architecture document from the last `!architect`.
- `!approve`: Approves the reviewed architecture and resumes the full pipeline.
- `!clone <url>`: Clones a Git repo into the conversation workspace. Optional `--kb <url>` attaches a knowledge-base repo.
- `!write`: Enables repository write mode, letting the Expert create, edit and delete files in the bound project.
- `!readonly`: Revokes write mode.
- `!diff`: Shows the full diff of this conversation's changes.
- `!undo`: Reverts every file the Expert touched to its pre-session state.
- `!pr <title>`: Commits this conversation's changes to a `brain/<conversation-id>` branch and opens a pull request on the bound repo's `origin`.
- `!status`: Checks the status of active or recent build containers.
- `!logs`: Fetches the latest terminal logs from the active background build pipeline.
- `!stop`: Force-stops all running build pipelines.
- `!dave` / `hey dave`: Force the current turn to use the Fast Orchestrator.
- `!expert` / `hey expert`: Force the current turn to use the Expert Model.


## System prompt in Open WebUI for Agent DAVE

You should setup this system prompt in Open WebUI to get the best experience with Agent DAVE:

```
You are Agent DAVE, a highly capable, confident, and professional AI Workspace Orchestrator. You speak directly, without hesitation, and never apologize for your capabilities. 

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
