#!/usr/bin/env bash
# Run the Kindle helper from any working directory.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$ROOT/scripts/kindle_helper.py" "$@"
fi

exec python "$ROOT/scripts/kindle_helper.py" "$@"
