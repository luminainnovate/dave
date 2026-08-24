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
    procps \
    git \
    tree \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (use --break-system-packages for Debian 12+)
RUN pip3 install --break-system-packages httpx

# Install cline cli globally
RUN npm install -g cline

# Writable HOME for an arbitrary unprivileged uid.
#
# The container ran as root purely by default, and every file it wrote into the
# mounted workspace came out root-owned - which locked the user out of their own
# project tree. Running as their uid fixes that, but only if HOME is writable:
# cline stores provider settings under CLINE_DIR, npm wants $HOME/.npm, and git
# wants somewhere for its config. /root is 0700 and unreachable to anyone else.
#
# Mode 0777 rather than a chown to a fixed uid, so BUILDER_UID can be anything
# without a rebuild. This is an ephemeral single-tenant container whose only
# secret is a dummy API key, so the looser mode buys portability cheaply.
RUN mkdir -p /home/builder/.config/Cline \
    && chmod -R 0777 /home/builder
ENV HOME=/home/builder \
    CLINE_DIR=/home/builder/.config/Cline \
    NPM_CONFIG_CACHE=/home/builder/.npm

# Cline's bundled AI SDK emits two DeprecationWarnings per request
# ("providerOptions key 'openai-compatible'. Use 'openaiCompatible'"). It is
# internal to Cline - reproduced against a stub HTTP server with no model
# involved, and unchanged in 3.0.57 - so no config in this repo can stop it at
# the source. Silencing it matters because the build log is how a failed
# iteration gets diagnosed, and two warnings per request bury the one line that
# says what actually went wrong.
#
# NODE_NO_WARNINGS rather than the narrower NODE_OPTIONS=--no-deprecation: the
# CLI is a bun-compiled single-file binary and does not honour NODE_OPTIONS.
# Measured in fresh containers, same prompt, stub server: baseline 2 warnings /
# 937 bytes, --no-deprecation 2 warnings / 937 bytes, NODE_NO_WARNINGS=1
# 0 warnings / 2 bytes. Only the blunt one works here.
ENV NODE_NO_WARNINGS=1

# Set up application directory
WORKDIR /app

# Copy pipeline scripts
COPY entrypoint.sh /app/entrypoint.sh
COPY distill.py /app/distill.py
# Baked in so the image works without the compose mount; the mount overrides
# these at runtime so prompts can be edited without a rebuild.
COPY prompts /app/prompts
RUN chmod +x /app/entrypoint.sh

# Workspace is mounted at runtime
WORKDIR /workspace

ENTRYPOINT ["/app/entrypoint.sh"]
