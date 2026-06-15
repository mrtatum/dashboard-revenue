#!/usr/bin/env bash
# One-command launcher for running the dashboard container on this laptop.
#
# Usage:
#   ./run-local.sh           # build (host arch), start, open browser
#   ./run-local.sh --logs    # also tail logs
#   ./run-local.sh --stop    # stop + remove container
#   ./run-local.sh --rebuild # docker compose down -v + rebuild from scratch
#
# Assumes Docker Desktop is running. Reads .env in this directory; if missing,
# it still starts the container but /rebuild won't work until you create .env
# from .env.example.

set -euo pipefail

cd "$(dirname "$0")"

IMAGE=dashboard-revenue:local
NAME=dashboard-revenue
PORT=8081

has_docker() { command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; }

require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "❌ docker command not found." >&2
        echo "   Install Docker Desktop for Mac: https://docs.docker.com/desktop/install/mac-install/" >&2
        exit 1
    fi
    if ! docker info >/dev/null 2>&1; then
        echo "❌ Docker is installed but not running. Open Docker Desktop and try again." >&2
        exit 1
    fi
}

case "${1:-}" in
    --stop)
        require_docker
        docker rm -f "$NAME" 2>/dev/null && echo "stopped $NAME" || echo "$NAME not running"
        exit 0
        ;;
    --rebuild)
        require_docker
        docker rm -f "$NAME" 2>/dev/null || true
        docker volume rm dashboard-sources dashboard-static dashboard-secrets 2>/dev/null || true
        ;;
esac

require_docker

# Build for the host architecture (Apple Silicon → arm64 native, x86 → amd64).
# Faster than the linux/amd64 pin used for the production server image.
echo "→ Building $IMAGE for the host architecture…"
docker build -t "$IMAGE" .

# Wipe any prior container with the same name.
docker rm -f "$NAME" 2>/dev/null || true

# Compose-style named volumes so the cache survives restarts.
for v in dashboard-sources dashboard-static dashboard-secrets; do
    docker volume create "$v" >/dev/null
done

# Pass env vars from .env if present. The container will boot either way; without
# .env, /healthz works but /rebuild will return 500 until you populate it.
ENV_ARGS=()
if [[ -f .env ]]; then
    echo "→ Using env from .env"
    ENV_ARGS=(--env-file .env)
else
    echo "⚠  No .env found — container will start but /rebuild won't work."
    echo "   Run:  cp .env.example .env  and fill in values from"
    echo "         python3 tools/get_refresh_token.py --client-id ... --tenant ... --share-url ..."
fi

echo "→ Starting $NAME on http://localhost:$PORT …"
docker run -d --name "$NAME" \
    -p "$PORT:8081" \
    "${ENV_ARGS[@]}" \
    -v dashboard-sources:/app/sources \
    -v dashboard-static:/app/static \
    -v dashboard-secrets:/app/secrets \
    --restart unless-stopped \
    "$IMAGE" >/dev/null

# Wait briefly for /healthz so we don't print "open the URL" before it's ready.
for i in $(seq 1 20); do
    if curl -fsS "http://localhost:$PORT/healthz" >/dev/null 2>&1; then break; fi
    sleep 1
done

echo
echo "✓ Container running."
echo "   Dashboard:  http://localhost:$PORT/dashboard"
echo "   Status:     http://localhost:$PORT/status"
echo "   Healthz:    http://localhost:$PORT/healthz"
echo
echo "Useful commands:"
echo "   docker logs -f $NAME"
echo "   docker exec -it $NAME sh"
echo "   ./run-local.sh --stop"
echo

if [[ "${1:-}" == "--logs" ]]; then
    docker logs -f "$NAME"
fi
