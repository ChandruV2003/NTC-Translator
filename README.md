# NTC Transcriptor

Internal transcription and translation control panel for NTC Newark.

This service is intentionally separate from `NTC-WebCall`. WebCall can publish audio for transcription, while this project owns transcription UI, translation settings, local/Mac mini transcription helpers, and translated audio output.

## Runtime

- Panel port: `1974` in-container, usually published as `6767`
- Entry point: `ntc_transcriptor_panel:app`
- Runtime data is expected under `data/` and is not committed
- Environment variables use the `NTC_*` prefix

## Endpoints

- Internal control panel: `/rooms/<room-slug>`
- Public Room A transcription display: `/transcribe`
- Public room-specific transcription display: `/transcribe/<room-slug>`
- Public read-only transcript polling API: `/api/public/transcribe/<room-slug>/segments`

The public transcription display is intentionally read-only. It does not expose caption ingest controls, translation settings, translated audio output, or internal room controls.

## Local Validation

```bash
python3 -m py_compile ntc_translator_app.py ntc_transcriptor_panel.py
python3 -m pytest test_ntc_translator_panel.py
```
