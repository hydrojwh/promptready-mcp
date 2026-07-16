#!/usr/bin/env bash
# Stdio MCP smoke: initialize + tools/list (no live token required).
# Usage (from mcp/ after pip install -e .):
#   ./scripts/smoke_stdio.sh
#   ./scripts/smoke_stdio.sh /path/to/python

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

# Ensure package is importable (editable install preferred).
if ! "$PY" -c "import promptready_mcp" 2>/dev/null; then
  echo "promptready_mcp not importable with: $PY" >&2
  echo "Run: cd mcp && python -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

OUT="$(
  printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
    '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
    | "$PY" -m promptready_mcp.server 2>/dev/null
)"

echo "$OUT"
echo "$OUT" | grep -q 'get_credits' || {
  echo "FAIL: get_credits not found in tools/list response" >&2
  exit 1
}
echo "OK: stdio smoke — get_credits registered"
