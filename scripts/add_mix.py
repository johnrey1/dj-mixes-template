#!/usr/bin/env python3
"""Upload a DJ mix to R2 and register it in site/mixes.json."""

import argparse
import array
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from mutagen import File as MutagenFile
from mutagen.mp3 import MP3
from slugify import slugify

REPO_ROOT = Path(__file__).resolve().parent.parent
MIXES_JSON = REPO_ROOT / "site" / "mixes.json"

REQUIRED_ENV_VARS = [
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_PUBLIC_URL",
]


def parse_args():
    parser = argparse.ArgumentParser(description="Upload a mix to R2 and add it to the manifest.")
    parser.add_argument("audio_file", type=Path, help="Path to the local mp3 file")
    parser.add_argument("--title", required=True, help="Mix title")
    parser.add_argument("--date", default=None, help="Mix date (YYYY-MM-DD), defaults to today")
    parser.add_argument("--bpm", default=None, help="BPM range, e.g. '132-136'")
    parser.add_argument("--notes", default=None, help="Freeform notes")
    parser.add_argument(
        "--type",
        choices=["recording", "live", "event"],
        default="recording",
        help="Mix category: 'recording' (default), 'live', or 'event'",
    )
    parser.add_argument("--artist", default=None, help="DJ name (required for --type event)")
    parser.add_argument("--event-name", default=None, help="Event edition, e.g. 'Your Event Name 2026' (only used for --type event)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing mix with the same id (same title + date), in R2 and/or the manifest",
    )
    args = parser.parse_args()

    if args.type == "event" and not args.artist:
        parser.error("--artist is required when --type is 'event'")

    return args


def load_env():
    load_dotenv(REPO_ROOT / ".env")
    missing = [var for var in REQUIRED_ENV_VARS if not os.environ.get(var)]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        print("Copy scripts/r2_config.example.env to .env and fill in real values.", file=sys.stderr)
        sys.exit(1)
    return {var: os.environ[var] for var in REQUIRED_ENV_VARS}


def load_audio(audio_path: Path):
    try:
        return MutagenFile(audio_path)
    except Exception:
        return None


def validate_mp3(audio_path: Path, audio):
    if not isinstance(audio, MP3):
        detected = type(audio).__name__ if audio is not None else "unrecognized"
        print(
            f"'{audio_path}' doesn't look like a valid MP3 file (mutagen detected: {detected}). "
            "Convert it to MP3 first.",
            file=sys.stderr,
        )
        sys.exit(1)


def read_duration_seconds(audio):
    if audio is not None and audio.info is not None:
        return round(audio.info.length)
    return None


def extract_tracklist(audio):
    """Read a freeform tracklist from the USLT (lyrics) tag, one track per line."""
    if audio is None or audio.tags is None:
        return []
    frames = audio.tags.getall("USLT")
    if not frames:
        return []
    raw = frames[0].text.replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in raw.split("\n") if line.strip()]


PEAKS_NUM_POINTS = 800


def extract_peaks(audio_path: Path, num_points: int = PEAKS_NUM_POINTS):
    """Decode audio via ffmpeg into a downsampled peaks array for instant waveform
    rendering (WaveSurfer skips its own slow client-side decode when given peaks +
    duration). Returns None if ffmpeg isn't available or decoding fails — the
    frontend falls back to decoding the file itself in that case."""
    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found — skipping waveform peaks (frontend will decode the file itself).", file=sys.stderr)
        return None

    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(audio_path), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"Warning: failed to generate waveform peaks ({e}). Continuing without them.", file=sys.stderr)
        return None

    raw = proc.stdout[: len(proc.stdout) - (len(proc.stdout) % 2)]
    samples = array.array("h")
    samples.frombytes(raw)
    if not samples:
        return None

    chunk_size = max(1, len(samples) // num_points)
    peaks = []
    for i in range(0, len(samples), chunk_size):
        chunk = samples[i : i + chunk_size]
        if not chunk:
            continue
        peaks.append(round(max(abs(s) for s in chunk) / 32768.0, 3))
    return peaks


def build_id(title: str, mix_date: str) -> str:
    return f"{mix_date}-{slugify(title)}"


def r2_client(env):
    return boto3.client(
        "s3",
        endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
    )


def r2_object_exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def load_manifest():
    if MIXES_JSON.exists():
        with MIXES_JSON.open("r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_manifest(mixes):
    with MIXES_JSON.open("w", encoding="utf-8") as f:
        json.dump(mixes, f, indent=2)
        f.write("\n")


def main():
    args = parse_args()

    if not args.audio_file.exists():
        print(f"File not found: {args.audio_file}", file=sys.stderr)
        sys.exit(1)

    audio = load_audio(args.audio_file)
    validate_mp3(args.audio_file, audio)

    env = load_env()
    client = r2_client(env)

    mix_date = args.date or date.today().isoformat()
    try:
        datetime.strptime(mix_date, "%Y-%m-%d")
    except ValueError:
        print(f"Invalid --date '{mix_date}', expected YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    mix_id = build_id(args.title, mix_date)
    object_key = f"mixes/{mix_id}.mp3"

    mixes = load_manifest()
    already_in_manifest = any(m.get("id") == mix_id for m in mixes)
    already_in_r2 = r2_object_exists(client, env["R2_BUCKET"], object_key)

    if (already_in_manifest or already_in_r2) and not args.force:
        print(
            f"A mix with id '{mix_id}' already exists "
            f"({'manifest' if already_in_manifest else ''}"
            f"{' + ' if already_in_manifest and already_in_r2 else ''}"
            f"{'R2' if already_in_r2 else ''}). Re-run with --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    duration_seconds = read_duration_seconds(audio)
    tracklist = extract_tracklist(audio)
    peaks = extract_peaks(args.audio_file)

    print(f"Uploading {args.audio_file} to r2://{env['R2_BUCKET']}/{object_key} ...")
    client.upload_file(str(args.audio_file), env["R2_BUCKET"], object_key)

    entry = {
        "id": mix_id,
        "title": args.title,
        "date": mix_date,
        "type": args.type,
        "artist": args.artist,
        "event_name": args.event_name,
        "duration_seconds": duration_seconds,
        "bpm_range": args.bpm,
        "notes": args.notes,
        "tracklist": tracklist,
        "peaks": peaks,
        "audio_url": f"{env['R2_PUBLIC_URL'].rstrip('/')}/{object_key}",
    }

    mixes = [m for m in mixes if m.get("id") != mix_id]
    mixes.insert(0, entry)
    save_manifest(mixes)

    print(f"Added '{args.title}' to {MIXES_JSON.relative_to(REPO_ROOT)}")
    print()
    print("Next step:")
    print(f'  git add -A && git commit -m "Add mix: {args.title}" && git push')


if __name__ == "__main__":
    main()
