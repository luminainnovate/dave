#!/bin/bash
set -euo pipefail

# =============================================================================
# Multi-Agent Cline Builder Pipeline — Entrypoint
# =============================================================================
# Three-phase pipeline:
#   Phase 1: Multi-pass context distillation (Architect → Engineer → Safety)
#   Phase 2: Iterative Cline CLI build/verify/safety cycle
#
# Environment Variables (set by orchestrator):
#   CONVERSATION_FILE  - Path to conversation JSON
#   AGENT_CONFIG_PATH  - Path to agent_config.json
#   OLLAMA_HOST        - Ollama API URL
#   EXPERT_CTX         - Context window size for distillation
#   CLINE_CTX          - Context window size for Cline agent
#   CLINERULES_PATH    - Output path for .clinerules
# =============================================================================

# Set the global config directory so ALL cline commands use it
export CLINE_DIR="/root/.config/Cline"

CONFIG_PATH="${AGENT_CONFIG_PATH:-/app/agent_config.json}"
CONVERSATION_FILE="${CONVERSATION_FILE:-/workspace/.build_conversation.json}"
CLINERULES_PATH="${CLINERULES_PATH:-/workspace/.clinerules}"
OLLAMA_HOST="${OLLAMA_HOST:-http://host.docker.internal:11434}"
CLINE_CTX="${CLINE_CTX:-32768}"

echo "========================================"
echo "🔨 Cline Builder Pipeline"
echo "========================================"
echo "  Config:       ${CONFIG_PATH}"
echo "  Conversation: ${CONVERSATION_FILE}"
echo "  Ollama:       ${OLLAMA_HOST}"
echo "  Distill CTX:  ${EXPERT_CTX}"
echo "  Cline CTX:    ${CLINE_CTX}"
echo "  Timestamp:    $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "========================================"

# --- Prerequisite Checks ---

if [ ! -f "$CONVERSATION_FILE" ]; then
    echo "✗ FATAL: Conversation file not found: ${CONVERSATION_FILE}"
    exit 1
fi

if [ ! -f "$CONFIG_PATH" ]; then
    echo "✗ FATAL: Config file not found: ${CONFIG_PATH}"
    exit 1
fi

# Wait for Ollama to be reachable (up to 30 seconds)
echo ""
echo "🔌 Checking Ollama connectivity..."
RETRIES=0
MAX_RETRIES=15
until curl -sf "${OLLAMA_HOST}/api/tags" > /dev/null 2>&1; do
    RETRIES=$((RETRIES + 1))
    if [ $RETRIES -ge $MAX_RETRIES ]; then
        echo "✗ FATAL: Cannot reach Ollama at ${OLLAMA_HOST} after ${MAX_RETRIES} attempts"
        exit 1
    fi
    echo "  Waiting for Ollama... (${RETRIES}/${MAX_RETRIES})"
    sleep 2
done
echo "  ✓ Ollama is reachable"

# --- Read Config ---
MAX_SIZE_MB=$(jq -r '.limits.max_project_size_mb // 2048' "$CONFIG_PATH")
MAX_ITERATIONS=$(jq -r '.limits.max_build_iterations // 3' "$CONFIG_PATH")
CLINE_MAX_TURNS=$(jq -r '.limits.cline_max_turns // 10' "$CONFIG_PATH")
CLINE_MODEL=$(jq -r '.models.cline // "qwen3.5:27b"' "$CONFIG_PATH")
CLINE_STARTUP=$(jq -r '.cline_startup_message // "Read .clinerules and execute all tasks."' "$CONFIG_PATH")

# --- Project Size Check ---
check_project_size() {
    local dir_size_mb
    dir_size_mb=$(du -sm /workspace 2>/dev/null | cut -f1)
    echo "  📦 Workspace size: ${dir_size_mb} MB / ${MAX_SIZE_MB} MB limit"
    if [ "$dir_size_mb" -gt "$MAX_SIZE_MB" ]; then
        echo "✗ FATAL: Workspace exceeds size limit (${dir_size_mb} MB > ${MAX_SIZE_MB} MB)"
        return 1
    fi
    return 0
}

# =============================================================================
# PHASE 1: Multi-Pass Context Distillation
# =============================================================================
echo ""
echo "========================================="
echo "📚 Phase 1: Context Distillation (4-pass)"
echo "   Workspace: $(pwd)"
echo "========================================="

# Run distillation with unbuffered output
PYTHONUNBUFFERED=1 python3 /app/distill.py
DISTILL_EXIT=$?

if [ $DISTILL_EXIT -ne 0 ]; then
    echo "✗ FATAL: Distillation failed (exit code ${DISTILL_EXIT})"
    exit 1
fi

if [ ! -f "$CLINERULES_PATH" ]; then
    echo "✗ FATAL: .clinerules file was not created"
    exit 1
fi

echo ""
echo "  ✓ .clinerules generated ($(wc -c < "$CLINERULES_PATH") bytes)"

# =============================================================================
# PHASE 2: Iterative Cline Build Cycle
# =============================================================================
echo ""
echo "========================================="
echo "🤖 Phase 2: Cline Build Cycle"
echo "   Output: /workspace"
echo "========================================="
echo "  Model:          ${CLINE_MODEL}"
echo "  Max iterations: ${MAX_ITERATIONS}"
echo "  Max turns/iter: ${CLINE_MAX_TURNS}"
echo ""

# --- Phase 2 Setup: Auto-Auth for CLI ---
echo "  🔑 Configuring local provider (Ollama)..."

