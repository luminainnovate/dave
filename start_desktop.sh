#!/bin/bash

# Desktop Node - AI Services Startup
# Called remotely by the Pi router via SSH when heavy tasks arrive.
# Only starts the orchestrator + ComfyUI (no Docker — Open WebUI runs on the Pi).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🚀 Starting Desktop AI services..."

# 1. Start Orchestrator Proxy
if pgrep -f "orchestrator.py" > /dev/null 2>&1; then
    echo "✅ Orchestrator already running."
else
    echo "🛡️ Starting Orchestrator..."
    if [ -d "orchestrator-env" ]; then
        nohup ./orchestrator-env/bin/python3 orchestrator.py > orchestrator.log 2>&1 < /dev/null &
        echo "✅ Orchestrator started (PID: $!)"
    else
        echo "⚠️ orchestrator-env not found. Run setup_workspace.sh first."
    fi
fi

# 2. Start ComfyUI
if pgrep -f "ComfyUI/main.py" > /dev/null 2>&1; then
    echo "✅ ComfyUI already running."
else
    echo "🎨 Starting ComfyUI..."
    if [ -d "comfy-env" ] && [ -d "ComfyUI" ]; then
        cd ComfyUI
        nohup ../comfy-env/bin/python3 main.py --normalvram --listen 0.0.0.0 > ../comfyui.log 2>&1 < /dev/null &
        cd "$SCRIPT_DIR"
        echo "✅ ComfyUI started (PID: $!)"
    else
        echo "⚠️ ComfyUI environment not found."
    fi
fi

echo "✅ Desktop services startup complete."
