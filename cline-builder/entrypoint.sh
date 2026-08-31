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
#   CLINE_CTX          - Context window the Cline agent is EXPECTED to run at.
#                        Not a setting. Cline talks to Ollama's OpenAI-compatible
#                        /v1 endpoint, which has no num_ctx option, so the real
#                        window is the server's OLLAMA_CONTEXT_LENGTH. This value
#                        is asserted against the running server before Phase 2 and
#                        the build aborts on mismatch (see assert_cline_ctx).
#   CLINERULES_PATH    - Output path for .clinerules
# =============================================================================

# Set the global config directory so ALL cline commands use it.
# Derived from HOME rather than hard-coded to /root, so the container can run as
# an unprivileged uid: /root is 0700 and every `cline` call would fail to write
# its provider settings. The Dockerfile sets both; this keeps an explicit
# override working and still does the right thing if someone runs as root.
export HOME="${HOME:-/root}"
export CLINE_DIR="${CLINE_DIR:-${HOME}/.config/Cline}"

# The pre-build snapshot commit needs an identity, and an unprivileged uid with
# no global gitconfig has none - the commit fails silently (it is 2>/dev/null),
# leaving no rollback point. Only set what the operator has not.
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-cline-builder}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-cline-builder@localhost}"
export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-$GIT_AUTHOR_NAME}"
export GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-$GIT_AUTHOR_EMAIL}"

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
# Must match orchestrator.py's CLINE_CTX. The two defaults drifting apart is how
# the banner ends up reporting a window nobody is running at.
CLINE_CTX="${CLINE_CTX:-163840}"
# 'full' runs distillation then the Cline build cycle.
# 'distill_only' stops at the review gate. Defaulted because set -u is active.
PIPELINE_MODE="${PIPELINE_MODE:-full}"
# Which role occupies distillation pass 1: 'architect' designs, 'bugfix'
# diagnoses. distill.py validates the value and owns the behaviour; this is here
# only so the banner and the review gate can name the right document.
DISTILL_DESIGN_PASS="${DISTILL_DESIGN_PASS:-architect}"

echo "========================================"
echo "🔨 Cline Builder Pipeline"
echo "========================================"
echo "  Config:       ${CONFIG_PATH}"
echo "  Conversation: ${CONVERSATION_FILE}"
echo "  Ollama:       ${OLLAMA_HOST}"
echo "  Distill CTX:  ${EXPERT_CTX}"
echo "  Cline CTX:    ${CLINE_CTX} (expected - asserted against the server before the build cycle)"
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
REVIEW_TIMEOUT=$(jq -r '.limits.review_timeout_secs // 1800' "$CONFIG_PATH")
# Master switch for the review phase. Off means the loop runs build → verify →
# safety exactly as it did before the phase existed.
#
# NOT written with `// true`: jq's alternative operator treats `false` as absent
# the same way it treats `null`, so `.limits.review_enabled // true` yields true
# for an explicit `"review_enabled": false` - the switch would be unturnoffable.
# Every other default in this file is a number or a string, where `//` is safe.
# An env var wins over the config, so a single build can be run without review
# via `-e REVIEW_ENABLED=false` without editing the mounted config.
REVIEW_ENABLED="${REVIEW_ENABLED:-$(jq -r 'if .limits.review_enabled == null then true else .limits.review_enabled end' "$CONFIG_PATH")}"
case "$REVIEW_ENABLED" in
    true|1|yes|on)   REVIEW_ENABLED=true ;;
    false|0|no|off)  REVIEW_ENABLED=false ;;
    *)
        echo "  ⚠ Unknown review_enabled '${REVIEW_ENABLED}' (expected true or false); defaulting to true."
        REVIEW_ENABLED=true
        ;;
