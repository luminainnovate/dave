#!/bin/bash
set -e

# Unified Local AI Workspace Installer Script
# Targets: WSL2 (Ubuntu)

echo "🚀 Starting Unified Local AI Workspace Setup..."

# 1. Ollama Models
echo "📥 Pulling Ollama Models (Orchestrator & Expert)..."
ollama pull qwen2.5:1.5b
ollama pull qwen3.5:27b

# 2. Docker Stack
echo "🐳 Deploying Docker Stack (SearXNG & Open WebUI)..."
docker compose up -d

# 3. Gatekeeper Proxy Setup
echo "🛡️ Clearing Port 8000 and Setting up Gatekeeper Proxy..."
sudo fuser -k 8000/tcp > /dev/null 2>&1
if [ ! -d "gatekeeper-env" ]; then
    python3 -m venv gatekeeper-env
fi
source gatekeeper-env/bin/activate
pip install fastapi uvicorn httpx python-multipart
# Use nohup to keep it running
nohup python3 gatekeeper.py > gatekeeper.log 2>&1 &
echo "✅ Gatekeeper is starting in the background (Port 8000)."
deactivate

# 4. ComfyUI Setup
echo "🎨 Setting up ComfyUI (This may take several minutes)..."
if [ ! -d "comfy-env" ]; then
    python3 -m venv comfy-env
fi
source comfy-env/bin/activate
if [ ! -d "ComfyUI" ]; then
    git clone https://github.com/comfyanonymous/ComfyUI.git
fi
cd ComfyUI
pip install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu130
pip install -r requirements.txt
# Launch ComfyUI with low VRAM settings
nohup python3 main.py --normalvram --listen 0.0.0.0 > ../comfyui.log 2>&1 &
echo "✅ ComfyUI is starting in the background (Port 8188)."
cd ..
deactivate

echo "--------------------------------------------------------"
echo "🎉 Setup Complete!"
echo "--------------------------------------------------------"
echo "🌐 Open WebUI: http://localhost:3000"
echo "🛡️ Gatekeeper Log: tail -f gatekeeper.log"
echo "🎨 ComfyUI Log: tail -f comfyui.log"
echo "--------------------------------------------------------"
echo "⚠️ REMINDER: Ensure you have opened your Windows Firewall port 3000"
echo "and configured mirrored networking in .wslconfig if accessing from LAN."
