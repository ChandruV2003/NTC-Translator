# Transcriptor

Simple local transcription for `.m4a`, `.mp3`, `.wav`, `.mp4`, and other audio/video files on macOS.

Transcriptor runs Whisper locally through `faster-whisper`, so there is no OpenAI API key and no per-file upload cost. The first run downloads the speech model, then later runs reuse it.

## Quick Start

```bash
git clone <your-github-repo-url> Transcriptor
cd Transcriptor
./install.sh
./transcriptor "/path/to/audio.m4a"
```

Outputs go to `transcripts/` by default.

Once this repo is on GitHub, the install flow for someone else is just:

```bash
git clone https://github.com/YOUR-USERNAME/Transcriptor.git
cd Transcriptor
./install.sh
./transcriptor "~/Downloads/audio.m4a"
```

## Examples

Transcribe one file:

```bash
./transcriptor "~/Downloads/interview.m4a"
```

Transcribe a whole folder:

```bash
./transcriptor "~/Downloads/audio-files"
```

Use a faster model:

```bash
./transcriptor "~/Downloads/audio-files" --model small
```

Use the default highest-quality model for rough audio:

```bash
./transcriptor "~/Downloads/audio-files" --model large-v3
```

Create subtitles too:

```bash
./transcriptor "~/Downloads/interview.m4a" --formats txt srt vtt
```

## Recommended Settings

For a 16 GB Apple Silicon MacBook:

- `small`: faster, usually good enough for clear audio
- `medium`: better for rough audio, slower, still reasonable on 16 GB RAM
- `large-v3`: best quality, much slower and heavier, but usable on 16 GB with the default `int8` compute type

Transcriptor defaults to `large-v3` for best quality. Use `--model medium` or `--model small` if speed matters more.

## Requirements

The installer handles the Python environment. It prefers Python 3.13, 3.12, or 3.11 because ML packages may not support the newest Homebrew Python immediately.

Homebrew is only needed if the Mac does not already have a compatible Python.

## Commands

```bash
./transcriptor --help
```

Common options:

- `--model small|medium|large-v3|large-v3-turbo`
- `--language en`
- `--output-dir transcripts`
- `--formats txt json srt vtt`
- `--device cpu`
- `--compute-type int8`

## Notes

The transcription stays local on the computer. No audio is sent to OpenAI by this tool.

## Publish To GitHub

If you have the GitHub CLI installed and logged in:

```bash
./scripts/publish_github.sh YOUR-USERNAME/Transcriptor private
```

Use `public` instead of `private` if you want the repo visible to anyone.