esac
# Reasoning effort for the review phase, passed to the Cline CLI's --thinking.
# Qwen Code's `/review --effort` is the same idea under a different name, but that
# is a different CLI and is not installed in this image; --thinking is how this
# pipeline expresses it. An unrecognised value would be rejected by the CLI and
# cost the whole phase, so it is validated here and the flag is omitted entirely
# when unset - which leaves the provider default, exactly as before this existed.
REVIEW_THINKING=$(jq -r '.limits.review_thinking_level // "medium"' "$CONFIG_PATH")
#
# Held as a string rather than an array because the cline call runs inside
# `script -c '...'`, which re-parses its argument in a fresh shell that no array
# can be exported into. The value is whitelisted immediately above, so the word
# splitting that expands it there acts on two known-safe tokens.
case "$REVIEW_THINKING" in
    none|low|medium|high|xhigh) REVIEW_THINKING_ARG="--thinking ${REVIEW_THINKING}" ;;
    "")                         REVIEW_THINKING_ARG="" ;;
    *)
        echo "  ⚠ Unknown review_thinking_level '${REVIEW_THINKING}' (expected none|low|medium|high|xhigh); falling back to medium."
        REVIEW_THINKING="medium"
        REVIEW_THINKING_ARG="--thinking medium"
        ;;
esac
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
# The operational policy the build agent runs under. The config value is a PATH
# relative to the config, not the text - jq returns "prompts/cline_startup.md",
# and for a long time that string was assigned here and then never used at all.
# The effect was that the whole TDD/anti-loop policy was never delivered: the
# build agent had no instruction against reading whole files, so it spent entire
# iteration budgets on read_files and never wrote a line.
CLINE_STARTUP_REF=$(jq -r '.cline_startup_message // ""' "$CONFIG_PATH")
CLINE_STARTUP="Read .clinerules and execute all tasks."
if [ -n "$CLINE_STARTUP_REF" ]; then
    STARTUP_PATH="$(dirname "$CONFIG_PATH")/${CLINE_STARTUP_REF}"
    if [ -f "$STARTUP_PATH" ]; then
        # The file holds a JSON string literal, so jq decodes the \n escapes into
        # real newlines. It ends with a trailing comma (it was lifted out of a
        # JSON object), on which jq prints the decoded value and THEN exits 5 -
        # so strip the comma first, and decide the fallback on whether anything
        # was decoded rather than on the exit code. Chaining `|| cat` instead
        # appends the raw escaped source to the decoded text, handing the agent
        # the whole policy twice.
        STARTUP_DECODED=$(sed 's/,[[:space:]]*$//' "$STARTUP_PATH" | jq -r '.' 2>/dev/null || true)
        if [ -n "$STARTUP_DECODED" ]; then
            CLINE_STARTUP="$STARTUP_DECODED"
        else
            # Plain Markdown rather than a JSON string; take it verbatim.
            CLINE_STARTUP=$(cat "$STARTUP_PATH")
        fi
        echo "  📜 Operational policy loaded (${#CLINE_STARTUP} chars) from ${STARTUP_PATH}"
    elif [ "${#CLINE_STARTUP_REF}" -gt 60 ]; then
        # Not a path - treat the config value itself as the policy text.
        CLINE_STARTUP="$CLINE_STARTUP_REF"
    else
        echo "  ⚠ Operational policy not found at ${STARTUP_PATH}; using the default one-liner."
    fi
fi

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

# --- Review Scope ---
# The review phase is handed the files the build phase actually wrote, not "the
# codebase". A 27b model on a 64k window that starts a review by exploring spends
# the whole budget exploring - the same failure the STABILITY PROTOCOL exists to
# prevent - and a review of untouched code re-reports findings the audit file
# already holds. The marker is touched immediately before Cline starts building,
# so `-newer` means precisely "written during this iteration's build phase".
#
# Prune list mirrors DU_EXCLUDES above: metadata and regenerable artifacts are
# never worth a review turn. The cap is a context guard, not a correctness one -
# a build that rewrote 200 files cannot be reviewed in one turn anyway, and the
# newest 25 are where this iteration's work is.
REVIEW_MARKER="/workspace/.cline_context/.review_marker"
REVIEW_MAX_FILES=25

