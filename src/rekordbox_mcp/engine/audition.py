"""Phase 2: audition transitions on the rekordbox decks and collect verdicts. No audio files.

Flow:
  audition_queue(plan)      riskiest untested transitions first
  stage_audition(plan)      writes those tracks' plan cues (ledger-safe) and fills an
                            "Audition" playlist A1, B1, A2, B2 ... with deck steps per pair
  (you play each pair; drop a GREEN hot cue near A's OUT DONE if it works, RED if not)
  collect_ratings(plan)     reads those hot cues back into the rating table
  import_ratings(json)      same, from the run sheet's Works / Doesn't buttons
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Optional

from . import apply as _apply
from . import profiles as _profiles
from .qa import flags_for, risk, transition_features
from .techniques import deck_steps

GOOD = {"green", "lime", "seagreen", "teal"}
BAD = {"red", "orange", "pink", "magenta"}
NEAR_BARS = 4


def _transitions(engine, plan: dict) -> list[dict]:
    """Per transition of a saved plan: the planned choices plus features, flags and risk."""
    prof = _profiles.load(plan["profile"])
    tr = plan["tracks"]
    fs = {t["track_id"]: engine.features(t["track_id"]) for t in tr}
    out = []
    for t, u in zip(tr, tr[1:]):
        fa, fb = fs[t["track_id"]], fs[u["track_id"]]
        ov = t["overlap"] or 0
        feats = transition_features(fa, fb, t["land"], u["land"], t["e"], t["family"], ov, t["technique"], prof, t["s"])
        fl = flags_for(feats, fa, fb, u["land"], prof, grids=False)
        o = ov or 8
        out.append(dict(pos=t["pos"], a_id=t["track_id"], b_id=u["track_id"], a_title=fa.title, b_title=fb.title,
                        family=t["family"], technique=t["technique"], overlap=ov,
                        out_start=t["e"] - o if t["family"] == "blend" else t["e"] - 8, out_done=t["e"],
                        b_in=u["s"] if t["family"] == "blend" else u["land"], b_land=u["land"],
                        bpm_a=fa.bpm, bpm_b=fb.bpm, features=feats, flags=fl, risk=risk(fl, feats)))
    return out


def audition_queue(engine, plan_id: int, limit: Optional[int] = None, include_rated: bool = False) -> dict:
    plan = engine.store.plan(plan_id)
    rated = {(r["a_id"], r["b_id"]) for r in engine.store.ratings()}
    rows = _transitions(engine, plan)
    if not include_rated:
        rows = [r for r in rows if (r["a_id"], r["b_id"]) not in rated]
    rows.sort(key=lambda r: (-r["risk"], r["pos"]))
    if limit:
        rows = rows[:limit]
    return {"plan_id": plan_id, "already_rated": len(rated),
            "queue": [{k: r[k] for k in ("pos", "a_id", "b_id", "a_title", "b_title", "family", "technique", "risk")}
                      | {"flags": [f["message"] for f in r["flags"]]} for r in rows]}


def stage_audition(engine, plan_id: int, pairs: Optional[list] = None, limit: int = 10,
                   playlist_name: str = "Audition", dry_run: bool = True) -> dict:
    """Write the cues for the pairs to test and fill the audition playlist (rekordbox must be closed)."""
    plan = engine.store.plan(plan_id)
    rows = _transitions(engine, plan)
    by_pair = {(r["a_id"], r["b_id"]): r for r in rows}
    by_pos = {r["pos"]: r for r in rows}
    if pairs:
        chosen = []
        for p in pairs:
            r = by_pos.get(int(p)) if isinstance(p, (int, str)) and str(p).isdigit() else by_pair.get((str(p[0]), str(p[1])))
            if r is None:
                raise ValueError(f"{p!r} is not a transition in plan {plan_id}")
            chosen.append(r)
    else:
        q = audition_queue(engine, plan_id, limit=limit)["queue"]
        chosen = [by_pair[(x["a_id"], x["b_id"])] for x in q]
    tracks = [t for r in chosen for t in (r["a_id"], r["b_id"])]
    steps = [{"pos": r["pos"], "a": r["a_title"], "b": r["b_title"], "technique": r["technique"], "risk": r["risk"],
              "flags": [f["message"] for f in r["flags"]],
              "steps": deck_steps(r["technique"], r["family"], r["overlap"] or 8, r["a_title"], r["b_title"],
                                  r["out_start"], r["out_done"], r["b_in"], r["b_land"], r["bpm_a"], r["bpm_b"])}
             for r in chosen]
    cue_diff = _apply.diff_plan(engine.lib, engine.store, plan_id, track_ids=sorted(set(tracks)))
    if dry_run:
        return {"dry_run": True, "pairs": len(chosen), "playlist": playlist_name,
                "cue_changes": cue_diff["counts"], "deck_steps": steps}
    cues = _apply.apply_plan(engine.lib, engine.store, plan_id, dry_run=False, track_ids=sorted(set(tracks)))
    pl = engine.lib.replace_playlist(playlist_name, tracks, label=f"audition-plan{plan_id}")
    with engine.store.tx() as c:
        c.execute("INSERT INTO audition (plan_id, playlist_id, pairs, staged_at) VALUES (?,?,?,?)",
                  (plan_id, pl["playlist_id"], json.dumps([[r["a_id"], r["b_id"]] for r in chosen]),
                   _dt.datetime.now().isoformat(timespec="seconds")))
    return {"dry_run": False, "pairs": len(chosen), "playlist": pl, "cues": cues, "deck_steps": steps}


def collect_ratings(engine, plan_id: int) -> dict:
    """Read GREEN / RED hot cues placed near each A's OUT DONE into the rating table."""
    plan = engine.store.plan(plan_id)
    ledger = {r["cue_id"] for r in engine.store.ledger(live_only=False)}
    rows = _transitions(engine, plan)
    found, conflicts = [], []
    for r in rows:
        f = engine.features(r["a_id"])
        done_t = f.time_at(min(r["out_done"], f.n_bars)) or 0.0
        win = NEAR_BARS * f.bar_sec
        c = engine.lib.content(r["a_id"])
        verdicts = set()
        for q in engine.lib.cues(c):
            if q["type"] != "hot" or str(q["cue_id"]) in ledger:
                continue
            if abs(q["position_sec"] - done_t) > win:
                continue
            col = (q["color"] or "").lower()
            if col in GOOD:
                verdicts.add(True)
            elif col in BAD:
                verdicts.add(False)
        if len(verdicts) == 1:
            works = verdicts.pop()
            engine.store.add_rating(r["a_id"], r["b_id"], works, source="rekordbox", plan_id=plan_id,
                                    features=r["features"])
            found.append({"pos": r["pos"], "a": r["a_title"], "b": r["b_title"], "works": works})
        elif len(verdicts) == 2:
            conflicts.append({"pos": r["pos"], "a": r["a_title"], "b": r["b_title"]})
    return {"plan_id": plan_id, "rated": found, "conflicts": conflicts,
            "note": "Rating cues are hot cues you set; the engine never deletes them."}


def import_ratings(engine, payload: str | dict) -> dict:
    """Ratings copied from the run sheet: {plan_id, ratings: [{a, b, works}]}."""
    d = json.loads(payload) if isinstance(payload, str) else payload
    pid = d.get("plan_id") or None
    feats = {}
    if pid:
        try:
            feats = {(r["a_id"], r["b_id"]): r["features"] for r in _transitions(engine, engine.store.plan(int(pid)))}
        except ValueError:
            pid = None
    n = 0
    for r in d.get("ratings") or []:
        engine.store.add_rating(str(r["a"]), str(r["b"]), bool(r["works"]), source="sheet", plan_id=pid,
                                features=feats.get((str(r["a"]), str(r["b"]))))
        n += 1
    return {"imported": n, "plan_id": pid}
