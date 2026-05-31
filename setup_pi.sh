#!/bin/bash

# =============================================================================
# br.ai.n Router Node — One-Time Setup for Raspberry Pi
# =============================================================================
# Installs all dependencies, pulls models, and optionally creates a
# systemd service for auto-start on boot.
#
# Usage: bash setup_pi.sh
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo "🧠 br.ai.n Router Node — Pi Setup"
echo "========================================"
echo ""

ERRORS=0

# --- 1. System Dependencies ---
echo "📦 [1/8] Installing system packages..."
sudo apt update -qq
sudo apt install -y wakeonlan openssh-client curl
if [ $? -eq 0 ]; then
    echo "✅ System packages installed."
else
    echo "⚠️  Some packages failed. Continuing..."
    ERRORS=$((ERRORS + 1))
fi
echo ""

# --- 2. Docker ---
echo "🐳 [2/8] Setting up Docker..."
if command -v docker &> /dev/null; then
    echo "✅ Docker already installed ($(docker --version 2>/dev/null))."
else
    echo "   Installing Docker via official script..."
    curl -fsSL https://get.docker.com | sh
    if [ $? -eq 0 ]; then
        echo "✅ Docker installed."
    else
        echo "❌ Docker installation failed."
        ERRORS=$((ERRORS + 1))
    fi
fi

# Ensure current user is in docker group
if ! groups | grep -q docker; then
    echo "   Adding $(whoami) to the docker group..."
    sudo usermod -aG docker "$(whoami)"
    echo "   ⚠️  Log out and back in for docker group to take effect."
    echo "   Then re-run this script."
fi
echo ""

# --- 3. Ollama ---
echo "🤖 [3/8] Setting up Ollama..."
if command -v ollama &> /dev/null; then
    echo "✅ Ollama already installed."
else
    echo "   Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
    if [ $? -eq 0 ]; then
        echo "✅ Ollama installed."
    else
        echo "❌ Ollama installation failed."
        ERRORS=$((ERRORS + 1))
    fi
fi
echo ""

# --- 4. Start Ollama & Pull Model ---
echo "📥 [4/8] Pulling router model..."

# Try systemd first, fall back to manual start
if systemctl is-active --quiet ollama 2>/dev/null; then
    echo "   Ollama service is running."
elif systemctl list-unit-files | grep -q ollama 2>/dev/null; then
    echo "   Starting Ollama service..."
    sudo systemctl enable ollama
    sudo systemctl start ollama
    sleep 3
else
    echo "   Ollama service not found. Starting manually..."
    ollama serve > /dev/null 2>&1 &
    OLLAMA_PID=$!
    sleep 5
fi

ollama pull qwen2.5:1.5b
if [ $? -eq 0 ]; then
    echo "✅ Model qwen2.5:1.5b ready."
else
    echo "❌ Failed to pull model. Is Ollama running?"
    ERRORS=$((ERRORS + 1))
fi

# Clean up manual ollama if we started it
if [ -n "$OLLAMA_PID" ]; then
    kill $OLLAMA_PID 2>/dev/null || true
fi
echo ""

# --- 5. Python Virtual Environment ---
echo "🐍 [5/8] Setting up Python environment..."
if [ -d "router-env" ]; then
    echo "   router-env already exists. Upgrading packages..."
else
    python3 -m venv router-env
    if [ $? -ne 0 ]; then
        echo "❌ Failed to create venv. Install with: sudo apt install python3-venv"
        ERRORS=$((ERRORS + 1))
    fi
fi

if [ -d "router-env" ]; then
    ./router-env/bin/pip install --upgrade pip -q
    ./router-env/bin/pip install fastapi uvicorn httpx python-dotenv -q
    echo "✅ Python environment ready."
else
    echo "❌ Cannot install Python packages without venv."
    ERRORS=$((ERRORS + 1))
fi
echo ""

# --- 6. Configuration (.env) ---
echo "📝 [6/8] Configuration..."
if [ -f ".env" ]; then
    echo "✅ .env already exists (skipping)."
else
    cp .env.example .env
    echo "   Created .env from template."
    echo ""
    echo "   ╔════════════════════════════════════════════════════╗"
    echo "   ║  ⚠️  IMPORTANT: Edit .env before starting!        ║"
    echo "   ║                                                    ║"
    echo "   ║  At minimum, set:                                  ║"
    echo "   ║    WAKER_TOKEN=<your desktop-waker secret token>   ║"
    echo "   ║    DESKTOP_IP=<your desktop LAN IP>                ║"
    echo "   ╚════════════════════════════════════════════════════╝"
