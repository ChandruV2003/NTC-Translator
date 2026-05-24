import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ntc_translator_app import create_app


def _basic_auth(password: str) -> dict[str, str]:
    encoded = base64.b64encode(f"ntc:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


class NTCCaptionPanelTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "ntccast.db"
        self.app = create_app(
            {
                "TESTING": True,
                "NTC_DB_PATH": str(self.db_path),
                "NTC_TRANSLATOR_PANEL_PASSWORD": "panel-password",
                "NTC_TRANSLATOR_AUTH_ENABLED": "1",
                "NTC_ADMIN_PASSWORD": "",
                "NTC_TRANSCRIPTION_BASE_URL": "",
                "NTC_TRANSLATION_AUDIO_DIR": str(Path(self.tempdir.name) / "translation-audio"),
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_caption_panel_requires_password_and_shows_recent_segments(self):
        self.app.ntc_store.record_transcript_segment(
            "room-a",
            host_slug="hp-envy-16-ad0xx",
            provider="local_cmd",
            model="tiny",
            text="Testing internal captions.",
        )

        denied = self.client.get("/rooms/room-a")
        self.assertEqual(denied.status_code, 401)

        response = self.client.get("/rooms/room-a", headers=_basic_auth("panel-password"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"The Translator", response.data)
        self.assertIn(b"NTC Newark", response.data)
        self.assertIn(b"Latest Caption", response.data)
        self.assertIn(b"Transcript", response.data)
        self.assertIn(b"Testing internal captions.", response.data)
        self.assertIn(b"Room A", response.data)
        self.assertIn(b'data-ntc-branding="ntc-bg"', response.data)
        self.assertNotIn(b"Open Captions", response.data)

    def test_caption_panel_controls_room_caption_ingest(self):
        response = self.client.get("/rooms/room-a", headers=_basic_auth("panel-password"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Live Caption Ingest", response.data)
        self.assertIn(b"Captions are OFF", response.data)

        enabled = self.client.post(
            "/rooms/room-a/captions",
            data={"caption_enabled": "1"},
            headers=_basic_auth("panel-password"),
            follow_redirects=True,
        )

        self.assertEqual(enabled.status_code, 200)
        self.assertIn(b"Captions are ON", enabled.data)
        self.assertTrue(self.app.ntc_store.get_room("room-a")["transcription_enabled"])

        disabled = self.client.post(
            "/rooms/room-a/captions",
            data={"caption_enabled": "0"},
            headers=_basic_auth("panel-password"),
            follow_redirects=True,
        )

        self.assertEqual(disabled.status_code, 200)
        self.assertIn(b"Captions are OFF", disabled.data)
        self.assertFalse(self.app.ntc_store.get_room("room-a")["transcription_enabled"])

    def test_caption_panel_api_returns_segments_after_id(self):
        first_id = self.app.ntc_store.record_transcript_segment(
            "room-a",
            host_slug="hp-envy-16-ad0xx",
            provider="local_cmd",
            model="tiny",
            text="First line.",
        )
        self.app.ntc_store.record_transcript_segment(
            "room-a",
            host_slug="hp-envy-16-ad0xx",
            provider="local_cmd",
            model="tiny",
            text="Second line.",
        )

        response = self.client.get(
            f"/api/rooms/room-a/segments?after_id={first_id}",
            headers=_basic_auth("panel-password"),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([segment["text"] for segment in payload["segments"]], ["Second line."])

    def test_caption_panel_api_can_read_segments_from_transcription_service(self):
        self.app.config["NTC_TRANSCRIPTION_BASE_URL"] = "http://ntc-transcription:1975/"
        mocked_response = Mock()
        mocked_response.json.return_value = {
            "room_slug": "room-a",
            "segments": [
                {
                    "id": 99,
                    "room_slug": "room-a",
                    "received_at": "2026-05-24T20:00:00+00:00",
                    "text": "Segment from transcription container.",
                    "is_final": True,
                }
            ],
        }

        with patch("ntc_translator_app.requests.get", return_value=mocked_response) as mocked_get:
            response = self.client.get(
                "/api/rooms/room-a/segments?after_id=12",
                headers=_basic_auth("panel-password"),
            )

        self.assertEqual(response.status_code, 200)
        mocked_response.raise_for_status.assert_called_once_with()
        mocked_get.assert_called_once()
        args, kwargs = mocked_get.call_args
        self.assertEqual(args[0], "http://ntc-transcription:1975/api/internal/transcription/room-a/segments")
        self.assertEqual(kwargs["params"], {"after_id": 12})
        payload = response.get_json()
        self.assertEqual([segment["text"] for segment in payload["segments"]], ["Segment from transcription container."])

    def test_caption_panel_can_disable_auth_for_tailscale_only_port(self):
        self.app.config["NTC_TRANSLATOR_AUTH_ENABLED"] = "0"

        response = self.client.get("/rooms/room-a")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"The Translator", response.data)

    def test_caption_panel_controls_envy_translation_output_gate(self):
        response = self.client.get("/rooms/room-a", headers=_basic_auth("panel-password"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Translated Audio Output", response.data)
        self.assertIn(b"Room output is OFF", response.data)
        self.assertIn(b"Turn ON", response.data)
        self.assertIn(b"Translation Settings", response.data)
        self.assertIn(b"Chinese (Mandarin)", response.data)

        enabled = self.client.post(
            "/rooms/room-a/translation-output",
            data={"translation_output_enabled": "1"},
            headers=_basic_auth("panel-password"),
            follow_redirects=True,
        )

        self.assertEqual(enabled.status_code, 200)
        self.assertIn(b"Room output is ON", enabled.data)
        self.assertIn(b"Turn OFF", enabled.data)
        host = self.app.ntc_store.get_host("hp-envy-16-ad0xx")
        self.assertTrue(host["translation_output_enabled"])

        disabled = self.client.post(
            "/rooms/room-a/translation-output",
            data={"translation_output_enabled": "0"},
            headers=_basic_auth("panel-password"),
            follow_redirects=True,
        )

        self.assertEqual(disabled.status_code, 200)
        host = self.app.ntc_store.get_host("hp-envy-16-ad0xx")
        self.assertFalse(host["translation_output_enabled"])

    def test_caption_panel_updates_translation_target_language(self):
        response = self.client.post(
            "/rooms/room-a/translation-settings",
            data={"target_language": "es"},
            headers=_basic_auth("panel-password"),
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Spanish", response.data)
        host = self.app.ntc_store.get_host("hp-envy-16-ad0xx")
        self.assertEqual(host["translation_target_language"], "es")

    def test_caption_panel_queues_translation_test_wav_and_serves_file(self):
        audio_dir = Path(self.tempdir.name) / "translation-audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / "sample-zh-CN.wav").write_bytes(b"RIFFfake-wav")
        self.app.ntc_store.set_host_translation_output_enabled("hp-envy-16-ad0xx", True)

        response = self.client.post(
            "/rooms/room-a/translation-test",
            headers=_basic_auth("panel-password"),
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Recent Translation WAVs", response.data)
        self.assertIn(b"Open WAV", response.data)
        jobs = self.app.ntc_store.list_recent_translation_audio_jobs("room-a")
        self.assertEqual(jobs[0]["audio_filename"], "sample-zh-CN.wav")

        playback = self.client.get(
            "/translation-audio/sample-zh-CN.wav",
            headers=_basic_auth("panel-password"),
        )
        self.assertEqual(playback.status_code, 200)
        self.assertEqual(playback.data, b"RIFFfake-wav")

    def test_caption_panel_rejects_translation_test_when_output_is_off(self):
        audio_dir = Path(self.tempdir.name) / "translation-audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / "sample-zh-CN.wav").write_bytes(b"RIFFfake-wav")

        response = self.client.post(
            "/rooms/room-a/translation-test",
            headers=_basic_auth("panel-password"),
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.app.ntc_store.list_recent_translation_audio_jobs("room-a"), [])

    def test_caption_panel_rejects_translation_output_for_unsupported_room(self):
        response = self.client.get("/rooms/room-b", headers=_basic_auth("panel-password"))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Translated Audio Output", response.data)

        rejected = self.client.post(
            "/rooms/room-b/translation-output",
            data={"translation_output_enabled": "1"},
            headers=_basic_auth("panel-password"),
        )

        self.assertEqual(rejected.status_code, 400)
        host = self.app.ntc_store.get_host("hp-pavilion-14m-ba1xx")
        self.assertFalse(host["translation_output_enabled"])

    def test_caption_panel_health_does_not_require_password(self):
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
