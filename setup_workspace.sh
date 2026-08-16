#!/bin/bash
set -e

# Unified Local AI Workspace Installer Script
# Targets: WSL2 (Ubuntu)

echo "🚀 Starting Unified Local AI Workspace Setup..."

# 1. Ollama Models
echo "📥 Pulling Ollama Models (Orchestrator & Expert)..."
ollama pull qwen2.5:1.5b
ollama pull qwen3.8:27b

# 2. Docker Stack
echo "🐳 Deploying Docker Stack (SearXNG & Open WebUI)..."
docker compose up -d

# 3. Orchestrator Proxy Setup
echo "🛡️ Clearing Port 8000 and Setting up Orchestrator Proxy..."
sudo fuser -k 8000/tcp > /dev/null 2>&1 || true
if [ ! -f "orchestrator-env/bin/activate" ]; then
    rm -rf orchestrator-env
    python3 -m venv orchestrator-env
fi
source orchestrator-env/bin/activate
pip install fastapi uvicorn httpx python-multipart
# Use nohup to keep it running
nohup python3 orchestrator.py > orchestrator.log 2>&1 &
echo "✅ Orchestrator is starting in the background (Port 8000)."
deactivate

# 4. ComfyUI Setup
echo "🎨 Setting up ComfyUI (This may take several minutes)..."
if [ ! -f "comfy-env/bin/activate" ]; then
    rm -rf comfy-env
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
echo "🛡️ Orchestrator Log: tail -f orchestrator.log"
echo "🎨 ComfyUI Log: tail -f comfyui.log"
echo "--------------------------------------------------------"
echo "⚠️ REMINDER: Ensure you have opened your Windows Firewall port 3000"
echo "and configured mirrored networking in .wslconfig if accessing from LAN."
