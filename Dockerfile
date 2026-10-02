# syntax = docker/dockerfile:1.4
FROM python:3.12-slim

# ---- runtime ----
RUN pip install --no-cache-dir flask

WORKDIR /app

# copy scripts + entrypoint
COPY claim_daily.py /app/claim_daily.py
COPY webui.py /app/webui.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/claim_daily.py /app/entrypoint.sh

# token file lives in a volume (mounted at /data)
ENV TOKEN_FILE=/data/token.json
ENV LOG_LEVEL=INFO

# create non-root user
RUN useradd -m -u 1000 appuser && \
    mkdir -p /data && chown appuser:appuser /data

USER appuser

ENTRYPOINT ["/app/entrypoint.sh"]