changed_sources() {
    [ -f "$REVIEW_MARKER" ] || return 0
    find /workspace \
        \( -name .git -o -name node_modules -o -name .venv -o -name venv \
           -o -name __pycache__ -o -name .pytest_cache -o -name .knowledge_base \
           -o -name .cline_logs -o -name .cline_context -o -name dist \
           -o -name build -o -name .next -o -name target \) -prune -o \
        -type f -newer "$REVIEW_MARKER" -print 2>/dev/null \
        | grep -vE '\.(log|lock|pyc|map|min\.js|min\.css)$' \
        | grep -vE '/(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|\.gitignore|\.build_complete)$' \
        | sed 's|^/workspace/||' \
        | sort \
        | head -n "$REVIEW_MAX_FILES"
}

# =============================================================================
# PHASE 1: Multi-Pass Context Distillation
# =============================================================================
echo ""
echo "========================================="
echo "📚 Phase 1: Context Distillation (4-pass)"
echo "   Design pass: ${DISTILL_DESIGN_PASS}"
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
elif [ $DISTILL_EXIT -eq 4 ]; then
    echo "✗ STOPPED: the bug was never reproduced, so the diagnosis is unverified."
    echo "  Nothing was built and .clinerules was not written."
    echo "  Review /workspace/.cline_context/distill_bugfix.md, or re-run !bugfix"
    echo "  with a sharper description of the symptom."
    exit 1
elif [ $DISTILL_EXIT -ne 0 ]; then
    echo "✗ FATAL: Distillation failed (exit code ${DISTILL_EXIT})"
    exit 1
fi

# Review gate: stop here so the plan can be inspected (and edited) before any
# code is written. The approve run re-enters with DISTILL_RESUME=1.
if [ "$PIPELINE_MODE" = "distill_only" ]; then
    echo ""
    echo "========================================="
    echo "⏸️  Review gate: stopping before Phase 2"
    echo "   Review: /workspace/.cline_context/distill_${DISTILL_DESIGN_PASS}.md"
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
    #
    # The connection clauses are the same principle one layer out. Now that the
    # gate can reach a sibling package's suite, it can reach a suite that needs a
    # database - and this container talks to the host over the docker bridge,
    # where a Postgres published on 127.0.0.1 is not listening. That is an
    # environment fault: the agent has no docker CLI and no socket, so no edit it
    # can make will fix it, and calling it a test failure would wedge the build
    # loop against something outside the code. distill.py classifies the same
    # strings the same way for the bugfix reproduction gate.
    if [ $GATE_EXIT -eq 127 ] || grep -qiE "no module named pytest|command not found|could not determine executable|npm error|cannot find module" "$GATE_LOG"; then
        echo "  ⚠ TEST GATE SKIPPED: '${CMD}' could not run (runner not installed)."
        echo "    Completion is self-reported for this build."
        sed -n '1,5p' "$GATE_LOG" | sed 's/^/      /'
        return 0
    fi
    if grep -qiE "econnrefused|connection refused|could not connect to server|getaddrinfo" "$GATE_LOG"; then
        echo "  ⚠ TEST GATE SKIPPED: '${CMD}' could not reach a service it needs."
        echo "    This is the environment, not the code — nothing the build can edit"
        echo "    will fix it. Completion is self-reported for this build."
        echo "    A database on the host's loopback is unreachable from this container;"
        echo "    see the DATABASE_URL note in docker-compose.yml."
        grep -iE "econnrefused|connection refused|could not connect to server" "$GATE_LOG" \
            | head -n 3 | sed 's/^/      /'
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
if [ "$REVIEW_ENABLED" = "true" ]; then
    echo "  Review phase:   ON (effort ${REVIEW_THINKING:-provider default}, ${REVIEW_TIMEOUT}s)"
else
    echo "  Review phase:   OFF (limits.review_enabled = false)"
fi
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

