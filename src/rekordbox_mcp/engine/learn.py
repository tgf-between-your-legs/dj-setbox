"""Phase 4: learn what "works" from ratings.

A regularised logistic regression (plain numpy, no extra dependencies) over
qa.FEATURE_NAMES, trained per profile on rekordbox / run-sheet ratings (weight 1) and
tracklist-derived weak labels (low weight). Evaluation is k-fold AUC of the learned
probability against the hand-written risk score on the same folds.

A model only steers planning once it is `active`: trained on >= MIN_ACTIVE ratings and
beating the hand score on held-out data. Then planner/optimizer add
`scale * (0.5 - P(works))` to each candidate exit's cost.
"""
from __future__ import annotations

import json
from typing import Optional

import numpy as np

from . import profiles as _profiles
from .qa import FEATURE_NAMES, RISK_WEIGHTS, transition_features
from .store import Store, now

MIN_TRAIN = 20
MIN_ACTIVE = 100
COST_SCALE = 2.0


def hand_risk(f: dict) -> float:
    """The hand-written risk score, computed from a feature dict (the baseline to beat)."""
    r = 0.0
    r += RISK_WEIGHTS["vocal_stack"] * f.get("vocal_stack", 0)
    r += RISK_WEIGHTS["bass_overlap"] * (f.get("bass_overlap", 0) > 0.5)
    r += RISK_WEIGHTS["b_land_on_break"] * f.get("b_land_on_break", 0)
    r += RISK_WEIGHTS["weak_landing"] * (f.get("b_land_strength", 1) < 0.3)
    r += RISK_WEIGHTS["b_flat"] * f.get("b_flat", 0)
    r += RISK_WEIGHTS["key_clash"] * f.get("key_clash", 0)
    r += RISK_WEIGHTS["exit_off_phrase"] * f.get("exit_off_phrase", 0)
    return r + f.get("tempo_gap_pct", 0) / 10


def _xy(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    X = np.array([[float(r["features"].get(k, 0)) for k in FEATURE_NAMES] for r in rows], float)
    y = np.array([r["verdict"] for r in rows], float)
    w = np.array([r.get("weight", 1.0) for r in rows], float)
    return X, y, w


def fit(X: np.ndarray, y: np.ndarray, w: Optional[np.ndarray] = None, l2: float = 1.0, iters: int = 3000,
        lr: float = 0.1) -> dict:
    w = np.ones(len(y)) if w is None else w
    mu = X.mean(0); sd = X.std(0); sd[sd == 0] = 1
    Z = (X - mu) / sd
    beta = np.zeros(Z.shape[1]); b0 = 0.0
    sw = w.sum()
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(Z @ beta + b0)))
        g = (p - y) * w
        beta -= lr * ((Z.T @ g) / sw + l2 * beta / sw)
        b0 -= lr * g.sum() / sw
    return {"mu": mu.tolist(), "sd": sd.tolist(), "beta": beta.tolist(), "b0": float(b0)}


def predict(m: dict, X: np.ndarray) -> np.ndarray:
    Z = (X - np.array(m["mu"])) / np.array(m["sd"])
    return 1 / (1 + np.exp(-(Z @ np.array(m["beta"]) + m["b0"])))


def auc(score: np.ndarray, y: np.ndarray) -> Optional[float]:
    pos, neg = score[y == 1], score[y == 0]
    if not len(pos) or not len(neg):
        return None
    wins = (pos[:, None] > neg[None, :]).sum() + 0.5 * (pos[:, None] == neg[None, :]).sum()
    return float(wins / (len(pos) * len(neg)))


def evaluate(rows: list[dict], k: int = 5, seed: int = 0) -> dict:
    """k-fold AUC of the learned model vs the hand risk score (evaluated on strong labels only)."""
    rng = np.random.default_rng(seed)
    idx = rng.permutation(len(rows))
    folds = np.array_split(idx, min(k, len(rows)))
    learned, hand, ys = [], [], []
    for f in folds:
        test = [rows[i] for i in f if rows[i]["source"] != "tracklist"]
        train = [rows[i] for i in idx if i not in set(f)]
        if not test or len({r["verdict"] for r in train}) < 2:
            continue
        m = fit(*_xy(train))
        Xt, yt, _ = _xy(test)
        learned += list(predict(m, Xt)); hand += [-hand_risk(r["features"]) for r in test]; ys += list(yt)
    y = np.array(ys)
    return {"auc_learned": auc(np.array(learned), y), "auc_hand": auc(np.array(hand), y), "n_test": len(ys)}


def training_rows(store: Store) -> list[dict]:
    return [r for r in store.ratings() if r["features"]]


def train(store: Store, profile: str = "kpop", force_active: bool = False) -> dict:
    rows = training_rows(store)
    strong = [r for r in rows if r["source"] != "tracklist"]
    if len(strong) < MIN_TRAIN or len({r["verdict"] for r in rows}) < 2:
        return {"trained": False, "reason": f"need >= {MIN_TRAIN} ratings with both verdicts; have {len(strong)}",
                "ratings": len(strong), "weak_labels": len(rows) - len(strong)}
    m = fit(*_xy(rows))
    ev = evaluate(rows)
    better = ev["auc_learned"] is not None and ev["auc_hand"] is not None and ev["auc_learned"] > ev["auc_hand"]
    active = force_active or (len(strong) >= MIN_ACTIVE and better)
    data = {"features": FEATURE_NAMES, **m, "eval": ev}
    with store.tx() as c:
        if active:
            c.execute("UPDATE model SET active=0 WHERE profile=?", (profile,))
        cur = c.execute("INSERT INTO model (profile, created_at, n_train, active, data) VALUES (?,?,?,?,?)",
                        (profile, now(), len(rows), int(active), json.dumps(data)))
    weights = sorted(zip(FEATURE_NAMES, m["beta"]), key=lambda kv: -abs(kv[1]))[:8]
    return {"trained": True, "model_id": cur.lastrowid, "active": active, "n": len(rows), "eval": ev,
            "top_weights": [{"feature": k, "weight": round(v, 3)} for k, v in weights],
            "note": None if active else f"stays inactive until >= {MIN_ACTIVE} ratings and it beats the hand score"}


def active_model(store: Store, profile: str) -> Optional[dict]:
    r = store.q1("SELECT * FROM model WHERE profile=? AND active=1 ORDER BY model_id DESC LIMIT 1", (profile,))
    return json.loads(r["data"]) if r else None


def active_cost(store: Store, profile: str):
    """planner ExtraCost from the active model, or None."""
    m = active_model(store, profile)
    if not m or m.get("features") != FEATURE_NAMES:
        return None
    prof = _profiles.load(profile)

    def extra(fa, fb, La, Lb, fam, E, ov):
        f = transition_features(fa, fb, La, Lb, E, fam, ov if fam == "blend" else 0, None, prof)
        p = float(predict(m, np.array([[float(f.get(k, 0)) for k in FEATURE_NAMES]]))[0])
        return COST_SCALE * (0.5 - p)
    return extra
