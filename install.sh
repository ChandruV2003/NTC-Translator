#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

info() {
  printf '\033[1;34m[Transcriptom]\033[0m %s\n' "$1"
}

fail() {
  printf '\033[1;31m[Transcriptom]\033[0m %s\n' "$1" >&2
  exit 1
}

find_python() {
  for candidate in python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done

  if command -v python3 >/dev/null 2>&1; then
    version="$(python3 - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
)"
    case "$version" in
      3.11|3.12|3.13)
        command -v python3
        return 0
        ;;
    esac
  fi

  return 1
}

PYTHON_BIN="$(find_python || true)"

if [[ -z "${PYTHON_BIN:-}" ]]; then
  if command -v brew >/dev/null 2>&1; then
    info "Installing Python 3.13 with Homebrew..."
    brew install python@3.13
    PYTHON_BIN="$(command -v python3.13 || true)"
  else
    fail "Python 3.11, 3.12, or 3.13 is required. Install Homebrew from https://brew.sh, then rerun ./install.sh."
  fi
fi

info "Using Python: $("$PYTHON_BIN" --version)"

if [[ ! -d "$VENV_DIR" ]]; then
  info "Creating local virtual environment..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

info "Upgrading installer tools..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel

info "Installing Transcriptom dependencies..."
"$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/requirements.txt"

chmod +x "$ROOT_DIR/transcriptom"
chmod +x "$ROOT_DIR/scripts/check.sh" 2>/dev/null || true
chmod +x "$ROOT_DIR/scripts/publish_github.sh" 2>/dev/null || true

info "Done."
printf '\nTry it with:\n  ./transcriptom "/path/to/audio.m4a"\n\n'
