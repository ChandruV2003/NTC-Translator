#!/usr/bin/env python3
"""M4-local translation plus macOS TTS bridge for NTC translated room output."""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


LANGUAGE_NAMES = {
    "zh-CN": "Mandarin Chinese",
    "zh-TW": "Traditional Chinese",
    "es": "Spanish",
    "fr": "French",
    "hi": "Hindi",
    "ml": "Malayalam",
}

DEFAULT_VOICES = {
    "zh-CN": "Tingting",
    "zh-TW": "Meijia",
    "es": "Monica",
    "fr": "Thomas",
    "hi": "Lekha",
    "ml": "Lekha",
}


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _target_language_name(target_language):
    normalized = (target_language or "zh-CN").strip() or "zh-CN"
    return LANGUAGE_NAMES.get(normalized, normalized)


def _voice_for_language(target_language):
    normalized = (target_language or "zh-CN").strip() or "zh-CN"
    env_key = "NTC_TTS_VOICE_" + normalized.upper().replace("-", "_")
    return (os.getenv(env_key) or DEFAULT_VOICES.get(normalized) or "").strip()


def _clean_translation(text, target_language):
    cleaned = (text or "").strip()
    cleaned = cleaned.strip("`").strip()
    prefixes = [
        "Translation:",
        "Translated text:",
        "Mandarin Chinese:",
        "Chinese:",
        _target_language_name(target_language) + ":",
    ]
    for prefix in prefixes:
        if cleaned.lower().startswith(prefix.lower()):
            cleaned = cleaned[len(prefix):].strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def _contains_cjk(text):
    return any("\u4e00" <= char <= "\u9fff" for char in text or "")


def _needs_retry(translated_text, target_language):
    normalized = (target_language or "").strip().lower()
    if normalized.startswith("zh"):
        return not _contains_cjk(translated_text)
    return False


def _translation_messages(text, target_language, *, strict):
    language_name = _target_language_name(target_language)
    if strict and target_language == "zh-CN":
        return [
            {
                "role": "system",
                "content": (
                    "You are a church interpreter. Translate English transcript fragments "
                    "into Simplified Mandarin Chinese. Output Chinese only. Do not copy "
                    "the English. Do not explain."
                ),
            },
            {
                "role": "user",
                "content": f"Translate this to Simplified Mandarin Chinese.\n\n{text}",
            },
        ]
    return [
        {
            "role": "system",
            "content": (
                "You are a church interpreter. Translate English transcript fragments accurately "
                f"and naturally into {language_name}. Preserve names, Bible references, and meaning. "
                "Return only the translated text. Do not explain."
            ),
        },
        {
            "role": "user",
            "content": f"Translate this to {language_name}. Do not copy the source text.\n\n{text}",
        },
    ]


class LocalTranslator:
    def __init__(self, model_id, max_tokens):
        self.model_id = model_id
        self.max_tokens = max(16, int(max_tokens or 160))
        self.model = None
        self.tokenizer = None
        self.load_seconds = 0.0
        self.lock = threading.Lock()

    @property
    def loaded(self):
        return self.model is not None and self.tokenizer is not None

    def ensure_loaded(self):
        with self.lock:
            if self.loaded:
                return
            started_at = time.monotonic()
            from mlx_lm import load

            self.model, self.tokenizer = load(self.model_id)
            self.load_seconds = time.monotonic() - started_at

    def translate(self, text, target_language):
        self.ensure_loaded()
        translated = ""
        for strict in (False, True):
            translated = self._translate_once(text, target_language, strict=strict)
            if not _needs_retry(translated, target_language):
                return translated
        return translated

    def _translate_once(self, text, target_language, *, strict):
        messages = _translation_messages(text, target_language, strict=strict)
        try:
            prompt = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        except Exception:
            language_name = _target_language_name(target_language)
            prompt = (
                "Translate the following church-service transcript fragment into "
                f"{language_name}. Return only the translation.\n\n{text}\n\nTranslation:"
            )

        from mlx_lm import generate

        with self.lock:
            translated = generate(
                self.model,
                self.tokenizer,
                prompt=prompt,
                max_tokens=self.max_tokens,
                verbose=False,
            )
        return _clean_translation(translated, target_language)


class TranslationTTSServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address,
        handler_cls,
        *,
        translator,
        api_token,
        max_body_bytes,
        max_queued_requests,
        queue_timeout_seconds,
        say_timeout_seconds,
        data_format,
        quiet,
    ):
        super().__init__(server_address, handler_cls)
        self.translator = translator
        self.api_token = api_token
        self.max_body_bytes = max_body_bytes
        self.max_queued_requests = max(1, int(max_queued_requests or 1))
        self.queue_timeout_seconds = max(0.0, float(queue_timeout_seconds or 0.0))
        self.say_timeout_seconds = max(5.0, float(say_timeout_seconds or 60.0))
        self.data_format = data_format
        self.quiet = quiet
        self.request_slots = threading.BoundedSemaphore(self.max_queued_requests)
        self.stats_lock = threading.Lock()
        self.started_at = time.time()
        self.active_requests = 0
        self.accepted_requests = 0
        self.completed_requests = 0
        self.failed_requests = 0
        self.rejected_requests = 0

    def stats(self):
        with self.stats_lock:
            return {
                "active_requests": self.active_requests,
                "queued_capacity": self.max_queued_requests,
                "accepted_requests": self.accepted_requests,
                "completed_requests": self.completed_requests,
                "failed_requests": self.failed_requests,
                "rejected_requests": self.rejected_requests,
            }

    @contextlib.contextmanager
    def request_slot(self):
        started_at = time.monotonic()
        acquired = self.request_slots.acquire(timeout=self.queue_timeout_seconds)
        queue_wait_seconds = time.monotonic() - started_at
        if not acquired:
            with self.stats_lock:
                self.rejected_requests += 1
            yield False, queue_wait_seconds
            return
        with self.stats_lock:
            self.active_requests += 1
            self.accepted_requests += 1
        try:
            yield True, queue_wait_seconds
        finally:
            with self.stats_lock:
                self.active_requests = max(0, self.active_requests - 1)
            self.request_slots.release()


