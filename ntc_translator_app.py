"""Internal translator panel for NTC live transcripts and translated audio controls."""

from __future__ import annotations

import base64
import hmac
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, redirect, render_template_string, request, send_file, url_for

from ntc_env import install_legacy_env_aliases
from ntc_branding import install_branding
from ntc_store import NTCStore

install_legacy_env_aliases()


ROOM_SLUG_ALIASES = {
    "study-room": "room-a",
    "meeting-hall": "room-b",
}
VISIBLE_ROOM_SLUGS = ("room-a", "room-b")


def _canonical_room_slug(room_slug: str | None) -> str:
    normalized = (room_slug or "").strip()
    return ROOM_SLUG_ALIASES.get(normalized, normalized)

TRANSLATION_LANGUAGE_OPTIONS = [
    {"code": "zh-CN", "label": "Chinese (Mandarin)"},
    {"code": "es", "label": "Spanish"},
    {"code": "ml", "label": "Malayalam"},
    {"code": "hi", "label": "Hindi"},
    {"code": "fr", "label": "French"},
]

TRANSLATION_LANGUAGE_LABELS = {item["code"]: item["label"] for item in TRANSLATION_LANGUAGE_OPTIONS}


def create_app(test_config: dict | None = None, *, store: NTCStore | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        NTC_DB_PATH=os.getenv("NTC_DB_PATH"),
        NTC_TRANSLATOR_PANEL_PASSWORD=os.getenv(
            "NTC_TRANSLATOR_PANEL_PASSWORD",
            os.getenv("NTC_CAPTIONS_PANEL_PASSWORD", ""),
        ),
        NTC_ADMIN_PASSWORD=os.getenv("NTC_ADMIN_PASSWORD", ""),
        NTC_TRANSLATOR_AUTH_ENABLED=os.getenv(
            "NTC_TRANSLATOR_AUTH_ENABLED",
            os.getenv("NTC_CAPTIONS_AUTH_ENABLED", "1"),
        ),
        NTC_TRANSLATOR_TITLE=os.getenv(
            "NTC_TRANSLATOR_TITLE",
            os.getenv("NTC_CAPTIONS_TITLE", "The Translator"),
        ),
        NTC_TRANSLATOR_POLL_MS=int(os.getenv("NTC_TRANSLATOR_POLL_MS", os.getenv("NTC_CAPTIONS_POLL_MS", "1000"))),
        NTC_TRANSCRIPTION_BASE_URL=os.getenv("NTC_TRANSCRIPTION_BASE_URL", ""),
        NTC_TRANSLATION_AUDIO_DIR=os.getenv("NTC_TRANSLATION_AUDIO_DIR", "/app/data/translation-audio"),
    )
    if test_config:
        app.config.update(test_config)

    install_branding(app)
    ntc_store = store or NTCStore(app.config.get("NTC_DB_PATH"))
    app.ntc_store = ntc_store

    def _panel_password() -> str:
        return (
            app.config.get("NTC_TRANSLATOR_PANEL_PASSWORD")
            or app.config.get("NTC_ADMIN_PASSWORD")
            or ""
        ).strip()

    def _auth_enabled() -> bool:
        value = str(app.config.get("NTC_TRANSLATOR_AUTH_ENABLED", "1")).strip().lower()
        return value not in {"0", "false", "no", "off"}

    def _authorized() -> bool:
        if not _auth_enabled():
            return True
        expected = _panel_password()
        if not expected:
            return True
        header = request.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header.removeprefix("Basic ").strip()).decode("utf-8")
        except Exception:
            return False
        _username, separator, password = decoded.partition(":")
        return bool(separator) and hmac.compare_digest(password, expected)

    def _require_auth():
        if _authorized():
            return None
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="The Translator"'},
        )

    def _rooms():
        active_meeting = ntc_store.get_active_meeting()
        active_room_slug = active_meeting["room_slug"] if active_meeting else ""
        hosts = {host["room_slug"]: host for host in ntc_store.list_hosts(include_secret=False)}
        rooms = []
        for room in ntc_store.list_rooms():
            if room["slug"] not in VISIBLE_ROOM_SLUGS:
                continue
            host = hosts.get(room["slug"]) or {}
            runtime = host.get("runtime") or {}
            host_slug = host.get("slug", "")
            rooms.append(
                {
                    **room,
                    "caption_enabled": bool(room.get("transcription_enabled")),
                    "transcription_enabled": bool(room.get("transcription_enabled")),
                    "active": room["slug"] == active_room_slug,
                    "host_slug": host_slug,
                    "host_label": host.get("label", ""),
                    "host_online": bool(runtime.get("last_seen_at")),
                    "current_device": runtime.get("current_device", ""),
                    "translation_output_supported": host_slug == "hp-envy-16-ad0xx",
                    "translation_output_enabled": bool(host.get("translation_output_enabled")),
                    "translation_target_language": host.get("translation_target_language", "zh-CN"),
                    "translation_target_language_label": TRANSLATION_LANGUAGE_LABELS.get(
                        host.get("translation_target_language", "zh-CN"),
                        host.get("translation_target_language", "zh-CN"),
                    ),
                }
            )
        return rooms

    def _room_or_404(room_slug: str):
        room_slug = _canonical_room_slug(room_slug)
        for room in _rooms():
            if room["slug"] == room_slug:
                return room
        return None

    @app.before_request
    def require_panel_auth():
        if request.endpoint == "healthz":
            return None
        return _require_auth()

    @app.get("/healthz")
    def healthz():
        try:
            ntc_store.list_rooms()
            return jsonify({"ok": True, "timestamp": datetime.now(timezone.utc).isoformat()})
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            app.logger.exception("caption panel health check failed")
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.get("/")
    def index():
        rooms = _rooms()
        active = next((room for room in rooms if room["active"]), None)
        selected = active or next((room for room in rooms if room["caption_enabled"]), None) or (rooms[0] if rooms else None)
        if not selected:
            return render_template_string(
                CAPTION_PANEL_TEMPLATE,
                title=app.config["NTC_TRANSLATOR_TITLE"],
                rooms=[],
                room=None,
                latest_segment=None,
                segments=[],
                translation_jobs=[],
                recent_meetings=[],
                language_options=TRANSLATION_LANGUAGE_OPTIONS,
                poll_ms=app.config["NTC_TRANSLATOR_POLL_MS"],
            )
        return redirect(url_for("room_captions", room_slug=selected["slug"]))

    @app.get("/rooms/<room_slug>")
    def room_captions(room_slug: str):
        room = _room_or_404(room_slug)
        if not room:
            return jsonify({"error": "unknown room"}), 404
        recent_segments = list(reversed(ntc_store.list_transcript_segments(room_slug, limit=40)))
        return render_template_string(
            CAPTION_PANEL_TEMPLATE,
            title=app.config["NTC_TRANSLATOR_TITLE"],
            rooms=_rooms(),
            room=room,
            latest_segment=recent_segments[-1] if recent_segments else None,
            segments=recent_segments,
            translation_jobs=ntc_store.list_recent_translation_audio_jobs(room_slug, limit=8),
            recent_meetings=ntc_store.list_meeting_sessions(limit=8),
            language_options=TRANSLATION_LANGUAGE_OPTIONS,
            poll_ms=app.config["NTC_TRANSLATOR_POLL_MS"],
        )

    @app.post("/rooms/<room_slug>/transcription")
    @app.post("/rooms/<room_slug>/captions")
    def set_room_transcription(room_slug: str):
        room = _room_or_404(room_slug)
        if not room:
            return jsonify({"error": "unknown room"}), 404
        value = str(
            request.form.get("transcription_enabled", request.form.get("caption_enabled", ""))
        ).strip().lower()
        updated = ntc_store.set_room_transcription_enabled(
            room_slug,
            value in {"1", "true", "yes", "on"},
        )
        if not updated:
            return jsonify({"error": "unknown room"}), 404
        return redirect(url_for("room_captions", room_slug=room_slug))

    @app.post("/rooms/<room_slug>/translation-output")
    def set_translation_output(room_slug: str):
        room = _room_or_404(room_slug)
        if not room:
            return jsonify({"error": "unknown room"}), 404
        if not room["translation_output_supported"]:
            return jsonify({"error": "translation output is not supported for this room"}), 400
        value = str(request.form.get("translation_output_enabled", "")).strip().lower()
        ntc_store.set_host_translation_output_enabled(
            room["host_slug"],
            value in {"1", "true", "yes", "on"},
        )
        return redirect(url_for("room_captions", room_slug=room_slug))

    @app.post("/rooms/<room_slug>/translation-settings")
    def set_translation_settings(room_slug: str):
        room = _room_or_404(room_slug)
        if not room:
            return jsonify({"error": "unknown room"}), 404
        if not room["translation_output_supported"]:
            return jsonify({"error": "translation settings are not supported for this room"}), 400
        target_language = (request.form.get("target_language") or "zh-CN").strip()
        if target_language not in TRANSLATION_LANGUAGE_LABELS:
            return jsonify({"error": "unsupported target language"}), 400
        ntc_store.set_host_translation_target_language(room["host_slug"], target_language)
        return redirect(url_for("room_captions", room_slug=room_slug))

    def _translation_audio_dir() -> Path:
        path = Path(app.config.get("NTC_TRANSLATION_AUDIO_DIR") or "/app/data/translation-audio")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _safe_audio_filename(filename: str) -> str:
        safe = Path(filename or "").name
        if not safe or safe != filename or safe.startswith(".") or not safe.lower().endswith(".wav"):
            raise ValueError("invalid audio filename")
        return safe

    @app.post("/rooms/<room_slug>/translation-test")
    def queue_translation_test(room_slug: str):
        room = _room_or_404(room_slug)
        if not room:
            return jsonify({"error": "unknown room"}), 404
        if not room["translation_output_supported"]:
            return jsonify({"error": "translation output is not supported for this room"}), 400
        if not room["translation_output_enabled"]:
            return jsonify({"error": "turn translated audio output on before queueing a test WAV"}), 409
        filename = f"sample-{room['translation_target_language']}.wav"
        audio_path = _translation_audio_dir() / filename
        if not audio_path.exists():
            return jsonify({"error": f"sample WAV is not available for {room['translation_target_language']}"}), 404
        ntc_store.enqueue_translation_audio_job(
            room["host_slug"],
            room_slug=room_slug,
            target_language=room["translation_target_language"],
            audio_filename=filename,
            source_text="Welcome to NTC Newark WebCall. Please enter your four-digit PIN.",
            translated_text="欢迎来到NTC纽瓦克WebCall。请输入您的四位数PIN码。" if room["translation_target_language"] == "zh-CN" else "",
        )
        return redirect(url_for("room_captions", room_slug=room_slug))

    @app.get("/translation-audio/<filename>")
    def translation_audio_file(filename: str):
        try:
            safe = _safe_audio_filename(filename)
        except ValueError:
            return jsonify({"error": "invalid audio filename"}), 400
        audio_path = _translation_audio_dir() / safe
        if not audio_path.exists():
            return jsonify({"error": "audio file not found"}), 404
        return send_file(audio_path, mimetype="audio/wav", conditional=True)

    @app.get("/api/rooms/<room_slug>/segments")
    def room_segments(room_slug: str):
        room = _room_or_404(room_slug)
        if not room:
            return jsonify({"error": "unknown room"}), 404
        try:
            after_id = int(request.args.get("after_id", "0") or "0")
        except ValueError:
            after_id = 0
        transcription_payload = _transcription_segments_payload(room["slug"], after_id=after_id)
        if transcription_payload is not None:
            return jsonify(transcription_payload)
        segments = ntc_store.list_transcript_segments_after(room_slug, after_id=after_id, limit=80)
        return jsonify({"room_slug": room_slug, "segments": segments})

    def _transcription_segments_payload(room_slug: str, *, after_id: int):
        base_url = (app.config.get("NTC_TRANSCRIPTION_BASE_URL") or "").strip().rstrip("/")
        if not base_url:
            return None
        try:
            response = requests.get(
                f"{base_url}/api/internal/transcription/{quote(room_slug, safe='')}/segments",
                params={"after_id": max(0, int(after_id or 0))},
                timeout=3,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("room_slug") != room_slug or not isinstance(payload.get("segments"), list):
                raise RuntimeError("transcription service returned an invalid segment payload")
            return payload
        except Exception as exc:  # pragma: no cover - runtime fallback guard
            app.logger.warning("transcription service segment fetch failed room_slug=%s error=%s", room_slug, exc)
            return None

    return app


CAPTION_PANEL_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ title }}</title>
    <style>
      :root {
        --bg: #07121e;
        --panel: rgba(9, 20, 34, 0.88);
        --panel-2: rgba(18, 34, 53, 0.92);
        --panel-3: rgba(126, 197, 255, 0.08);
        --line: rgba(137, 202, 255, 0.18);
        --line-strong: rgba(137, 202, 255, 0.48);
        --text: #edf7ff;
        --muted: #9fb2c6;
        --accent: #8fd3ff;
        --accent-2: #8ff5c8;
        --good: #74ddb4;
        --good-soft: rgba(116, 221, 180, 0.12);
        --bad: #ff9a9a;
        --bad-soft: rgba(255, 154, 154, 0.12);
        --mono: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
        color-scheme: dark;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        background:
          radial-gradient(circle at 10% 0%, rgba(143, 211, 255, 0.22), transparent 30rem),
          radial-gradient(circle at 100% 12%, rgba(116, 221, 180, 0.14), transparent 24rem),
          linear-gradient(145deg, #050a12, var(--bg));
        color: var(--text);
        font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      main {
        width: min(1240px, calc(100vw - 32px));
        margin: 0 auto;
        padding: 30px 0 44px;
      }
      header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 1rem;
        margin-bottom: 1.15rem;
      }
      h1, h2, p { margin: 0; }
      h1 {
        margin-top: 0.24rem;
        font-size: clamp(34px, 5.2vw, 68px);
        letter-spacing: -0.055em;
        line-height: 0.92;
      }
      h2 { font-size: clamp(22px, 2vw, 30px); letter-spacing: -0.03em; }
      .eyebrow {
        color: var(--accent);
        font: 800 0.78rem var(--mono);
        letter-spacing: 0.18em;
        text-transform: uppercase;
      }
      .hero-note {
        max-width: 46rem;
        margin-top: 0.7rem;
        color: var(--muted);
        font-size: 1.02rem;
        line-height: 1.5;
      }
      .room-tabs {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr));
        gap: 0.8rem;
        flex-wrap: wrap;
        margin: 1.2rem 0 1rem;
      }
      .room-tab,
      .pill {
        border: 1px solid var(--line);
        border-radius: 999px;
        background: var(--panel-2);
        color: var(--text);
        text-decoration: none;
      }
      .room-tab {
        display: inline-flex;
        justify-content: space-between;
        align-items: center;
        gap: 0.75rem;
        min-width: 0;
        padding: 0.82rem 1rem;
      }
      .room-tab strong { font-size: 1rem; }
      .room-tab span {
        color: var(--muted);
        font: 800 0.68rem var(--mono);
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }
      .room-tab.is-active {
        border-color: var(--line-strong);
        background: linear-gradient(135deg, rgba(143, 211, 255, 0.17), rgba(116, 221, 180, 0.08));
      }
      .status-row {
        display: flex;
        gap: 0.55rem;
        flex-wrap: wrap;
        margin-top: 0.85rem;
      }
      .pill {
        padding: 0.46rem 0.7rem;
        color: var(--muted);
        font: 800 0.74rem var(--mono);
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }
      .pill.good {
        color: var(--good);
        border-color: rgba(116, 221, 180, 0.42);
        background: var(--good-soft);
      }
      .pill.bad {
        color: var(--bad);
        border-color: rgba(255, 154, 154, 0.42);
        background: var(--bad-soft);
      }
      .board {
        border: 1px solid var(--line);
        border-radius: 28px;
        background:
          linear-gradient(180deg, rgba(255, 255, 255, 0.035), transparent 22rem),
          var(--panel);
        box-shadow: 0 24px 90px rgba(0, 0, 0, 0.35);
        overflow: hidden;
      }
      .board-head {
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        align-items: center;
        padding: clamp(18px, 2.3vw, 30px);
        border-bottom: 1px solid var(--line);
        background: rgba(255, 255, 255, 0.025);
      }
      .board-head p { color: var(--muted); }
      .content-grid {
        display: grid;
        grid-template-columns: minmax(0, 1.08fr) minmax(20rem, 0.92fr);
        grid-template-areas:
          "latest controls"
          "transcript controls";
        gap: 1rem;
        padding: 1rem;
      }
      .latest-card,
      .caption-control,
      .translation-control,
      .translation-settings,
      .translation-jobs,
      .caption-line {
        border: 1px solid var(--line);
        background: rgba(255, 255, 255, 0.03);
        border-radius: 20px;
      }
      .latest-card {
        grid-area: latest;
        display: grid;
        align-content: start;
        min-height: 14rem;
        padding: clamp(18px, 2.5vw, 30px);
        background:
          radial-gradient(circle at top right, rgba(143, 211, 255, 0.16), transparent 18rem),
          rgba(255, 255, 255, 0.035);
      }
      .latest-text {
        color: var(--text);
        margin-top: 0.7rem;
        font-size: clamp(24px, 2.55vw, 40px);
        font-weight: 850;
        line-height: 1.12;
        letter-spacing: -0.02em;
      }
      .controls-stack {
        grid-area: controls;
        display: grid;
        align-content: start;
        gap: 1rem;
      }
      .caption-control,
      .translation-control {
        display: grid;
        gap: 1rem;
        padding: 1rem;
        background:
          linear-gradient(135deg, rgba(143, 211, 255, 0.10), rgba(116, 221, 180, 0.05)),
          rgba(255, 255, 255, 0.03);
      }
      .caption-control strong,
      .translation-control strong {
        display: block;
        margin-bottom: 0.25rem;
        font-size: 1.45rem;
        letter-spacing: -0.03em;
      }
      .caption-control p,
      .translation-control p {
        color: var(--muted);
        line-height: 1.4;
      }
      .gate-button {
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 0.72rem 1rem;
        background: rgba(255, 255, 255, 0.04);
        color: var(--text);
        cursor: pointer;
        font-weight: 850;
        white-space: nowrap;
        width: 100%;
      }
      .gate-button.is-on {
        border-color: rgba(116, 221, 180, 0.5);
        background: rgba(116, 221, 180, 0.14);
        color: var(--good);
      }
      .gate-button.is-off {
        border-color: rgba(255, 154, 154, 0.42);
        background: var(--bad-soft);
        color: var(--bad);
      }
      .translation-settings,
      .translation-jobs {
        padding: 1rem;
      }
      .settings-row {
        display: flex;
        align-items: end;
        gap: 0.7rem;
        flex-wrap: wrap;
        margin-top: 0.7rem;
      }
      .settings-row label {
        display: grid;
        gap: 0.35rem;
        color: var(--muted);
        font: 800 0.72rem var(--mono);
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }
      .settings-row select {
        min-width: 15rem;
        border: 1px solid var(--line);
        border-radius: 14px;
        background: var(--panel-2);
        color: var(--text);
        padding: 0.7rem 0.8rem;
        font: 800 1rem ui-sans-serif, system-ui, sans-serif;
      }
      .secondary-button,
      .audio-link {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        border: 1px solid var(--line);
        border-radius: 14px;
        background: rgba(143, 211, 255, 0.08);
        color: var(--text);
        padding: 0.72rem 0.9rem;
        text-decoration: none;
        font-weight: 850;
      }
      .secondary-button:disabled {
        cursor: not-allowed;
        opacity: 0.45;
      }
      .job-list {
        display: grid;
        gap: 0.5rem;
        margin-top: 0.7rem;
      }
      .job-row {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 0.8rem;
        border: 1px solid var(--line);
        border-radius: 16px;
        padding: 0.7rem;
      }
      .job-row p { color: var(--muted); }
      .transcript-panel {
        grid-area: transcript;
        display: grid;
        align-content: start;
        gap: 0.65rem;
        max-height: 46vh;
        overflow: auto;
      }
      .transcript-title {
        color: var(--muted);
        font: 800 0.76rem var(--mono);
        letter-spacing: 0.12em;
        text-transform: uppercase;
      }
      .captions {
        display: grid;
        gap: 0.55rem;
      }
      .caption-line {
        padding: 0.9rem 1rem;
        color: #d9eaff;
        font-size: clamp(15px, 1.18vw, 19px);
        font-weight: 700;
        line-height: 1.35;
      }
      .caption-word {
        display: inline-block;
        opacity: 1;
        transform: translateY(0);
      }
      .caption-word.is-new {
        opacity: 0;
        transform: translateY(0.24em);
        animation: caption-word-reveal 360ms cubic-bezier(0.22, 1, 0.36, 1) forwards;
        animation-delay: var(--word-delay, 0ms);
      }
      @keyframes caption-word-reveal {
        to {
          opacity: 1;
          transform: translateY(0);
        }
      }
      .caption-meta {
        margin-bottom: 0.38rem;
        color: var(--muted);
        font: 800 0.68rem var(--mono);
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }
      .empty {
        border: 1px dashed var(--line);
        border-radius: 20px;
        padding: 2rem;
        color: var(--muted);
        font-size: 1.25rem;
      }
      @media (max-width: 720px) {
        header { align-items: start; flex-direction: column; }
        .room-tab { width: 100%; }
        .board-head { align-items: start; flex-direction: column; }
        .content-grid {
          grid-template-columns: 1fr;
          grid-template-areas:
            "latest"
            "controls"
            "transcript";
        }
        .settings-row { align-items: stretch; flex-direction: column; }
        .gate-button { width: 100%; }
        .settings-row select,
        .secondary-button,
        .audio-link { width: 100%; }
        .job-row { align-items: stretch; flex-direction: column; }
        .latest-card { min-height: 12rem; }
        .transcript-panel { max-height: none; }
      }
      @media (prefers-reduced-motion: reduce) {
        .caption-word.is-new {
          animation: none;
          opacity: 1;
          transform: none;
        }
      }
    </style>
  </head>
  <body>
    <main>
      <header>
        <div>
          <div class="eyebrow">NTC Newark</div>
          <h1>{{ title }}</h1>
          <p class="hero-note">Internal transcription, translated audio tests, and room output controls. This panel is isolated from the public WebCall and dial-in audio path.</p>
        </div>
        {% if room %}
          <span class="pill {% if room.active %}good{% else %}bad{% endif %}">{{ "Meeting Live" if room.active else "Standby" }}</span>
        {% endif %}
      </header>

      {% if rooms %}
        <nav class="room-tabs" aria-label="Rooms">
          {% for item in rooms %}
            <a class="room-tab {% if room and item.slug == room.slug %}is-active{% endif %}" href="{{ url_for('room_captions', room_slug=item.slug) }}">
              <strong>{{ item.label }}</strong>
              <span>{{ "Live" if item.active else ("Ready" if item.host_online else "Offline") }}</span>
            </a>
          {% endfor %}
        </nav>
      {% endif %}

      {% if room %}
        <section class="board">
          <div class="board-head">
            <div>
              <h2>{{ room.label }}</h2>
              <p>{{ room.current_device or "Waiting for source audio metadata." }}</p>
            </div>
            <div class="status-row">
              <span class="pill {% if room.transcription_enabled %}good{% else %}bad{% endif %}">Transcription {{ "On" if room.transcription_enabled else "Off" }}</span>
              <span class="pill {% if room.host_online %}good{% else %}bad{% endif %}">Agent {{ "Seen" if room.host_online else "Not Seen" }}</span>
              <span class="pill" id="poll-status">Polling</span>
            </div>
          </div>
          <div class="content-grid">
            <div class="controls-stack">
              <section class="caption-control">
                <div>
                  <div class="caption-meta">Live Transcription Ingest</div>
                  <strong>Transcription is {{ "ON" if room.transcription_enabled else "OFF" }}</strong>
                  <p>This starts or stops the transcription listener for this room while source audio keeps running.</p>
                </div>
                <form method="post" action="{{ url_for('set_room_transcription', room_slug=room.slug) }}">
                  <input type="hidden" name="transcription_enabled" value="{{ "0" if room.transcription_enabled else "1" }}">
                  <button class="gate-button {% if room.transcription_enabled %}is-on{% else %}is-off{% endif %}" type="submit">
                    {{ "Turn Transcription OFF" if room.transcription_enabled else "Turn Transcription ON" }}
                  </button>
                </form>
              </section>
              {% if room.translation_output_supported %}
              <section class="translation-control">
                <div>
                  <div class="caption-meta">Translated Audio Output</div>
                  <strong>Room output is {{ "ON" if room.translation_output_enabled else "OFF" }}</strong>
                  <p>Target language: {{ room.translation_target_language_label }}. This only controls the Envy translator output.</p>
                </div>
                <form method="post" action="{{ url_for('set_translation_output', room_slug=room.slug) }}">
                  <input type="hidden" name="translation_output_enabled" value="{{ "0" if room.translation_output_enabled else "1" }}">
                  <button class="gate-button {% if room.translation_output_enabled %}is-on{% else %}is-off{% endif %}" type="submit">
                    {{ "Turn OFF" if room.translation_output_enabled else "Turn ON" }}
                  </button>
                </form>
              </section>
              <section class="translation-settings">
                <div class="caption-meta">Translation Settings</div>
                <form class="settings-row" method="post" action="{{ url_for('set_translation_settings', room_slug=room.slug) }}">
                  <label>
                    Target Language
                    <select name="target_language">
                      {% for option in language_options %}
                        <option value="{{ option.code }}" {% if option.code == room.translation_target_language %}selected{% endif %}>{{ option.label }}</option>
                      {% endfor %}
                    </select>
                  </label>
                  <button class="secondary-button" type="submit">Save Language</button>
                </form>
                <form class="settings-row" method="post" action="{{ url_for('queue_translation_test', room_slug=room.slug) }}">
                  <button class="secondary-button" type="submit" {% if not room.translation_output_enabled %}disabled{% endif %}>Queue Test WAV To Envy</button>
                  {% if not room.translation_output_enabled %}
                    <span class="caption-meta">Turn output ON before queueing a playback test.</span>
                  {% endif %}
                </form>
              </section>
              <section class="translation-jobs">
                <div class="caption-meta">Recent Translation WAVs</div>
                {% if translation_jobs %}
                  <div class="job-list">
                    {% for job in translation_jobs %}
                    <div class="job-row">
                      <div>
                        <strong>#{{ job.id }} · {{ job.target_language }} · {{ job.status }}</strong>
                        <p>{{ job.translated_text or job.source_text or job.audio_filename }}</p>
                      </div>
                      <a class="audio-link" href="{{ url_for('translation_audio_file', filename=job.audio_filename) }}">Open WAV</a>
                    </div>
                    {% endfor %}
                  </div>
                {% else %}
                  <p>No translated WAV jobs have been queued yet.</p>
                {% endif %}
              </section>
              {% endif %}
              <section class="translation-jobs">
                <div class="caption-meta">Recent Service Stats</div>
                {% if recent_meetings %}
                  <div class="job-list">
                    {% for meeting in recent_meetings %}
                    <div class="job-row">
                      <div>
                        <strong>#{{ meeting.id }} · {{ meeting.room_label }} · {{ meeting.started_at }}</strong>
                        <p>
                          {{ meeting.transcript_segment_count }} transcription segments ·
                          {{ meeting.transcript_character_count }} chars ·
                          {{ meeting.listener_count }} listener{% if meeting.listener_count != 1 %}s{% endif %} ·
                          {{ meeting.incident_count }} incident{% if meeting.incident_count != 1 %}s{% endif %}
                        </p>
                      </div>
                    </div>
                    {% endfor %}
                  </div>
                {% else %}
                  <p>No completed or active services have been tracked yet.</p>
                {% endif %}
              </section>
            </div>
            <section class="latest-card" aria-live="polite">
              <div class="caption-meta">Latest Transcription</div>
              <div class="latest-text" id="latest-caption">
                {% if latest_segment %}
                  {{ latest_segment.text }}
                {% else %}
                  Waiting for speech.
                {% endif %}
              </div>
            </section>
            <section class="transcript-panel">
              <div class="transcript-title">Transcript</div>
              <div class="captions" id="captions" data-room-slug="{{ room.slug }}" data-poll-ms="{{ poll_ms }}">
                {% for segment in segments %}
                  <article class="caption-line" data-segment-id="{{ segment.id }}">
                    <div class="caption-meta">{{ segment.received_at }}</div>
                    {{ segment.text }}
                  </article>
                {% endfor %}
                {% if not segments %}
                  <div class="empty" id="empty-state">No transcription lines received yet.</div>
                {% endif %}
              </div>
            </section>
          </div>
        </section>
      {% else %}
        <section class="board">
          <div class="content-grid">
            <div class="empty">No visible rooms are configured.</div>
          </div>
        </section>
      {% endif %}
    </main>
    {% if room %}
    <script>
      (() => {
        const board = document.getElementById("captions");
        const latestCaption = document.getElementById("latest-caption");
        if (!board) return;
        const status = document.getElementById("poll-status");
        const roomSlug = board.dataset.roomSlug;
        const pollMs = Number(board.dataset.pollMs || "1000");
        let lastId = Math.max(0, ...[...board.querySelectorAll("[data-segment-id]")].map((node) => Number(node.dataset.segmentId || "0")));
        const seen = new Set([...board.querySelectorAll("[data-segment-id]")].map((node) => node.dataset.segmentId));

        function renderWordStream(parent, text, animate = false) {
          parent.replaceChildren();
          const parts = String(text || "").replace(/\\s+/g, " ").trim().split(/(\\s+)/);
          let wordIndex = 0;
          for (const part of parts) {
            if (!part) continue;
            if (/^\\s+$/.test(part)) {
              parent.append(" ");
              continue;
            }
            const word = document.createElement("span");
            word.className = animate ? "caption-word is-new" : "caption-word";
            word.textContent = part;
            if (animate) {
              word.style.setProperty("--word-delay", `${Math.min(wordIndex * 42, 1400)}ms`);
              wordIndex += 1;
            }
            parent.appendChild(word);
          }
        }

        function appendSegment(segment) {
          const id = String(segment.id || "");
          if (!id || seen.has(id) || !segment.text) return;
          seen.add(id);
          lastId = Math.max(lastId, Number(id));
          document.getElementById("empty-state")?.remove();
          const article = document.createElement("article");
          article.className = "caption-line";
          article.dataset.segmentId = id;
          const meta = document.createElement("div");
          meta.className = "caption-meta";
          meta.textContent = segment.received_at || "";
          const text = document.createElement("span");
          renderWordStream(text, segment.text, true);
          article.append(meta, text);
          board.appendChild(article);
          if (latestCaption) renderWordStream(latestCaption, segment.text, true);
          while (board.querySelectorAll(".caption-line").length > 80) {
            board.querySelector(".caption-line")?.remove();
          }
          article.scrollIntoView({ block: "end", behavior: "smooth" });
        }

        async function poll() {
          try {
            const response = await fetch(`/api/rooms/${encodeURIComponent(roomSlug)}/segments?after_id=${lastId}`, { cache: "no-store" });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const payload = await response.json();
            for (const segment of payload.segments || []) appendSegment(segment);
            status.textContent = "Connected";
            status.classList.add("good");
            status.classList.remove("bad");
          } catch (error) {
            status.textContent = "Reconnecting";
            status.classList.add("bad");
            status.classList.remove("good");
          } finally {
            window.setTimeout(poll, Math.max(500, pollMs));
          }
        }
        window.setTimeout(poll, Math.max(500, pollMs));
      })();
    </script>
    {% endif %}
  </body>
</html>
"""


app = create_app()


if __name__ == "__main__":
    host = os.getenv("NTC_TRANSLATOR_HOST", os.getenv("NTC_CAPTIONS_HOST", "0.0.0.0"))
    port = int(os.getenv("NTC_TRANSLATOR_PORT", os.getenv("NTC_CAPTIONS_PORT", "1974")))
    app.run(host=host, port=port)
