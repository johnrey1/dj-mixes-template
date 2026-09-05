"""Unit tests for the pure helpers in scripts/add_mix.py.

These deliberately avoid anything that needs R2 credentials or ffmpeg — the CI
runner has neither. `add_mix` calls load_env() only inside main(), so importing
it here is side-effect free.
"""

import array
import math

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

    def test_novelty_nudges_guesses_toward_real_onsets(self):
        ar = 2000
        track_len = 60          # seconds
        onset_in_track = 8      # loudness jumps 8s into each track
        samples = array.array("h")
        for t in range(4):
            for s in range(track_len * ar):
                sec = s / ar
                amp = 12000 if sec >= onset_in_track else 200
                samples.append(int(amp * math.sin(2 * math.pi * 220 * sec)))

        out = add_mix.suggest_timestamps(["A", "B", "C", "D"], 4 * track_len, samples, ar=ar)
        secs = _secs(out)

        assert secs[0] == 0                     # first track pinned
        assert secs == sorted(secs)
        # Even split would give 60/120/180; real onsets are 68/128/188.
        for guess, onset in zip(secs[1:], [68, 128, 188]):
            assert abs(guess - onset) < 20
            assert abs(guess - onset) < abs(guess - (onset - onset_in_track))
