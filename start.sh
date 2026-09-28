#!/usr/bin/env bash
# Start BrandBatch locally (macOS / Linux): website + render worker.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
PORT="${PORT:-8000}"

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer is required: https://www.python.org/downloads/"; exit 1
fi

if [ ! -x .venv/bin/python ]; then
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
    || { echo "Python 3.10 or newer is required."; exit 1; }
  echo "Creating virtual environment..."
  "$PY" -m venv .venv
fi
VPY="$ROOT/.venv/bin/python"

echo "Checking dependencies..."
"$VPY" -m pip install --disable-pip-version-check -q -r requirements.txt

# persistent secret key so logins survive restarts
[ -f .secret_key ] || "$VPY" -c "import secrets; open('.secret_key','w').write(secrets.token_hex(32))"
export SECRET_KEY="$(cat .secret_key)"
export DATABASE_URL="${DATABASE_URL:-sqlite:///$ROOT/brandbatch.db}"
export STORAGE_DIR="${STORAGE_DIR:-$ROOT/storage}"

"$VPY" worker.py &
WORKER_PID=$!
cleanup() { kill "$WORKER_PID" 2>/dev/null || true; wait "$WORKER_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

( sleep 4; command -v open >/dev/null && open "http://127.0.0.1:$PORT" || command -v xdg-open >/dev/null && xdg-open "http://127.0.0.1:$PORT" ) >/dev/null 2>&1 &

echo
echo "  BrandBatch is running at http://127.0.0.1:$PORT"
echo "  Press Ctrl+C to stop (website and worker)."
echo
"$VPY" -m flask --app wsgi run --host 127.0.0.1 --port "$PORT"
