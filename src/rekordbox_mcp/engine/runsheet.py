"""Printable run sheet (one self-contained HTML page) for a saved plan.

Shows the BPM arc, the technique legend (doubles as a filter), and one card per transition
with the exact cue bars and times, why the move was picked, QA flags, and Works / Doesn't
buttons whose results can be pasted back with import_ratings.
"""
from __future__ import annotations

import html
import re
from collections import Counter
from pathlib import Path
from typing import Optional

from .techniques import TECHNIQUES

TEMPLATE = Path(__file__).parent / "templates" / "runsheet.html"
FAMCLS = {"blend": "fam-blend", "fx": "fam-fx", "cut": "fam-cut"}
e = html.escape


def _mmss(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def auto_sections(bpms: list[float], min_len: int = 6, jump: float = 18.0) -> list[tuple[int, int, str, str]]:
    """Split the set into parts where the tempo has moved a long way from where the part began."""
    out, start = [], 1
    for i in range(2, len(bpms) + 1):
        if i - start >= min_len and abs(bpms[i - 1] - bpms[start - 1]) > jump:
            out.append((start, i - 1)); start = i
    out.append((start, len(bpms)))
    if len(out) > 1 and out[-1][1] - out[-1][0] + 1 < 3:
        a, _ = out[-2]; out[-2:] = [(a, len(bpms))]
    res = []
    for k, (a, b) in enumerate(out, 1):
        seg = bpms[a - 1:b]
        name = "Opener" if k == 1 else ("Finish" if k == len(out) else f"Part {k}")
        res.append((a, b, name, f"{min(seg):g}-{max(seg):g} BPM"))
    return res


def render(plan: dict, times: Optional[dict] = None, title: Optional[str] = None,
           sections: Optional[list] = None, flags: Optional[dict] = None) -> str:
    """plan: Store.plan(...) output. times: {track_id: [downbeat seconds...]} for cue times.
    flags: {(a_id, b_id): [flag dicts]} from qa.flags_for, shown on the cards."""
    tr = plan["tracks"]; N = len(tr)
    info = {t["pos"]: t["info"] for t in tr}
    rows = [info[t["pos"]]["out"] for t in tr if t["pos"] < N and info[t["pos"]].get("out")]
    TR = {t["pos"]: dict(t=info[t["pos"]].get("title", t["track_id"]), a=info[t["pos"]].get("artist", ""),
                         bpm=info[t["pos"]].get("bpm", 0), cam=info[t["pos"]].get("camelot") or "",
                         id=t["track_id"]) for t in tr}
    P = {t["pos"]: t for t in tr}
    times = times or {}
    flags = flags or {}
    sections = sections or auto_sections([TR[n]["bpm"] for n in range(1, N + 1)])
    cnt = Counter(r["technique"] for r in rows)

    def tm(n: int, bar: Optional[int]) -> str:
        ts = times.get(TR[n]["id"])
        if bar is None or not ts:
            return ""
        if bar > len(ts):
            return "end"
        s = ts[bar - 1]
        return f"{int(s // 60)}:{s % 60:05.2f}"

    # BPM arc
    W, H, PL, PR, PT, PB = 960, 220, 44, 14, 16, 30
    bp = [TR[n]["bpm"] for n in TR]
    lo, hi = (min(bp) // 10) * 10 - 5, (max(bp) // 10 + 1) * 10 + 5
    X = lambda n: PL + (n - 1) * (W - PL - PR) / max(1, N - 1)
    Y = lambda b: PT + (hi - b) * (H - PT - PB) / (hi - lo)
    svg = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="BPM across the set, with each transition marked by type">']
    for s0, s1, name, _ in sections:
        x0, x1 = X(s0) - 6, X(s1) + 6
        svg.append(f'<rect x="{x0:.1f}" y="{PT}" width="{x1 - x0:.1f}" height="{H - PT - PB}" class="band"/>')
        svg.append(f'<text x="{(x0 + x1) / 2:.1f}" y="{H - 10}" class="ax mid">{e(name)}</text>')
    step = 20 if hi - lo > 60 else 10
    for b in range(int(lo // step * step + step), int(hi), step):
        svg.append(f'<line x1="{PL}" x2="{W - PR}" y1="{Y(b):.1f}" y2="{Y(b):.1f}" class="grid"/>'
                   f'<text x="{PL - 8}" y="{Y(b) + 4:.1f}" class="ax end">{b}</text>')
    svg.append('<polyline points="' + " ".join(f"{X(n):.1f},{Y(TR[n]['bpm']):.1f}" for n in range(1, N + 1)) + '" class="bpm"/>')
    for r in rows:
        n = r["a"]; fam = FAMCLS[TECHNIQUES[r["technique"]].family]
        cx = (X(n) + X(n + 1)) / 2; cy = (Y(TR[n]["bpm"]) + Y(TR[n + 1]["bpm"])) / 2
        svg.append(f'<a href="#t{n}"><circle cx="{cx:.1f}" cy="{cy:.1f}" r="5" class="dot {fam}"><title>{n}. '
                   f'{e(TECHNIQUES[r["technique"]].name)}: {e(TR[n]["t"])} into {e(TR[n + 1]["t"])}</title></circle></a>')
    svg.append("</svg>")

    legend = [f'<button class="tech-card" type="button" data-filter="{k}" aria-pressed="false" id="f-{k}">'
              f'<span class="chip {FAMCLS[t.family]}">{e(t.name)}</span><span class="count">{cnt[k]}×</span>'
              f'<span class="how">{e(t.how)}</span></button>' for k, t in TECHNIQUES.items() if cnt[k]]

    def cue(lab: str, col: str, bar: int, t: str) -> str:
        where = "end of track" if t == "end" else f"bar {bar} <i>{e(t or '')}</i>"
        return f'<span class="cue c-{col}"><b>{e(lab)}</b> {where}</span>'

    cards = []
    for s0, s1, name, sub in sections:
        cards.append(f'<h3 class="sec"><span>{e(name)}</span><small>{e(sub)} · tracks {s0}-{s1}</small></h3>')
        for r in rows:
            n = r["a"]
            if not (s0 <= n <= s1):
                continue
            a, b = TR[n], TR[n + 1]; t = TECHNIQUES[r["technique"]]
            blend = r["family"] == "blend"
            meta = (f"{r['overlap']}-bar blend" if blend else "cut") + f" · tempo gap {r['gap_pct']:g}%"
            if blend:
                out = [cue("OUT START", "r", r["out_start"], tm(n, r["out_start"])), cue("OUT DONE", "r", r["out_done"], tm(n, r["out_done"]))]
                ins = [cue("IN START", "g", r["b_in"], tm(n + 1, r["b_in"])), cue("IN DONE", "g", r["b_land"], tm(n + 1, r["b_land"]))]
            else:
                out = [cue("OUT START", "r", r["out_start"], tm(n, r["out_start"])),
                       cue("OUT DONE: " + (t.out_word or "CUT"), "r", r["out_done"], tm(n, r["out_done"]))]
                ins = [cue("IN (start + done)", "g", r["b_land"], tm(n + 1, r["b_land"]))]
            # the note already explains a key clash; don't repeat it as a flag
            fl = [f for f in (flags.get((a["id"], b["id"])) or []) if f.get("code") != "key_clash"]
            fl_html = ("<ul class=\"flags\">" + "".join(f"<li>{e(f['message'])}</li>" for f in fl) + "</ul>") if fl else ""
            pa, pb = P[n], P[n + 1]
            cards.append(f'''<article class="tx" id="t{n}" data-tech="{r['technique']}">
<div class="num">{n}<small>→{n + 1}</small></div>
<div class="body">
<div class="head"><span class="chip {FAMCLS[t.family]}">{e(t.name)}</span><span class="meta">{e(meta)}</span></div>
<div class="flow">
<div class="side"><div class="lbl">Out</div><div class="trk">{e(a['t'])} <span class="by">{e(a['a'])}</span></div><div class="spec">{a['bpm']:g} BPM · {e(a['cam'])} · <b>plays {_mmss(pa['play_sec'] or 0)}</b> (bars {pa['s']}-{pa['e']})</div><div class="cues">{''.join(out)}</div></div>
<div class="side"><div class="lbl">In</div><div class="trk">{e(b['t'])} <span class="by">{e(b['a'])}</span></div><div class="spec">{b['bpm']:g} BPM · {e(b['cam'])} · <b>plays {_mmss(pb['play_sec'] or 0)}</b> (bars {pb['s']}-{pb['e']})</div><div class="cues">{''.join(ins)}</div></div>
</div>
<p class="note">{e(r['note'])}</p>{fl_html}
<p class="small">{e(r['tempo'])} <span class="key{' warn' if r['clash'] else ''}">Key: {e(r['key'])}</span></p>
<div class="rate" data-a="{e(a['id'])}" data-b="{e(b['id'])}"><button type="button" class="yes">Works</button><button type="button" class="no">Doesn't</button></div>
</div>
<label class="done" for="done-{n}"><input type="checkbox" id="done-{n}" data-n="{n}"> Practised</label>
</article>''')

    nb = sum(r["family"] == "blend" for r in rows)
    play = sorted(P[n]["play_sec"] or 0 for n in P)
    s = plan.get("summary") or {}
    brakes = ", ".join(f"#{r['a']}" for r in rows if r["technique"] == "brake") or "none"
    stops = ", ".join(f"#{r['a']}" for r in rows if r["technique"] == "stop") or "none"
    clashes = ", ".join(f"#{r['a']}" for r in rows if r["clash"] and r["family"] == "blend")
    rules = [
        ("Never the same move twice", "Each transition uses a different technique from the one before it."),
        ("Hook to hook", f"Every track lands on its hook or drop and leaves after it: {_mmss(play[0])} to {_mmss(play[-1])} each (median {_mmss(play[len(play) // 2])})."),
        ("Save the big ones", f"Brakes: {brakes}. Dead stop: {stops}."),
        ("Let tempo move", "In a blend the new track plays at the old tempo, then you ease it to its own over 16-32 bars."),
        ("Vocals decide the move", "When only one track sings, blend. When both sing, cut or filter."),
        ("Key clashes stay short", (clashes + " blend across a key clash. Keep those to drums or filter.") if clashes else "No blend crosses a key clash."),
        ("Cue colours", "Green IN START where a song comes in under the last one, green IN DONE where it plays alone, red OUT START where the next song comes in, red OUT DONE where it's gone. Cut-ins have one green cue."),
    ]
    name = title or plan.get("name") or f"Plan {plan.get('plan_id')}"
    stats = (f"<span><b>{N}</b>tracks</span><span><b>{min(bp):g}→{max(bp):g}</b>BPM</span>"
             f"<span><b>{nb}</b>blends</span><span><b>{len(rows) - nb}</b>cuts</span>"
             f"<span><b>~{_mmss(play[len(play) // 2])}</b>per track</span>"
             + (f"<span><b>~{round(s['minutes'])} min</b>set</span>" if s.get("minutes") else ""))
    key = re.sub(r"[^a-z0-9]+", "-", f"rbmcp-plan-{plan.get('plan_id', 0)}".lower())
    page = TEMPLATE.read_text(encoding="utf-8")
    rep = {"%%TITLE%%": e(name), "%%HEADLINE%%": f"{e(name)}: <em>every in and out</em>",
           "%%LEDE%%": e("Each card shows where the next track starts, where to cut, and the move to use. "
                         "Bars and times come from the rekordbox beat grids; vocal and bass checks from rekordbox's own analysis."),
           "%%STATS%%": stats, "%%SVG%%": "\n".join(svg), "%%LEGEND%%": "\n".join(legend), "%%CARDS%%": "\n".join(cards),
           "%%RULES%%": "\n".join(f"    <li><b>{e(a)}</b>{e(b)}</li>" for a, b in rules),
           "%%STORAGE_KEY%%": key, "%%PLAN_ID%%": str(int(plan.get("plan_id") or 0))}
    for k, v in rep.items():
        page = page.replace(k, v)
    return page
