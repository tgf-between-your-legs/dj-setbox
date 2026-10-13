"""Phase 3: optimise a set. Two layers:

1. `optimize_points` - for a FIXED order, dynamic programming over each track's landing
   candidates picks (landing, exit, family) for every track so the summed transition cost
   (planner.exit_score + cut penalty + learned cost) is minimal. Exact for that cost.
2. `optimize_order` - simulated annealing over the running order (optionally choosing a
   subset of a bigger crate) against a fast pairwise cost: tempo, key, energy curve and
   region preferences, with pins (opener, closer, fixed positions, adjacent pairs).

Both respect constraints; the result is fed back through planner.plan for techniques and cues.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .. import rbcore
from .features import ROW_VOCAL, TrackFeatures, find_drops
from .planner import (ExtraCost, PlanInputs, camelot_ok, exit_score, first_land, overlap_for, refine_landing,
                      tempo_gap)

INF = float("inf")


# ----------------------------------------------------------------------------- points (fixed order)
def landing_candidates(f: TrackFeatures, prof: dict, fixed: Optional[int], hint: Optional[int], k: int = 4) -> list[int]:
    if fixed:
        return [int(fixed)]
    cands: list[int] = []
    base = refine_landing(f, int(hint or first_land(f, prof["landing"]["default_bar"])), prof)
    cands.append(base)
    if hint:
        cands.append(int(hint))
    for d in find_drops(f, top=k * 2):
        if d["bar"] not in cands and d["bar"] <= f.n_bars - prof["play"]["min_bars_after_landing"] - 2:
            cands.append(d["bar"])
    return [c for c in cands if 1 <= c <= f.n_bars][:k]


def exit_choices(fa: TrackFeatures, fb: TrackFeatures, La: int, Lb: int, sA: int, prof: dict,
                 forced_family: Optional[str], pinned_exit: Optional[int], extra: Optional[ExtraCost]) -> list[tuple[float, int, str, int]]:
    """Every allowed (cost, E, family, overlap); cost uses the same terms as planner.plan_cost."""
    ov = overlap_for(fb, Lb, prof)
    tempo = tempo_gap(fa.bpm, fb.bpm, prof["tempo"]["half_double_time"]) <= prof["tempo"]["blend_max_pct"] / 100
    fams = [forced_family] if forced_family else (["blend", "cut"] if tempo else ["cut"])
    vs = prof["blend"]["vocal_stack_pct"]
    Es = [pinned_exit] if pinned_exit else range(La + prof["play"]["min_bars_after_landing"], fa.n_bars - 1)
    out = []
    for fam in fams:
        for E in Es:
            if fam == "blend" and not forced_family and fa.win(E - ov, E, ROW_VOCAL) >= vs and fb.win(Lb - ov, Lb, ROW_VOCAL) >= vs:
                continue
            sc = exit_score(fa, La, E, ov, fam, sA, prof, mins=-1e9)
            if sc is None:
                if not pinned_exit:
                    continue
                sc = 5.0
            if fam == "cut" and tempo:
                sc += prof["blend"]["prefer_blend_bonus"]
            if extra is not None:
                sc += extra(fa, fb, La, Lb, fam, E, ov)
            out.append((float(sc), int(E), fam, ov))
    return out


def optimize_points(fs: list[TrackFeatures], inp: PlanInputs, prof: dict, respect_exit_pins: bool = True,
                    extra: Optional[ExtraCost] = None, k_land: int = 4) -> dict:
    """DP over (track, landing, entered-by-blend). Returns landings/exits/families and total cost."""
    N = len(fs)
    ids = [f.id for f in fs]
    cand = [landing_candidates(f, prof, inp.landings.get(f.id), inp.hints.get(f.id), k_land) for f in fs]
    # state: (landing index, entered_by_blend) -> (cost, back pointer, choice)
    best: list[dict] = [{} for _ in range(N)]
    for li, L in enumerate(cand[0]):
        best[0][(li, False)] = (0.0, None, None)
    for i in range(N - 1):
        fa, fb = fs[i], fs[i + 1]
        pin = inp.pair_pins.get((ids[i], ids[i + 1])) or {}
        pe = inp.exit_pins.get(ids[i]) if respect_exit_pins else None
        for (li, blended), (cost, _, _) in best[i].items():
            La = cand[i][li]
            for lj, Lb in enumerate(cand[i + 1]):
                ovA = overlap_for(fa, La, prof)
                sA = 1 if i == 0 else max(1, La - ovA if blended else La)
                choices = exit_choices(fa, fb, La, Lb, sA, prof, pin.get("family"), pe, extra)
                if not choices:
                    continue
                c, E, fam, ov = min(choices)
                key = (lj, fam == "blend")
                tot = cost + c
                if key not in best[i + 1] or tot < best[i + 1][key][0]:
                    best[i + 1][key] = (tot, (li, blended), (E, fam, sA))
    if not best[N - 1]:
        raise ValueError("no feasible plan: every exit was outside the play-length window; try another profile")
    key = min(best[N - 1], key=lambda k: best[N - 1][k][0])
    total = best[N - 1][key][0]
    lands, exits, fams = {}, {}, {}
    for i in range(N - 1, -1, -1):
        li, _ = key
        lands[ids[i]] = cand[i][li]
        _, back, choice = best[i][key]
        if i > 0:
            E, fam, _ = choice
            exits[ids[i - 1]] = E; fams[(ids[i - 1], ids[i])] = fam
            key = back
    return {"landings": lands, "exits": exits, "families": fams, "cost": round(float(total), 3),
            "candidates": {ids[i]: cand[i] for i in range(N)}}


# ----------------------------------------------------------------------------- order
@dataclass
class OrderConstraints:
    opener: Optional[str] = None
    closer: Optional[str] = None
    fixed: dict[str, int] = field(default_factory=dict)         # track -> 1-based position
    adjacent: list[tuple[str, str]] = field(default_factory=list)
    exclude: set[str] = field(default_factory=set)
    include: set[str] = field(default_factory=set)              # must be in the set when choosing a subset
    size: Optional[int] = None                                   # choose this many from the crate
    energy_curve: list[tuple[float, float]] = field(default_factory=list)   # (0..1 position, 0..1 energy)
    bpm_curve: list[tuple[float, float]] = field(default_factory=list)      # (0..1, bpm) optional target
    prefer: list[dict] = field(default_factory=list)            # {ids: [...], region: [x0, x1], weight}
    weights: dict[str, float] = field(default_factory=dict)


DEFAULT_W = {"tempo": 1.0, "key": 1.2, "energy_step": 2.0, "curve": 6.0, "bpm_curve": 0.05, "prefer": 3.0,
             "pin": 1000.0, "down_tempo": 0.5}


def track_energy(fs: list[TrackFeatures], landings: dict[str, int]) -> dict[str, float]:
    """0..1 energy per track: low+mid level around the landing, blended with tempo rank."""
    raw = {}
    for f in fs:
        L = landings.get(f.id) or first_land(f)
        raw[f.id] = f.energy(L, L + 8)
    vals = np.array(list(raw.values())) if raw else np.zeros(1)
    lo, hi = float(vals.min()), float(vals.max())
    bpms = np.array([f.bpm for f in fs]) if fs else np.zeros(1)
    blo, bhi = float(bpms.min()), float(bpms.max())
    out = {}
    for f in fs:
        e = (raw[f.id] - lo) / (hi - lo) if hi > lo else 0.5
        b = (f.bpm - blo) / (bhi - blo) if bhi > blo else 0.5
        out[f.id] = round(0.6 * e + 0.4 * b, 4)
    return out


def _interp(curve: list[tuple[float, float]], x: float) -> float:
    pts = sorted(curve)
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0 or 1)
    return pts[-1][1]


def pair_matrix(fs: list[TrackFeatures], energy: dict[str, float], prof: dict, w: dict) -> np.ndarray:
    n = len(fs); M = np.zeros((n, n))
    hd = prof["tempo"]["half_double_time"]; bl = prof["tempo"]["blend_max_pct"] / 100
    for i, a in enumerate(fs):
        ka = a.key
        for j, b in enumerate(fs):
            if i == j:
                M[i, j] = INF; continue
            g = tempo_gap(a.bpm, b.bpm, hd)
            c = w["tempo"] * (g * 100) * (0.5 if g <= bl else 1.0)
            if b.bpm < a.bpm and g > bl:
                c += w["down_tempo"] * g * 100
            ks, _ = rbcore.key_compatibility(ka, b.key)
            c += w["key"] * (3 - ks) * (1.0 if g <= bl else 0.3)   # key matters less when you cut
            c += w["energy_step"] * max(0.0, energy[a.id] - energy[b.id] - 0.25)
            M[i, j] = c
    return M


def optimize_order(fs: list[TrackFeatures], prof: dict, cons: Optional[OrderConstraints] = None,
                   landings: Optional[dict[str, int]] = None, iters: Optional[int] = None, seed: int = 7,
                   start: Optional[list[str]] = None) -> dict:
    cons = cons or OrderConstraints()
    w = DEFAULT_W | cons.weights
    pool = [f for f in fs if f.id not in cons.exclude]
    idx = {f.id: i for i, f in enumerate(pool)}
    energy = track_energy(pool, landings or {})
    M = pair_matrix(pool, energy, prof, w)
    n_all = len(pool)
    size = min(cons.size or n_all, n_all)
    rng = random.Random(seed)
    prefer = [(set(p["ids"]), p["region"], p.get("weight", 1.0)) for p in cons.prefer]

    def cost(order: list[int]) -> float:
        n = len(order); c = 0.0
        for x, y in zip(order, order[1:]):
            c += M[x, y]
        for k, t in enumerate(order):
            pos = k / max(1, n - 1)
            tid = pool[t].id
            if cons.energy_curve:
                c += w["curve"] * (energy[tid] - _interp(cons.energy_curve, pos)) ** 2
            if cons.bpm_curve:
                c += w["bpm_curve"] * abs(pool[t].bpm - _interp(cons.bpm_curve, pos))
            for ids, (x0, x1), wt in prefer:
                if tid in ids and not (x0 <= pos <= x1):
                    c += w["prefer"] * wt * min(abs(pos - x0), abs(pos - x1))
        # pins (soft but huge, so moves can pass through infeasible states)
        if cons.opener and pool[order[0]].id != cons.opener:
            c += w["pin"]
        if cons.closer and pool[order[-1]].id != cons.closer:
            c += w["pin"]
        where = {pool[t].id: k + 1 for k, t in enumerate(order)}
        for tid, p in cons.fixed.items():
            if where.get(tid) != p:
                c += w["pin"]
        for a, b in cons.adjacent:
            if a not in where or b not in where or where[b] != where[a] + 1:
                c += w["pin"]
        for tid in cons.include:
            if tid not in where:
                c += w["pin"]
        return c

    # initial order: given start, else BPM-sorted (with pins placed)
    if start:
        cur = [idx[t] for t in start if t in idx][:size]
    else:
        cur = sorted(range(n_all), key=lambda i: (pool[i].bpm, energy[pool[i].id]))
        must = [idx[t] for t in cons.include | {x for x in (cons.opener, cons.closer) if x} | set(cons.fixed) if t in idx]
        if size < n_all:
            rest = [i for i in cur if i not in must]
            keep = set(must) | set(rest[:max(0, size - len(must))])
            cur = [i for i in cur if i in keep]
    if cons.opener in idx and idx[cons.opener] in cur:
        cur.remove(idx[cons.opener]); cur.insert(0, idx[cons.opener])
    if cons.closer in idx and idx[cons.closer] in cur:
        cur.remove(idx[cons.closer]); cur.append(idx[cons.closer])
    out_pool = [i for i in range(n_all) if i not in cur]
    cur_c = cost(cur)
    best, best_c = list(cur), cur_c
    n = len(cur)
    iters = iters if iters is not None else max(4000, 300 * n)
    T0 = max(1.0, cur_c / max(1, n) * 0.5)
    for it in range(iters):
        T = T0 * (1 - it / iters) + 1e-3
        cand = list(cur); new_pool = out_pool
        r = rng.random()
        if n < 2:
            break
        i, j = sorted(rng.sample(range(n), 2))
        if r < 0.4:
            cand[i:j + 1] = reversed(cand[i:j + 1])          # 2-opt
        elif r < 0.75:
            cand[i], cand[j] = cand[j], cand[i]              # swap
        elif r < 0.9 or not out_pool:
            x = cand.pop(i); cand.insert(j, x)               # move
        else:
            k = rng.randrange(len(out_pool))                 # replace from the crate
            new_pool = list(out_pool); new_pool[k], cand[i] = cand[i], new_pool[k]
        c = cost(cand)
        if c < cur_c or rng.random() < math.exp(-(c - cur_c) / T):
            cur, cur_c, out_pool = cand, c, new_pool
            if c < best_c:
                best, best_c = list(cand), c
    order = [pool[i].id for i in best]
    where = {t: k + 1 for k, t in enumerate(order)}
    violations = []
    if cons.opener and order[0] != cons.opener:
        violations.append("opener")
    if cons.closer and order[-1] != cons.closer:
        violations.append("closer")
    violations += [f"fixed {t}@{p}" for t, p in cons.fixed.items() if where.get(t) != p]
    violations += [f"adjacent {a}->{b}" for a, b in cons.adjacent if where.get(b) != (where.get(a) or -9) + 1]
    violations += [f"include {t}" for t in cons.include if t not in where]
    return {"order": order, "cost": round(float(best_c), 3), "violations": violations,
            "energy": {t: energy[t] for t in order}, "iters": iters}


def order_cost(fs: list[TrackFeatures], order: list[str], prof: dict, cons: Optional[OrderConstraints] = None,
               landings: Optional[dict[str, int]] = None) -> float:
    """Pairwise-model cost of a given order (for comparing against a hand-made one)."""
    cons = cons or OrderConstraints()
    r = optimize_order([f for f in fs if f.id in set(order)], prof, cons, landings, iters=0, start=order)
    return r["cost"]


__all__ = ["optimize_points", "optimize_order", "OrderConstraints", "track_energy", "landing_candidates",
           "order_cost", "camelot_ok"]
