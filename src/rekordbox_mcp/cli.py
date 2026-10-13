"""Command line for the engine: `rbmcp <command>` (same operations as the MCP tools).

Examples:
  rbmcp plan --playlist 123 --profile kpop          plan in the current order, print summary
  rbmcp sheet 4                                     write the run sheet for plan 4
  rbmcp apply 4 --write                             write plan 4's cues (rekordbox closed)
  rbmcp optimize --playlist 123 --constraints c.json
  rbmcp grid --playlist 123                         list tracks whose beat grid looks off
  rbmcp tracklist mix.txt --profile house           learn from a pro mix's tracklist
  rbmcp web                                         local web app on http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _engine():
    from .engine.service import Engine
    return Engine()


def _out(d) -> None:
    from .rbcore import public
    print(json.dumps(public(d), ensure_ascii=False, indent=1, default=str))


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="rbmcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("plan", help="plan a playlist in its current order")
    s.add_argument("--playlist", required=True); s.add_argument("--profile", default="kpop")
    s.add_argument("--opener"); s.add_argument("--closer"); s.add_argument("--name")

    s = sub.add_parser("optimize", help="optimise order and points")
    s.add_argument("--playlist", required=True); s.add_argument("--profile")
    s.add_argument("--constraints", help="JSON file (see parse_constraints schema)")
    s.add_argument("--keep-order", action="store_true"); s.add_argument("--free-landings", action="store_true")
    s.add_argument("--iters", type=int); s.add_argument("--name")

    s = sub.add_parser("diff", help="what apply would change"); s.add_argument("plan_id", type=int)
    s = sub.add_parser("apply", help="write a plan's cues (dry run unless --write)")
    s.add_argument("plan_id", type=int); s.add_argument("--write", action="store_true")
    s = sub.add_parser("sheet", help="write the HTML run sheet"); s.add_argument("plan_id", type=int)
    s.add_argument("--out"); s.add_argument("--title")
    s = sub.add_parser("export", help="export a plan"); s.add_argument("plan_id", type=int)
    s.add_argument("--format", default="rekordbox_xml"); s.add_argument("--out")
    s = sub.add_parser("queue", help="audition queue, riskiest first"); s.add_argument("plan_id", type=int)
    s.add_argument("--limit", type=int, default=10)
    s = sub.add_parser("audition", help="stage pairs to audition"); s.add_argument("plan_id", type=int)
    s.add_argument("--limit", type=int, default=10); s.add_argument("--write", action="store_true")
    s = sub.add_parser("collect", help="read green/red rating hot cues"); s.add_argument("plan_id", type=int)
    s = sub.add_parser("train", help="fit transition weights from ratings"); s.add_argument("--profile", default="kpop")
    s = sub.add_parser("tracklist", help="import a tracklist with start times"); s.add_argument("file")
    s.add_argument("--profile", default="kpop"); s.add_argument("--dj", default=""); s.add_argument("--llm", action="store_true")
    s = sub.add_parser("grid", help="flag tracks whose beat grid looks off"); s.add_argument("--playlist", required=True)
    s = sub.add_parser("seed", help="import a seed JSON (landings, pins, reference plan)"); s.add_argument("file")
    s.add_argument("--playlist")
    s = sub.add_parser("plans", help="list saved plans"); s.add_argument("--playlist")
    s = sub.add_parser("names", help="fetch readable titles for encrypted streamed tracks"); s.add_argument("--playlist", required=True)
    sub.add_parser("web", help="run the local web app")

    a = ap.parse_args(argv)
    if a.cmd == "web":
        from .webapp.app import main as web
        web(); return 0
    e = _engine()
    if a.cmd == "plan":
        p = e.plan_set(playlist_id=a.playlist, profile=a.profile, opener=a.opener, closer=a.closer, name=a.name)
        _out({"plan_id": p["plan_id"], "summary": p["summary"], "warnings": p["warnings"]})
    elif a.cmd == "optimize":
        cons = json.loads(Path(a.constraints).read_text(encoding="utf-8")) if a.constraints else None
        p = e.optimize_set(playlist_id=a.playlist, constraints=cons, profile=a.profile, keep_order=a.keep_order,
                           free_landings=a.free_landings, iters=a.iters, name=a.name)
        _out({"plan_id": p["plan_id"], "summary": p["summary"], "optimizer": p["optimizer"]})
    elif a.cmd == "diff":
        _out(e.diff_plan(a.plan_id)["counts"])
    elif a.cmd == "apply":
        r = e.apply_plan(a.plan_id, dry_run=not a.write)
        _out(r["counts"] if not a.write else r)
    elif a.cmd == "sheet":
        _out(e.render_run_sheet(a.plan_id, a.out, a.title))
    elif a.cmd == "export":
        _out(e.export_plan(a.plan_id, a.format, a.out))
    elif a.cmd == "queue":
        _out(e.audition_queue(a.plan_id, a.limit))
    elif a.cmd == "audition":
        _out(e.stage_audition(a.plan_id, limit=a.limit, dry_run=not a.write))
    elif a.cmd == "collect":
        _out(e.collect_ratings(a.plan_id))
    elif a.cmd == "train":
        _out(e.train(a.profile))
    elif a.cmd == "tracklist":
        _out(e.import_tracklist(Path(a.file).read_text(encoding="utf-8"), a.profile, name=Path(a.file).stem,
                                dj=a.dj, use_llm=a.llm))
    elif a.cmd == "grid":
        from .engine.features import check_grid
        rows = []
        for t in e.playlist_ids(a.playlist):
            f = e.features(t); g = check_grid(f)
            if g["suspicious"]:
                rows.append({"id": t, "title": f.title, "offset": g["suggested_beat_offset"],
                             "confidence": g["confidence"], "note": g["note"]})
        _out(rows)
    elif a.cmd == "seed":
        _out(e.import_seed(a.file, a.playlist))
    elif a.cmd == "names":
        _out(e.resolve_names(a.playlist))
    elif a.cmd == "plans":
        _out(e.store.plans(a.playlist))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
