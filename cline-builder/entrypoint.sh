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

# --- VRAM Safety Guard ---
# Ensure that we release the GPU expert model when the container shuts down
# regardless of success or failure.
cleanup_vram() {
    echo ""
    echo "========================================"
    echo "🧹 VRAM VACUUM: Releasing GPU Expert..."
    echo "========================================"
    # Send shutdown signal to orchestrator
    ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://host.docker.internal:8000}"
    curl -s -X POST "${ORCHESTRATOR_URL}/v1/shutdown_expert" > /dev/null || true
    echo "  ✓ Expert unloaded."
}
trap cleanup_vram EXIT INT TERM

CONFIG_PATH="${AGENT_CONFIG_PATH:-/app/agent_config.json}"
CONVERSATION_FILE="${CONVERSATION_FILE:-/workspace/.cline_context/conversation.json}"
CLINERULES_PATH="${CLINERULES_PATH:-/workspace/.clinerules}"
OLLAMA_HOST="${OLLAMA_HOST:-http://host.docker.internal:11434}"
CLINE_CTX="${CLINE_CTX:-131072}"
# 'full' runs distillation then the Cline build cycle.
# 'distill_only' stops at the review gate. Defaulted because set -u is active.
PIPELINE_MODE="${PIPELINE_MODE:-full}"

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

