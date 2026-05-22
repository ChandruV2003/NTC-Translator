#!/usr/bin/env python3
"""Tiny local transcription HTTP bridge for NTC.

This is intended for an M-series Mac mini or any other local machine that has
whisper.cpp or another local speech-to-text command installed. NTC posts a
WAV body and this service returns JSON: {"text": "..."}.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ntc_env import install_legacy_env_aliases

install_legacy_env_aliases()


def extract_transcription_text(payload) -> str:
    if payload is None:
        return ""
    if isinstance(payload, dict):
        text = payload.get("text") or payload.get("transcript")
        if text:
            return str(text).strip()
        segments = payload.get("segments")
        if isinstance(segments, list):
            return " ".join(str(segment.get("text", "")).strip() for segment in segments if isinstance(segment, dict)).strip()
        return ""
    raw_text = str(payload).strip()
    if not raw_text:
        return ""
    try:
        return extract_transcription_text(json.loads(raw_text))
    except (TypeError, ValueError):
        pass
    cleaned_lines = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^\[[0-9:. ]+-->\s*[0-9:. ]+\]\s*", "", line).strip()
        if line:
            cleaned_lines.append(line)
    return " ".join(cleaned_lines).strip()


class TranscriptionHandler(BaseHTTPRequestHandler):
    server_version = "NTCLocalTranscription/1.0"

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if urlsplit(self.path).path != "/healthz":
            self.send_error(404)
            return
        self._send_json({"ok": True})

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if urlsplit(self.path).path != "/transcribe":
            self.send_error(404)
            return
        command_template = self.server.command_template
        if not command_template:
            self.send_error(503, "local transcription command is not configured")
            return
        content_length = int(self.headers.get("Content-Length") or "0")
        if content_length <= 0:
            self.send_error(400, "missing WAV body")
            return
        wav_bytes = self.rfile.read(content_length)
        query = parse_qs(urlsplit(self.path).query)
        model = query.get("model", [self.server.default_model])[0]
        language = query.get("language", [self.server.default_language])[0]
        prompt = query.get("prompt", [self.server.default_prompt])[0]
        try:
            text = self._run_command(command_template, wav_bytes, model=model, language=language, prompt=prompt)
        except subprocess.TimeoutExpired:
            self.send_error(504, "local transcription command timed out")
            return
        except Exception as exc:
            self.send_error(500, str(exc)[:240])
            return
        self._send_json({"text": text})

    def log_message(self, fmt, *args):
        if self.server.quiet:
            return
        super().log_message(fmt, *args)

    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _run_command(self, command_template: str, wav_bytes: bytes, *, model: str, language: str, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="ntc-local-stt-") as temp_dir:
            audio_path = Path(temp_dir) / "chunk.wav"
            audio_path.write_bytes(wav_bytes)
            substitutions = {
                "audio": shlex.quote(str(audio_path)),
                "model": shlex.quote(model),
                "language": shlex.quote(language),
                "prompt": shlex.quote(prompt),
            }
            command = command_template.format(**substitutions)
            if "{audio}" not in command_template:
                command = f"{command} {shlex.quote(str(audio_path))}"
            completed = subprocess.run(
                shlex.split(command),
                capture_output=True,
                check=False,
                text=True,
                timeout=self.server.timeout_seconds,
            )
        if completed.returncode != 0:
            error_text = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"local transcription command failed: {error_text[:240]}")
        return extract_transcription_text(completed.stdout)


class TranscriptionServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_cls, *, command_template: str, timeout_seconds: float, quiet: bool):
        super().__init__(server_address, handler_cls)
        self.command_template = command_template
        self.timeout_seconds = timeout_seconds
        self.default_model = os.getenv("NTC_LOCAL_TRANSCRIPTION_MODEL", "local")
        self.default_language = os.getenv("NTC_LOCAL_TRANSCRIPTION_LANGUAGE", "en")
        self.default_prompt = os.getenv("NTC_LOCAL_TRANSCRIPTION_PROMPT", "")
        self.quiet = quiet


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local NTC transcription HTTP bridge.")
    parser.add_argument("--host", default=os.getenv("NTC_LOCAL_TRANSCRIPTION_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("NTC_LOCAL_TRANSCRIPTION_PORT", "8765")))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("NTC_LOCAL_TRANSCRIPTION_TIMEOUT_SECONDS", "25")))
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--command",
        default=os.getenv("NTC_LOCAL_TRANSCRIPTION_COMMAND", ""),
        help="Command template. Use {audio}, {model}, {language}, and {prompt}.",
    )
    args = parser.parse_args()
    if not args.command:
        parser.error("--command or NTC_LOCAL_TRANSCRIPTION_COMMAND is required")
    server = TranscriptionServer(
        (args.host, args.port),
        TranscriptionHandler,
        command_template=args.command,
        timeout_seconds=args.timeout,
        quiet=args.quiet,
    )
    print(f"NTC local transcription listening on http://{args.host}:{args.port}/transcribe")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
