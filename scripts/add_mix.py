#!/usr/bin/env python3
"""Upload a DJ mix to R2 and register it in site/mixes.json."""

import argparse
import array
import json
import math
import os
import re
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
    parser.add_argument(
        "--suggest-timestamps",
        action="store_true",
        help=(
            "Propose a start time for each tracklist entry that has no explicit "
            "timestamp prefix, and write the result into the manifest (review it "
            "in 'git diff' before pushing). Requires a tracklist in the mp3's USLT tag."
        ),
    )
    parser.add_argument(
        "--suggest-only",
        action="store_true",
        help="Print the suggested tracklist and exit — no R2 upload, no manifest write.",
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


def _clock_to_seconds(clock: str) -> int:
    """'12:34' -> 754, '1:02:05' -> 3725."""
    seconds = 0
    for part in clock.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds


_WRAPPED_TS_RE = re.compile(
    r"^[\[(]\s*(\d{1,2}(?::\d{2}){1,2})\s*[\])]\s*[-–—]?\s*(.*)$"
)
_BARE_TS_RE = re.compile(
    r"^(\d{1,2}(?::\d{2}){1,2})(?:\s*[-–—]\s*|\s{2,})(.*)$"
)


def parse_timestamp_prefix(line: str):
    """Split a leading timestamp off a tracklist line.

    Returns (seconds, title). ``seconds`` is None when the line has no clearly
    delimited leading timestamp — a bare "12:34 Title" with a single space is
    treated as plain text so titles like "2:54 AM - Artist" survive intact.

    Accepted forms:
        [12:34] Title      (12:34) Title
        12:34 - Title      12:34 – Title      12:34  Title   (2+ spaces)
    Both ``m:ss`` and ``h:mm:ss`` clocks are supported.
    """
    text = line.strip()
    m = _WRAPPED_TS_RE.match(text) or _BARE_TS_RE.match(text)
    if m:
        return _clock_to_seconds(m.group(1)), m.group(2).strip()
    return None, text


def extract_tracklist(audio):
    """Read a freeform tracklist from the USLT (lyrics) tag, one track per line.

    A line carrying a leading timestamp (see ``parse_timestamp_prefix``) becomes
    ``{"time_seconds": int, "title": str}``; every other line stays a plain string.
    """
    if audio is None or audio.tags is None:
        return []
    frames = audio.tags.getall("USLT")
    if not frames:
        return []
    raw = frames[0].text.replace("\r\n", "\n").replace("\r", "\n")
    entries = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        seconds, title = parse_timestamp_prefix(line)
        entries.append(title if seconds is None else {"time_seconds": seconds, "title": title})
    return entries


DECODE_SAMPLE_RATE = 8000
PEAKS_NUM_POINTS = 800
NOVELTY_BIN_SECONDS = 0.5


def decode_mono_pcm(audio_path: Path, ar: int = DECODE_SAMPLE_RATE):
    """Decode audio to a mono signed-16-bit PCM sample array via ffmpeg.

    Returns None when ffmpeg is unavailable or decoding fails — callers treat that
    as "no audio analysis available" (the frontend decodes the file itself for the
    waveform; timestamp suggestion falls back to an even split)."""
    if shutil.which("ffmpeg") is None:
        print(
            "ffmpeg not found — skipping audio analysis (waveform peaks + timestamp nudging).",
            file=sys.stderr,
        )
        return None

    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(audio_path), "-ac", "1", "-ar", str(ar), "-f", "s16le", "-"],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"Warning: ffmpeg decode failed ({e}). Continuing without audio analysis.", file=sys.stderr)
        return None

    raw = proc.stdout[: len(proc.stdout) - (len(proc.stdout) % 2)]
    samples = array.array("h")
    samples.frombytes(raw)
    return samples if len(samples) else None