fi
echo ""

# --- 7. Docker Images ---
echo "🐳 [7/8] Pulling Docker images..."
if command -v docker &> /dev/null; then
    # Try docker compose (v2) first, fall back to docker-compose (v1)
    if docker compose version > /dev/null 2>&1; then
        docker compose -f docker-compose.pi.yml pull
    elif command -v docker-compose &> /dev/null; then
        docker-compose -f docker-compose.pi.yml pull
    else
        echo "⚠️  Neither 'docker compose' nor 'docker-compose' found."
        echo "   You may need to log out/in for docker group access, then re-run."
        ERRORS=$((ERRORS + 1))
    fi

    if [ $? -eq 0 ]; then
        echo "✅ Docker images ready."
    fi
else
    echo "⚠️  Docker not available. Skipping image pull."
    echo "   Log out/in and re-run this script, or run:"
    echo "   docker compose -f docker-compose.pi.yml pull"
    ERRORS=$((ERRORS + 1))
fi
echo ""

# --- 8. Make Scripts Executable ---
chmod +x start_router.sh start_desktop.sh 2>/dev/null

# --- 9. Systemd Service (Optional) ---
echo "─────────────────────────────────────────"
read -p "🔄 [8/8] Install as systemd service (auto-start on boot)? [y/N] " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]]; then
    SERVICE_FILE="/etc/systemd/system/bob-router.service"

    sudo tee "$SERVICE_FILE" > /dev/null << SERVICEEOF
[Unit]
Description=Bob AI Router Node
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${SCRIPT_DIR}/router-env/bin/python3 ${SCRIPT_DIR}/router.py
Restart=on-failure
RestartSec=10
Environment=PATH=${SCRIPT_DIR}/router-env/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
SERVICEEOF

    sudo systemctl daemon-reload
    sudo systemctl enable bob-router
    echo "✅ Systemd service installed and enabled."
    echo "   Start:   sudo systemctl start bob-router"
    echo "   Logs:    journalctl -u bob-router -f"
    echo "   Stop:    sudo systemctl stop bob-router"
else
    echo "   Skipped. Start manually with: ./start_router.sh"
fi
echo ""

# --- 10. SSH Key Check ---
echo "🔑 Checking SSH access to desktop..."
# Read values from .env
DESKTOP_SSH_HOST=$(grep -E "^DESKTOP_SSH_HOST=" .env 2>/dev/null | cut -d= -f2)
DESKTOP_SSH_USER=$(grep -E "^DESKTOP_SSH_USER=" .env 2>/dev/null | cut -d= -f2)

# Fallbacks
if [ -z "$DESKTOP_SSH_HOST" ]; then
    DESKTOP_SSH_HOST=$(grep -E "^DESKTOP_IP=" .env 2>/dev/null | cut -d= -f2)
fi
if [ -z "$DESKTOP_SSH_HOST" ] || [ -z "$DESKTOP_SSH_USER" ]; then
    echo "⚠️  Missing DESKTOP_SSH_HOST, DESKTOP_IP, or DESKTOP_SSH_USER in .env"
    echo "   Skipping SSH access check. Please define them to verify SSH connectivity."
else
    if ssh -o ConnectTimeout=3 -o BatchMode=yes -o StrictHostKeyChecking=no "${DESKTOP_SSH_USER}@${DESKTOP_SSH_HOST}" "echo ok" > /dev/null 2>&1; then
        echo "✅ SSH access to ${DESKTOP_SSH_USER}@${DESKTOP_SSH_HOST} confirmed."
    else
        echo "⚠️  Cannot SSH into ${DESKTOP_SSH_USER}@${DESKTOP_SSH_HOST}"
        echo "   This is needed to start desktop services remotely."
        echo "   Set up passwordless SSH with:"
        echo "     ssh-copy-id ${DESKTOP_SSH_USER}@${DESKTOP_SSH_HOST}"
    fi
fi
echo ""

# --- Summary ---
echo "========================================"
if [ $ERRORS -eq 0 ]; then
    echo "✅ Setup complete! No errors."
else
    echo "⚠️  Setup complete with $ERRORS warning(s)."
    echo "   Fix the issues above, then re-run: bash setup_pi.sh"
fi
echo ""
echo "Next steps:"
echo "  1. Edit .env:  nano .env"
echo "  2. Start:      ./start_router.sh"
echo "  3. Test:       curl http://localhost:8001/health"
echo "========================================"
