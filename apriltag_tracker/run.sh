#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
URL="http://localhost:${PORT}"

if [ ! -d ".venv" ]; then
  echo "Creating Python virtual environment in apriltag_tracker/.venv ..."
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv || uv venv .venv
  else
    python3 -m venv .venv
  fi
fi

if ! .venv/bin/python -c "import fastapi, uvicorn, cv2, pupil_apriltags" >/dev/null 2>&1; then
  echo "Installing dependencies from requirements.txt ..."
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python .venv/bin/python -r requirements.txt
  else
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
  fi
fi

echo "Starting Trackside AprilTag Lap Counter on ${URL} ..."
.venv/bin/python -m uvicorn app:app --host "${HOST}" --port "${PORT}" &
SERVER_PID=$!

cleanup() {
  echo ""
  echo "Stopping AprilTag Tracker (PID ${SERVER_PID}) ..."
  kill "${SERVER_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Wait until FastAPI responds before opening Google Chrome
for _ in $(seq 1 40); do
  if curl -fsS "${URL}/api/state" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if [ "${NO_BROWSER:-0}" != "1" ]; then
  if [ "$(uname -s)" = "Darwin" ]; then
    open -a "Google Chrome" "${URL}" 2>/dev/null || open "${URL}" || true
  elif command -v google-chrome >/dev/null 2>&1; then
    google-chrome "${URL}" >/dev/null 2>&1 &
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${URL}" >/dev/null 2>&1 &
  fi
fi

wait "${SERVER_PID}"