cline auth \
    -p openai \
    -k "dummy" \
    -m "$CLINE_MODEL" \
    -b "${OLLAMA_HOST}/v1"

ITERATION=0
BUILD_COMPLETE=false

while [ $ITERATION -lt $MAX_ITERATIONS ] && [ "$BUILD_COMPLETE" = false ]; do
    ITERATION=$((ITERATION + 1))
    echo ""
    echo "─── Iteration ${ITERATION}/${MAX_ITERATIONS} ───"

    # Pre-flight size check
    if ! check_project_size; then
        echo "✗ Build aborted: project size limit exceeded"
        exit 1
    fi

# --- Build Phase ---
    echo "  🔧 Running Cline (Build mode)..."

    # [NEW] Dynamic Timeout and Messaging
    CURRENT_TIMEOUT=300
    BUILD_MSG="$CLINE_STARTUP"

    if [ $ITERATION -eq $MAX_ITERATIONS ]; then
        echo "  🚨 FINAL ROUND: Shifting to Stabilization and Debugging..."
        BUILD_MSG="CRITICAL: This is the FINAL iteration (${ITERATION} of ${MAX_ITERATIONS}). Your directive is now STABILIZATION. You must ignore the previous 'move on' anti-loop rules. Revisit any TODOs, uncommented code, or failing tests. Your sole priority is to ensure the application compiles, runs correctly end-to-end, and is completely usable. Take your time and fix the root causes of any remaining bugs."
        CURRENT_TIMEOUT=600  # Give it 10 full minutes for deep debugging
    elif [ $ITERATION -gt 1 ]; then
        BUILD_MSG="Continue building the project. Review what was done in the previous iteration, fix any issues, and complete remaining tasks from .clinerules. This is iteration ${ITERATION} of ${MAX_ITERATIONS}. Remember: keep momentum and don't get stuck on one bug."
    fi

    set +e
    cline task -v -y \
        -m "$CLINE_MODEL" \
        --timeout "$CURRENT_TIMEOUT" \
        "$BUILD_MSG" \
        2>&1 | tee "/workspace/.build_log_iter_${ITERATION}.txt"
    CLINE_EXIT=$?
    set -e

    echo "  ↳ Cline exited with code ${CLINE_EXIT}"

    # Post-build size check
    if ! check_project_size; then
        echo "✗ Build aborted: project grew beyond size limit"
        exit 1
    fi

    # --- Verification Phase ---
    echo "  🔍 Running Cline (Verification mode)..."

    # [NEW] Instructions to maintain the issue list and write a README
    VERIFY_MSG="Review the project in /workspace. 
    1) Verify all tasks from .clinerules are implemented and the code runs without errors. 
    2) MUST DO: Create a 'README.md' file that clearly explains what the project is and EXACTLY how to run it. 
    3) Check if '/workspace/.build_issues.md' already exists. If it does, READ it. Cross off or remove the issues that were fixed in this iteration, and keep the ones that still need work. Do not hallucinate uncompleted tasks. 
    4) If the app is 100% working, safe, and has a README, create a file at '/workspace/.build_complete' containing 'VERIFIED'. If issues remain, ensure they are accurately documented in '.build_issues.md'."

    set +e
    cline task -v -y \
        -m "$CLINE_MODEL" \
        --timeout 400 \
        "$VERIFY_MSG" \
        2>&1 | tee "/workspace/.verify_log_iter_${ITERATION}.txt"
    set -e

    # --- Safety Phase ---
    echo "  🛡️ Running Cline (Safety audit)..."

    SAFETY_MSG="Perform a security and safety audit of all code in /workspace. Check for: 1) Input validation, 2) Path traversal, 3) Hardcoded secrets, 4) Injection risks, 5) Infinite loops/resource leaks, 6) Missing error handling. 
    If you find critical issues, attempt to FIX THEM DIRECTLY in the code. 
    If you fix them or the code is already safe, append 'SAFE' to /workspace/.build_complete. 
    If you cannot fix them, add them to /workspace/.build_issues.md."

    set +e
    cline task -v -y \
        -m "$CLINE_MODEL" \
        --timeout 400 \
        "$SAFETY_MSG" \
        2>&1 | tee "/workspace/.safety_log_iter_${ITERATION}.txt"
    set -e

    # --- Check completion ---
    if [ -f "/workspace/.build_complete" ]; then
        COMPLETE_CONTENT=$(cat /workspace/.build_complete)
        if echo "$COMPLETE_CONTENT" | grep -q "VERIFIED" && echo "$COMPLETE_CONTENT" | grep -q "SAFE"; then
            echo ""
            echo "  ✅ Build VERIFIED and SAFE on iteration ${ITERATION}"
            BUILD_COMPLETE=true
        else
            echo "  ⚠ .build_complete exists but not fully verified/safe yet"
            rm -f /workspace/.build_complete
        fi
    else
        echo "  ⚠ Build not yet complete, will retry..."
    fi
done

# =============================================================================
# SUMMARY
# =============================================================================
echo ""
echo "========================================"
if [ "$BUILD_COMPLETE" = true ]; then
    echo "✅ BUILD PIPELINE COMPLETE"
    echo "   Iterations used: ${ITERATION}/${MAX_ITERATIONS}"
else
    echo "⚠️  BUILD PIPELINE ENDED (max iterations reached)"
    echo "   Iterations used: ${ITERATION}/${MAX_ITERATIONS}"
    echo "   Check .build_issues.md for remaining work"
fi

# Final size report
FINAL_SIZE=$(du -sm /workspace 2>/dev/null | cut -f1)
echo "   Final workspace size: ${FINAL_SIZE} MB"
echo "========================================"