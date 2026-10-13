"""Transition QA from rekordbox's own data: the checks that catch most bad mixes before you hear them.

Flags: vocal stacking, bass overlap during a blend, a landing on a 1-bar break (the drop is
the bar after), a weak landing, a flat/mid-mood track (check by ear), an exit off the
phrase grid, a key clash, a tempo gap too wide to blend, and a suspicious beat grid.
`transition_features` is the numeric vector ratings are stored with and the learner trains on.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from .. import rbcore
from .features import ROW_LOW, ROW_VOCAL, TrackFeatures, check_grid
from .planner import best_exit, camelot_ok, overlap_for, tempo_gap
from .techniques import TECHNIQUES

FEATURE_NAMES = [
    "tempo_gap_pct", "key_score", "key_clash", "blend", "overlap_bars",
    "a_vocal", "b_vocal", "vocal_stack", "bass_overlap",
    "a_exit_strength", "b_land_strength", "b_land_on_break", "b_flat",
    "a_play_dev", "exit_off_phrase", "bpm_delta_abs",
] + [f"tech_{t}" for t in TECHNIQUES]

RISK_WEIGHTS = {
    "vocal_stack": 3.0, "bass_overlap": 2.0, "b_land_on_break": 3.0, "weak_landing": 2.0, "b_flat": 1.5,
    "key_clash": 1.5, "tempo_too_wide_for_blend": 3.0, "exit_off_phrase": 1.0, "grid_suspect": 2.5,
}


def transition_features(fa: TrackFeatures, fb: TrackFeatures, La: int, Lb: int, E: int, family: str, overlap: int,
                        technique: Optional[str], prof: dict, sA: Optional[int] = None) -> dict:
    o = overlap or 0
    gap = tempo_gap(fa.bpm, fb.bpm, prof["tempo"]["half_double_time"]) * 100
    kscore, _ = rbcore.key_compatibility(_key_name(fa), _key_name(fb))
    av = fa.win(E - max(o, 4), E, ROW_VOCAL); bv = fb.win(Lb - max(o, 4), Lb, ROW_VOCAL)
    if family == "blend" and o:
        bass = float(np.mean([min(fa.row(min(E - o + i, fa.n_bars))[ROW_LOW], fb.row(max(1, Lb - o + i))[ROW_LOW])
                              for i in range(o)])) / 100
    else:
        bass = 0.0
    sA = sA if sA is not None else La
    play = (E - sA) * fa.bar_sec
    s_b = fb.strength
    feats = {
        "tempo_gap_pct": round(gap, 2), "key_score": kscore, "key_clash": int(not camelot_ok(fa.camelot, fb.camelot)),
        "blend": int(family == "blend"), "overlap_bars": o, "a_vocal": round(av / 100, 3), "b_vocal": round(bv / 100, 3),
        "vocal_stack": int(family == "blend" and av >= prof["blend"]["vocal_stack_pct"]
                           and bv >= prof["blend"]["vocal_stack_pct"]),
        "bass_overlap": round(bass, 3),
        "a_exit_strength": round(float(fa.strength[min(E, len(fa.strength) - 1)]), 3),
        "b_land_strength": round(float(s_b[min(Lb, len(s_b) - 1)]), 3),
        "b_land_on_break": int(fb.is_break(Lb)),
        "b_flat": int(_flat(fb)),
        "a_play_dev": round(abs(play - prof["play"]["target_sec"]) / prof["play"]["target_sec"], 3),
        "exit_off_phrase": int((E - La) % prof["phrase_bars"] != 0 and fa.strength[min(E, len(fa.strength) - 1)] < 0.6),
        "bpm_delta_abs": round(abs(fb.bpm - fa.bpm), 2),
    }
    for t in TECHNIQUES:
        feats[f"tech_{t}"] = int(technique == t)
    return feats


def _key_name(f: TrackFeatures) -> Optional[str]:
    return f.key


def _flat(f: TrackFeatures) -> bool:
    """Mid-mood pop with no strong section change: drop detection is unreliable, check by ear."""
    s = f.strength
    return bool(f.mood == "mid" and (np.sort(s)[::-1][:3].mean() if len(s) else 0) < 0.6)


def flags_for(feats: dict, fa: TrackFeatures, fb: TrackFeatures, Lb: int, prof: dict, grids: bool = True) -> list[dict]:
    out = []

    def add(code: str, msg: str) -> None:
        out.append({"code": code, "weight": RISK_WEIGHTS.get(code, 1.0), "message": msg})

    if feats["vocal_stack"]:
        add("vocal_stack", "both tracks sing through the blend; cut or filter instead")
    if feats["bass_overlap"] > 0.5:
        add("bass_overlap", f"both basslines run through the overlap ({feats['bass_overlap']:.0%}); swap on one bar")
    if feats["b_land_on_break"]:
        add("b_land_on_break", f"bar {Lb} of {fb.title} is a 1-bar break; the drop is probably bar {Lb + 1}")
    if feats["b_land_strength"] < 0.3:
        add("weak_landing", f"{fb.title} barely changes at bar {Lb}; check the landing by ear")
    if feats["b_flat"]:
        add("b_flat", f"{fb.title} is flat mid-mood pop; landing picks are least certain")
    if feats["key_clash"]:
        add("key_clash", f"keys clash ({fa.camelot} into {fb.camelot}); keep the overlap short or filtered")
    if feats["blend"] and feats["tempo_gap_pct"] > prof["tempo"]["blend_max_pct"]:
        add("tempo_too_wide_for_blend", f"tempo gap {feats['tempo_gap_pct']}% is too wide to blend")
    if feats["exit_off_phrase"]:
        add("exit_off_phrase", "A leaves off its phrase grid and not on a section change")
    if grids:
        for f in (fa, fb):
            g = check_grid(f)
            if g["suspicious"]:
                add("grid_suspect", f"{f.title}: {g['advice']}")
    return out


def risk(flags: list[dict], feats: dict) -> float:
    return round(sum(f["weight"] for f in flags) + feats["tempo_gap_pct"] / 10, 2)


def transition_report(fa: TrackFeatures, fb: TrackFeatures, La: int, Lb: int, prof: dict,
                      E: Optional[int] = None, family: Optional[str] = None, technique: Optional[str] = None) -> dict:
    """Best way from A (landing La) into B (landing Lb), its features, QA flags and risk."""
    sA = max(1, La - 8)
    res = best_exit(fa, fb, La, Lb, sA, prof)
    ov = overlap_for(fb, Lb, prof)
    if E is None:
        E, family = (res[1], res[2]) if res else (min(La + 16, fa.n_bars), "cut")
    family = family or (res[2] if res else "cut")
    feats = transition_features(fa, fb, La, Lb, E, family, ov if family == "blend" else 0, technique, prof, sA)
    fl = flags_for(feats, fa, fb, Lb, prof)
    return {"a": {"id": fa.id, "title": fa.title, "bpm": fa.bpm, "camelot": fa.camelot, "landing": La},
            "b": {"id": fb.id, "title": fb.title, "bpm": fb.bpm, "camelot": fb.camelot, "landing": Lb},
            "family": family, "a_exit": E, "overlap": ov if family == "blend" else 0,
            "cost": round(float(res[0]), 3) if res else None, "features": feats, "flags": fl,
            "risk": risk(fl, feats)}
