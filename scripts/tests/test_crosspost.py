"""Tests for scripts/crosspost.py.

Network is never touched: fetch_manifest / audio_reachable are monkeypatched and
the manifest path is redirected into tmp_path.
"""

import json

import pytest

import add_mix
import crosspost

MANIFEST = "https://friend.pages.dev/mixes.json"
SOURCE_ID = "2026-10-10-2026-10-10"
REMOTE = [
    {
        "id": SOURCE_ID,
        "title": "2026-10-10",
        "date": "2026-10-10",
        "type": "recording",
        "duration_seconds": 11989,
        "tracklist": ["A", "B"],
        "peaks": [0.1, 0.2],
        "audio_url": "https://cdn.example/mixes/x.mp3",
    },
    {"id": "other", "title": "Other", "audio_url": "https://cdn.example/other.mp3"},
]


class TestParseShareLink:
    def test_root_link(self):
        assert crosspost.parse_share_link("https://friend.pages.dev/?mix=abc") == (MANIFEST, "abc")

    def test_ignores_t_param_and_fragment(self):
        url = "https://friend.pages.dev/?mix=abc&t=754#x"
        assert crosspost.parse_share_link(url) == (MANIFEST, "abc")

    def test_subpath_site(self):
        assert crosspost.parse_share_link("https://h.example/dj/?mix=a")[0] == "https://h.example/dj/mixes.json"

    def test_subpath_without_trailing_slash(self):
        assert crosspost.parse_share_link("https://h.example/dj?mix=a")[0] == "https://h.example/dj/mixes.json"

    def test_index_html(self):
        assert crosspost.parse_share_link("https://h.example/dj/index.html?mix=a")[0] == "https://h.example/dj/mixes.json"

    def test_surrounding_whitespace(self):
        assert crosspost.parse_share_link("  https://friend.pages.dev/?mix=abc \n")[1] == "abc"

    @pytest.mark.parametrize(
        "bad",
        ["", "friend.pages.dev/?mix=abc", "ftp://h/?mix=a", "javascript:alert(1)", "https://friend.pages.dev/", "https://friend.pages.dev/?mix="],
    )
    def test_rejects_unusable_links(self, bad):
        with pytest.raises(ValueError):
            crosspost.parse_share_link(bad)


class TestBuildPointer:
    def test_minimal_pointer_has_no_copied_metadata(self):
        entry = crosspost.build_pointer(MANIFEST, SOURCE_ID)
        assert entry == {"id": SOURCE_ID, "source": {"manifest": MANIFEST, "id": SOURCE_ID}}

    def test_only_set_overrides_are_kept(self):
        entry = crosspost.build_pointer(
            MANIFEST, SOURCE_ID, local_id="mine", via="SSR",
            overrides={"title": "T", "type": None, "notes": None, "bpm_range": "130"},
        )
        assert entry == {
            "id": "mine",
            "source": {"manifest": MANIFEST, "id": SOURCE_ID, "via": "SSR"},
            "title": "T",
            "bpm_range": "130",
        }


class TestFindSourceEntry:
    def test_found_and_missing(self):
        assert crosspost.find_source_entry(REMOTE, SOURCE_ID)["title"] == "2026-10-10"
        assert crosspost.find_source_entry(REMOTE, "nope") is None

    def test_tolerates_junk_entries(self):
        assert crosspost.find_source_entry([None, "x", 3, {"id": "a"}], "a") == {"id": "a"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "mixes.json"
    path.write_text(json.dumps([{"id": "mine-1", "title": "Mine", "audio_url": "u"}]))
    monkeypatch.setattr(add_mix, "MIXES_JSON", path)
    monkeypatch.setattr(crosspost, "fetch_manifest", lambda url: REMOTE)
    monkeypatch.setattr(crosspost, "audio_reachable", lambda url: True)
    return path


def _manifest(path):
    return json.loads(path.read_text())


class TestMain:
    LINK = f"https://friend.pages.dev/?mix={SOURCE_ID}"

    def test_writes_pointer_and_prepends(self, env, capsys):
        assert crosspost.main([self.LINK, "--title", "DJR b2b SSR", "--type", "live"]) == 0
        mixes = _manifest(env)
        assert [m["id"] for m in mixes] == [SOURCE_ID, "mine-1"]
        assert mixes[0] == {
            "id": SOURCE_ID,
            "source": {"manifest": MANIFEST, "id": SOURCE_ID},
            "title": "DJR b2b SSR",
            "type": "live",
        }
        assert "peaks" not in mixes[0] and "audio_url" not in mixes[0]  # nothing copied
        assert "Nothing was uploaded" in capsys.readouterr().out

    def test_dry_run_does_not_write(self, env, capsys):
        before = env.read_text()
        assert crosspost.main([self.LINK, "--dry-run"]) == 0
        assert env.read_text() == before
        assert "would write" in capsys.readouterr().out

    def test_id_collision_needs_force(self, env, capsys):
        assert crosspost.main([self.LINK, "--id", "mine-1"]) == 1
        assert "already exists" in capsys.readouterr().err
        assert len(_manifest(env)) == 1

        assert crosspost.main([self.LINK, "--id", "mine-1", "--force"]) == 0
        mixes = _manifest(env)
        assert len(mixes) == 1 and mixes[0]["source"]["id"] == SOURCE_ID

    def test_unknown_mix_id(self, env, capsys):
        assert crosspost.main(["https://friend.pages.dev/?mix=ghost"]) == 1
        assert "no mix with id 'ghost'" in capsys.readouterr().err

    def test_refuses_to_chain_a_crosspost(self, env, monkeypatch, capsys):
        chained = [{"id": SOURCE_ID, "source": {"manifest": "https://orig.example/mixes.json", "id": "z"}}]
        monkeypatch.setattr(crosspost, "fetch_manifest", lambda url: chained)
        assert crosspost.main([self.LINK]) == 1
        err = capsys.readouterr().err
        assert "no audio_url" in err and "https://orig.example/mixes.json" in err

    def test_fetch_failure_is_reported(self, env, monkeypatch, capsys):
        def boom(url):
            raise RuntimeError("could not read: offline")
        monkeypatch.setattr(crosspost, "fetch_manifest", boom)
        assert crosspost.main([self.LINK]) == 1
        assert "offline" in capsys.readouterr().err
        assert len(_manifest(env)) == 1

    def test_unreachable_audio_only_warns(self, env, monkeypatch, capsys):
        monkeypatch.setattr(crosspost, "audio_reachable", lambda url: False)
        assert crosspost.main([self.LINK]) == 0
        assert "did not answer" in capsys.readouterr().err

    def test_bad_link(self, env, capsys):
        assert crosspost.main(["not a link"]) == 1
        assert "Bad link" in capsys.readouterr().err
