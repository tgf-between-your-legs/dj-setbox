"""Phase 4 stand-in for mix mining: learn from a pasted tracklist with start times.

No audio is needed. Each line is matched to a library track (string similarity first; the
Haiku tier only for the leftovers), then the timestamps plus rekordbox's grid give how long
the DJ played each track and roughly where they left it. Consecutive matched pairs become
weak "works" labels (weight WEAK_WEIGHT) for the learner.
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Optional

from . import llm
from .planner import first_land, refine_landing
from .qa import transition_features
from .store import Store, now

WEAK_WEIGHT = 0.2
TS = re.compile(r"^\s*[\[(]?\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\s*[\])]?\s*[-–.|]?\s*(.*)$")


def parse_lines(text: str) -> list[dict]:
    """'[00:03:15] Artist - Title' / '1:02:03 Artist – Title' / '12. 05:40 Artist - Title' -> rows."""
    out = []
    for raw in text.splitlines():
        line = re.sub(r"^\s*\d{1,3}[.)]\s+", "", raw).strip()
        if not line:
            continue
        m = TS.match(line)
        if m:
            h, mi, s, rest = m.groups()
            start = int(h or 0) * 3600 + int(mi) * 60 + int(s)
        else:
            start, rest = None, line
        rest = re.sub(r"\s*\[[^\]]*\]\s*$", "", rest).strip()   # trailing [LABEL]
        artist, title = (rest.split(" - ", 1) + [""])[:2] if " - " in rest else ("", rest)
        out.append({"raw": raw.strip(), "start_sec": start, "artist": artist.strip(), "title": title.strip()})
    return out


def _norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\(.*?\)|\[.*?\]|feat\..*$|ft\..*$", " ", s)
    return re.sub(r"[^a-z0-9가-힣ぁ-んァ-ン一-龥]+", " ", s).strip()


def match(rows: list[dict], library: list[dict], cutoff: float = 0.82) -> list[dict]:
    keys = {}
    for t in library:
        keys.setdefault(_norm(f"{t.get('artist') or ''} {t.get('title') or ''}"), str(t["id"]))
        keys.setdefault(_norm(t.get("title") or ""), str(t["id"]))
    names = list(keys)
    for r in rows:
        best, score = None, 0.0
        for q in (_norm(f"{r['artist']} {r['title']}"), _norm(r["title"])):
            if not q:
                continue
            if q in keys:
                best, score = keys[q], 1.0
                break
            m = difflib.get_close_matches(q, names, n=1, cutoff=cutoff)
            if m:
                sc = difflib.SequenceMatcher(None, q, m[0]).ratio()
                if sc > score:
                    best, score = keys[m[0]], sc
        r["track_id"], r["match_score"], r["match_source"] = best, round(score, 3), ("string" if best else None)
    return rows


MATCH_SCHEMA = {"type": "object", "properties": {"matches": {"type": "array", "items": {
    "type": "object", "properties": {"line": {"type": "integer"}, "id": {"type": ["string", "null"]}},
    "required": ["line", "id"]}}}, "required": ["matches"]}


def match_with_llm(rows: list[dict], library: list[dict], client_: Any = None) -> list[dict]:
    """Ask the bulk tier to match lines string matching missed (only ids from the candidate list count)."""
    todo = [(i, r) for i, r in enumerate(rows) if not r.get("track_id")]
    if not todo:
        return rows
    ids = {str(t["id"]) for t in library}
    lib_lines = "\n".join(f"{t['id']}\t{t.get('artist') or ''} - {t.get('title') or ''}" for t in library[:2000])
    for ch in llm.chunks(todo, 60):
        q = "\n".join(f"{i}\t{r['raw']}" for i, r in ch)
        res = llm.tool_call("Match each tracklist line to the library track it names, or null if none. Only use ids "
                            "from the library list. Remixes/edits only match the same remix/edit.",
                            f"Library:\n{lib_lines}\n\nLines:\n{q}", "match_lines", MATCH_SCHEMA, job="bulk", client_=client_)
        for m in res.get("matches", []):
            if m.get("id") in ids and 0 <= m["line"] < len(rows) and not rows[m["line"]].get("track_id"):
                rows[m["line"]].update(track_id=m["id"], match_score=0.7, match_source="llm")
    return rows


def estimate(rows: list[dict], feats: dict, prof: dict, landings: Optional[dict] = None) -> list[dict]:
    """Fill est_land / est_exit / play_sec per row from timestamps + grid.

    The tracklist time of a track is taken as the moment it took over, i.e. its landing;
    the next track's time is when it left, so exit = landing + played bars, snapped to the phrase grid.
    """
    landings = landings or {}
    pb = prof["phrase_bars"]
    for i, r in enumerate(rows):
        f = feats.get(r.get("track_id"))
        if f is None or r.get("start_sec") is None:
            continue
        L = landings.get(f.id) or refine_landing(f, first_land(f, prof["landing"]["default_bar"]), prof)
        r["est_land"] = L
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if nxt and nxt.get("start_sec") is not None and nxt["start_sec"] > r["start_sec"]:
            played = nxt["start_sec"] - r["start_sec"]
            bars = played / f.bar_sec
            r["play_sec"] = played
            r["est_exit"] = int(min(f.n_bars, L + max(pb, round(bars / pb) * pb)))
    return rows


def import_tracklist(store: Store, text: str, library: list[dict], feats_loader, prof: dict, name: str = "",
                     dj: str = "", url: str = "", use_llm: bool = False, client_: Any = None) -> dict:
    rows = match(parse_lines(text), library)
    if use_llm:
        rows = match_with_llm(rows, library, client_)
    feats = {}
    for r in rows:
        if r.get("track_id") and r["track_id"] not in feats:
            try:
                feats[r["track_id"]] = feats_loader(r["track_id"])
            except Exception:
                pass
    rows = estimate(rows, feats, prof, {t: v["bar"] for t, v in store.landings().items()})
    with store.tx() as c:
        cur = c.execute("INSERT INTO tracklist (name, dj, source_url, created_at) VALUES (?,?,?,?)", (name, dj, url, now()))
        tlid = cur.lastrowid
        c.executemany("INSERT INTO tracklist_entry VALUES (?,?,?,?,?,?,?,?,?)",
                      [(tlid, i + 1, r["raw"], r.get("start_sec"), r.get("track_id"), r.get("match_score"),
                        r.get("match_source"), r.get("est_land"), r.get("est_exit")) for i, r in enumerate(rows)])
    labels = 0
    for r, s in zip(rows, rows[1:]):
        if r.get("track_id") in feats and s.get("track_id") in feats and r.get("est_exit") and s.get("est_land"):
            fa, fb = feats[r["track_id"]], feats[s["track_id"]]
            from .planner import tempo_gap
            fam = "blend" if tempo_gap(fa.bpm, fb.bpm, prof["tempo"]["half_double_time"]) <= prof["tempo"]["blend_max_pct"] / 100 else "cut"
            f = transition_features(fa, fb, r["est_land"], s["est_land"], r["est_exit"], fam,
                                    prof["blend"]["overlap_long"] if fam == "blend" else 0, None, prof)
            store.add_rating(fa.id, fb.id, True, source="tracklist", weight=WEAK_WEIGHT, features=f,
                             note=f"tracklist {tlid}")
            labels += 1
    matched = sum(1 for r in rows if r.get("track_id"))
    plays = sorted(r["play_sec"] for r in rows if r.get("play_sec"))
    return {"tracklist_id": tlid, "lines": len(rows), "matched": matched, "weak_labels": labels,
            "median_play_sec": plays[len(plays) // 2] if plays else None,
            "unmatched": [r["raw"] for r in rows if not r.get("track_id")][:30]}