class TranslationTTSHandler(BaseHTTPRequestHandler):
    server_version = "NTCTranslationTTS/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        if not self.server.quiet:
            super().log_message(fmt, *args)

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path not in {"/healthz", "/readyz", "/stats"}:
            self.send_error(404)
            return
        if path == "/stats" and not self._authorized():
            self._send_json({"error": "unauthorized"}, status=401)
            return
        self._send_json(
            {
                "ok": True,
                "model": self.server.translator.model_id,
                "model_loaded": self.server.translator.loaded,
                "load_seconds": round(self.server.translator.load_seconds, 3),
                "uptime_seconds": round(time.time() - self.server.started_at, 3),
                "max_body_bytes": self.server.max_body_bytes,
                **self.server.stats(),
            }
        )

    def do_POST(self):  # noqa: N802
        request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        if urlsplit(self.path).path not in {"/translate-tts", "/v1/translate-tts"}:
            self.send_error(404)
            return
        if not self._authorized():
            self._send_json({"error": "unauthorized", "request_id": request_id}, status=401)
            return
        try:
            content_length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send_json({"error": "invalid Content-Length", "request_id": request_id}, status=400)
            return
        if content_length <= 0:
            self._send_json({"error": "missing JSON body", "request_id": request_id}, status=400)
            return
        if content_length > self.server.max_body_bytes:
            with self.server.stats_lock:
                self.server.rejected_requests += 1
            self._send_json(
                {
                    "error": "request body too large",
                    "request_id": request_id,
                    "max_body_bytes": self.server.max_body_bytes,
                },
                status=413,
            )
            return

        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "invalid JSON body", "request_id": request_id}, status=400)
            return

        text = " ".join(str(payload.get("text") or "").split())
        target_language = (payload.get("target_language") or "zh-CN").strip() or "zh-CN"
        voice = str(payload.get("voice") or _voice_for_language(target_language)).strip()
        if not text:
            self._send_json({"error": "missing text", "request_id": request_id}, status=400)
            return
        if not voice:
            self._send_json({"error": f"no macOS voice configured for {target_language}", "request_id": request_id}, status=400)
            return

        with self.server.request_slot() as (accepted, queue_wait_seconds):
            if not accepted:
                self._send_json(
                    {
                        "error": "translation queue is full",
                        "request_id": request_id,
                        "queue_wait_seconds": round(queue_wait_seconds, 3),
                    },
                    status=429,
                )
                return
            try:
                translation_started = time.monotonic()
                translated_text = self.server.translator.translate(text, target_language)
                translation_seconds = time.monotonic() - translation_started
                if not translated_text:
                    raise RuntimeError("empty translation")

                tts_started = time.monotonic()
                wav_bytes = self._render_wav(translated_text, voice)
                tts_seconds = time.monotonic() - tts_started
                with self.server.stats_lock:
                    self.server.completed_requests += 1
                self._send_json(
                    {
                        "ok": True,
                        "request_id": request_id,
                        "target_language": target_language,
                        "voice": voice,
                        "source_text": text,
                        "translated_text": translated_text,
                        "audio_mime_type": "audio/wav",
                        "audio_base64": base64.b64encode(wav_bytes).decode("ascii"),
                        "queue_wait_seconds": round(queue_wait_seconds, 3),
                        "translation_seconds": round(translation_seconds, 3),
                        "tts_seconds": round(tts_seconds, 3),
                    }
                )
            except Exception as exc:
                with self.server.stats_lock:
                    self.server.failed_requests += 1
                self._send_json({"error": str(exc), "request_id": request_id}, status=500)

    def _authorized(self):
        expected = self.server.api_token
        if not expected:
            return True
        auth = self.headers.get("Authorization", "")
        token = self.headers.get("X-NTC-Translation-Token", "")
        return auth == f"Bearer {expected}" or token == expected

    def _render_wav(self, text, voice):
        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile(prefix="ntc-translation-", suffix=".wav", delete=False) as temp_file:
                temp_path = temp_file.name
            command = [
                "/usr/bin/say",
                "-v",
                voice,
                "--file-format=WAVE",
                "--data-format",
                self.server.data_format,
                "-o",
                temp_path,
                text,
            ]
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.server.say_timeout_seconds,
            )
            if completed.returncode != 0:
                raise RuntimeError((completed.stderr or completed.stdout or f"say exited {completed.returncode}").strip())
            return Path(temp_path).read_bytes()
        finally:
            if temp_path:
                try:
                    Path(temp_path).unlink(missing_ok=True)
                except OSError:
                    pass

    def _send_json(self, payload, status=200):
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("NTC_TRANSLATION_TTS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("NTC_TRANSLATION_TTS_PORT", "8767")))
    parser.add_argument(
        "--model",
        default=os.getenv("NTC_TRANSLATION_MODEL", "mlx-community/Qwen2.5-1.5B-Instruct-4bit"),
    )
    parser.add_argument("--api-token", default=os.getenv("NTC_TRANSLATION_TTS_TOKEN", ""))
    parser.add_argument("--max-body-kb", type=int, default=int(os.getenv("NTC_TRANSLATION_TTS_MAX_BODY_KB", "128")))
    parser.add_argument("--max-queued-requests", type=int, default=int(os.getenv("NTC_TRANSLATION_TTS_MAX_QUEUE", "2")))
    parser.add_argument("--queue-timeout", type=float, default=float(os.getenv("NTC_TRANSLATION_TTS_QUEUE_TIMEOUT", "120")))
    parser.add_argument("--say-timeout", type=float, default=float(os.getenv("NTC_TRANSLATION_TTS_SAY_TIMEOUT", "60")))
    parser.add_argument("--max-tokens", type=int, default=int(os.getenv("NTC_TRANSLATION_MAX_TOKENS", "180")))
    parser.add_argument("--data-format", default=os.getenv("NTC_TRANSLATION_TTS_DATA_FORMAT", "LEI16@22050"))
    parser.add_argument("--preload", action="store_true", default=os.getenv("NTC_TRANSLATION_TTS_PRELOAD", "0") == "1")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    translator = LocalTranslator(args.model, args.max_tokens)
    server = TranslationTTSServer(
        (args.host, args.port),
        TranslationTTSHandler,
        translator=translator,
        api_token=args.api_token,
        max_body_bytes=max(1, args.max_body_kb) * 1024,
        max_queued_requests=args.max_queued_requests,
        queue_timeout_seconds=args.queue_timeout,
        say_timeout_seconds=args.say_timeout,
        data_format=args.data_format,
        quiet=args.quiet,
    )
    if args.preload:
        translator.ensure_loaded()
    print(
        f"NTC translation/TTS server listening on {args.host}:{args.port} "
        f"model={args.model} loaded={translator.loaded}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
