#!/bin/bash

# Raspberry Pi Router Node - Startup Script
# Starts Docker (Open WebUI + SearXNG) and the Router proxy.
# For first-time setup, run setup_pi.sh first.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🚀 Launching Router Node (Raspberry Pi)..."

# 1. Start Docker Stack (Open WebUI + SearXNG only)
if command -v docker &> /dev/null; then
    echo "🐳 Starting Docker (Open WebUI, SearXNG)..."
    if docker compose version > /dev/null 2>&1; then
        docker compose -f docker-compose.pi.yml up -d
    elif command -v docker-compose &> /dev/null; then
        docker-compose -f docker-compose.pi.yml up -d
    else
        echo "⚠️  docker compose not found. Skipping Docker services."
    fi
else
    echo "⚠️  Docker not installed. Skipping Docker services."
    echo "   Run setup_pi.sh or install Docker manually."
fi

# 2. Ensure Ollama is running
if command -v ollama &> /dev/null; then
    if systemctl is-active --quiet ollama 2>/dev/null; then
        echo "✅ Ollama is running."
    elif systemctl list-unit-files 2>/dev/null | grep -q ollama; then
        echo "🤖 Starting Ollama service..."
        sudo systemctl start ollama
        sleep 2
    else
        echo "🤖 Starting Ollama manually..."
        ollama serve > /dev/null 2>&1 &
        sleep 3
    fi
else
    echo "⚠️  Ollama not installed. Run setup_pi.sh first."
fi

# 3. Start Router Proxy
echo "🛡️ Starting Router on port 8001..."
pkill -f "python3 router.py" > /dev/null 2>&1 || true
fuser -k 8001/tcp > /dev/null 2>&1 || true
sleep 2

if [ -d "router-env" ]; then
    nohup ./router-env/bin/python3 router.py > router.log 2>&1 < /dev/null &
    echo "✅ Router running (PID: $!, see router.log)"
else
    echo "❌ router-env not found. Run setup_pi.sh first."
    exit 1
fi

# 4. Print access info
PI_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "──────────────────────────────────────────"
echo "✅ Router Node is live."
echo ""
echo "   Open WebUI:  http://${PI_IP}:3000"
echo "   Router API:  http://${PI_IP}:8001"
echo "   Health:      http://${PI_IP}:8001/health"
echo "──────────────────────────────────────────"
