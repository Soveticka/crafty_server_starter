FROM python:3.11-slim AS base

LABEL maintainer="Crafty Server Watcher"
LABEL description="Auto-hibernate and wake Minecraft servers via Crafty API v2"

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install PyYAML (only external dependency)
RUN pip install --no-cache-dir pyyaml

# Copy application code
COPY crafty_server_watcher/ /app/crafty_server_watcher/

# Default config path inside the container
ENV crafty_server_watcher_CONFIG=/config/config.yaml

# Expose common Minecraft ports (override in docker-compose)
# Users will map their specific ports via -p or docker-compose
EXPOSE 25565
EXPOSE 19132/udp

# Health check endpoint (default port 8095)
EXPOSE 8095
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8095/health')" || exit 1

ENTRYPOINT ["python", "-m", "crafty_server_watcher"]
CMD ["--config", "/config/config.yaml"]
