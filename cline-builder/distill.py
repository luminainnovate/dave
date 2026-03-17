#!/usr/bin/env python3
"""
Multi-Pass Context Distillation Engine

Reads a conversation JSON file, runs it through 4 expert LLM passes
(Architect → Engineer → Test Engineer → Safety Inspector), and writes a combined
.clinerules file for the Cline CLI agent.

Handles conversation chunking when content exceeds the context window.
Manages Ollama model loading/unloading between passes for VRAM safety.
Isolates context between passes using Markdown boundaries.
"""

import json
import os
import time
import httpx
import threading

# --- Configuration ---
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://host.docker.internal:11434")
CONFIG_PATH = os.environ.get("AGENT_CONFIG_PATH", "/app/agent_config.json")
CONVERSATION_PATH = os.environ.get("CONVERSATION_FILE", "/workspace/.build_conversation.json")
OUTPUT_PATH = os.environ.get("CLINERULES_PATH", "/workspace/.clinerules")
STATUS_PATH = os.environ.get("DISTILL_STATUS_PATH", "/workspace/.distill_status")
CONTEXT_WINDOW = int(os.environ.get("EXPERT_CTX", "16384"))

# Rough approximation: 1 token ≈ 4 characters
CHARS_PER_TOKEN = 4
# Reserve tokens for system prompt + response
RESERVED_TOKENS = 2048
CHUNK_OVERLAP_TOKENS = 200

# Keep the chunk size small to prevent CPU ingestion stalls
TARGET_CHUNK_SIZE = 2048


