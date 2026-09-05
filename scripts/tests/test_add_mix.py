"""Unit tests for the pure helpers in scripts/add_mix.py.

These deliberately avoid anything that needs R2 credentials or ffmpeg — the CI
runner has neither. `add_mix` calls load_env() only inside main(), so importing
it here is side-effect free.
"""

import array
import math

import pytest

import add_mix


# --------------------------------------------------------------------------- #
# parse_timestamp_prefix
# --------------------------------------------------------------------------- #

class TestParseTimestampPrefix:
    def test_wrapped_bracket(self):
        assert add_mix.parse_timestamp_prefix("[12:34] Artist - Title") == (754, "Artist - Title")

    def test_wrapped_paren_hms(self):
        assert add_mix.parse_timestamp_prefix("(1:02:05) Closer") == (3725, "Closer")

    def test_wrapped_no_title(self):
        assert add_mix.parse_timestamp_prefix("[0:00]") == (0, "")

    def test_bare_with_dash(self):
        assert add_mix.parse_timestamp_prefix("12:34 - Track C") == (754, "Track C")

    def test_bare_with_en_dash(self):
        assert add_mix.parse_timestamp_prefix("12:34 – Track D") == (754, "Track D")

    def test_bare_with_two_spaces(self):
        assert add_mix.parse_timestamp_prefix("12:34  Track E") == (754, "Track E")

    def test_zero_opener(self):
        assert add_mix.parse_timestamp_prefix("0:00 - Opener") == (0, "Opener")

    def test_single_space_is_plain_text(self):
        assert add_mix.parse_timestamp_prefix("12:34 Single Space") == (None, "12:34 Single Space")

    def test_title_starting_with_a_time_is_plain_text(self):
        assert add_mix.parse_timestamp_prefix("2:54 AM - Artist") == (None, "2:54 AM - Artist")

    def test_no_clock(self):
        assert add_mix.parse_timestamp_prefix("No clock here") == (None, "No clock here")

    def test_plain_numbered_word(self):
        assert add_mix.parse_timestamp_prefix("Track 3") == (None, "Track 3")

    def test_leading_and_trailing_whitespace(self):
        assert add_mix.parse_timestamp_prefix("   [1:00] Padded   ") == (60, "Padded")

    def test_ms_vs_hms_math(self):
        assert add_mix.parse_timestamp_prefix("[3:00] x")[0] == 180
        assert add_mix.parse_timestamp_prefix("[1:00:00] x")[0] == 3600
        assert add_mix.parse_timestamp_prefix("[1:01:01] x")[0] == 3661


def test_format_clock_round_trips_through_the_prefix_parser():
    for secs in (0, 5, 59, 60, 754, 3599, 3600, 3725):
        clock = add_mix.format_clock(secs)
        assert add_mix.parse_timestamp_prefix(f"[{clock}] x") == (secs, "x")


# --------------------------------------------------------------------------- #
# extract_tracklist
# --------------------------------------------------------------------------- #

class _FakeUSLT:
    def __init__(self, text):
        self.text = text


class _FakeTags:
    def __init__(self, uslt_text):
        self._uslt = uslt_text

    def getall(self, key):
        if key == "USLT" and self._uslt is not None:
            return [_FakeUSLT(self._uslt)]
        return []


class _FakeAudio:
    def __init__(self, uslt_text):
        self.tags = _FakeTags(uslt_text)


class TestExtractTracklist:
    def test_mixed_structured_and_plain(self):
        audio = _FakeAudio("Opener\n[3:00] Second - Song\n12:34 - Third\nPlain Closer")
        assert add_mix.extract_tracklist(audio) == [
            "Opener",
            {"time_seconds": 180, "title": "Second - Song"},
            {"time_seconds": 754, "title": "Third"},
            "Plain Closer",
        ]

    def test_blank_lines_skipped_and_crlf_normalised(self):
        audio = _FakeAudio("A\r\n\r\n  \r\nB")
        assert add_mix.extract_tracklist(audio) == ["A", "B"]

    def test_no_uslt_frame(self):
        assert add_mix.extract_tracklist(_FakeAudio(None)) == []

    def test_audio_is_none(self):
        assert add_mix.extract_tracklist(None) == []


# --------------------------------------------------------------------------- #
# suggest_timestamps
# --------------------------------------------------------------------------- #

def _secs(entries):
    return [e["time_seconds"] for e in entries]


