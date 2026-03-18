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
CONVERSATION_FILE="${CONVERSATION_FILE:-/workspace/.cline_context/conversation.json}"
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
echo "  Project:      ${PROJECT_NAME:-<unnamed>}"
echo "  Timestamp:    $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "========================================"

# Setup Directories
mkdir -p /workspace/.cline_context
mkdir -p /workspace/.cline_logs

# Migrate legacy structures from older pipelines (backwards compatibility)
mv /workspace/.build_log_iter_*.txt /workspace/.cline_logs/ 2>/dev/null || true
mv /workspace/.verify_log_iter_*.txt /workspace/.cline_logs/ 2>/dev/null || true
mv /workspace/.safety_log_iter_*.txt /workspace/.cline_logs/ 2>/dev/null || true
mv /workspace/.distill_*.md /workspace/.cline_context/ 2>/dev/null || true
if [ -f "/workspace/.build_conversation.json" ]; then
    # Move the old conversation out of the root, but don't overwrite the new one 
    mv -n /workspace/.build_conversation.json /workspace/.cline_context/legacy_conversation.json 2>/dev/null || true
fi
if [ -f "/workspace/.build_issues.md" ] && [ ! -f "/workspace/.cline_context/.build_issues.md" ]; then
    mv /workspace/.build_issues.md /workspace/.cline_context/ 2>/dev/null || true
fi

# Clear previous run artifacts to ensure no confusion
echo "🧹 Cleaning previous run logs and distillation files..."
rm -f /workspace/.cline_logs/*.txt
rm -f /workspace/.cline_context/distill_*.md
rm -f /workspace/.build_complete

# --- Noise Suppression Bootstrap ---
# Ensure node_modules and metadata are physically ignored by the agent's tools
echo "🚩 Bootstrapping noise suppression (.gitignore)..."
{
    echo "node_modules/"
    echo ".git/"
    echo ".venv/"
    echo "venv/"
    echo ".cline_logs/"
    echo ".cline_context/"
    echo "__pycache__/"
    echo ".pytest_cache/"
    echo "*.log"
} >> /workspace/.gitignore_builder

# Sort and unique the gitignore if it exists, otherwise use our builder version
if [ -f "/workspace/.gitignore" ]; then
    sort -u /workspace/.gitignore /workspace/.gitignore_builder -o /workspace/.gitignore
else
    cp /workspace/.gitignore_builder /workspace/.gitignore
fi
rm /workspace/.gitignore_builder

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

# Reset completion state in case this is a rebuild
rm -f /workspace/.build_complete

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

    CURRENT_TIMEOUT=600 # 10 minutes
    BUILD_MSG="$CLINE_STARTUP"

    if [ $ITERATION -eq $MAX_ITERATIONS ]; then
        echo "  🚨 FINAL ROUND: Shifting to Stabilization and Debugging..."
        BUILD_MSG="CRITICAL: This is the FINAL iteration (${ITERATION} of ${MAX_ITERATIONS}). Your directive is now STABILIZATION. You must ignore the previous 'move on' anti-loop rules. Revisit any TODOs, uncommented code, or failing tests. Your sole priority is to ensure the application compiles, runs correctly end-to-end, and is completely usable. Take your time and fix the root causes of any remaining bugs."
        CURRENT_TIMEOUT=1200  # Give it 20 minutes for deep debugging
    elif [ $ITERATION -gt 1 ]; then
        BUILD_MSG="Continue building the project. Review what was done in the previous iteration, fix any issues, and complete remaining tasks from .clinerules. This is iteration ${ITERATION} of ${MAX_ITERATIONS}. Remember: keep momentum and don't get stuck on one bug."
    fi

    set +e
    cline task -v -y \
        -m "$CLINE_MODEL" \
        --timeout "$CURRENT_TIMEOUT" \
        "$BUILD_MSG" \
        2>&1 | tee "/workspace/.cline_logs/build_log_iter_${ITERATION}.txt"
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

    VERIFY_MSG="Review the current project. 
    1) Verify all tasks from .clinerules are implemented and the code runs without errors. 
    2) MUST DO: Create a 'README.md' file that clearly explains what the project is and EXACTLY how to run it. 
    3) Check if '.cline_context/.build_issues.md' already exists. If it does, READ it. Cross off or remove the issues that were fixed in this iteration, and keep the ones that still need work. Do not hallucinate uncompleted tasks. 
    4) If the app is 100% working, safe, and has a README, create a file named '.build_complete' in the root directory containing 'VERIFIED'. If issues remain, ensure they are accurately documented in '.cline_context/.build_issues.md'.
    5) Before testing, clean up old servers using highly specific targets (e.g., 'pkill -f "http.server"')."
    set +e
    cline task -v -y \
        -m "$CLINE_MODEL" \
        --timeout 600 \
        "$VERIFY_MSG" \
        2>&1 | tee "/workspace/.cline_logs/verify_log_iter_${ITERATION}.txt"
    set -e

    # --- Safety Phase ---
    echo "  🛡️ Running Cline (Safety audit)..."

    SAFETY_MSG="Perform a security and safety audit of all code in the current project. Check for: 1) Input validation, 2) Path traversal, 3) Hardcoded secrets, 4) Injection risks, 5) Infinite loops/resource leaks, 6) Missing error handling. 
    If you find critical issues, attempt to FIX THEM DIRECTLY in the code. 
    If you fix them or the code is already safe, append 'SAFE' to the '.build_complete' file. 
    If you cannot fix them, add them to '.cline_context/.build_issues.md'.
    Before testing, clean up old servers using highly specific targets (e.g., 'pkill -f "http.server"')."
    set +e
    cline task -v -y \
        -m "$CLINE_MODEL" \
        --timeout 600 \
        "$SAFETY_MSG" \
        2>&1 | tee "/workspace/.cline_logs/safety_log_iter_${ITERATION}.txt"
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
    echo "   Check .cline_context/.build_issues.md for remaining work"
fi

# Final size report
FINAL_SIZE=$(du -sm /workspace 2>/dev/null | cut -f1)
echo "   Final workspace size: ${FINAL_SIZE} MB"
echo "========================================"