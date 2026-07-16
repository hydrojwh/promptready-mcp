#!/usr/bin/env bash
# Live P0 smoke: call get_credits against production (or PROMPTREADY_BASE_URL).
# Requires PROMPTREADY_ACCESS_TOKEN in the environment (do not commit tokens).
#
# Usage:
#   export PROMPTREADY_ACCESS_TOKEN='eyJ...'
#   ./scripts/smoke_live_credits.sh
#
# Optional:
#   PROMPTREADY_BASE_URL=https://promptready.space

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${1:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
  else
    PY="python3"
  fi
fi

export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

"$PY" - <<'PY'
import asyncio, json, sys
from promptready_mcp.config import load_settings
from promptready_mcp.server import get_credits

settings = load_settings()
if not settings.access_token:
    print("FAIL: not logged in.", file=sys.stderr)
    print("Run: promptready-mcp-login", file=sys.stderr)
    print("(or export PROMPTREADY_ACCESS_TOKEN)", file=sys.stderr)
    sys.exit(2)

raw = asyncio.run(get_credits())
data = json.loads(raw)
print(json.dumps(data, indent=2, ensure_ascii=False))
if not data.get("ok"):
    print("FAIL: get_credits returned ok=false", file=sys.stderr)
    sys.exit(1)
if "credit_balance" not in data:
    print("FAIL: missing credit_balance", file=sys.stderr)
    sys.exit(1)
print(f"OK: live credits for {data.get('email', '?')} = {data.get('credit_balance')}")
PY
