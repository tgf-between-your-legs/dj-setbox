"""Set planner: for a fixed running order, decide where each track lands, where it leaves,
how each transition is done, and which memory cues mark it.

Pure: takes TrackFeatures + PlanInputs + a profile, returns a plan dict. No library or DB
access, so it is fast to test and safe to call from an optimizer loop.

Model of a short-play set (all bars are grid bars, 1-based):
  s  where the track starts playing (under the previous track if that was a blend)
  L  landing: the hook/drop the track is played for
  E  OUT DONE: the bar the next track takes over
Blend: the next track starts `overlap` bars before its own landing, under A's last `overlap`
bars, so A's E and B's L coincide. Cut: B starts right on its landing when A stops at E.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from .features import ROW_BASS, ROW_VOCAL, TrackFeatures
from .techniques import TECHNIQUES, options as tech_options

# learned adjustment: (A, B, A's landing, B's landing, family, E, overlap) -> extra cost
ExtraCost = Callable[[TrackFeatures, TrackFeatures, int, int, str, int, int], float]


@dataclass
class PlanInputs:
    order: list[str]
    landings: dict[str, int] = field(default_factory=dict)     # fixed (verified) landings
    hints: dict[str, int] = field(default_factory=dict)        # approximate landings to refine
    exit_pins: dict[str, int] = field(default_factory=dict)
    pair_pins: dict[tuple[str, str], dict] = field(default_factory=dict)  # {family?, technique?}
    grid_fix: dict[str, int] = field(default_factory=dict)     # beat offset of the real bar
    names: dict[str, tuple[str, str]] = field(default_factory=dict)       # display (title, artist)


# ----------------------------------------------------------------------------- helpers
def tempo_gap(bpm_a: float, bpm_b: float, half_double: bool = False) -> float:
    """Fractional tempo gap A -> B (optionally allowing half/double time)."""
    if not bpm_a:
        return 1.0
    gap = abs(bpm_b - bpm_a) / bpm_a
    if half_double:
        for r in (2.0, 0.5):
            gap = min(gap, abs(bpm_b * r - bpm_a) / bpm_a)
    return gap


def camelot_ok(x: Optional[str], y: Optional[str]) -> bool:
    if not x or not y:
        return True
    nx, lx, ny, ly = int(x[:-1]), x[-1], int(y[:-1]), y[-1]
    return (lx == ly and min((nx - ny) % 12, (ny - nx) % 12) <= 1) or nx == ny


def first_land(f: TrackFeatures, default_bar: int = 9) -> int:
    c = f.chorus_starts()
    return c[0] if c else default_bar


def refine_landing(f: TrackFeatures, pick: int, prof: dict, opener: bool = False) -> int:
    """Move an approximate landing to the strongest real change nearby (or keep it)."""
    lp = prof["landing"]; s = f.strength
    cands = list(range(max(2, pick - lp["search_back"]), min(f.n_bars - 8, pick + lp["search_fwd"])))
    if not cands:
        return max(1, min(pick, f.n_bars))
    sc = {b: s[b] + lp["bass_weight"] * max(0.0, f.bass_up(b)) for b in cands}
    best = max(sc, key=sc.get)
    out = pick if sc.get(pick, 0) >= lp["keep_hint_ratio"] * sc[best] or sc[best] < lp["min_strength"] else best
    if opener and 0 <= pick < len(s) and s[pick] > 0.3:
        out = pick
    return out


def overlap_for(f: TrackFeatures, L: int, prof: dict) -> int:
    """Blend length into B: long if B's bars before its landing are one section, else short."""
    long_, short_ = prof["blend"]["overlap_long"], prof["blend"]["overlap_short"]
    s = f.strength
    if L - long_ < 1:
        return max(1, L - 1)
    inside = max(s[L - long_ + 1:L]) if L - long_ + 1 >= 2 else 0
    if inside <= max(0.45, 0.8 * s[L]):
        return long_
    return short_ if L - short_ >= 1 else L - 1


