#!/bin/bash

# Unified Workspace Start Script
# Orchestrates Docker, Orchestrator Proxy, and ComfyUI

echo "🚀 Launching Unified Local AI Workspace..."

# 1. Start Docker Stack
echo "🐳 Starting Docker (Open WebUI, SearXNG)..."
docker compose up -d

echo "🏗️  Ensuring Cline Builder is built..."
docker compose --profile build build cline-builder

# 2. Start Orchestrator Proxy
echo "🛡️ Clearing Port 8000 and Starting Orchestrator Proxy..."
sudo fuser -k 8000/tcp > /dev/null 2>&1 || true
# Check if venv exists
if [ -d "orchestrator-env" ]; then
    nohup ./orchestrator-env/bin/python3 orchestrator.py > orchestrator.log 2>&1 &
    echo "✅ Orchestrator running (See orchestrator.log)"
else
    echo "⚠️ Orchestrator environment not found. Please run setup_workspace.sh first."
fi

# 3. Start ComfyUI
echo "🎨 Clearing Port 8188 and Starting ComfyUI..."
sudo fuser -k 8188/tcp > /dev/null 2>&1
if [ -d "comfy-env" ] && [ -d "ComfyUI" ]; then
    cd ComfyUI
    nohup ../comfy-env/bin/python3 main.py --normalvram --listen 0.0.0.0 > ../comfyui.log 2>&1 &
    cd ..
    echo "✅ ComfyUI running (See comfyui.log)"
else
    echo "⚠️ ComfyUI environment not found. Please run setup_workspace.sh first."
fi

echo "--------------------------------------------------------"
echo "✅ All services triggered. Open WebUI: http://localhost:3000"
echo "--------------------------------------------------------"
