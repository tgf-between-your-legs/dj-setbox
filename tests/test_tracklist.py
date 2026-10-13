import synth
from rekordbox_mcp.engine import planner, tracklist
from rekordbox_mcp.engine.planner import PlanInputs


def test_parse_formats():
    rows = tracklist.parse_lines("[00:00] A - One\n1:02:03 B – Two\n12. 05:40 C - Three [LABEL]\nno time here")
    assert [r["start_sec"] for r in rows] == [0, 3723, 340, None]
    assert rows[2]["title"] == "Three" and rows[0]["artist"] == "A"


def test_match():
    lib = [{"id": "1", "title": "Supernova", "artist": "aespa"}, {"id": "2", "title": "Magnetic", "artist": "ILLIT"}]
    rows = tracklist.match(tracklist.parse_lines("0:00 aespa - Supernova (Extended)\n3:00 ILLIT - Magnetic\n6:00 X - Nope"), lib)
    assert [r["track_id"] for r in rows] == ["1", "2", None]


def test_estimate_recovers_exits_within_a_phrase(kpop):
    fs = synth.crate(6)
    p = planner.plan(fs, PlanInputs(order=[f.id for f in fs]), kpop)
    F = {f.id: f for f in fs}
    # build the tracklist a listener would write: each track's time = when its landing hits
    t, rows = 0.0, []
    tr = p["tracks"]
    for i, x in enumerate(tr):
        rows.append({"raw": x["track_id"], "start_sec": t, "track_id": x["track_id"]})
        if i + 1 < len(tr):
            t += (x["e"] - x["land"]) * F[x["track_id"]].bar_sec
    lands = {x["track_id"]: x["land"] for x in tr}
    est = tracklist.estimate(rows, F, kpop, lands)
    for r, x in zip(est[:-1], tr[:-1]):
        assert abs(r["est_exit"] - x["e"]) <= kpop["phrase_bars"]
