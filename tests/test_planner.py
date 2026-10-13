import pytest

import synth
from rekordbox_mcp.engine import planner
from rekordbox_mcp.engine.planner import PlanInputs, place


def _plan(kpop, fs, **kw):
    return planner.plan(fs, PlanInputs(order=[f.id for f in fs], **kw), kpop)


def test_every_cue_is_coloured_and_every_track_cued(kpop):
    fs = synth.crate(8)
    p = _plan(kpop, fs)
    assert p["cues"] and all(c["color"] in ("green", "red") for c in p["cues"])
    by = {}
    for c in p["cues"]:
        by.setdefault(c["track_id"], []).append(c)
    for f in fs:
        roles = {c["role"] for c in by[f.id]}
        assert "out_done" in roles and roles & {"in", "in_done"}


def test_no_technique_twice_in_a_row(kpop):
    p = _plan(kpop, synth.crate(10))
    techs = [r["technique"] for r in p["transitions"]]
    assert all(a != b for a, b in zip(techs, techs[1:]))


def test_blend_lands_where_a_leaves(kpop):
    p = _plan(kpop, synth.crate(10))
    tr = {t["pos"]: t for t in p["tracks"]}
    for r in p["transitions"]:
        b = tr[r["b"]]
        if r["family"] == "blend":
            assert b["s"] == b["land"] - r["overlap"]
        else:
            assert b["s"] == b["land"]


def test_pins_are_respected(kpop):
    fs = synth.crate(6)
    ids = [f.id for f in fs]
    p = _plan(kpop, fs, landings={ids[2]: 65}, exit_pins={ids[1]: 81},
              pair_pins={(ids[3], ids[4]): {"family": "cut", "technique": "brake"}})
    t = {x["track_id"]: x for x in p["tracks"]}
    assert t[ids[2]]["land"] == 65
    assert t[ids[1]]["e"] == 81
    r = next(r for r in p["transitions"] if r["a_id"] == ids[3])
    assert r["family"] == "cut" and r["technique"] == "brake"


def test_grid_fix_moves_cues_two_beats_early(kpop):
    fs = synth.crate(4)
    p = _plan(kpop, fs, grid_fix={fs[1].id: -2})
    shifted = [c for c in p["cues"] if c["track_id"] == fs[1].id]
    assert shifted and all(c["beat"] == 3 for c in shifted)
    assert place(33, -2) == (32, 3) and place(33, 0) == (33, 1) and place(33, 1) == (33, 2)


def test_display_names_used_in_comments(kpop):
    fs = synth.crate(3)
    p = _plan(kpop, fs, names={fs[1].id: ("Short Name", "Someone")})
    assert any("Short Name" in c["comment"] for c in p["cues"] if c["track_id"] == fs[0].id)


def test_profiles_change_play_length(kpop):
    from rekordbox_mcp.engine import profiles
    fs = synth.crate(6)
    short = _plan(kpop, fs)["summary"]["play_sec_median"]
    long = planner.plan(fs, PlanInputs(order=[f.id for f in fs]), profiles.load("house"))["summary"]["play_sec_median"]
    assert long > short


def test_one_track_is_rejected(kpop):
    with pytest.raises(ValueError):
        _plan(kpop, synth.crate(1))


def test_tempo_gap_half_double():
    assert planner.tempo_gap(87, 174) == pytest.approx(1.0)
    assert planner.tempo_gap(87, 174, half_double=True) == pytest.approx(0.0)