def exit_score(fa: TrackFeatures, La: int, E: int, ov: int, fam: str, sA: int, prof: dict, mins: float) -> Optional[float]:
    """Cost of leaving A at bar E (lower is better); None if E is not allowed."""
    s = fa.strength; play = prof["play"]; pb = prof["phrase_bars"]
    dur = (E - sA) * fa.bar_sec
    if dur < play["min_sec"] or dur > play["max_sec"]:
        return None
    if s[E] < mins:
        return None
    sc = -s[E] + abs(dur - play["target_sec"]) / 12
    if s[E] < 0.6 and (E - La) % pb:
        sc += 0.8
    if (E - La) % max(1, pb // 2):
        sc += 0.8
    if fam == "blend":
        inside = list(range(E - ov + 1, E))
        worst = max((s[x] for x in inside), default=0)
        rise = max((fa.energy(x, x + 2) - fa.energy(x - 2, x) for x in inside), default=0)
        if worst > 0.8 * s[E]:
            sc += 1.0 + worst
        if rise > 0.2:
            sc += 1.5
        sc -= 0.5 * min(1.0, s[E - ov])
    return sc


def best_exit(fa: TrackFeatures, fb: TrackFeatures, La: int, Lb: int, sA: int, prof: dict,
              forced_family: Optional[str] = None, extra: Optional[ExtraCost] = None) -> Optional[tuple[float, int, str, int]]:
    """(cost, E, family, overlap) for the best way out of A into B, or None."""
    ov = overlap_for(fb, Lb, prof)
    tempo = tempo_gap(fa.bpm, fb.bpm, prof["tempo"]["half_double_time"]) <= prof["tempo"]["blend_max_pct"] / 100
    fams = ["blend", "cut"] if tempo else ["cut"]
    vs = prof["blend"]["vocal_stack_pct"]
    best = None
    for mins in prof["exit"]["strength_ladder"]:
        if best:
            break
        for fam in fams:
            for E in range(La + prof["play"]["min_bars_after_landing"], fa.n_bars - 1):
                sc = exit_score(fa, La, E, ov, fam, sA, prof, mins)
                if sc is None:
                    continue
                if fam == "blend":
                    if fa.win(E - ov, E, ROW_VOCAL) >= vs and fb.win(Lb - ov, Lb, ROW_VOCAL) >= vs:
                        continue    # two vocals stacked: cut instead
                elif tempo:
                    sc += prof["blend"]["prefer_blend_bonus"]
                if extra is not None:
                    sc += extra(fa, fb, La, Lb, fam, E, ov)
                if best is None or sc < best[0]:
                    best = (sc, E, fam)
    if best is None:
        return None
    sc, E, fam = best
    if forced_family:
        fam = forced_family
    return sc, E, fam, ov


def closer_exit(f: TrackFeatures, L: int, sA: int, prof: dict) -> int:
    c = prof["closer"]; s = f.strength
    cands = [b for b in range(L + c["min_bars_after_landing"], f.n_bars - 1)
             if s[b] >= 0.3 and (b - sA) * f.bar_sec <= c["max_sec"]]
    if cands:
        return max(cands, key=lambda b: s[b] - abs((b - sA) * f.bar_sec - c["target_sec"]) / 15)
    return min(L + c["fallback_bars"], max(L + 1, f.n_bars))


def fallback_exit(f: TrackFeatures, L: int, prof: dict) -> int:
    """Used when no bar passes the exit rules: leave on the last whole phrase after the landing."""
    pb = prof["phrase_bars"]
    k = max(1, (f.n_bars - 1 - L) // pb)
    return min(L + pb * k, f.n_bars)


def short_name(t: str, n: int = 22) -> str:
    return t if len(t) <= n else t[:n - 1].rstrip() + "."


def place(bar: int, beat_offset: int) -> tuple[int, int]:
    """(bar, beat) on the rekordbox grid for a real bar start that is `beat_offset` beats from the grid's bar."""
    if beat_offset == 0:
        return bar, 1
    idx = (bar - 1) * 4 + beat_offset
    return idx // 4 + 1, idx % 4 + 1


# ----------------------------------------------------------------------------- planner
def plan(fs: list[TrackFeatures], inp: PlanInputs, prof: dict, extra: Optional[ExtraCost] = None) -> dict:
    N = len(fs)
    if N < 2:
        raise ValueError("a set needs at least two tracks")
    F = {i + 1: f for i, f in enumerate(fs)}
    ID = {i: F[i].id for i in F}
    warnings: list[str] = []

    def title(i: int) -> str:
        nm = inp.names.get(ID[i])
        return (nm[0] if nm and nm[0] else F[i].title) or ID[i]

    def artist(i: int) -> str:
        nm = inp.names.get(ID[i])
        return (nm[1] if nm and nm[1] is not None else F[i].artist) or ""

    # 1) landings
    LAND: dict[int, int] = {}
    for i in F:
        if ID[i] in inp.landings:
            LAND[i] = int(inp.landings[ID[i]])
        else:
            pick = int(inp.hints.get(ID[i]) or first_land(F[i], prof["landing"]["default_bar"]))
            LAND[i] = refine_landing(F[i], pick, prof, opener=(i == 1))
        if not 1 <= LAND[i] <= F[i].n_bars:
            raise ValueError(f"{title(i)}: landing bar {LAND[i]} is outside the track (1-{F[i].n_bars})")

    # 2) exits and families, in order (each start depends on how the previous one ended)
    PLAN: dict[int, dict] = {}; FAM: dict[int, str] = {}; OV: dict[int, int] = {}; COST: dict[int, float] = {}
    for a in range(1, N + 1):
        L = LAND[a]
        sA = 1 if a == 1 else (L - OV[a] if FAM[a - 1] == "blend" else L)
        sA = max(1, sA)
        if a == N:
            PLAN[a] = dict(s=sA, L=L, E=inp.exit_pins.get(ID[a]) or closer_exit(F[a], L, sA, prof))
            break
        b = a + 1
        pin = inp.pair_pins.get((ID[a], ID[b])) or {}
        res = best_exit(F[a], F[b], L, LAND[b], sA, prof, forced_family=pin.get("family"), extra=extra)
        if res is None:
            # nothing fits the play-length window (e.g. an opener that starts at bar 1 with a late hook):
            # search again with the window opened up, so the length penalty still pulls toward the target
            loose = {**prof, "play": {**prof["play"], "min_sec": 0, "max_sec": 10 ** 6}}
            res = best_exit(F[a], F[b], L, LAND[b], sA, loose, forced_family=pin.get("family"), extra=extra)
            if res is None:
                res = (99.0, fallback_exit(F[a], L, prof), pin.get("family") or "cut", overlap_for(F[b], LAND[b], prof))
            warnings.append(f"{title(a)}: no exit fits {prof['play']['min_sec']}-{prof['play']['max_sec']}s of play; "
                            f"leaving on bar {res[1]} ({round((res[1] - sA) * F[a].bar_sec)}s)")
        cost, E, fam, ov = res
        E = inp.exit_pins.get(ID[a], E)
        PLAN[a] = dict(s=sA, L=L, E=E); FAM[a] = fam; COST[a] = cost
        OV[b] = ov if fam == "blend" else 0

    # 3) techniques: pinned ones first, then by fit, never the same twice in a row
    allowed = set(prof["techniques"]["allowed"])
    rise = {a: F[a + 1].bpm - F[a].bpm for a in range(1, N)}
    stop_at = None
    cands = [a for a in range(1, N) if FAM[a] == "cut" and rise[a] >= prof["techniques"]["dead_stop_min_rise_bpm"]]
    pinned_stop = any((p or {}).get("technique") == "stop" for p in inp.pair_pins.values())  # one stop per set
    if cands and "stop" in allowed and not pinned_stop:
        stop_at = max(cands, key=lambda a: rise[a])

    def opts(a: int) -> list[str]:
        b = a + 1; o = OV.get(b, 0); E = PLAN[a]["E"]; Lb = PLAN[b]["L"]
        av = F[a].win(E - max(o, 4), E, ROW_VOCAL); bv = F[b].win(Lb - max(o, 4), Lb, ROW_VOCAL)
        bbass = F[b].win(Lb - o, Lb, ROW_BASS)
        return [t for t in tech_options(FAM[a], av, bv, bbass, rise[a]) if t in allowed] or ["cut"]

    TECH: dict[int, str] = {}; used: dict[str, int] = {}
    for a in range(1, N):
        pp = inp.pair_pins.get((ID[a], ID[a + 1])) or {}
        pin = pp.get("technique")
        # strict pins (set by you) always win; soft ones (imported) only where they suit and don't repeat
        if pin and (pp.get("strict", True) or (pin in opts(a) + ["stop"] and pin != TECH.get(a - 1))):
            TECH[a] = pin; used[pin] = used.get(pin, 0) + 1; continue
        if a == stop_at:
            TECH[a] = "stop"; continue
        o = [t for t in opts(a) if t != TECH.get(a - 1)] or opts(a)
        if "brake" in o and used.get("brake", 0) >= prof["techniques"]["max_brakes"] and len(o) > 1:
            o.remove("brake")
        first = o[0]
        pick = min(o[:3], key=lambda t: (used.get(t, 0) - (1.5 if t == first else 0)))
        TECH[a] = pick; used[pick] = used.get(pick, 0) + 1

    # 4) cues and per-transition notes
    CUES: dict[int, list[tuple[int, str, str, str]]] = {i: [] for i in F}
    rows = []
    for a in range(1, N):
        b = a + 1; p, q = PLAN[a], PLAN[b]; tech = TECH[a]; fam = FAM[a]; E = p["E"]; o = OV.get(b, 0) or 8
        sa, sb = short_name(title(a))[:16], short_name(title(b))[:16]
        if fam == "blend":
            CUES[a] += [(E - o, "red", f"OUT START: {sb} in, {o}-bar {tech}", "out_start"),
                        (E, "red", f"OUT DONE: {sb} drops, A out", "out_done")]
            CUES[b] += [(q["s"], "green", f"IN START: under {sa} ({o} bars)", "in_start"),
                        (q["L"], "green", f"IN DONE: drop, {sa} gone", "in_done")]
        else:
            word = TECHNIQUES[tech].out_word or "CUT"
            CUES[a] += [(E - 8, "red", f"OUT START: {sb} ready (8 bars)", "out_start"),
                        (E, "red", f"OUT DONE: {word} -> {sb}", "out_done")]
            CUES[b] += [(q["L"], "green", f"IN: drops on {sa} {word.lower()}", "in")]
        bo = OV.get(b, 0)
        av = F[a].win(E - max(bo, 4), E, ROW_VOCAL); bv = F[b].win(q["L"] - max(bo, 4), q["L"], ROW_VOCAL)
        d = F[b].bpm - F[a].bpm; gap = tempo_gap(F[a].bpm, F[b].bpm, prof["tempo"]["half_double_time"])
        clash = not camelot_ok(F[a].camelot, F[b].camelot)
        why = []
        if fam == "blend":
            why.append(f"{short_name(title(a))} is {'singing' if av >= 50 else 'instrumental'} in its last {o} bars; "
                       f"{short_name(title(b))}'s build ({q['s']}-{q['L'] - 1}) is {'sung' if bv >= 50 else 'instrumental'}.")
            tempo_note = "Same tempo." if abs(d) < 1 else \
                f"B plays at {F[a].bpm:g} during the {o} bars, then ease it to {F[b].bpm:g} ({'+' if d > 0 else ''}{d:.0f} BPM)."
        else:
            if gap > prof["tempo"]["blend_max_pct"] / 100:
                why.append(f"Tempo jumps {abs(d):.0f} BPM, too far to blend.")
            else:
                why.append("Both are singing at this point, so a blend would put two vocals on top of each other.")
            why.append(f"{short_name(title(b))} starts right on its hook at bar {q['L']}.")
            tempo_note = f"B at its own {F[b].bpm:g} BPM ({'+' if d > 0 else ''}{d:.0f})."
        if clash:
            why.append(f"Keys clash ({F[a].camelot} into {F[b].camelot}); keep the overlap thin.")
        rows.append(dict(a=a, b=b, a_id=ID[a], b_id=ID[b], family=fam, technique=tech, overlap=bo,
                         out_start=E - o if fam == "blend" else E - 8, out_done=E,
                         b_in=q["s"] if fam == "blend" else q["L"], b_land=q["L"],
                         a_vocal=round(av), b_vocal=round(bv), gap_pct=round(gap * 100, 1), bpm_delta=round(d, 2),
                         key=f"{'clash' if clash else 'ok'} ({F[a].camelot}→{F[b].camelot})", clash=clash,
                         cost=round(float(COST.get(a, 0.0)), 3), note=" ".join(why), tempo=tempo_note))
    pN = PLAN[N]
    CUES[N] += [(pN["E"] - 8, "red", "OUT START: last 8 bars of the set", "out_start"),
                (pN["E"], "red", "OUT DONE: echo out, end of set", "out_done")]
    CUES[1] = [(1, "green", "IN START: set opener, start here", "in_start"),
               (PLAN[1]["L"], "green", "IN DONE: hook", "in_done")] + CUES[1]

    cues = []
    for i in F:
        bars = [c[0] for c in CUES[i]]
        if len(bars) != len(set(bars)):
            warnings.append(f"{title(i)}: two cues on the same bar {sorted(bars)}; plan is cramped")
        for bar, color, comment, role in sorted(CUES[i]):
            gb, gbeat = place(bar, inp.grid_fix.get(ID[i], 0))
            cues.append(dict(track_id=ID[i], bar=gb, beat=gbeat, color=color, comment=comment, role=role))

    tracks = []
    for i in F:
        p = PLAN[i]
        tracks.append(dict(pos=i, track_id=ID[i], s=p["s"], land=p["L"], e=p["E"],
                           family=FAM.get(i), technique=TECH.get(i), overlap=OV.get(i + 1, 0) if i < N else None,
                           play_sec=round((p["E"] - p["s"]) * F[i].bar_sec),
                           info=dict(title=title(i), artist=artist(i), bpm=F[i].bpm, camelot=F[i].camelot)))
    for r in rows:
        tracks[r["a"] - 1]["info"]["out"] = r
    play = [t["play_sec"] for t in tracks]
    total = sum(play) - sum(r["overlap"] * F[r["a"]].bar_sec for r in rows if r["family"] == "blend")
    srt = sorted(play)
    summary = dict(tracks=N, blends=sum(r["family"] == "blend" for r in rows), cuts=sum(r["family"] == "cut" for r in rows),
                   cues=len(cues), minutes=round(total / 60, 1), play_sec_min=srt[0], play_sec_median=srt[len(srt) // 2],
                   play_sec_max=srt[-1], cost=round(float(sum(COST.values())), 3),
                   techniques={t: sum(r["technique"] == t for r in rows) for t in sorted(set(TECH.values()))})
    return dict(tracks=tracks, transitions=rows, cues=cues, summary=summary, warnings=warnings,
                profile=prof["name"])


def plan_cost(fs: list[TrackFeatures], p: dict, prof: dict) -> float:
    """Re-score an existing plan's choices with the planner's own cost (for comparing plans)."""
    F = {f.id: f for f in fs}
    tr = p["tracks"]; total = 0.0
    for t, u in zip(tr, tr[1:]):
        fa, fb = F[t["track_id"]], F[u["track_id"]]
        ov = overlap_for(fb, u["land"], prof)
        sc = exit_score(fa, t["land"], t["e"], ov, t["family"], t["s"], prof, mins=-1e9)
        if sc is None:
            sc = 5.0     # outside the play-length window
        if t["family"] == "cut" and tempo_gap(fa.bpm, fb.bpm, prof["tempo"]["half_double_time"]) <= prof["tempo"]["blend_max_pct"] / 100:
            sc += prof["blend"]["prefer_blend_bonus"]
        total += sc
    return round(float(total), 3)


__all__ = ["PlanInputs", "plan", "plan_cost", "best_exit", "overlap_for", "exit_score", "refine_landing",
           "first_land", "tempo_gap", "camelot_ok", "place", "np"]