def load_config() -> dict:
    """Load the agent configuration file."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_conversation() -> list:
    """Load the conversation messages from the JSON file."""
    with open(CONVERSATION_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def conversation_to_text(messages: list) -> str:
    """Flatten conversation messages into a readable text block."""
    parts = []
    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        if content.strip():
            parts.append(f"[{role}]\n{content}")
    return "\n\n---\n\n".join(parts)


def chunk_text(text: str, max_tokens: int) -> list[str]:
    """
    Split text into chunks that fit within the token budget.
    """
    max_chars = max_tokens * CHARS_PER_TOKEN
    overlap_chars = CHUNK_OVERLAP_TOKENS * CHARS_PER_TOKEN

    if len(text) <= max_chars:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + max_chars
        
        if end >= len(text):
            chunks.append(text[start:])
            break
            
        break_point = text.rfind("\n\n", start + max_chars // 2, end)
        if break_point == -1:
            break_point = text.rfind("\n", start + max_chars // 2, end)
        if break_point == -1:
            break_point = text.rfind(". ", start + max_chars // 2, end)
        if break_point != -1:
            end = break_point + 1

        chunks.append(text[start:end])
        
        new_start = end - overlap_chars
        if new_start <= start:
            new_start = start + 1
        start = new_start

    return chunks


def unload_model(client: httpx.Client, model_name: str):
    """Ask Ollama to unload a model from VRAM."""
    try:
        client.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": model_name, "keep_alive": 0},
            timeout=10.0
        )
        print(f"  ↳ Unloaded model: {model_name}")
        time.sleep(1)
    except Exception as e:
        print(f"  ⚠ Failed to unload {model_name}: {e}")


def call_llm(client: httpx.Client, model: str, system_prompt: str, user_content: str, prior_context: str = "") -> str:
    """
    Send a synchronous chat completion request to Ollama.
    Uses generic extraction prompts for chunks to prevent template deadlocks.
    """
    prior_tokens = len(prior_context) // CHARS_PER_TOKEN
    available_tokens = CONTEXT_WINDOW - RESERVED_TOKENS - prior_tokens
    
    if available_tokens < 1000:
        available_tokens = 1000

    chunk_limit = min(available_tokens, TARGET_CHUNK_SIZE)
    chunks = chunk_text(user_content, chunk_limit)

    print(f"  ↳ Input: {len(user_content) + len(prior_context)} chars total. (Ctx: {CONTEXT_WINDOW}, Chunk Limit: {chunk_limit})", flush=True)

    if len(chunks) == 1:
        full_input = ""
        if prior_context:
            full_input += f"### PREVIOUS ANALYSES\n{prior_context}\n\n---\n\n"
        full_input += f"### CURRENT TASK\n{user_content}"
        
        print(f"    ↳ Preparing Single-pass (Ingesting {len(full_input)} chars)...", flush=True)
        return _single_llm_call(client, model, system_prompt, full_input)

    print(f"    ↳ Processing into {len(chunks)} parts...", flush=True)
    partial_results = []

    # [FIX] A relaxed, generic system prompt for the chunks so it doesn't deadlock trying to fill out a template it doesn't have data for.
    relaxed_chunk_system_prompt = (
        "You are acting as an information extractor. Your final formatting goal will be defined later. "
        "For now, review the provided text chunk and extract ANY raw technical details, facts, or logical "
        "requirements that stand out. Output simple bullet points. Do not attempt to use formal templates."
    )

    for i, chunk in enumerate(chunks):
        part_label = f"Part {i + 1}/{len(chunks)}"
        
        chunk_prompt = ""
        if prior_context:
            chunk_prompt += f"### PREVIOUS ANALYSES\n{prior_context}\n\n---\n\n"
            
        chunk_prompt += (
            f"### CURRENT TASK (PART {i + 1} OF {len(chunks)})\n"
            f"Extract key technical information from this text chunk.\n\n"
            f"{chunk}"
        )
        
        print(f"    ↳ Preparing {part_label} (Payload: {len(chunk_prompt)} chars)...", flush=True)
        # Use the relaxed prompt for the chunks
        result = _single_llm_call(client, model, relaxed_chunk_system_prompt, chunk_prompt, part_label)
        partial_results.append(result)

    print("    ↳ All parts finished. Starting Merge Pass...", flush=True)
    
    # [FIX] Now we apply your STRICT system prompt to the merged bullets
    merge_prompt = (
        "You previously extracted technical details from a larger conversation in parts. "
        "Below are the raw extracted bullet points.\n\n"
        "Using ONLY these details (and the PREVIOUS ANALYSES if provided), write your final response. "
        "You MUST strictly adhere to your system prompt instructions and template formatting.\n\n"
    )
    for i, part in enumerate(partial_results):
        merge_prompt += f"#### EXTRACTED FACTS (PART {i + 1})\n{part}\n\n"

    # Use the REAL system prompt here
    return _single_llm_call(client, model, system_prompt, merge_prompt, "Merging Parts")


def _single_llm_call(client: httpx.Client, model: str, system_prompt: str, user_content: str, label: str = "Inference") -> str:
    """Execute a single LLM API call with streaming for live feedback."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "stream": True,
        "options": {
            "num_ctx": CONTEXT_WINDOW,
            "temperature": 0.3,
        },
        "keep_alive": "3m"
    }

    first_token_received = threading.Event()
    
    def heartbeat():
        start_wait = time.time()
        while not first_token_received.is_set():
            time.sleep(5)
            if not first_token_received.is_set():
                elapsed = int(time.time() - start_wait)
                # Improved heartbeat to show exactly how long it's taking
                print(f"      ↳ [Waiting for LLM... {elapsed}s]", flush=True)

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()

    try:
        full_response = []
        print(f"    {label:15} [Generating...]\n    ↳ ", end="", flush=True)
        start_time = time.time()
        
        with httpx.Client() as stream_client:
            with stream_client.stream("POST", f"{OLLAMA_HOST}/api/chat", json=payload, timeout=600.0) as resp:
                if resp.status_code != 200:
                    first_token_received.set()
                    print("\n  ✗ LLM returned status", resp.status_code)
                    return f"[ERROR: LLM returned status {resp.status_code}]"
                
                dot_count = 0
                for line in resp.iter_lines():
                    if not line:
                        continue
                    
                    if not first_token_received.is_set():
                        first_token_received.set()

                    chunk_data = json.loads(line)
                    # Check for actual text vs blank token hallucination
                    token = chunk_data.get("message", {}).get("content", "")
                    if token:
                        full_response.append(token)
                        dot_count += 1
                        if dot_count % 20 == 0:
                            print(".", end="", flush=True)
                        if dot_count % 1000 == 0:
                            print(f"({dot_count})", end="", flush=True)
                    
                    if chunk_data.get("done"):
                        break
        
        elapsed = time.time() - start_time
        print(f" ✓ ({elapsed:.1f}s)", flush=True)
        return "".join(full_response)

    except httpx.TimeoutException:
        first_token_received.set()
        print("\n  ✗ LLM request timed out (600s)")
        return "[ERROR: Request timed out]"
    except Exception as e:
        first_token_received.set()
        print(f"\n  ✗ LLM request failed: {e}")
        return f"[ERROR: {e}]"


def update_status(status: str):
    """Write current status to a file in the workspace."""
    try:
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            f.write(status)
    except Exception as e:
        print(f"  ⚠ Failed to update status file: {e}")