class TestSuggestTimestamps:
    def test_even_split_no_samples(self):
        out = add_mix.suggest_timestamps(["A", "B", "C", "D"], 600, None)
        assert _secs(out) == [0, 150, 300, 450]
        assert [e["title"] for e in out] == ["A", "B", "C", "D"]

    def test_samples_none_three_tracks(self):
        out = add_mix.suggest_timestamps(["A", "B", "C"], 300, None)
        assert _secs(out) == [0, 100, 200]

    def test_interior_anchor_is_preserved_and_neighbours_split_around_it(self):
        tl = ["A", "B", {"time_seconds": 300, "title": "C"}, "D", "E"]
        out = add_mix.suggest_timestamps(tl, 600, None)
        secs = _secs(out)
        assert secs[0] == 0
        assert secs[2] == 300           # anchor untouched
        assert secs[1] == 150           # even split of [0, 300]
        assert secs[3] == 400 and secs[4] == 500  # even split of [300, 600]
        assert secs == sorted(secs)
        assert len(set(secs)) == len(secs)  # strictly ascending

    def test_explicit_first_track_anchor(self):
        out = add_mix.suggest_timestamps([{"time_seconds": 12, "title": "A"}, "B"], 120, None)
        assert _secs(out) == [12, 66]   # 66 = midpoint of [12, 120]

    def test_result_is_always_strictly_ascending_even_in_a_tight_span(self):
        out = add_mix.suggest_timestamps(["A", "B", "C", "D", "E"], 3, None)
        assert _secs(out) == sorted(set(_secs(out)))

    def test_novelty_nudges_guesses_toward_real_spectral_transitions(self):
        np = pytest.importorskip("numpy")
        pytest.importorskip("librosa")

        ar = 22050
        track_len = 45          # seconds per track slot
        shift_in_track = 8      # the timbre/pitch flips 8s into each slot
        n_tracks = 4
        total = n_tracks * track_len

        # Each slot k plays a distinct fundamental (180/270/360/450 Hz — different
        # pitch classes), but for the first `shift_in_track` seconds it still
        # carries the *previous* slot's tone, so the real transition sits 8s past
        # the even-split boundary. A quiet 120 BPM click keeps beat tracking honest.
        t = np.arange(total * ar) / ar
        slot = np.minimum((t // track_len).astype(int), n_tracks - 1)
        into = t - slot * track_len
        tone_idx = np.where(into >= shift_in_track, slot, np.maximum(slot - 1, 0))
        freq = 180.0 + 90.0 * tone_idx
        sig = 9000.0 * np.sin(2 * np.pi * freq * t)
        sig += np.where(np.arange(t.size) % (ar // 2) < 40, 8000.0, 0.0)
        sig = np.clip(sig, -32000, 32000).astype(np.int16)
        samples = array.array("h", sig.tobytes())

        out = add_mix.suggest_timestamps(["A", "B", "C", "D"], total, samples, ar=ar)
        secs = _secs(out)

        assert secs[0] == 0                     # first track pinned
        assert secs == sorted(secs)
        # Even split gives 45/90/135; the real transitions are at 53/98/143.
        for guess, transition, boundary in zip(secs[1:], [53, 98, 143], [45, 90, 135]):
            assert abs(guess - transition) < 20
            assert abs(guess - transition) < abs(guess - boundary)


class TestNoveltyHelpers:
    def test_checkerboard_novelty_peaks_at_segment_boundary(self):
        np = pytest.importorskip("numpy")
        pytest.importorskip("librosa")

        rng = np.random.default_rng(0)
        a = rng.normal(size=(1, 12))
        b = rng.normal(size=(1, 12))
        n = 80
        feat = np.vstack([np.repeat(a, n, axis=0), np.repeat(b, n, axis=0)])
        feat = feat + 0.01 * rng.normal(size=feat.shape)
        feat = feat / (np.linalg.norm(feat, axis=1, keepdims=True) + 1e-9)

        nov = add_mix._checkerboard_novelty(feat, half=32)
        assert abs(int(np.argmax(nov)) - n) <= 3

    def test_novelty_curve_is_none_for_empty_audio(self):
        assert add_mix._novelty_curve(array.array("h"), ar=22050) is None

    def test_novelty_curve_falls_back_to_a_list_for_short_audio(self):
        # ~10s: too short for the 64s checkerboard kernel, so the energy-delta
        # fallback runs. Must return a plain list, never raise.
        short = array.array("h", [12000, -12000, 400, -400] * (22050 * 10 // 4))
        out = add_mix._novelty_curve(short, ar=22050)
        assert isinstance(out, list) and len(out) > 3
        assert all(v >= 0.0 for v in out)


class TestSnapToBeat:
    def test_snaps_to_nearest_beat_within_tolerance(self):
        beats = [0.0, 2.0, 4.0, 6.0, 8.0]
        assert add_mix._snap_to_beat(4.3, beats, 0.0, 10.0, max_delta=1.0) == 4.0

    def test_leaves_base_when_no_beat_is_close_enough(self):
        assert add_mix._snap_to_beat(5.0, [0.0, 10.0], 0.0, 10.0, max_delta=1.0) == 5.0

    def test_never_snaps_across_a_neighbour_bound(self):
        # nearest beat (7.0) sits past `upper`, so the guess is left alone
        assert add_mix._snap_to_beat(6.9, [3.0, 7.0], 0.0, 6.5, max_delta=2.0) == 6.9

    def test_no_beats_is_a_noop(self):
        assert add_mix._snap_to_beat(5.0, None, 0.0, 10.0) == 5.0
        assert add_mix._snap_to_beat(5.0, [], 0.0, 10.0) == 5.0
