#!/bin/zsh
set -euo pipefail

HOME_DIR="/Users/c.s.d.v.r.s."
PORT="${NTC_TRANSLATION_TTS_PORT:-8767}"
HOST="${NTC_TRANSLATION_TTS_HOST:-100.66.210.59}"
MODEL="${NTC_TRANSLATION_MODEL:-mlx-community/Qwen2.5-1.5B-Instruct-4bit}"
SERVER="${HOME_DIR}/Developer/NTC-Translator/tools/m4_translation_tts_server.py"
PLIST="${HOME_DIR}/Library/LaunchAgents/org.ntc.translation-tts-server.plist"
LOG_DIR="${HOME_DIR}/Library/Logs/NTC"

if /usr/sbin/lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  exit 0
fi

TOKEN="$(/usr/bin/python3 - "${PLIST}" <<'PY'
import plistlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit
with path.open("rb") as handle:
    payload = plistlib.load(handle)
print((payload.get("EnvironmentVariables") or {}).get("NTC_TRANSLATION_TTS_TOKEN", ""))
PY
)"

mkdir -p "${LOG_DIR}"
export NTC_TRANSLATION_TTS_TOKEN="${TOKEN}"
nohup /usr/bin/python3 "${SERVER}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --model "${MODEL}" \
  --quiet \
  >>"${LOG_DIR}/translation-tts-server.log" \
  2>>"${LOG_DIR}/translation-tts-server.err.log" &
