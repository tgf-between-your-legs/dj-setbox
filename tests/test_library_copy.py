"""Integration tests against a COPY of a real rekordbox library (skipped when there isn't one).

Point RBMCP_TEST_LIBRARY at a folder holding a copied master.db (+ -wal), and optionally
RBMCP_TEST_PLAYLIST at a playlist id with >= 4 analysed tracks. Never the live library.
"""
import json
import os

import pytest

from conftest import GOLDEN_SEED


def _playlist(e):
    pid = os.environ.get("RBMCP_TEST_PLAYLIST")
    if pid:
        return pid
    if GOLDEN_SEED.exists():
        seed = json.loads(GOLDEN_SEED.read_text(encoding="utf-8"))
        if seed.get("playlist_id"):
            return seed["playlist_id"]
    for p in e.lib.playlists():
        if p["type"] == "playlist" and (p["tracks"] or 0) >= 6:
            return p["id"]
    pytest.skip("no playlist with >= 6 tracks")


def _hot_cues(e, ids):
    out = set()
    for t in ids:
        for q in e.lib.cues(e.lib.content(t)):
            if q["type"] == "hot":
                out.add((t, q["cue_id"], q["slot"], round(q["position_sec"], 3)))
    return out


def test_golden_reference_plan(live_engine):
    """With the seed's landings and pins, the planner reproduces the reference cues exactly."""
    if not GOLDEN_SEED.exists():
        pytest.skip("no golden seed (local/legacy/seed_v6.json or RBMCP_GOLDEN_SEED)")
    e = live_engine
    seed = json.loads(GOLDEN_SEED.read_text(encoding="utf-8"))
    e.import_seed(GOLDEN_SEED, adopt=False)
    p = e.plan_set(order=seed["order"], save=False, use_model=False)
    got = {}
    for q in p["cues"]:
        got.setdefault(q["track_id"], []).append([q["bar"], q["beat"], q["color"], q["comment"]])
    bad = [t for t, v in seed["cues"].items() if sorted(got.get(t, [])) != sorted(v["cues"])]
    assert bad == []
    assert sum(len(v["cues"]) for v in seed["cues"].values()) == p["summary"]["cues"]


def test_apply_keeps_hand_set_cues_and_is_idempotent(live_engine):
    e = live_engine
    pl = _playlist(e)
    ids = e.playlist_ids(pl)[:8]
    hot_before = _hot_cues(e, ids)
    p = e.plan_set(order=ids, playlist_id=pl, use_model=False)
    d = e.diff_plan(p["plan_id"])
    r = e.apply_plan(p["plan_id"], dry_run=False)
    assert r["ok"] and r["verify"]["add"] == 0 and r["verify"]["delete"] == 0
    assert r["backup"] and "PROTECTED-plan" in r["backup"]
    assert _hot_cues(e, ids) == hot_before                     # hand-set hot cues untouched
    assert r["added"] == d["counts"]["add"]
    again = e.apply_plan(p["plan_id"], dry_run=False)          # nothing left to do: no backup taken
    assert again["added"] == 0 and again["deleted"] == 0 and again["backup"] is None


