# NTC Translator

Internal transcription and translation control panel for NTC Newark.

This service is intentionally separate from `NTC-WebCall` and `NTC-Transcription`. WebCall can publish audio for transcription, `NTC-Transcription` owns the public transcript display/API, and this project owns internal translator controls, translation settings, and translated audio output.

## Runtime

- Panel port: `1974` in-container, usually published as `6767`
- Entry point: `ntc_translator_panel:app`
- Runtime data is expected under `data/` and is not committed
- Environment variables use the `NTC_*` prefix

## Endpoints

- Internal control panel: `/rooms/<room-slug>`
- Internal transcript polling API: `/api/rooms/<room-slug>/segments`

The public transcription display is owned by `NTC-Transcription`.

## Local Validation

```bash
python3 -m py_compile ntc_translator_app.py ntc_translator_panel.py
python3 -m unittest test_ntc_translator_panel.py
```
