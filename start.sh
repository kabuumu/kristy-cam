#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

HOST="127.0.0.1"
PORT="8420"
BASE_URL="http://${HOST}:${PORT}"
HEALTH_URL="${BASE_URL}/api/health"

open_browser() {
  if command -v open >/dev/null 2>&1; then
    open "${BASE_URL}" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${BASE_URL}" >/dev/null 2>&1 || true
  fi
}

# 1. Single-instance / port-conflict guard
if command -v curl >/dev/null 2>&1; then
  HTTP_BODY=""
  HTTP_CODE=""
  if HTTP_RESP="$(curl -sS --max-time 2 -w $'\n%{http_code}' "${HEALTH_URL}" 2>/dev/null)"; then
    HTTP_CODE="${HTTP_RESP##*$'\n'}"
    HTTP_BODY="${HTTP_RESP%$'\n'*}"
    if [ "${HTTP_CODE}" = "200" ] && echo "${HTTP_BODY}" | grep -q "sims-vertical-studio"; then
      echo "Sims 4 Vertical Video Studio is already running at ${BASE_URL}. Opening browser..."
      open_browser
      exit 0
    else
      echo "[ERROR] Port ${PORT} on ${HOST} is already in use by another application (HTTP ${HTTP_CODE})." >&2
      exit 1
    fi
  elif command -v nc >/dev/null 2>&1 && nc -z "${HOST}" "${PORT}" >/dev/null 2>&1; then
    echo "[ERROR] Port ${PORT} on ${HOST} is already in use by another application." >&2
    exit 1
  fi
fi

# 2. Ensure uv is installed
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv package manager..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "[ERROR] Failed to locate uv after installation." >&2
  exit 1
fi

# 3. Sync locked Python 3.12 environment
echo "Verifying locked Python environment..."
if ! uv sync --locked; then
  echo "[ERROR] Failed to sync Python dependencies. Check internet connection on first run." >&2
  exit 1
fi

# 4. Bootstrap FFmpeg, bundled fonts, and Kokoro ONNX models
echo "Verifying runtime assets and Kokoro models..."
if ! uv run python -m backend.bootstrap; then
  echo "[ERROR] Runtime asset or FFmpeg verification failed." >&2
  exit 1
fi

# 5. Launch backend server and open browser once healthy
echo "Starting Sims 4 Vertical Video Studio at ${BASE_URL}..."
uv run uvicorn backend.main:app --host "${HOST}" --port "${PORT}" &
SERVER_PID=$!

cleanup() {
  if kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    kill "${SERVER_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup INT TERM

for _ in $(seq 1 30); do
  if ! kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    wait "${SERVER_PID}"
    exit $?
  fi
  if command -v curl >/dev/null 2>&1 && curl -sf --max-time 1 "${HEALTH_URL}" >/dev/null 2>&1; then
    open_browser
    break
  fi
  sleep 0.5
done

wait "${SERVER_PID}"
