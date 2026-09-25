# Service agent : HTTP server (Python, bibliothèque standard) autour du CLI Pi (Node).
FROM node:22-slim

ARG PI_VERSION=0.73.1
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @mariozechner/pi-coding-agent@${PI_VERSION} \
 && npm cache clean --force

# OpenTelemetry in venv
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir \
      "opentelemetry-sdk>=1.27" "opentelemetry-exporter-otlp-proto-http>=1.27"

WORKDIR /app
COPY server.py ./
COPY pi/ ./pi/

# Pi écrit sa configuration dans PI_CODING_AGENT_DIR : on la place dans /tmp (tmpfs),
# le système de fichiers du conteneur restant en lecture seule.
ENV HOME=/tmp \
    PI_CODING_AGENT_DIR=/tmp/pi-agent \
    PI_OFFLINE=1 \
    PI_TELEMETRY=0 \
    PYTHONUNBUFFERED=1

USER node
EXPOSE 8000
CMD ["/opt/venv/bin/python", "/app/server.py"]
