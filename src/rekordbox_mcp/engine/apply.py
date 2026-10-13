"""Ledger-safe cue writes: diff a plan against the library, then change only what the engine owns.

Rules:
* The engine only ever deletes cues that are in its ledger (cues it wrote or adopted).
  Hot cues and memory cues you set by hand are never touched, whatever their colour.
* A library cue that already matches a planned cue (same colour, comment and position) is
  adopted into the ledger instead of being rewritten.
* Every write is one batch: one PROTECTED backup, one commit, then a verify pass that
  re-diffs and must come back empty.
"""
from __future__ import annotations

from typing import Optional

from .. import rbcore
from .store import Store

POS_TOL = 0.02  # seconds


def _desired(lib: rbcore.Library, plan: dict, only: Optional[set[str]] = None) -> dict[str, list[dict]]:
    by_track: dict[str, list[dict]] = {}
    grids: dict[str, dict] = {}
    for q in plan["cues"]:
        tid = str(q["track_id"])
        if only is not None and tid not in only:
            continue
        if tid not in grids:
            grids[tid] = lib.analysis(lib.content(tid))["grid"]
        pos = lib.time_at_bar(grids[tid], float(q["bar"]), int(q["beat"]))
        by_track.setdefault(tid, []).append(dict(q, position_sec=round(pos, 3)))
    return by_track


def diff_plan(lib: rbcore.Library, store: Store, plan_id: int, track_ids: Optional[list[str]] = None) -> dict:
    """What apply_plan would do: add / delete / adopt / keep, per track (optionally only some tracks)."""
    plan = store.plan(plan_id)
    for q in plan["cues"]:
        if (q["color"] or "").lower() not in rbcore.MEMORY_COLORS:
            raise ValueError(f"plan {plan_id} has a cue without a valid colour: {q}")
    want = _desired(lib, plan, {str(t) for t in track_ids} if track_ids else None)
    ledger = {r["cue_id"]: r for r in store.ledger(want.keys())}
    out = {"plan_id": plan_id, "add": [], "delete": [], "adopt": [], "keep": 0, "foreign": [], "vanished": [],
           "uncoloured_foreign": 0}
    lib_ids: set[str] = set()
    for tid, desired in want.items():
        c = lib.content(tid)
        have = lib.cues(c)
        lib_ids |= {str(h["cue_id"]) for h in have}
        unmatched = list(desired)
        for h in have:
            hid = str(h["cue_id"])
            match = None
            if h["type"] == "memory":
                for d in unmatched:
                    if (h["color"] == d["color"] and (h["comment"] or "") == d["comment"]
                            and abs(h["position_sec"] - d["position_sec"]) <= POS_TOL):
                        match = d
                        break
            if match is not None:
                unmatched.remove(match)
                if hid in ledger:
                    out["keep"] += 1
                else:
                    out["adopt"].append({"cue_id": hid, "track_id": tid, "position_ms": int(round(h["position_sec"] * 1000)),
                                         "color": h["color"], "comment": h["comment"] or ""})
            elif hid in ledger:
                out["delete"].append({"cue_id": hid, "track_id": tid, "position": h["position"],
                                      "color": h["color"], "comment": h["comment"]})
            else:
                out["foreign"].append({"cue_id": hid, "track_id": tid, "slot": h["slot"], "position": h["position"],
                                       "color": h["color"], "comment": h["comment"], "created": h["created"]})
                if h["type"] == "memory" and h["color"] is None:
                    out["uncoloured_foreign"] += 1
        for d in unmatched:
            out["add"].append({"track_id": tid, "bar": d["bar"], "beat": d["beat"], "color": d["color"],
                               "comment": d["comment"], "position_sec": d["position_sec"]})
    out["vanished"] = [cid for cid, r in ledger.items() if cid not in lib_ids]
    out["counts"] = {k: len(out[k]) for k in ("add", "delete", "adopt", "foreign", "vanished")} | {"keep": out["keep"]}
    return out


def apply_plan(lib: rbcore.Library, store: Store, plan_id: int, dry_run: bool = True,
               track_ids: Optional[list[str]] = None) -> dict:
    d = diff_plan(lib, store, plan_id, track_ids)
    if dry_run:
        return {"dry_run": True, **d}
    if d["vanished"]:
        store.ledger_mark_deleted(d["vanished"])  # removed by hand in rekordbox: forget them
    if d["adopt"]:
        store.ledger_add([dict(r, plan_id=plan_id) for r in d["adopt"]])
    res = lib.write_cues([{k: a[k] for k in ("track_id", "position_sec", "color", "comment")} for a in d["add"]],
                         [x["cue_id"] for x in d["delete"]], label=f"plan{plan_id}")
    if res["created"]:
        store.ledger_add([dict(r, plan_id=plan_id) for r in res["created"]])
    if res["deleted"]:
        store.ledger_mark_deleted([x["cue_id"] for x in res["deleted"]])
    lib.reopen()
    v = diff_plan(lib, store, plan_id, track_ids)
    ok = not v["add"] and not v["delete"]
    if ok and not track_ids:
        store.mark_applied(plan_id, res["backup"])
    return {"dry_run": False, "ok": ok, "backup": res["backup"], "added": len(res["created"]),
            "deleted": len(res["deleted"]), "adopted": len(d["adopt"]), "foreign_untouched": len(d["foreign"]),
            "verify": v["counts"],
            "rollback": (f"close rekordbox, then copy {res['backup']} (and its -wal if present) over master.db"
                         if res["backup"] else None)}


def adopt_matching(lib: rbcore.Library, store: Store, plan_id: int) -> dict:
    """Seed the ledger from cues already in the library that match a plan (no library writes)."""
    d = diff_plan(lib, store, plan_id)
    if d["adopt"]:
        store.ledger_add([dict(r, plan_id=plan_id) for r in d["adopt"]])
    return {"adopted": len(d["adopt"]), "already_owned": d["keep"], "missing": len(d["add"]),
            "foreign": len(d["foreign"])}


def release_plan_cues(lib: rbcore.Library, store: Store, track_ids: Optional[list[str]] = None,
                      dry_run: bool = True) -> dict:
    """Delete every engine-owned cue on the given tracks (never a hand-set one)."""
    rows = store.ledger(track_ids)
    if dry_run:
        return {"dry_run": True, "would_delete": len(rows)}
    res = lib.write_cues([], [r["cue_id"] for r in rows], label="release")
    store.ledger_mark_deleted([r["cue_id"] for r in rows])
    return {"dry_run": False, "deleted": len(res["deleted"]), "backup": res["backup"]}
