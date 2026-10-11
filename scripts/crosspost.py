#!/usr/bin/env python3
"""Cross-post a mix hosted on someone else's site into site/mixes.json.

Writes a *pointer*, not a copy: the entry names the source manifest + mix id and
carries only the fields you chose to override locally. The browser resolves the
rest (title, date, tracklist, peaks, audio_url, ...) from the source's
mixes.json at load time, so the source stays the single source of truth and
nothing is uploaded to your R2 bucket. No R2 credentials, ffmpeg, or mp3 needed.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import add_mix

# Cloudflare 403s urllib's default "Python-urllib" agent on pages.dev sites.
USER_AGENT = "dj-mixes-crosspost/1.0 (+https://github.com/johnrey1/dj-mixes-template)"
FETCH_TIMEOUT_SECONDS = 15
MANIFEST_NAME = "mixes.json"


def parse_share_link(link: str):
    """Split a share link into ``(manifest_url, mix_id)``.

    ``https://host/?mix=<id>`` -> (``https://host/mixes.json``, ``<id>``). The
    manifest is assumed to live next to the page the link points at, so sites
    served from a sub-path work too. Raises ValueError on anything that isn't an
    http(s) link carrying a ``mix`` parameter."""
    parts = urllib.parse.urlsplit(link.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("expected an http(s) share link like https://host/?mix=<id>")
    mix_id = (urllib.parse.parse_qs(parts.query).get("mix") or [""])[0].strip()
    if not mix_id:
        raise ValueError("share link has no ?mix=<id> parameter")

    path = parts.path or "/"
    last = path.rsplit("/", 1)[-1]
    if "." in last:                    # .../index.html -> its directory
        path = path[: len(path) - len(last)]
    elif not path.endswith("/"):       # .../dj -> .../dj/
        path += "/"
    manifest_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path + MANIFEST_NAME, "", ""))
    return manifest_url, mix_id


def fetch_manifest(url: str):
    """GET a mixes.json and return the parsed list. Raises RuntimeError with a
    human-readable message on any network / HTTP / shape problem."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url} returned HTTP {e.code}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RuntimeError(f"could not read {url}: {e}") from e
    if not isinstance(data, list):
        raise RuntimeError(f"{url} is not a mixes.json array")
    return data


def audio_reachable(url: str) -> bool:
    """HEAD the audio URL. Advisory only — a False here never blocks the add."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def find_source_entry(entries, mix_id: str):
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == mix_id:
            return entry
    return None


def build_pointer(manifest_url, source_id, local_id=None, via=None, overrides=None):
    """The manifest entry to write. Only fields the user actually set are kept —
    everything else is resolved from the source at page load."""
    source = {"manifest": manifest_url, "id": source_id}
    if via:
        source["via"] = via
    entry = {"id": local_id or source_id, "source": source}
    for key, value in (overrides or {}).items():
        if value is not None:
            entry[key] = value
    return entry


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Cross-post a mix from another site: add a pointer to site/mixes.json (no audio copied)."
    )
    parser.add_argument("link", help="Share link to the mix, e.g. https://their-site.pages.dev/?mix=<id>")
    parser.add_argument("--manifest-url", default=None, help="Override the mixes.json URL derived from the link")
    parser.add_argument("--id", dest="local_id", default=None, help="Your deep-link id (default: the source's id)")
    parser.add_argument("--via", default=None, help="Label for the 'via' link (default: the source's hostname)")
    parser.add_argument("--title", default=None, help="Override the title on your site")
    parser.add_argument(
        "--type", choices=["recording", "live", "event"], default=None,
        help="Override the category on your site (the source's own category rarely matches yours)",
    )
    parser.add_argument("--notes", default=None, help="Override the notes")
    parser.add_argument("--artist", default=None, help="Override the DJ name")
    parser.add_argument("--event-name", default=None, help="Override the event edition")
    parser.add_argument("--bpm", default=None, help="Override the BPM range")
    parser.add_argument("--force", action="store_true", help="Replace an existing entry with the same id")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be written; don't touch the manifest")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    try:
        manifest_url, source_id = parse_share_link(args.link)
    except ValueError as e:
        print(f"Bad link: {e}", file=sys.stderr)
        return 1
    if args.manifest_url:
        manifest_url = args.manifest_url

    try:
        remote = fetch_manifest(manifest_url)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    source_entry = find_source_entry(remote, source_id)
    if source_entry is None:
        print(f"Error: no mix with id '{source_id}' in {manifest_url}", file=sys.stderr)
        return 1
    if not source_entry.get("audio_url"):
        hint = ""
        if isinstance(source_entry.get("source"), dict) and source_entry["source"].get("manifest"):
            hint = f" It is itself a cross-post of {source_entry['source']['manifest']} — link to the original."
        print(f"Error: '{source_id}' has no audio_url of its own.{hint}", file=sys.stderr)
        return 1

    entry = build_pointer(
        manifest_url,
        source_id,
        local_id=args.local_id,
        via=args.via,
        overrides={
            "title": args.title,
            "type": args.type,
            "notes": args.notes,
            "artist": args.artist,
            "event_name": args.event_name,
            "bpm_range": args.bpm,
        },
    )

    mixes = add_mix.load_manifest()
    clash = any(m.get("id") == entry["id"] for m in mixes)
    if clash and not args.force:
        print(
            f"A mix with id '{entry['id']}' already exists in the manifest. "
            "Re-run with --force to replace it, or pass --id to pick another.",
            file=sys.stderr,
        )
        return 1

    shown = args.title or source_entry.get("title") or source_id
    print(f"Source:   {manifest_url}  [{source_id}]")
    print(f"Resolves: {shown}")
    meta = [source_entry.get("date")]
    if source_entry.get("duration_seconds"):
        meta.append(add_mix.format_clock(source_entry["duration_seconds"]))
    meta.append(f"{len(source_entry.get('tracklist') or [])} tracklist entries")
    print("          " + " · ".join(str(p) for p in meta if p))
    if not audio_reachable(source_entry["audio_url"]):
        print("Warning: the source's audio_url did not answer a HEAD request right now.", file=sys.stderr)

    if args.dry_run:
        print("\n--dry-run: would write\n" + json.dumps(entry, indent=2, ensure_ascii=False))
        return 0

    mixes = [m for m in mixes if m.get("id") != entry["id"]]
    mixes.insert(0, entry)
    add_mix.save_manifest(mixes)

    try:
        where = add_mix.MIXES_JSON.relative_to(add_mix.REPO_ROOT)
    except ValueError:
        where = add_mix.MIXES_JSON
    print(f"\nAdded pointer '{entry['id']}' to {where}")
    print("Nothing was uploaded — the page reads the source's mixes.json (and audio) directly.")
    print()
    print("Next step:")
    print(f'  git add -A && git commit -m "Cross-post: {shown}" && git push')
    return 0


if __name__ == "__main__":
    sys.exit(main())
