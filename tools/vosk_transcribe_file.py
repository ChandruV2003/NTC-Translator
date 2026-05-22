#!/usr/bin/env python3
"""Transcribe a WAV file with a local Vosk model.

This helper is intentionally process-per-file so it can be used by
tools/local_transcription_server.py without coupling the WebCall app to Vosk.
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Transcribe one WAV file with Vosk.")
    parser.add_argument("--model", required=True, help="Path to a Vosk model directory.")
    parser.add_argument("--audio", required=True, help="Path to a mono/stereo PCM WAV file.")
    args = parser.parse_args()

    model_path = Path(args.model).expanduser().resolve()
    audio_path = Path(args.audio).expanduser().resolve()
    if not model_path.exists():
        print(f"model not found: {model_path}", file=sys.stderr)
        return 2
    if not audio_path.exists():
        print(f"audio not found: {audio_path}", file=sys.stderr)
        return 2

    try:
        from vosk import KaldiRecognizer, Model, SetLogLevel
    except Exception as exc:
        print(f"vosk import failed: {exc}", file=sys.stderr)
        return 2

    SetLogLevel(-1)
    model = Model(str(model_path))
    with wave.open(str(audio_path), "rb") as wav_file:
        if wav_file.getcomptype() != "NONE":
            print("compressed WAV is not supported", file=sys.stderr)
            return 2
        recognizer = KaldiRecognizer(model, wav_file.getframerate())
        recognizer.SetWords(False)
        while True:
            data = wav_file.readframes(4000)
            if not data:
                break
            recognizer.AcceptWaveform(data)

    payload = json.loads(recognizer.FinalResult() or "{}")
    text = (payload.get("text") or "").strip()
    print(json.dumps({"text": text}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
