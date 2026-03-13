#!/bin/bash

# Unified Workspace Start Script
# Orchestrates Docker, Gatekeeper Proxy, and ComfyUI

echo "🚀 Launching Unified Local AI Workspace..."

# 1. Start Docker Stack
echo "🐳 Starting Docker (Ollama, SearXNG, Open WebUI)..."
docker compose up -d

# 2. Start Gatekeeper Proxy
echo "🛡️ Clearing Port 8000 and Starting Gatekeeper Proxy..."
sudo fuser -k 8000/tcp > /dev/null 2>&1
# Check if venv exists
if [ -d "gatekeeper-env" ]; then
    nohup ./gatekeeper-env/bin/python3 gatekeeper.py > gatekeeper.log 2>&1 &
    echo "✅ Gatekeeper running (See gatekeeper.log)"
else
    echo "⚠️ Gatekeeper environment not found. Please run setup_workspace.sh first."
fi

# 3. Start ComfyUI
echo "🎨 Starting ComfyUI..."
if [ -d "comfy-env" ] && [ -d "ComfyUI" ]; then
    cd ComfyUI
    nohup ../comfy-env/bin/python3 main.py --lowvram --listen 0.0.0.0 > ../comfyui.log 2>&1 &
    cd ..
    echo "✅ ComfyUI running (See comfyui.log)"
else
    echo "⚠️ ComfyUI environment not found. Please run setup_workspace.sh first."
fi

echo "--------------------------------------------------------"
echo "✅ All services triggered. Open WebUI: http://localhost:3000"
echo "--------------------------------------------------------"
