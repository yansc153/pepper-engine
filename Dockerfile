# PepperBot content_2 — VPS production image
# Base: Playwright 1.49 on Ubuntu Noble (bundles Chromium + CJK fonts + deps)
FROM mcr.microsoft.com/playwright:v1.49-noble

# Python 3.11 + cron + tini + tools needed at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-venv \
    python3-pip \
    cron \
    curl \
    ca-certificates \
    tzdata \
    rsync \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/local/bin/python3

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Python deps first for layer caching
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --break-system-packages --no-cache-dir -r /app/requirements.txt

# Application code (secrets/, data/, logs/ excluded via .dockerignore;
# they come in at runtime via docker-compose volumes)
COPY src/        /app/src/
COPY config/     /app/config/
COPY voice/      /app/voice/
COPY templates/  /app/templates/
COPY writer/     /app/writer/
COPY ops/        /app/ops/
COPY docs/       /app/docs/
COPY scripts/    /app/scripts/
COPY crontab.txt /app/crontab.txt

# Mark all shell entrypoints executable
RUN chmod +x /app/scripts/*.sh

# Install cron table (must be 0644 owned by root)
RUN cp /app/crontab.txt /etc/cron.d/pepperbot \
    && chmod 0644 /etc/cron.d/pepperbot \
    && crontab /etc/cron.d/pepperbot

# Ensure runtime dirs exist even if no volume is mounted
RUN mkdir -p /app/logs /app/data /app/tmp_images /app/secrets

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