def downsample_maxabs(samples, num_points: int):
    """Downsample a PCM array to ``num_points`` normalized (0..1) max-abs magnitudes.

    This is the waveform-peaks representation WaveSurfer consumes (peaks + duration
    let it skip its own slow client-side decode)."""
    if not samples:
        return None
    chunk_size = max(1, len(samples) // num_points)
    out = []
    for i in range(0, len(samples), chunk_size):
        chunk = samples[i : i + chunk_size]
        if not chunk:
            continue
        out.append(round(max(abs(s) for s in chunk) / 32768.0, 3))
    return out


def _novelty_curve(samples, ar: int = DECODE_SAMPLE_RATE, bin_seconds: float = NOVELTY_BIN_SECONDS):
    """Positive first difference of a smoothed log-energy envelope.

    Peaks mark moments where loudness rises — a decent proxy for where a new track
    enters a mix. Returns one value per ``bin_seconds`` window, or None if the
    audio is too short to analyse."""
    if not samples:
        return None
    num_bins = max(1, int(len(samples) / (ar * bin_seconds)))
    env = downsample_maxabs(samples, num_bins)
    if not env or len(env) < 3:
        return None
    logs = [math.log(e + 1e-4) for e in env]
    smooth = logs[:]
    for i in range(1, len(logs) - 1):
        smooth[i] = (logs[i - 1] + logs[i] + logs[i + 1]) / 3.0
    nov = [0.0]
    for i in range(1, len(smooth)):
        delta = smooth[i] - smooth[i - 1]
        nov.append(delta if delta > 0 else 0.0)
    return nov


def _nudge_to_novelty(base, window, lower, upper, novelty, bin_secs):
    """Return the time of the strongest novelty spike within ``base ± window``,
    constrained to ``(lower, upper)``. Falls back to ``base`` when nothing stands out."""
    lo_t = max(lower, base - window)
    hi_t = min(upper, base + window)
    if hi_t <= lo_t:
        return base
    lo_b = max(0, int(lo_t / bin_secs))
    hi_b = min(len(novelty) - 1, int(hi_t / bin_secs))
    best_b, best_v = None, 0.0
    for b in range(lo_b, hi_b + 1):
        if novelty[b] > best_v:
            best_v, best_b = novelty[b], b
    if best_b is None:
        return base
    return (best_b + 0.5) * bin_secs


def suggest_timestamps(tracklist, duration_seconds, samples, ar: int = DECODE_SAMPLE_RATE):
    """Propose a start time (seconds) for every track.

    ``tracklist`` is the list returned by ``extract_tracklist`` (str |
    {"time_seconds", "title"}). Entries that already carry ``time_seconds`` are
    anchors and are never moved. Each un-anchored run between anchors is placed by
    an even split across that span, then — when ``samples`` is available — each
    guess is nudged toward a nearby energy transition without crossing a
    neighbour. Track 1 anchors at 0 when it has no explicit time.

    Returns a list of ``{"time_seconds": int, "title": str}``, strictly ascending.
    Beatmatched blends have no real loudness break, so treat the output as a
    starting point to hand-correct, not ground truth."""
    n = len(tracklist)
    titles = [t["title"] if isinstance(t, dict) else t for t in tracklist]
    times = [float(t["time_seconds"]) if isinstance(t, dict) else None for t in tracklist]
    if n and times[0] is None:
        times[0] = 0.0

    total = float(duration_seconds) if duration_seconds else None
    novelty = _novelty_curve(samples, ar=ar) if samples else None
    bin_secs = (len(samples) / float(ar)) / len(novelty) if novelty else None

    i = 0
    while i < n:
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < n and times[j] is None:
            j += 1
        left = times[i - 1]
        if j < n:
            right = times[j]
        elif total is not None:
            right = total
        else:
            right = left + (j - i + 1) * 60.0
        slot = (right - left) / (j - i + 1)
        for k in range(i, j):
            base = left + slot * (k - i + 1)
            lower = times[k - 1] + 1.0
            upper = right - 1.0 if j < n else right
            if novelty and bin_secs:
                base = _nudge_to_novelty(base, slot / 2.0, lower, upper, novelty, bin_secs)
            times[k] = min(max(base, lower), upper)
        i = j

    out = []
    prev = -1
    for t, title in zip(times, titles):
        value = int(round(t if t is not None else 0))
        if value <= prev:
            value = prev + 1
        prev = value
        out.append({"time_seconds": value, "title": title})
    return out


def format_clock(seconds: int) -> str:
    """Inverse of the tracklist-prefix clock: 754 -> '12:34', 3725 -> '1:02:05'."""
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def print_tracklist_table(tracklist):
    print("\nSuggested tracklist (review before committing — see 'git diff'):")
    for entry in tracklist:
        print(f"  {format_clock(entry['time_seconds']):>8}  {entry['title']}")
    print()


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

    mix_date = args.date or date.today().isoformat()
    try:
        datetime.strptime(mix_date, "%Y-%m-%d")
    except ValueError:
        print(f"Invalid --date '{mix_date}', expected YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    mix_id = build_id(args.title, mix_date)
    object_key = f"mixes/{mix_id}.mp3"

    duration_seconds = read_duration_seconds(audio)
    tracklist = extract_tracklist(audio)

    want_suggestions = args.suggest_timestamps or args.suggest_only
    if want_suggestions and not tracklist:
        print(
            "--suggest-timestamps needs a tracklist in the mp3's USLT (lyrics) tag; none found.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --suggest-only is a read-only preview: no R2 credentials, no upload, no
    # manifest write. Everything below this point that touches R2 is skipped.
    env = client = None
    mixes = []
    if not args.suggest_only:
        env = load_env()
        client = r2_client(env)
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

    # One ffmpeg decode feeds both the waveform peaks and (optionally) the
    # timestamp nudging. None => ffmpeg missing or decode failed; both consumers
    # degrade gracefully.
    samples = decode_mono_pcm(args.audio_file)
    peaks = downsample_maxabs(samples, PEAKS_NUM_POINTS) if samples is not None else None

    if want_suggestions:
        if samples is None:
            print("Using an even split — no audio analysis available.", file=sys.stderr)
        tracklist = suggest_timestamps(tracklist, duration_seconds, samples)
        print_tracklist_table(tracklist)
        if args.suggest_only:
            return

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
