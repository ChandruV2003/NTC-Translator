#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"$ROOT_DIR/.venv/bin/python" -m compileall -q "$ROOT_DIR/transcriptom_app"
"$ROOT_DIR/transcriptom" --help >/dev/null

printf 'Checks passed.\n'
