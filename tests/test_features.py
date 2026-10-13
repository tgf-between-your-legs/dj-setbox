import numpy as np

import synth
from rekordbox_mcp.engine import features as F


def test_rows_and_flags():
    f = synth.make("a", 128, synth.CLUB)
    assert f.n_bars == sum(s[1] for s in synth.CLUB) + 2   # the grid runs 8 beats past the last section
    assert len(f.rows) == f.n_bars and len(f.rows[0]) == 9
    # bass is on through the first chorus (bars 34-49) and off in the intro
    assert all(f.row(b)[F.ROW_BASS] for b in range(35, 49))
    assert not any(f.row(b)[F.ROW_BASS] for b in range(2, 16))


def test_find_drops_skips_the_one_bar_break():
    f = synth.make("a", 128, synth.CLUB)
    drops = F.find_drops(f)
    bars = [d["bar"] for d in drops]
    assert 34 in bars and 33 not in bars            # bar 33 is the 1-bar break before the drop
    d34 = next(d for d in drops if d["bar"] == 34)
    assert any("1-bar break" in n for n in d34["notes"])
    assert f.is_break(33)


def test_check_grid_detects_half_bar_offset():
    assert not F.check_grid(synth.make("a", 128, synth.CLUB))["suspicious"]
    g = F.check_grid(synth.make("b", 128, synth.CLUB, offset_beats=2))
    assert g["suspicious"] and g["suggested_beat_offset"] == -2


def test_json_roundtrip_is_exact():
    f = synth.make("a", 124.5, synth.POP)
    g = F.TrackFeatures.from_json(f.to_json())
    assert np.array_equal(f.beat, g.beat) and f.rows == g.rows
    assert np.array_equal(f.strength, g.strength)


def test_chorus_starts_need_bass():
    f = synth.make("p", 120, synth.POP)
    assert f.chorus_starts() == [33, 65]


def test_encrypted_streaming_titles_get_a_readable_fallback():
    t, a = F.display_name("$A7:v1:abc==", "$A7:v1:def==", "spotify:track:4cOdK2wGLETKBW3PvgPWqT")
    assert t == "Spotify track 4cOdK2wG" and a == ""
    assert F.display_name("Song", "Artist", r"C:\Music\x.mp3") == ("Song", "Artist")
    assert F.streaming_ref(r"C:\Music\x.mp3") is None and F.streaming_ref("/home/a/b.mp3") is None
    assert F.streaming_ref("soundcloud:tracks:123") == ("soundcloud", "123")
