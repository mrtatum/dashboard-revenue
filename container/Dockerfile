# syntax=docker/dockerfile:1.7
# Pipeline dashboard web service.
#
# Local build (native arch — fast on Apple Silicon and Intel):
#   docker build -t dashboard-revenue:latest container/
#
# Multi-arch build for distribution (requires buildx + a registry):
#   docker buildx build --platform linux/amd64,linux/arm64 \
#       -t ghcr.io/<you>/dashboard-revenue:latest --push container/
#
# Run:    docker run --rm -p 8080:8080 \
#             -e MS_CLIENT_ID='<your-app-client-id>' \
#             -e MS_REFRESH_TOKEN='<from-get_refresh_token.py>' \
#             -e ONEDRIVE_DRIVE_ID='<from-get_refresh_token.py>' \
#             -e ONEDRIVE_ITEM_ID='<from-get_refresh_token.py>' \
#             -v dashboard-sources:/app/sources \
#             -v dashboard-static:/app/static \
#             -v dashboard-secrets:/app/secrets \
#             dashboard-revenue:latest

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps: ca-certs for TLS to Graph; nothing else needed since openpyxl is pure-Python.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# App code + template. The template is the dashboard HTML/CSS/JS shell with
# the __EMBEDDED_JSON__ marker; bake.py substitutes that marker with freshly
# downloaded OneDrive data at /rebuild time.
COPY app /app/app
COPY template /app/template

# Build-time assertion: the template must be present in the image so that
# the container's only external dependency at runtime is OneDrive.
RUN test -f /app/template/Pipeline_Dashboard.template.html \
 && grep -q __EMBEDDED_JSON__ /app/template/Pipeline_Dashboard.template.html \
 || (echo "ERROR: template missing or has no __EMBEDDED_JSON__ marker" && exit 1)

# Writable dirs (mountable as volumes). /app/secrets stores rotated refresh tokens.
RUN mkdir -p /app/sources /app/static /app/secrets \
 && useradd -u 10001 -m -d /home/dashboard dashboard \
 && chown -R dashboard:dashboard /app/sources /app/static /app/secrets \
 && chmod 700 /app/secrets
USER dashboard

EXPOSE 8080

# Healthcheck hits the cheap /healthz route.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# Gunicorn: 2 workers, 120s timeout to allow a long rebuild on the worker thread.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", \
     "--timeout", "600", "--access-logfile", "-", "app.server:app"]