# --- Phase 2 Setup: Context Window Assertion ---
#
# CLINE_CTX cannot be *applied*. Cline authenticates as openai-compatible against
# ${CLINE_BASE_URL}/v1, and Ollama's OpenAI-compatible endpoint accepts no num_ctx
# option, so the agent's real window is whatever the Ollama service was started
# with (OLLAMA_CONTEXT_LENGTH), clamped by the model's own trained maximum.
#
# So we assert instead of configure: warm the model through the exact door Cline
# uses, ask the server what window it actually gave us, and abort on disagreement.
# Running a 27B agent at 4k when the plan was budgeted for 64k produces silent
# mid-build truncation that looks like the model being stupid, not like a config
# error - the same failure mode BudgetInfeasible exists to prevent in distill.py.
# What the Cline CLI believes an openai-compatible model's window is. Not a
# preference and not configurable: the value is hardcoded in the CLI binary
# (`openai-compatible") t.contextWindow=128000, t.maxInputTokens=128000`) and no
# flag, env var or providers.json field overrides it.
#
# It matters because Cline's context compaction (--compaction, default agentic)
# fires against this number. Serve it less and compaction never triggers before
# the server's real wall: the conversation grows until the slot is full, the
# turn dies on finish_reason=length, and the turn before that emits a tool call
# cut off mid-argument - which surfaces as "invalid JSON arguments" and looks
# like a model defect rather than a window defect.
CLINE_ASSUMED_CTX=128000

# Bring the build model up before Phase 2.
#
# Nothing else does. The llama.cpp server is started lazily by whoever first
# asks the orchestrator to load a model, and in a normal run that is distill's
# preload. With DISTILL_RESUME=1 and every pass reused from disk, distill prints
# "no model needed" and never makes that call - so Phase 2 pointed Cline at a
# port with nothing behind it and got ConnectionRefused on its first request,
# after the /props assertion had already shrugged at the same dead socket.
#
# /internal/model/load blocks until the server answers /health, so returning
# from here means the model is genuinely ready. It is idempotent: an already
# running server is reported ready and returns immediately.
ensure_cline_model_loaded() {
    [ "$CLINE_PROVIDER" = "llamacpp" ] || return 0

    echo "  ⏳ Loading ${CLINE_MODEL} via the orchestrator..."
    local body http
    body=$(jq -nc --arg m "$CLINE_MODEL" --arg p "$CLINE_PROVIDER" --arg b "$CLINE_BASE_URL" \
           '{model: $m, provider: $p, base_url: $b}')
    # Generous timeout: a cold 27B at 128k is a ~17GB read plus KV allocation,
    # and the orchestrator serialises loads behind its GPU lock.
    # No `|| echo 000` fallback: curl already writes 000 to stdout on a
    # connection failure, and the fallback appended a second one ("000000").
    http=$(curl -s -m 600 -o /tmp/model_load.json -w '%{http_code}' \
           -X POST "${ORCHESTRATOR_URL}/internal/model/load" \
           -H "Content-Type: application/json" -d "$body" 2>/dev/null)
    http="${http:-000}"

    if [ "$http" != "200" ]; then
        echo ""
        echo "  ✗ FATAL: the orchestrator could not load ${CLINE_MODEL} (HTTP ${http})."
        [ -s /tmp/model_load.json ] && echo "    $(head -c 300 /tmp/model_load.json)"
        echo "    Cline would fail on its first request anyway; aborting with the cause named."
        return 1
    fi
    echo "  ✓ Model reported ready by the orchestrator"
    return 0
}

# Read the window actually in force from a llama.cpp server.
# /props reports it at .default_generation_settings.n_ctx (verified against the
# running build); with -np 1 that is the whole window rather than a slot's share.
#
# Sets PROPS_HTTP and PROPS_CTX rather than returning one string, because "the
# server is not there" and "the server is there but did not report n_ctx" need
# different verdicts and a bare empty string cannot tell them apart.
llamacpp_props() {
    PROPS_HTTP=$(curl -s -m 30 -o /tmp/props.json -w '%{http_code}' \
                 "${CLINE_BASE_URL}/props" 2>/dev/null)
    PROPS_HTTP="${PROPS_HTTP:-000}"
    PROPS_CTX=$(jq -r '.default_generation_settings.n_ctx // .n_ctx // empty' \
                /tmp/props.json 2>/dev/null || echo "")
}

