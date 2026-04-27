from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from faster_whisper import WhisperModel
from tqdm import tqdm


DEFAULT_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".flac",
    ".m4a",
    ".m4v",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    source: str
    model: str
    language: str
    duration: float
    text: str
    segments: list[Segment]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcriptor",
        description="Transcribe audio/video files locally with faster-whisper.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="Audio/video file(s) or folder(s) to transcribe.",
    )
    parser.add_argument(
        "--model",
        default="large-v3",
        help="Whisper model to use. Try small for speed or large-v3 for best quality. Default: large-v3.",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Spoken language code, or 'auto' to detect. Default: en.",
    )
    parser.add_argument(
        "--output-dir",
        default="transcripts",
        help="Folder for transcript outputs. Default: transcripts.",
    )
    parser.add_argument(
        "--formats",
        nargs="+",
        choices=("txt", "json", "srt", "vtt"),
        default=["txt"],
        help="Output formats. Default: txt.",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="Inference device. Use cpu on Apple Silicon for reliable setup. Default: cpu.",
    )
    parser.add_argument(
        "--compute-type",
        default="int8",
        help="Inference compute type. int8 is memory-friendly. Default: int8.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="When a folder is provided, search it recursively.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-transcribe even if all requested output files already exist.",
    )
    return parser


def expand_user_path(raw_path: str) -> Path:
    return Path(raw_path).expanduser().resolve()


def discover_files(raw_paths: Iterable[str], recursive: bool) -> list[Path]:
    files: list[Path] = []

    for raw_path in raw_paths:
        path = expand_user_path(raw_path)
        if path.is_file():
            files.append(path)
            continue

        if path.is_dir():
            iterator = path.rglob("*") if recursive else path.iterdir()
            files.extend(
                child
                for child in iterator
                if child.is_file() and child.suffix.lower() in DEFAULT_EXTENSIONS
            )
            continue

        raise FileNotFoundError(f"Path not found: {path}")

    unique_files = sorted(set(files))
    if not unique_files:
        raise FileNotFoundError("No supported audio/video files found.")

    return unique_files


def safe_stem(path: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem).strip("._-")
    return stem or "transcript"


def output_paths(source: Path, output_dir: Path, formats: Iterable[str]) -> dict[str, Path]:
    stem = safe_stem(source)
    return {fmt: output_dir / f"{stem}.{fmt}" for fmt in formats}


def should_skip(source: Path, output_dir: Path, formats: Iterable[str], overwrite: bool) -> bool:
    if overwrite:
        return False
    return all(path.exists() for path in output_paths(source, output_dir, formats).values())


def format_timestamp(seconds: float, separator: str) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{millis:03}"


def write_txt(path: Path, transcript: Transcript) -> None:
    path.write_text(transcript.text.strip() + "\n", encoding="utf-8")


def write_json(path: Path, transcript: Transcript) -> None:
    payload = asdict(transcript)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_srt(path: Path, transcript: Transcript) -> None:
    blocks = []
    for index, segment in enumerate(transcript.segments, start=1):
        start = format_timestamp(segment.start, ",")
        end = format_timestamp(segment.end, ",")
        blocks.append(f"{index}\n{start} --> {end}\n{segment.text.strip()}")
    path.write_text("\n\n".join(blocks).strip() + "\n", encoding="utf-8")


def write_vtt(path: Path, transcript: Transcript) -> None:
    blocks = ["WEBVTT"]
    for segment in transcript.segments:
        start = format_timestamp(segment.start, ".")
        end = format_timestamp(segment.end, ".")
        blocks.append(f"{start} --> {end}\n{segment.text.strip()}")
    path.write_text("\n\n".join(blocks).strip() + "\n", encoding="utf-8")


def save_transcript(source: Path, output_dir: Path, formats: Iterable[str], transcript: Transcript) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = output_paths(source, output_dir, formats)

    for fmt, path in paths.items():
        if fmt == "txt":
            write_txt(path, transcript)
        elif fmt == "json":
            write_json(path, transcript)
        elif fmt == "srt":
            write_srt(path, transcript)
        elif fmt == "vtt":
            write_vtt(path, transcript)


def transcribe_file(
    model: WhisperModel,
    source: Path,
    model_name: str,
    language: str | None,
) -> Transcript:
    segments_iter, info = model.transcribe(
        str(source),
        language=language,
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
    )

    segments: list[Segment] = []
    for segment in segments_iter:
        text = segment.text.strip()
        if text:
            segments.append(Segment(start=segment.start, end=segment.end, text=text))

    full_text = " ".join(segment.text for segment in segments).strip()
    detected_language = getattr(info, "language", None) or language or "unknown"
    duration = float(getattr(info, "duration", 0.0) or 0.0)

    return Transcript(
        source=str(source),
        model=model_name,
        language=detected_language,
        duration=duration,
        text=full_text,
        segments=segments,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        files = discover_files(args.paths, recursive=args.recursive)
    except FileNotFoundError as exc:
        parser.error(str(exc))

    output_dir = expand_user_path(args.output_dir)
    pending_files = [
        path
        for path in files
        if not should_skip(path, output_dir, args.formats, overwrite=args.overwrite)
    ]

    skipped_count = len(files) - len(pending_files)
    if skipped_count:
        print(f"Skipping {skipped_count} file(s) with existing outputs. Use --overwrite to redo them.")

    if not pending_files:
        print("Nothing to transcribe.")
        return 0

    language = None if args.language.lower() == "auto" else args.language

    print(f"Loading model '{args.model}' on {args.device} ({args.compute_type})...", flush=True)
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)

    failures: list[tuple[Path, Exception]] = []
    for source in tqdm(pending_files, desc="Transcribing", unit="file"):
        try:
            transcript = transcribe_file(model, source, model_name=args.model, language=language)
            save_transcript(source, output_dir, args.formats, transcript)
        except Exception as exc:  # noqa: BLE001 - CLI should continue through batch failures.
            failures.append((source, exc))
            tqdm.write(f"Failed: {source} ({exc})")

    if failures:
        print("\nSome files failed:", file=sys.stderr)
        for source, exc in failures:
            print(f"  - {source}: {exc}", file=sys.stderr)
        return 1

    print(f"Done. Outputs written to: {output_dir}")
    return 0
