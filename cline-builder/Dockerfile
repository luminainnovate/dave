# Multi-Agent Cline Builder
# Node 24 + Python 3 for running @cline/cli and distillation scripts
FROM node:24-slim

# Install Python 3 and system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    jq \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (use --break-system-packages for Debian 12+)
RUN pip3 install --break-system-packages httpx

# Install cline cli globally
RUN npm install -g cline

# Set up application directory
WORKDIR /app

# Copy pipeline scripts
COPY entrypoint.sh /app/entrypoint.sh
COPY distill.py /app/distill.py
RUN chmod +x /app/entrypoint.sh

# Workspace is mounted at runtime
WORKDIR /workspace

ENTRYPOINT ["/app/entrypoint.sh"]