assert_cline_ctx() {
    if [ "$CLINE_PROVIDER" = "llamacpp" ]; then
        echo "  📏 Verifying context window (Cline assumes ${CLINE_ASSUMED_CTX})..."

        local actual
        llamacpp_props
        actual="$PROPS_CTX"

        # An unreachable server is fatal, not a warning. Continuing here buys
        # nothing: Cline's very next request dies on ConnectionRefused, and the
        # soft warning that preceded it reads like an unrelated quibble about
        # metadata rather than "the model is not running".
        if [ "$PROPS_HTTP" != "200" ]; then
            echo ""
            echo "  ✗ FATAL: no llama.cpp server answering at ${CLINE_BASE_URL}/props"
            echo "      HTTP status: ${PROPS_HTTP} (000 = nothing listening)"
            echo ""
            echo "    The build agent has no model to talk to. Check that the orchestrator"
            echo "    is running and that its VRAM sweep has not just killed the server."
            return 1
        fi

        if ! [[ "$actual" =~ ^[0-9]+$ ]]; then
            echo "  ⚠ WARNING: ${CLINE_BASE_URL}/props answered but reported no n_ctx."
            echo "    Expected window ${CLINE_CTX} is UNVERIFIED - the build continues, but if"
            echo "    it dies mid-iteration on 'maximum output token limit', this is the first"
            echo "    thing to check."
            CLINE_CTX_ACTUAL="unverified"
            return 0
        fi

        if [ "$actual" -lt "$CLINE_ASSUMED_CTX" ]; then
            echo ""
            echo "  ✗ FATAL: the server's window is smaller than Cline assumes."
            echo "      Cline assumes (hardcoded): ${CLINE_ASSUMED_CTX}"
            echo "      actually in force:         ${actual}"
            echo ""
            echo "    Cline compacts against ${CLINE_ASSUMED_CTX}, so it will not compact before"
            echo "    it runs off the end of ${actual}. The build would spend its iteration"
            echo "    budget and die on 'maximum output token limit' with nothing written."
            echo "    Raise LLAMACPP_SERVER_CTX in orchestrator.py to at least"
            echo "    ${CLINE_ASSUMED_CTX} and restart the orchestrator. Mind the KV cost:"
            echo "    128k needs --cache-type-k/v q8_0 to fit -ngl all on 24GB."
            return 1
        fi

        if [ "$actual" -ne "$CLINE_CTX" ]; then
            echo "  ⚠ Cline CTX: server reports ${actual}, CLINE_CTX says ${CLINE_CTX}."
            echo "    Above Cline's assumption either way, so the build proceeds - but the"
            echo "    two should be brought back into agreement."
        fi

        CLINE_CTX_ACTUAL="$actual"
        echo "  ✓ Cline CTX: ${actual} tokens confirmed by the llama.cpp server"
        return 0
    fi

    if [ "$CLINE_PROVIDER" != "ollama" ]; then
        echo "  ℹ Cline CTX: assertion skipped (provider '${CLINE_PROVIDER}' is neither Ollama"
        echo "    nor llama.cpp); expected window ${CLINE_CTX} is unverified."
        return 0
    fi

    echo "  📏 Verifying context window (expected ${CLINE_CTX})..."

    # Warm through /v1 with no num_ctx, exactly as Cline will. This forces Ollama
    # to (re)instantiate the runner at its default window, so what /api/ps reports
    # afterwards is the window the build will actually run at - not one left over
    # from distillation, which loads the same model with an explicit num_ctx.
    curl -sf -m 300 -X POST "${CLINE_AUTH_URL}/chat/completions" \
        -H "Content-Type: application/json" \
        -d "$(jq -nc --arg m "$CLINE_MODEL" \
              '{model: $m, messages: [{role: "user", content: "hi"}], max_tokens: 1}')" \
        > /dev/null 2>&1 || {
        echo "  ✗ FATAL: could not reach ${CLINE_AUTH_URL}/chat/completions to warm ${CLINE_MODEL}."
        echo "    Cline would fail on its first request anyway; aborting now with the cause named."
        return 1
    }

    # Ollama reports the loaded runner's window on /api/ps. Older builds omit the
    # field; treat that as unverifiable rather than as a mismatch.
    local ps_json actual
    ps_json=$(curl -sf -m 30 "${CLINE_BASE_URL}/api/ps" 2>/dev/null || echo "")
    actual=$(printf '%s' "$ps_json" | jq -r --arg m "$CLINE_MODEL" '
        [.models[]? | select(.model == $m or .name == $m
                             or .model == ($m + ":latest") or .name == ($m + ":latest"))
                    | .context_length // empty] | first // empty' 2>/dev/null || echo "")

    if ! [[ "$actual" =~ ^[0-9]+$ ]]; then
        echo "  ⚠ WARNING: ${CLINE_BASE_URL}/api/ps did not report a context_length for"
        echo "    ${CLINE_MODEL} (Ollama too old, or the model unloaded immediately)."
        echo "    Expected window ${CLINE_CTX} is UNVERIFIED - the build continues, but if it"
        echo "    truncates mid-iteration, check the server's OLLAMA_CONTEXT_LENGTH first."
        CLINE_CTX_ACTUAL="unverified"
        return 0
    fi

    if [ "$actual" -ne "$CLINE_CTX" ]; then
        echo ""
        echo "  ✗ FATAL: context window mismatch."
        echo "      expected (CLINE_CTX):  ${CLINE_CTX}"
        echo "      actually in force:     ${actual}"
        echo ""
        echo "    The build agent would run at ${actual} tokens while the distilled plan was"
        echo "    budgeted for ${CLINE_CTX}. CLINE_CTX cannot fix this on its own - it is an"
        echo "    expectation, not a setting. Change the Ollama service's OLLAMA_CONTEXT_LENGTH"
        echo "    to ${CLINE_CTX} and restart it (or lower CLINE_CTX in orchestrator.py to"
        echo "    ${actual} if the smaller window is what you want). Keep EXPERT_CTX, DISTILL_CTX,"
        echo "    CLINE_CTX and OLLAMA_CONTEXT_LENGTH in agreement."
        return 1
    fi

    CLINE_CTX_ACTUAL="$actual"
    echo "  ✓ Cline CTX: ${actual} tokens confirmed by the Ollama server"
    return 0
}

CLINE_CTX_ACTUAL="unverified"
# Order matters: bring the model up, then measure it. Asserting first only ever
# measured whatever the previous phase happened to leave running.
if ! ensure_cline_model_loaded; then
    exit 1
fi
if ! assert_cline_ctx; then
    exit 1
fi

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

    # Verify and safety each carry their own [STABILITY PROTOCOL]; build was the
    # only phase without one, and it is the phase with the largest surface to
    # explore. Both halves matter: the policy says how to work, this says where to
    # start, and neither was reaching the agent before.
    BUILD_MSG="${CLINE_STARTUP}

[STABILITY PROTOCOL]: Do not begin by reading the codebase end to end. Use the SYMBOL SKELETON in '.clinerules' to navigate, and read only the specific files a task names, in ranges, never whole. If you have made three tool calls without an edit, stop reading and make the smallest edit that advances the next unchecked task.

${BUILD_MSG}"
    BUILD_MSG=$(act_mode "$BUILD_MSG")
    # Timestamp reference for changed_sources(). Must be the last thing before
    # Cline starts, or files the previous phase wrote leak into the review scope.
    touch "$REVIEW_MARKER"
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

    # --- Review Phase ---
    # Between building and verifying, because verify's job is "does it run and do
    # the tests pass" and safety's is "is it dangerous". Neither asks whether the
    # code is CORRECT beyond what its own tests assert, or whether it is fast or
    # clean. quality_audit.md existed for exactly those findings but nothing ever
    # systematically produced them - the build agent appended to it only when it
    # happened to notice something while working. This is the producer.
    REVIEW_FILES=""
    if [ "$REVIEW_ENABLED" = "true" ]; then
        REVIEW_FILES=$(changed_sources)
    fi
    if [ "$REVIEW_ENABLED" != "true" ]; then
        echo "  ⏭️  Review phase disabled (limits.review_enabled = false)."
    elif [ -z "$REVIEW_FILES" ]; then
        # A build turn that wrote nothing (timed out mid-read, or exhausted its
        # retries) leaves nothing to review, and a review with no scope degrades
        # into an unbounded codebase crawl that costs a full timeout.
        echo "  ⏭️  Skipping review: the build phase wrote no source files."
    else
        echo "  🔬 Running Cline (Review mode, effort=${REVIEW_THINKING:-provider default}) over $(printf '%s\n' "$REVIEW_FILES" | wc -l) changed file(s)..."
        generate_session_state "$ITERATION" "review"

        REVIEW_MSG="YOUR TASK THIS TURN: review the code that has just been written, for CORRECTNESS, SECURITY, PERFORMANCE and CODE QUALITY. That is the whole job — you are not building anything new this turn, and you are not finishing anything left unfinished.

    IMPORTANT: First read '.cline_context/.session_state.md' to understand what has been done so far.
    [STABILITY PROTOCOL]: Review ONLY the files listed below. Do not read the rest of the codebase, do not go hunting for related files, and do not run the test suite — the Verify phase runs it immediately after you. Read each listed file once, in ranges if it is long.

    FILES WRITTEN BY THIS ITERATION'S BUILD PHASE:
${REVIEW_FILES}

    Assess each file on four axes, in this priority order:
    1) CORRECTNESS: logic that does not do what '.clinerules' says it should — off-by-one, inverted condition, unhandled null/None, an unreachable branch, a swallowed error, an A§4 contract implemented with the wrong signature or return type.
    2) SECURITY: injection, path traversal, hardcoded secrets, missing input validation, unsafe deserialization. Flag everything you see; the Safety phase audits the whole project separately after Verify.
    3) PERFORMANCE: queries inside loops, N+1 access patterns, unbounded reads of files or result sets, work repeated per-iteration that belongs outside the loop, a column filtered on with no index.
    4) QUALITY: duplicated logic, dead code, TODOs the build phase left behind, names that contradict what the code does.

    WHAT TO DO WITH WHAT YOU FIND:
    5) Append EVERY finding to '.cline_context/quality_audit.md' using appendToFile, one bullet each, formatted exactly as:
       - [<CORRECTNESS|SECURITY|PERFORMANCE|QUALITY>] <path>:<line> — <the defect> — <the fix>
       A finding you do not write down is a finding that does not survive this iteration. Write them down BEFORE you start fixing anything.
    6) Then FIX DIRECTLY, in the code, only the CORRECTNESS and SECURITY findings. Leave PERFORMANCE and QUALITY findings in the audit file for a later iteration to pick up — do not refactor working code now, and do not rename anything.
    7) ANTI-LOOP: if a fix takes more than 2 attempts, revert your changes to that file, leave the finding in the audit file, and move to the next one.
    8) If you find nothing on a file, say so and move on. Do not invent findings to have something to report.
    9) Do NOT create or modify '.build_complete'. The Verify and Safety phases own completion.
    10) CONTINUITY: Watch for '[STABILITY MONITOR]' markers in history. If a turn was cut off, do not re-read from the beginning; pick up exactly where you left off."
        REVIEW_MSG=$(act_mode "$REVIEW_MSG")
        set +e
        export CLINE_MODEL REVIEW_MSG CLINE_MAX_RETRIES REVIEW_TIMEOUT REVIEW_THINKING_ARG
        script -q -e -c 'cline -v --auto-approve true \
            -P openai-compatible \
            -m "$CLINE_MODEL" \
            $REVIEW_THINKING_ARG \
            --timeout "$REVIEW_TIMEOUT" \
            --retries "$CLINE_MAX_RETRIES" \
            "$REVIEW_MSG"' \
            "/workspace/.cline_logs/review_log_iter_${ITERATION}.txt"
        set -e
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
echo "   Cline context window: ${CLINE_CTX_ACTUAL} (as reported by the server)"
echo "========================================"