def test_replan_only_deletes_ledger_cues(live_engine):
    e = live_engine
    pl = _playlist(e)
    ids = e.playlist_ids(pl)[:6]
    p1 = e.plan_set(order=ids, playlist_id=pl, use_model=False)
    e.apply_plan(p1["plan_id"], dry_run=False)
    # a hand-made coloured memory cue the engine must never touch
    e.lib.write_cues([{"track_id": ids[0], "position_sec": 1.0, "color": "blue", "comment": "my own marker"}], [], "test")
    e.store.set_landing(ids[2], e.features(ids[2]).n_bars // 2, source="ear")
    p2 = e.plan_set(order=ids, playlist_id=pl, use_model=False)
    d = e.diff_plan(p2["plan_id"])
    ledger = {r["cue_id"] for r in e.store.ledger(ids)}
    assert all(x["cue_id"] in ledger for x in d["delete"])
    r = e.apply_plan(p2["plan_id"], dry_run=False)
    assert r["ok"]
    mine = [q for q in e.lib.cues(e.lib.content(ids[0])) if q["comment"] == "my own marker"]
    assert len(mine) == 1


def test_uncoloured_memory_cues_are_refused(live_engine):
    e = live_engine
    tid = e.playlist_ids(_playlist(live_engine))[0]
    with pytest.raises(ValueError):
        e.lib.write_cues([{"track_id": tid, "position_sec": 2.0, "color": None, "comment": "x"}], [], "t")


def test_audition_round_trip(live_engine):
    e = live_engine
    pl = _playlist(e)
    ids = e.playlist_ids(pl)[:6]
    p = e.plan_set(order=ids, playlist_id=pl, use_model=False)
    q = e.audition_queue(p["plan_id"])["queue"]
    assert len(q) == len(ids) - 1 and q[0]["risk"] >= q[-1]["risk"]
    dry = e.stage_audition(p["plan_id"], limit=2)
    assert dry["dry_run"] and len(dry["deck_steps"]) == 2
    st = e.stage_audition(p["plan_id"], pairs=[1, 2], dry_run=False)
    assert st["playlist"]["tracks"] == 3                       # A1 B1(=A2) B2, consecutive duplicate collapsed
    tracks = [t["id"] for t in e.lib.playlist_tracks(st["playlist"]["playlist_id"])]
    assert tracks == ids[:3]
    # rate transition 1 as works with a green hot cue near A's OUT DONE
    a = e.store.plan(p["plan_id"])["tracks"][0]
    f = e.features(a["track_id"])
    e.lib.set_cue(a["track_id"], slot="P", position_sec=f.time_at(a["e"]), color="green", comment="works")
    got = e.collect_ratings(p["plan_id"])
    assert [r["pos"] for r in got["rated"]] == [1] and got["rated"][0]["works"] is True
    assert e.store.ratings()[0]["features"]["blend"] in (0, 1)


def test_run_sheet_and_exports(live_engine, tmp_path):
    e = live_engine
    pl = _playlist(e)
    ids = e.playlist_ids(pl)[:5]
    p = e.plan_set(order=ids, playlist_id=pl, use_model=False)
    r = e.render_run_sheet(p["plan_id"], path=str(tmp_path / "s.html"))
    assert (tmp_path / "s.html").read_text(encoding="utf-8").count('class="tx"') == 4
    for fmt in ("rekordbox_xml", "m3u8", "serato_crate", "json"):
        out = e.export_plan(p["plan_id"], fmt, path=str(tmp_path / f"x.{fmt}"))
        assert out["bytes"] > 0


def test_optimizer_on_real_tracks(live_engine):
    e = live_engine
    pl = _playlist(e)
    ids = e.playlist_ids(pl)[:10]
    base = e.plan_set(order=ids, playlist_id=pl, use_model=False)
    opt = e.optimize_set(crate_ids=ids, keep_order=True, constraints={"opener": ids[0], "closer": ids[-1]})
    assert opt["optimizer"]["plan_cost"] <= e.plan_cost(base["plan_id"]) + 1e-6
    free = e.optimize_set(crate_ids=ids, constraints={"opener": ids[0], "closer": ids[-1]}, iters=3000)
    order = [t["track_id"] for t in free["tracks"]]
    assert order[0] == ids[0] and order[-1] == ids[-1] and sorted(order) == sorted(ids)


def test_hand_set_hot_cues_exist_in_fixture(live_engine):
    """Guard for the ledger test: it only proves something if the tracks carry hand-set hot cues."""
    e = live_engine
    ids = e.playlist_ids(_playlist(e))[:8]
    if not _hot_cues(e, ids):
        pytest.skip("first 8 tracks have no hot cues; ledger test is weak on this library")


def test_known_bad_grids_are_flagged(live_engine):
    """Every track the seed marks as half a bar off is flagged by check_grid with offset -2."""
    if not GOLDEN_SEED.exists():
        pytest.skip("no golden seed")
    from rekordbox_mcp.engine.features import check_grid
    seed = json.loads(GOLDEN_SEED.read_text(encoding="utf-8"))
    bad = seed.get("half_grid") or []
    if not bad:
        pytest.skip("seed lists no bad grids")
    for tid in bad:
        g = check_grid(live_engine.features(tid))
        assert g["suspicious"] and g["suggested_beat_offset"] == -2, g


def test_resolve_names_uses_injected_fetch(live_engine):
    e = live_engine
    pl = _playlist(e)
    calls = []
    r = e.resolve_names(pl, fetch=lambda url: calls.append(url) or {"title": "Resolved Title"})
    enc = [t for t in e.lib.playlist_tracks(pl) if (t["title"] or "").startswith("$A7:")]
    if not enc:
        pytest.skip("no encrypted streamed titles in this playlist")
    assert len(r["resolved"]) + len(r["failed"]) == len(enc)
    assert all("open.spotify.com/oembed" in u for u in calls)
    tid = r["resolved"][0]["id"]
    p = e.plan_set(order=e.playlist_ids(pl), save=False, use_model=False)
    assert any(t["info"]["title"] == "Resolved Title" for t in p["tracks"] if t["track_id"] == tid)