def run_distillation():
    """Execute the 4-pass distillation pipeline."""
    print("=" * 60, flush=True)
    print("🧠 Multi-Pass Context Distillation Engine", flush=True)
    print("=" * 60, flush=True)

    config = load_config()
    models = config.get("models", {})
    prompts = config.get("prompts", {})

    messages = load_conversation()
    conversation_text = conversation_to_text(messages)
    print(f"\n📄 Conversation: {len(messages)} messages, {len(conversation_text)} chars", flush=True)

    passes = [
        ("architect",     "🏗️  Pass 1/4: System Architect"),
        ("engineer",      "⚙️  Pass 2/4: Engineer"),
        ("test_engineer", "🧪  Pass 3/4: Test Engineer"),
        ("safety",        "🛡️  Pass 4/4: Safety Inspector"),
    ]

    results = {}
    previous_model = None

    with httpx.Client() as client:
        for pass_key, pass_label in passes:
            print(f"\n{pass_label}", flush=True)
            print("-" * 40, flush=True)
            update_status(f"Distilling: {pass_label}")

            model = models.get(pass_key, models.get("architect"))
            prompt = prompts.get(pass_key, "Analyze the following conversation.")

            if previous_model and previous_model != model:
                print(f"  ↳ Switching model: {previous_model} → {model}")
                unload_model(client, previous_model)

            prior_context = ""
            if results:
                prior_context = "\n\n".join(
                    f"#### {k.upper()} ANALYSIS\n{v}" 
                    for k, v in results.items()
                )

            if pass_key in ["architect", "engineer"]:
                target_content = conversation_text
            else:
                # Passes 3 and 4 only read the previous plans. They do not get chunked!
                target_content = "Review the PREVIOUS ANALYSES provided above based strictly on your system role and required template format. Do not invent new features or write source code."

            result = call_llm(client, model, prompt, target_content, prior_context)
            results[pass_key] = result
            previous_model = model
            print(f"  ✓ Complete ({len(result)} chars)")

            intermediate_path = f"/workspace/.distill_{pass_key}.md"
            try:
                with open(intermediate_path, "w", encoding="utf-8") as f:
                    f.write(f"# Distillation Intermediate: {pass_key.title()}\n\n{result}")
                print(f"  ↳ Saved intermediate result to {intermediate_path}")
            except Exception as e:
                print(f"  ⚠ Failed to save intermediate result: {e}")

        if previous_model:
            unload_model(client, previous_model)

    print(f"\n📝 Writing {OUTPUT_PATH}", flush=True)
    update_status("Assembling .clinerules...")
    clinerules = assemble_clinerules(results, config)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(clinerules)

    update_status("Distillation complete.")
    print(f"  ✓ Written ({len(clinerules)} chars)", flush=True)
    print("=" * 60, flush=True)
    print("✅ Distillation complete", flush=True)
    print("=" * 60, flush=True)


def assemble_clinerules(results: dict, config: dict) -> str:
    """Combine the 4-pass results into a structured .clinerules document."""
    limits = config.get("limits", {})

    doc = [
        "# Project Build Specification",
        "",
        "> Auto-generated by Multi-Agent Distillation Pipeline",
        f"> Timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "",
        "## ⚠️ CORE DIRECTIVES (High Priority)",
        "The following requirements are non-negotiable and must be prioritized over all other implementation details:",
        "",
    ]

    if "test_engineer" in results:
        doc.append("### 🧪 Critical Test & Quality Gates")
        doc.append(results["test_engineer"])
        doc.append("")

    if "safety" in results:
        doc.append("### 🛡️ Safety & Security Mitigations")
        doc.append(results["safety"])
        doc.append("")

    doc.extend([
        "## Constraints",
        "",
        f"- Max project size: {limits.get('max_project_size_mb', 4096)} MB",
        f"- Max build iterations: {limits.get('max_build_iterations', 5)}",
        "- ANTI-LOOP RULE: Never attempt the same bug fix more than twice.",
        "- If a test fails repeatedly, comment it out, add a TODO, and proceed to the next file.",
        "- Do not get stuck. Finishing the checklist is more important than passing every test.",
        "- You must verify each major component after implementation",
        "- Run all safety checks before declaring the build complete",
        "",
    ])

    section_map = {
        "architect": ("Architecture & Directory Structure", "🏗️"),
        "engineer": ("Implementation Roadmap", "⚙️"),
    }

    for key, (title, icon) in section_map.items():
        if key in results:
            doc.extend([
                f"## {icon} {title}",
                "",
                results[key],
                "",
            ])

    return "\n".join(doc)


if __name__ == "__main__":
    run_distillation()