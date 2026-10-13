import numpy as np

from rekordbox_mcp.engine import learn
from rekordbox_mcp.engine.qa import FEATURE_NAMES


def _feats(rng, stack):
    f = {k: 0 for k in FEATURE_NAMES}
    f.update(tempo_gap_pct=float(rng.uniform(0, 12)), vocal_stack=int(stack), b_land_strength=float(rng.uniform(.2, 2)),
             bass_overlap=float(rng.uniform(0, 1)), a_play_dev=float(rng.uniform(0, .5)))
    return f


def test_auc():
    assert learn.auc(np.array([.9, .8, .1]), np.array([1, 1, 0])) == 1.0
    assert learn.auc(np.array([.1]), np.array([1])) is None


def test_train_needs_data(store):
    r = learn.train(store)
    assert r["trained"] is False


def test_train_learns_hidden_rule(store):
    # verdict depends on bass_overlap, which the hand score barely weighs: the model should beat it
    rng = np.random.default_rng(0)
    for i in range(140):
        f = _feats(rng, stack=rng.random() < .2)
        works = f["bass_overlap"] < 0.5
        store.add_rating(f"a{i}", f"b{i}", works, "sheet", features=f)
    r = learn.train(store)
    assert r["trained"] and r["eval"]["auc_learned"] > r["eval"]["auc_hand"]
    assert r["active"] is True
    assert learn.active_model(store, "kpop")["features"] == FEATURE_NAMES


def test_active_cost_is_none_without_model(store):
    assert learn.active_cost(store, "kpop") is None
