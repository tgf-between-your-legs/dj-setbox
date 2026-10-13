import synth
from rekordbox_mcp.engine import planner, runsheet
from rekordbox_mcp.engine.planner import PlanInputs


def test_render_has_a_card_per_transition(kpop, store):
    fs = synth.crate(7)
    p = planner.plan(fs, PlanInputs(order=[f.id for f in fs]), kpop)
    pid = store.save_plan(playlist_id=None, name="Test Set", profile="kpop", params={}, summary=p["summary"],
                          tracks=p["tracks"], cues=p["cues"])
    times = {f.id: [float(f.times[j]) for j in f.db] for f in fs}
    page = runsheet.render(store.plan(pid), times=times)
    assert "%%" not in page
    assert page.count('class="tx"') == 6 and page.count('class="rate"') == 6
    assert "Test Set" in page and "rbmcp-plan-" in page


def test_auto_sections_cover_everything():
    s = runsheet.auto_sections([100, 101, 102, 103, 104, 105, 130, 131, 132, 133, 134, 135, 170, 171])
    assert s[0][0] == 1 and s[-1][1] == 14
    assert all(a[1] + 1 == b[0] for a, b in zip(s, s[1:]))
