import synth
from rekordbox_mcp.engine import optimizer as O
from rekordbox_mcp.engine import planner
from rekordbox_mcp.engine.planner import PlanInputs


def test_dp_never_worse_than_planner(kpop):
    fs = synth.crate(8)
    inp = PlanInputs(order=[f.id for f in fs])
    hand = planner.plan(fs, inp, kpop)
    pts = O.optimize_points(fs, inp, kpop)
    final = PlanInputs(order=inp.order, landings=pts["landings"], exit_pins=pts["exits"],
                       pair_pins={k: {"family": v} for k, v in pts["families"].items()})
    opt = planner.plan(fs, final, kpop)
    assert planner.plan_cost(fs, opt, kpop) <= planner.plan_cost(fs, hand, kpop) + 1e-6
    assert abs(planner.plan_cost(fs, opt, kpop) - pts["cost"]) < 1e-6


def test_dp_respects_fixed_landings_and_exit_pins(kpop):
    fs = synth.crate(6)
    ids = [f.id for f in fs]
    inp = PlanInputs(order=ids, landings={ids[1]: 66}, exit_pins={ids[2]: 81})
    pts = O.optimize_points(fs, inp, kpop)
    assert pts["landings"][ids[1]] == 66 and pts["exits"][ids[2]] == 81


def test_order_pins_and_subset(kpop):
    fs = synth.crate(12, step=3)
    ids = [f.id for f in fs]
    cons = O.OrderConstraints(opener=ids[5], closer=ids[0], fixed={ids[7]: 3}, adjacent=[(ids[2], ids[9])],
                              size=8, include={ids[11]}, exclude={ids[4]})
    r = O.optimize_order(fs, kpop, cons, iters=6000)
    o = r["order"]
    assert r["violations"] == []
    assert len(o) == 8 and o[0] == ids[5] and o[-1] == ids[0] and o[2] == ids[7]
    assert o.index(ids[9]) == o.index(ids[2]) + 1 and ids[11] in o and ids[4] not in o


def test_order_beats_shuffled_start(kpop):
    import random
    fs = synth.crate(10, step=4)
    start = [f.id for f in fs]; random.Random(1).shuffle(start)
    shuffled = O.order_cost(fs, start, kpop)
    r = O.optimize_order(fs, kpop, start=start, iters=5000)
    assert r["cost"] <= shuffled


def test_energy_curve_pulls_high_energy_late(kpop):
    fs = synth.crate(10, step=4)
    cons = O.OrderConstraints(energy_curve=[(0, 0), (1, 1)])
    r = O.optimize_order(fs, kpop, cons, iters=6000)
    e = [r["energy"][t] for t in r["order"]]
    assert sum(e[-3:]) > sum(e[:3])
