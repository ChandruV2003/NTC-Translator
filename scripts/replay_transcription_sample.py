#!/usr/bin/env python3
"""Replay saved audio through the NTC transcription path.

This is a diagnostic harness. It does not publish room audio or affect active
calls. It normalizes a recording to the same 16 kHz mono WAV format used by the
live transcription worker, sends chunks to a configured local transcriber, and
writes resulting text to the NTC transcript table.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import wave


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_telnyx_recording import load_env  # noqa: E402
from ntc_env import install_legacy_env_aliases  # noqa: E402
from ntc_store import NTCStore  # noqa: E402
from tools.local_transcription_server import extract_transcription_text  # noqa: E402

install_legacy_env_aliases()


TARGET_SAMPLE_RATE_HZ = 16_000
TARGET_CHANNELS = 1
TARGET_SAMPLE_WIDTH_BYTES = 2


def _default_provider() -> str:
    configured = os.getenv("NTC_TRANSCRIPTION_PROVIDER", "").strip().lower()
    if configured in {"local_http", "local_cmd"}:
        return configured
    if os.getenv("NTC_TRANSCRIPTION_LOCAL_URL", "").strip():
        return "local_http"
    if os.getenv("NTC_TRANSCRIPTION_LOCAL_COMMAND", "").strip():
        return "local_cmd"
    return "local_http"


def _normalize_audio(source_path: Path, target_path: Path, *, limit_seconds: float | None) -> None:
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
    ]
    if limit_seconds and limit_seconds > 0:
        command.extend(["-t", str(limit_seconds)])
    command.extend(
        [
            "-ac",
            str(TARGET_CHANNELS),
            "-ar",
            str(TARGET_SAMPLE_RATE_HZ),
            "-sample_fmt",
            "s16",
            str(target_path),
        ]
    )
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        error_text = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"ffmpeg failed for {source_path}: {error_text[:240]}")


def _wav_bytes(raw_frames: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(TARGET_CHANNELS)
        wav_file.setsampwidth(TARGET_SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(TARGET_SAMPLE_RATE_HZ)
        wav_file.writeframes(raw_frames)
    return output.getvalue()


def _iter_wav_chunks(path: Path, *, chunk_seconds: float):
    frames_per_chunk = max(TARGET_SAMPLE_RATE_HZ, int(TARGET_SAMPLE_RATE_HZ * max(1.0, chunk_seconds)))
    with wave.open(str(path), "rb") as wav_file:
        if wav_file.getnchannels() != TARGET_CHANNELS:
            raise ValueError(f"{path} was not normalized to mono")
        if wav_file.getframerate() != TARGET_SAMPLE_RATE_HZ:
            raise ValueError(f"{path} was not normalized to {TARGET_SAMPLE_RATE_HZ} Hz")
        if wav_file.getsampwidth() != TARGET_SAMPLE_WIDTH_BYTES:
            raise ValueError(f"{path} was not normalized to PCM16")
        chunk_index = 0
        while True:
            frames = wav_file.readframes(frames_per_chunk)
            if not frames:
                break
            start_seconds = chunk_index * frames_per_chunk / TARGET_SAMPLE_RATE_HZ
            duration_seconds = len(frames) / float(TARGET_SAMPLE_RATE_HZ * TARGET_SAMPLE_WIDTH_BYTES)
            yield chunk_index, start_seconds, duration_seconds, _wav_bytes(frames)
            chunk_index += 1


def _transcribe_local_http(wav_bytes: bytes, *, url: str, model: str, language: str, prompt: str, timeout: float) -> str:
    if not url:
        raise RuntimeError("NTC_TRANSCRIPTION_LOCAL_URL is not configured")
    params = {}
    if model:
        params["model"] = model
    if language:
        params["language"] = language
    if prompt:
        params["prompt"] = prompt
    target_url = url
    if params:
        separator = "&" if "?" in target_url else "?"
        target_url = f"{target_url}{separator}{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        target_url,
        data=wav_bytes,
        method="POST",
        headers={"Content-Type": "audio/wav", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=max(5.0, timeout)) as response:
        raw_text = response.read().decode("utf-8", errors="replace")
    try:
        return extract_transcription_text(json.loads(raw_text))
    except ValueError:
        return extract_transcription_text(raw_text)


def _transcribe_local_cmd(wav_bytes: bytes, *, command_template: str, model: str, language: str, prompt: str, timeout: float) -> str:
    if not command_template:
        raise RuntimeError("NTC_TRANSCRIPTION_LOCAL_COMMAND is not configured")
    with tempfile.TemporaryDirectory(prefix="ntc-replay-stt-") as temp_dir:
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
            text=True,
            check=False,
            timeout=max(5.0, timeout),
        )
    if completed.returncode != 0:
        error_text = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"local transcription command failed: {error_text[:240]}")
    return extract_transcription_text(completed.stdout)


def _transcribe_chunk(wav_bytes: bytes, args) -> str:
    if args.provider == "local_http":
        return _transcribe_local_http(
            wav_bytes,
            url=args.local_url,
            model=args.model,
            language=args.language,
            prompt=args.prompt,
            timeout=args.timeout,
        )
    if args.provider == "local_cmd":
        return _transcribe_local_cmd(
            wav_bytes,
            command_template=args.local_command,
            model=args.model,
            language=args.language,
            prompt=args.prompt,
            timeout=args.timeout,
        )
    raise ValueError(f"unsupported provider for diagnostic replay: {args.provider}")


def _replay_file(source_path: Path, args, store: NTCStore) -> list[dict]:
    recorded_segments: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="ntc-replay-audio-") as temp_dir:
        normalized_path = Path(temp_dir) / "normalized.wav"
        _normalize_audio(source_path, normalized_path, limit_seconds=args.limit_seconds)
        replay_started_at = datetime.now(timezone.utc)
        for chunk_index, start_seconds, duration_seconds, wav_bytes in _iter_wav_chunks(
            normalized_path,
            chunk_seconds=args.chunk_seconds,
        ):
            text = _transcribe_chunk(wav_bytes, args).strip()
            if not text:
                continue
            started_at = replay_started_at + timedelta(seconds=start_seconds)
            ended_at = started_at + timedelta(seconds=duration_seconds)
            segment = {
                "file": str(source_path),
                "chunk_index": chunk_index,
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "text": text,
            }
            if not args.dry_run:
                segment["id"] = store.record_transcript_segment(
                    args.room,
                    host_slug=None,
                    provider=f"replay:{args.provider}",
                    model=args.model,
                    started_at=segment["started_at"],
                    ended_at=segment["ended_at"],
                    received_at=datetime.now(timezone.utc).isoformat(),
                    text=text,
                    source="diagnostic-replay",
                )
            recorded_segments.append(segment)
    return recorded_segments


def main() -> int:
    env_parser = argparse.ArgumentParser(add_help=False)
    env_parser.add_argument("--env-file", default=".env")
    env_args, _ = env_parser.parse_known_args()
    load_env(Path(env_args.env_file))

    parser = argparse.ArgumentParser(description=__doc__, parents=[env_parser])
    parser.add_argument("audio_files", nargs="+", help="WAV/MP3/WebM files to replay through transcription")
    parser.add_argument("--db-path", default=os.getenv("NTC_DB_PATH", str(REPO_ROOT / "data" / "ntccast.db")))
    parser.add_argument("--room", default=os.getenv("NTC_REPLAY_ROOM", "room-a"))
    parser.add_argument("--provider", choices=("local_http", "local_cmd"), default=_default_provider())
    parser.add_argument("--local-url", default=os.getenv("NTC_TRANSCRIPTION_LOCAL_URL", ""))
    parser.add_argument("--local-command", default=os.getenv("NTC_TRANSCRIPTION_LOCAL_COMMAND", ""))
    parser.add_argument("--model", default=os.getenv("NTC_TRANSCRIPTION_MODEL", "local"))
    parser.add_argument("--language", default=os.getenv("NTC_TRANSCRIPTION_LANGUAGE", "en"))
    parser.add_argument("--prompt", default=os.getenv("NTC_TRANSCRIPTION_PROMPT", "Church service, Bible study, prayer meeting, sermon, NTC Newark."))
    parser.add_argument("--chunk-seconds", type=float, default=float(os.getenv("NTC_TRANSCRIPTION_CHUNK_SECONDS", "8.0")))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("NTC_TRANSCRIPTION_TIMEOUT_SECONDS", "25.0")))
    parser.add_argument("--limit-seconds", type=float, default=0.0, help="limit each input file for quick tests; 0 means full file")
    parser.add_argument("--dry-run", action="store_true", help="transcribe and print JSON without writing transcript rows")
    args = parser.parse_args()

    if args.provider == "local_http" and not args.local_url:
        args.local_url = os.getenv("NTC_TRANSCRIPTION_LOCAL_URL", "")
    if args.provider == "local_cmd" and not args.local_command:
        args.local_command = os.getenv("NTC_TRANSCRIPTION_LOCAL_COMMAND", "")
    if args.limit_seconds <= 0:
        args.limit_seconds = None

    store = NTCStore(args.db_path)
    summary = {
        "room": args.room,
        "provider": args.provider,
        "dry_run": bool(args.dry_run),
        "segments": [],
    }
    for audio_file in args.audio_files:
        summary["segments"].extend(_replay_file(Path(audio_file), args, store))

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