# Clear previous run artifacts to ensure no confusion.
# The distill_*.md files are the exception on a resume run: they ARE the reviewed
# architecture that !approve exists to reuse, so wiping them here would force a
# regeneration with the approve directive as the design brief.
DISTILL_RESUME="${DISTILL_RESUME:-}"
rm -f /workspace/.cline_logs/*.txt
rm -f /workspace/.build_complete
case "$(echo "$DISTILL_RESUME" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes)
        echo "🧹 Cleaning previous run logs (keeping reviewed distillation for resume)..."
        ;;
    *)
        echo "🧹 Cleaning previous run logs and distillation files..."
        rm -f /workspace/.cline_context/distill_*.md
        ;;
esac

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
    echo ".knowledge_base/"
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

# Wait for backend providers to be reachable (up to 30 seconds each)
echo ""
echo "🔌 Checking backend connectivity..."

# Extract all unique base_urls from the config (handles both string and object model formats)
# For string models, the default is ollama_host; for objects, use base_url
DEFAULT_HOST=$(jq -r '.ollama_host // "http://host.docker.internal:11434"' "$CONFIG_PATH")
ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://host.docker.internal:8000}"

# Get base_urls paired with their provider type (skip llamacpp — orchestrator manages those on demand)
PROVIDER_URLS=$(jq -r --arg dh "$DEFAULT_HOST" '
  [.models | to_entries[] | .value |
    if type == "object" then
      select(.provider != "llamacpp") | .base_url // $dh
    else $dh
    end
  ] | unique | .[]' "$CONFIG_PATH" 2>/dev/null || echo "$DEFAULT_HOST")

# Check if any models use llamacpp (need orchestrator connectivity instead)
HAS_LLAMACPP=$(jq -r '[.models | to_entries[] | .value | select(type == "object" and .provider == "llamacpp")] | length' "$CONFIG_PATH" 2>/dev/null || echo "0")

for BASE_URL in $PROVIDER_URLS; do
    # Determine the health endpoint based on the URL
    # Ollama uses /api/tags, OpenAI-compatible uses /v1/models
    HEALTH_URL="${BASE_URL}/api/tags"
    if [[ "$BASE_URL" != *":11434"* ]]; then
        HEALTH_URL="${BASE_URL}/v1/models"
    fi

    echo "  Checking ${BASE_URL}..."
    RETRIES=0
    MAX_RETRIES=15
    until curl -sf "${HEALTH_URL}" > /dev/null 2>&1; do
        RETRIES=$((RETRIES + 1))
        if [ $RETRIES -ge $MAX_RETRIES ]; then
            echo "  ⚠ WARNING: Cannot reach ${BASE_URL} after ${MAX_RETRIES} attempts (continuing anyway)"
            break
        fi
        echo "    Waiting... (${RETRIES}/${MAX_RETRIES})"
        sleep 2
    done
    if [ $RETRIES -lt $MAX_RETRIES ]; then
        echo "  ✓ ${BASE_URL} is reachable"
    fi
done

# If any models use llamacpp, verify the orchestrator is reachable (it manages llama-server lifecycle)
if [ "$HAS_LLAMACPP" -gt 0 ]; then
    echo "  Checking orchestrator (manages llamacpp)..."
    RETRIES=0
    MAX_RETRIES=10
    until curl -sf "${ORCHESTRATOR_URL}/health" > /dev/null 2>&1; do
        RETRIES=$((RETRIES + 1))
        if [ $RETRIES -ge $MAX_RETRIES ]; then
            echo "  ⚠ WARNING: Cannot reach orchestrator at ${ORCHESTRATOR_URL} (llamacpp models may fail)"
            break
        fi
        echo "    Waiting for orchestrator... (${RETRIES}/${MAX_RETRIES})"
        sleep 2
    done
    if [ $RETRIES -lt $MAX_RETRIES ]; then
        echo "  ✓ Orchestrator reachable (will manage llamacpp on demand)"
    fi
fi

# --- Read Config ---
MAX_SIZE_MB=$(jq -r '.limits.max_project_size_mb // 2048' "$CONFIG_PATH")
MAX_ITERATIONS=$(jq -r '.limits.max_build_iterations // 5' "$CONFIG_PATH")
# Consecutive-mistake budget passed to the Cline CLI's --retries flag.
# Default matches the CLI's own default so an absent config changes nothing.
CLINE_MAX_RETRIES=$(jq -r '.limits.cline_max_retries // 6' "$CONFIG_PATH")
# Per-phase wall-clock budget handed to the Cline CLI's --timeout flag. A run that
# exceeds it is killed mid-turn and the iteration is lost, so these scale with task
# complexity, not with model speed. Defaults match the values these replaced.
BUILD_TIMEOUT=$(jq -r '.limits.build_timeout_secs // 1800' "$CONFIG_PATH")
VERIFY_TIMEOUT=$(jq -r '.limits.verify_timeout_secs // 1800' "$CONFIG_PATH")
SAFETY_TIMEOUT=$(jq -r '.limits.safety_timeout_secs // 1800' "$CONFIG_PATH")
# The final iteration switches from building to stabilization: it inherits every
# bug the earlier rounds deferred, so it gets its own, larger budget.
FINAL_BUILD_TIMEOUT=$(jq -r '.limits.final_build_timeout_secs // empty' "$CONFIG_PATH")
[ -z "$FINAL_BUILD_TIMEOUT" ] && FINAL_BUILD_TIMEOUT="$BUILD_TIMEOUT"
# Extract cline model: handle both string ("model_name") and object ({"model": "..."}) formats
CLINE_MODEL=$(jq -r 'if (.models.cline | type) == "object" then .models.cline.model else (.models.cline // "qwen3.8:27b") end' "$CONFIG_PATH")
CLINE_PROVIDER=$(jq -r 'if (.models.cline | type) == "object" then (.models.cline.provider // "ollama") else "ollama" end' "$CONFIG_PATH")
CLINE_BASE_URL=$(jq -r --arg dh "$DEFAULT_HOST" 'if (.models.cline | type) == "object" then (.models.cline.base_url // $dh) else $dh end' "$CONFIG_PATH")
CLINE_STARTUP=$(jq -r '.cline_startup_message // "Read .clinerules and execute all tasks."' "$CONFIG_PATH")

# --- Project Size Check ---
# Mirrors the noise-suppression list above: VCS metadata and regenerable
# artifacts are never parsed by the agent, so they shouldn't count toward the cap.
DU_EXCLUDES=(
    --exclude=.git
    --exclude=node_modules
    --exclude=.venv
    --exclude=venv
    --exclude=__pycache__
    --exclude=.pytest_cache
    --exclude=.knowledge_base
    --exclude=.cline_logs
    --exclude=.cline_context
)

check_project_size() {
    local dir_size_mb
    dir_size_mb=$(du -sm "${DU_EXCLUDES[@]}" /workspace 2>/dev/null | cut -f1)
    echo "  📦 Workspace size: ${dir_size_mb} MB / ${MAX_SIZE_MB} MB limit (source only)"
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

if [ $DISTILL_EXIT -eq 3 ]; then
    echo "✗ STOPPED: the design passes reported blockers that could not be resolved"
    echo "  from the workspace. Nothing was built and .clinerules was not written."
    echo "  Answer the blockers listed above in chat, then re-run !build."
    exit 1
elif [ $DISTILL_EXIT -ne 0 ]; then
    echo "✗ FATAL: Distillation failed (exit code ${DISTILL_EXIT})"
    exit 1
fi

# Review gate: stop here so the architecture can be inspected (and edited) before
# any code is written. The approve run re-enters with DISTILL_RESUME=1.
if [ "$PIPELINE_MODE" = "distill_only" ]; then
    echo ""
    echo "========================================="
    echo "⏸️  Review gate: stopping before Phase 2"
    echo "   Review: /workspace/.cline_context/distill_architect.md"
    echo "   Approve in chat with: !approve"
    echo "========================================="
    exit 0
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
# GIT SAFETY NET (Component 7)
# =============================================================================
setup_git_safety() {
    cd /workspace
    
    # Try to initialize if missing, but don't fail if we can't
    if [ ! -d ".git" ]; then
        # Project has no git — initialize for local snapshot only
        if git init -q 2>/dev/null; then
            git config user.email "builder@local"
            git config user.name "Cline Builder"
            git add -A 2>/dev/null
            git commit -q -m "snapshot: pre-build state" 2>/dev/null
            echo "  📸 Created local snapshot (no remote, no pushing)"
        else
            echo "  ⚠️ Skipping git safety net (not a repository and cannot initialize)"
            return 0
        fi
    fi
    
    # Final check to ensure we are in a working tree
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        return 0
    fi
    
    # Always work on a branch, never on main/master
    local MAIN_BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "main")
    local BRANCH_NAME="agent/build-$(date +%s)"
    git checkout -b "$BRANCH_NAME" 2>/dev/null || true
    echo "  🌿 Working on branch: ${BRANCH_NAME} (based on ${MAIN_BRANCH})"
    echo "  💡 To review: git diff ${MAIN_BRANCH}"
    echo "  💡 To rollback: git checkout ${MAIN_BRANCH}"
}

echo ""
echo "🌿 Setting up git safety net..."
setup_git_safety

# =============================================================================
# SESSION STATE GENERATOR (Component 3)
# =============================================================================

# The agent is told to read .session_state.md as its FIRST ACTION every step, so
# whatever this function writes is charged against the build window before any
# work starts. Measured at 78066 characters (~26k tokens, 40% of a 64k window)
# with 96% of it in Previous Step Summaries: `tail -10` caps lines, not bytes,
# and a single log line carrying a tool payload ran to 6872 characters. Every
# section that concatenates a file it does not control is now byte-capped, the
# same way Agent Discovery Notes already was.
SESSION_STATE_ISSUES_BYTES=4000
SESSION_STATE_AUDIT_BYTES=4000
SESSION_STATE_NOTES_BYTES=3000
SESSION_STATE_SUMMARY_BYTES=6000
SESSION_STATE_LINE_CHARS=300

# =============================================================================
# TEST GATE
# =============================================================================
# The only objective signal in the pipeline.
#
# Completion was previously decided entirely by the agent: the verify prompt asks
# it to write '.build_complete' containing VERIFIED "if the app is 100% working",
# and the safety prompt appends SAFE. Nothing checked. `CLINE_EXIT` is captured
# after the build phase and only echoed; verify and safety exit codes are not
# captured at all. So the pipeline's definition of done was the model's opinion
# of its own work, which on a hard task is exactly where it is least reliable.
#
# This runs the project's own test command and requires exit 0 before that
# opinion is accepted. Failures are appended to .build_issues.md, which
# generate_session_state already feeds back into the next iteration - so a failed
# gate steers the next round instead of merely blocking this one.
TEST_GATE_TIMEOUT=$(jq -r '.limits.test_gate_timeout_secs // 900' "$CONFIG_PATH")

# Re-plan trigger. distill.py owns the decision (growth since the plan was
# written, against a budget of re-plans); these only pass the thresholds through.
REPLAN_GROWTH_BYTES=$(jq -r '.limits.replan_issue_growth_bytes // 2000' "$CONFIG_PATH")
MAX_REPLANS=$(jq -r '.limits.max_replans // 2' "$CONFIG_PATH")

run_test_gate() {
    local ITERATION=$1
    local CMD
    CMD=$(python3 /app/distill.py --test-command /workspace 2>/dev/null | head -n 1)

    if [ -z "$CMD" ]; then
        # No suite to run. Degrade to the previous behaviour rather than blocking
        # a project that legitimately has no tests - but say so, loudly, because
        # it means completion is back to being self-assessed.
        echo "  ⚠ TEST GATE SKIPPED: no runnable test command detected."
        echo "    Completion is self-reported for this build."
        return 0
    fi

    echo "  🧪 Test gate: ${CMD} (timeout ${TEST_GATE_TIMEOUT}s)"
    local GATE_LOG="/workspace/.cline_logs/test_gate_iter_${ITERATION}.txt"
    set +e
    timeout "$TEST_GATE_TIMEOUT" bash -c "cd /workspace && ${CMD}" > "$GATE_LOG" 2>&1
    local GATE_EXIT=$?
    set -e

    if [ $GATE_EXIT -eq 0 ]; then
        echo "  ✅ Test gate PASSED"
        return 0
    fi

    # A missing runner is not a failing test suite, and treating it as one would
    # block completion permanently for something the agent cannot fix. The image
    # ships Node and Python but only httpx on the Python side, so `pytest` and an
    # uninstalled node_modules are both realistic. Skip loudly instead.
    if [ $GATE_EXIT -eq 127 ] || grep -qiE "no module named pytest|command not found|could not determine executable|npm error|cannot find module" "$GATE_LOG"; then
        echo "  ⚠ TEST GATE SKIPPED: '${CMD}' could not run (runner not installed)."
        echo "    Completion is self-reported for this build."
        sed -n '1,5p' "$GATE_LOG" | sed 's/^/      /'
        return 0
    fi

    if [ $GATE_EXIT -eq 124 ]; then
        echo "  ❌ Test gate TIMED OUT after ${TEST_GATE_TIMEOUT}s"
    else
        echo "  ❌ Test gate FAILED (exit ${GATE_EXIT})"
    fi

    {
        echo ""
        echo "## Test gate failure — iteration ${ITERATION} ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
        echo "Command: \`${CMD}\` exited ${GATE_EXIT}."
        echo "This is the harness running your tests, not your own assessment."
        echo '```'
        tail -c 2000 "$GATE_LOG"
        echo '```'
    } >> /workspace/.cline_context/.build_issues.md
    echo "  ↳ Failure recorded in .build_issues.md for the next iteration."
    return 1
}

# Cline's system prompt states that every user message arrives wrapped in a
# <user_input mode="..."> tag and that the newest message's mode governs. In a
# headless run it sends the prompt bare, so the model goes looking for an
# attribute that is not there. The only concrete mode value left in its context is
# the word "plan" from that very explanation, so it concludes plan mode and
# refuses to edit - the build phase becomes an analysis it never acts on.
#
# Measured: the act-mode system prompt is 4253 chars with 25 tools, plan mode is
# 6014 with 24, so these runs were always in act mode. The mode was inferred, not
# imposed. Sending the tag back does not work either - Cline parses and strips its
# own wrapper - so the correction has to be prose that survives as message text.
act_mode() {
    printf '%s\n\n%s' \
        "[SESSION MODE: ACT] Implementation is allowed and expected in this session. No plan-mode constraint applies. Do not stop at analysis, do not ask to switch modes, and do not treat the absence of a user_input mode attribute as plan mode. Make the edits directly." \
        "$1"
}

generate_session_state() {
    local ITERATION=$1
    local STEP=$2
    local STATE_FILE="/workspace/.cline_context/.session_state.md"

    echo "# Session State (Auto-generated)" > "$STATE_FILE"
    echo "" >> "$STATE_FILE"
    echo "## Current Position" >> "$STATE_FILE"
    echo "- **Iteration**: ${ITERATION}/${MAX_ITERATIONS}" >> "$STATE_FILE"
    echo "- **Step**: ${STEP}" >> "$STATE_FILE"
    echo "- **Timestamp**: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$STATE_FILE"
    echo "" >> "$STATE_FILE"
    
    # Inject known issues if they exist
    if [ -f "/workspace/.cline_context/.build_issues.md" ]; then
        echo "## Known Issues (from previous steps)" >> "$STATE_FILE"
        tail -c "$SESSION_STATE_ISSUES_BYTES" /workspace/.cline_context/.build_issues.md >> "$STATE_FILE"
        echo "" >> "$STATE_FILE"
    fi

    # Inject quality audit if it exists
    if [ -f "/workspace/.cline_context/quality_audit.md" ]; then
        echo "## 🛡️ Architectural & Quality Critique" >> "$STATE_FILE"
        echo "> These notes represent the project's quality conscience. Address these critiques before implementation." >> "$STATE_FILE"
        tail -c "$SESSION_STATE_AUDIT_BYTES" /workspace/.cline_context/quality_audit.md >> "$STATE_FILE"
        echo "" >> "$STATE_FILE"
    fi

    # Inject analysis notes if agent wrote any
    if [ -f "/workspace/.cline_context/analysis_notes.md" ]; then
        echo "## Agent Discovery Notes" >> "$STATE_FILE"
        tail -c "$SESSION_STATE_NOTES_BYTES" /workspace/.cline_context/analysis_notes.md >> "$STATE_FILE"
        echo "" >> "$STATE_FILE"
    fi
    
    # Inject summaries from previous step logs.
    #
    # Assembled into a buffer first so the whole section can be byte-capped. The
    # per-line cut matters more than the line count: the grep matches on "✓",
    # which appears inside tool output as readily as in a summary, so a single
    # matched line can drag a multi-kilobyte payload into the agent's memory.
    #
    # Ordered by mtime, not by glob. The names sort by step before iteration
    # (build_log_iter_1, ..., safety_log_iter_1, ...), so alphabetical order puts
    # the oldest verify log after the newest build log - and the tail -c below
    # keeps whatever is last, which must be the most recent work.
    local SUMMARY_BUF
    SUMMARY_BUF=$(mktemp)
    local log
    while IFS= read -r log; do
        [ -f "$log" ] || continue
        echo "### $(basename "$log")" >> "$SUMMARY_BUF"
        grep -iE "(FINAL SUMMARY|attempt_completion|✓|✗|ERROR|TODO|BLOCKED)" "$log" 2>/dev/null \
            | tail -10 \
            | cut -c "1-${SESSION_STATE_LINE_CHARS}" >> "$SUMMARY_BUF" || true
        echo "" >> "$SUMMARY_BUF"
    done < <(ls -1tr /workspace/.cline_logs/*.txt 2>/dev/null)

    echo "## Previous Step Summaries" >> "$STATE_FILE"
    if [ "$(wc -c < "$SUMMARY_BUF")" -gt "$SESSION_STATE_SUMMARY_BYTES" ]; then
        echo "_(older step summaries elided - see .cline_logs/ for the full logs)_" >> "$STATE_FILE"
    fi
    tail -c "$SESSION_STATE_SUMMARY_BYTES" "$SUMMARY_BUF" >> "$STATE_FILE"
    rm -f "$SUMMARY_BUF"
}

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
echo "  Max retries:    ${CLINE_MAX_RETRIES}"
echo "  Phase timeouts: build ${BUILD_TIMEOUT}s (final ${FINAL_BUILD_TIMEOUT}s), verify ${VERIFY_TIMEOUT}s, safety ${SAFETY_TIMEOUT}s"
echo ""

# --- Phase 2 Setup: Auto-Auth for CLI ---
# Determine the API base URL for Cline (always use /v1 for OpenAI-compatible auth)
if [ "$CLINE_PROVIDER" = "ollama" ]; then
    CLINE_AUTH_URL="${CLINE_BASE_URL}/v1"
else
    CLINE_AUTH_URL="${CLINE_BASE_URL}/v1"
fi
echo "  🔑 Configuring provider (${CLINE_PROVIDER}) for Cline CLI..."

cline auth \
    -p openai-compatible \
    -k "dummy" \
    -m "$CLINE_MODEL" \
    -b "${CLINE_AUTH_URL}"

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

    # --- Re-plan Phase ---
    #
    # Before building again, check whether the plan still matches reality. Skipped
    # on the first iteration (no evidence yet) and on the last (its directive is
    # stabilization, and moving the target then guarantees unfinished work).
    if [ $ITERATION -gt 1 ] && [ $ITERATION -lt $MAX_ITERATIONS ]; then
        set +e
        PYTHONUNBUFFERED=1 python3 /app/distill.py --replan \
            "$REPLAN_GROWTH_BYTES" "$MAX_REPLANS"
        REPLAN_EXIT=$?
        set -e
        if [ $REPLAN_EXIT -ne 0 ]; then
            echo "  ⚠ Re-plan step exited ${REPLAN_EXIT}; continuing with the existing plan."
        fi
    fi

# --- Build Phase ---
    echo "  🔧 Running Cline (Build mode)..."
    generate_session_state "$ITERATION" "build"

    CURRENT_TIMEOUT="$BUILD_TIMEOUT"
    BUILD_MSG="IMPORTANT: First read '.cline_context/.session_state.md' to understand what has been done so far. Then read '.clinerules' and execute remaining implementation tasks."

    if [ $ITERATION -eq $MAX_ITERATIONS ]; then
        echo "  🚨 FINAL ROUND: Shifting to Stabilization and Debugging..."
        BUILD_MSG="IMPORTANT: First read '.cline_context/.session_state.md'. CRITICAL: This is the FINAL iteration (${ITERATION} of ${MAX_ITERATIONS}). Your directive is now STABILIZATION. Revisit any TODOs, uncommented code, or failing tests. Fix the root causes of any remaining bugs."
        CURRENT_TIMEOUT="$FINAL_BUILD_TIMEOUT"
    elif [ $ITERATION -gt 1 ]; then
        BUILD_MSG="IMPORTANT: First read '.cline_context/.session_state.md' to recover your memory. Continue building the project. Review what was done in the previous iteration, fix any issues, and complete remaining tasks from .clinerules. This is iteration ${ITERATION} of ${MAX_ITERATIONS}. Remember: keep momentum and don't get stuck on one bug."
    fi

    BUILD_MSG=$(act_mode "$BUILD_MSG")
    set +e
    export CLINE_MODEL CURRENT_TIMEOUT BUILD_MSG CLINE_MAX_RETRIES
    script -q -e -c 'cline -v --auto-approve true \
        -P openai-compatible \
        -m "$CLINE_MODEL" \
        --timeout "$CURRENT_TIMEOUT" \
        --retries "$CLINE_MAX_RETRIES" \
        "$BUILD_MSG"' \
        "/workspace/.cline_logs/build_log_iter_${ITERATION}.txt"
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
    generate_session_state "$ITERATION" "verify"

    VERIFY_MSG="IMPORTANT: First read '.cline_context/.session_state.md' to understand what has been done so far. 
    [STABILITY PROTOCOL]: Do not start by reading the entire codebase. Run the project's primary test suite immediately (check the TOOLCHAIN block in .clinerules for the correct command). Use the failures to identify which files actually need inspection.
    1) Verify all tasks from .clinerules are implemented and the code runs as expected. 
    2) [QUALITY RECONCILIATION]: Read '.cline_context/quality_audit.md'. If current implementation has resolved any of these critiques, REMOVE them from the file.
    3) MUST DO: Create a 'README.md' file that clearly explains what the project is and EXACTLY how to run it. 
    4) Check if '.cline_context/.build_issues.md' already exists. If it does, READ it. Cross off or remove the issues that were fixed in this iteration.
    5) If the app is 100% working, safe, and has a README, create a file named '.build_complete' in the root directory containing 'VERIFIED'.
    6) CONTINUITY: Watch for '[STABILITY MONITOR]' markers in history. If a turn was cut off, do not re-read from the beginning; pick up exactly where you left off.
    7) Before testing, if a port is in use, YOU MUST ONLY use 'npx kill-port <portnumber>' to free it."
    VERIFY_MSG=$(act_mode "$VERIFY_MSG")
    set +e
    export CLINE_MODEL VERIFY_MSG CLINE_MAX_RETRIES VERIFY_TIMEOUT
    script -q -e -c 'cline -v --auto-approve true \
        -P openai-compatible \
        -m "$CLINE_MODEL" \
        --timeout "$VERIFY_TIMEOUT" \
        --retries "$CLINE_MAX_RETRIES" \
        "$VERIFY_MSG"' \
        "/workspace/.cline_logs/verify_log_iter_${ITERATION}.txt"
    set -e

    # --- Safety Phase ---
    echo "  🛡️ Running Cline (Safety audit)..."
    generate_session_state "$ITERATION" "safety"

    SAFETY_MSG="IMPORTANT: First read '.cline_context/.session_state.md' to understand what has been done so far.
    [STABILITY PROTOCOL]: Do not perform an exhaustive top-to-bottom audit of every file. Use 'searchFiles' (grep) to hunt for hazardous patterns like 'unsafe', 'shell', or hardcoded paths. Only deep-dive into the specific files and lines that flag these risks.
    1) Audit for: Input validation, Path traversal, Hardcoded secrets, Injection risks, Infinite loops, Missing error handling. 
    2) If you find critical issues, attempt to FIX THEM DIRECTLY in the code. 
    3) If you fix them or the code is already safe, append 'SAFE' to the '.build_complete' file. 
    4) CONTINUITY: Watch for '[STABILITY MONITOR]' markers in history. If a turn was cut off, do not re-read from the beginning; pick up exactly where you left off.
    5) Before testing, if a port is in use, YOU MUST ONLY use 'npx kill-port <portnumber>' to free it."
    SAFETY_MSG=$(act_mode "$SAFETY_MSG")
    set +e
    export CLINE_MODEL SAFETY_MSG CLINE_MAX_RETRIES SAFETY_TIMEOUT
    script -q -e -c 'cline -v --auto-approve true \
        -P openai-compatible \
        -m "$CLINE_MODEL" \
        --timeout "$SAFETY_TIMEOUT" \
        --retries "$CLINE_MAX_RETRIES" \
        "$SAFETY_MSG"' \
        "/workspace/.cline_logs/safety_log_iter_${ITERATION}.txt"
    set -e

    # --- Check completion ---
    if [ -f "/workspace/.build_complete" ]; then
        COMPLETE_CONTENT=$(cat /workspace/.build_complete)
        if echo "$COMPLETE_CONTENT" | grep -q "VERIFIED" && echo "$COMPLETE_CONTENT" | grep -q "SAFE"; then
            # The agent's claim is necessary but not sufficient. Only the test
            # gate can turn it into a fact.
            if run_test_gate "$ITERATION"; then
                echo ""
                echo "  ✅ Build VERIFIED, SAFE and TESTS PASSING on iteration ${ITERATION}"
                BUILD_COMPLETE=true
            else
                echo "  ⚠ Agent reported complete, but the test gate failed — continuing."
                rm -f /workspace/.build_complete
            fi
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
FINAL_SIZE=$(du -sm "${DU_EXCLUDES[@]}" /workspace 2>/dev/null | cut -f1)
echo "   Final workspace size: ${FINAL_SIZE} MB (source only)"
echo "========================================"