"""Phase 5: local web app over the same engine (FastAPI + a single-page UI).

Run:  setbox-web                  (or: python -m rekordbox_mcp.webapp.app)
      http://127.0.0.1:8765
Binds to localhost only: it can write to your rekordbox library (with rekordbox closed).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..engine.service import Engine
from ..rbcore import public

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="dj-setbox set planner", version="0.2")
_lock = threading.RLock()       # one writer at a time; rekordbox's DB is not built for concurrency
_engine: Optional[Engine] = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = Engine()
    return _engine


@app.exception_handler(ValueError)
async def _value_error(_, exc: ValueError):
    return JSONResponse({"error": str(exc)}, status_code=400)


@app.exception_handler(RuntimeError)
async def _runtime_error(_, exc: RuntimeError):
    return JSONResponse({"error": str(exc)}, status_code=409)


class PlanReq(BaseModel):
    playlist_id: str
    profile: str = "kpop"
    opener: Optional[str] = None
    closer: Optional[str] = None
    pins: Optional[dict] = None
    name: Optional[str] = None


class OptReq(BaseModel):
    playlist_id: str
    profile: Optional[str] = None
    constraints: Optional[dict] = None
    keep_order: bool = False
    free_landings: bool = False
    iters: Optional[int] = None
    name: Optional[str] = None


class RatingReq(BaseModel):
    a: str
    b: str
    works: bool
    plan_id: Optional[int] = None


def _plan_view(pid: int) -> dict:
    from ..engine.audition import _transitions
    e = engine()
    plan = e.store.plan(pid)
    trans = _transitions(e, plan)
    rated = {(r["a_id"], r["b_id"]): bool(r["verdict"]) for r in e.store.ratings()}
    for t in plan["tracks"]:
        t["info"].pop("out", None)
    return public({**{k: plan[k] for k in ("plan_id", "playlist_id", "name", "profile", "status", "created_at",
                                          "summary", "tracks")},
                   "transitions": [{**{k: r[k] for k in ("pos", "a_id", "b_id", "a_title", "b_title", "family",
                                                          "technique", "overlap", "out_start", "out_done", "b_in",
                                                          "b_land", "risk")},
                                    "flags": [f["message"] for f in r["flags"]],
                                    "rating": rated.get((r["a_id"], r["b_id"]))} for r in trans]})


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict:
    e = engine()
    return {"ok": True, "rekordbox_running": e.lib.rekordbox_running(), "engine_db": str(e.store.path),
            "llm": bool(os.environ.get("ANTHROPIC_API_KEY"))}


@app.get("/api/profiles")
def profiles_() -> dict:
    from ..engine import profiles
    return {p: profiles.load(p)["description"] for p in profiles.available()}


@app.get("/api/playlists")
def playlists() -> list[dict]:
    with _lock:
        e = engine(); e.lib.reopen()
        return [p for p in e.lib.playlists() if p["type"] == "playlist" and p["tracks"]]


@app.get("/api/playlists/{pid}/tracks")
def crate(pid: str) -> list[dict]:
    from ..engine.optimizer import track_energy
    with _lock:
        e = engine(); e.lib.reopen()
        rows = e.crate(pid)
        fs = e.features_many([r["id"] for r in rows])
        en = track_energy(fs, {t: v["bar"] for t, v in e.store.landings().items()})
        for r in rows:
            r["energy"] = en.get(r["id"])
        return public(rows)


@app.get("/api/plans")
def plans(playlist_id: Optional[str] = None) -> list[dict]:
    return engine().store.plans(playlist_id)


@app.post("/api/plan")
def plan(req: PlanReq) -> dict:
    with _lock:
        e = engine(); e.lib.reopen()
        p = e.plan_set(playlist_id=req.playlist_id, profile=req.profile, opener=req.opener, closer=req.closer,
                       pins=req.pins, name=req.name)
        return _plan_view(p["plan_id"])


@app.post("/api/optimize")
def optimize(req: OptReq) -> dict:
    with _lock:
        e = engine(); e.lib.reopen()
        p = e.optimize_set(playlist_id=req.playlist_id, constraints=req.constraints, profile=req.profile,
                           keep_order=req.keep_order, free_landings=req.free_landings, iters=req.iters, name=req.name)
        return _plan_view(p["plan_id"]) | {"optimizer": public(p["optimizer"])}


@app.get("/api/plans/{pid}")
def get_plan(pid: int) -> dict:
    with _lock:
        return _plan_view(pid)


@app.get("/api/plans/{pid}/runsheet", response_class=HTMLResponse)
def runsheet(pid: int) -> str:
    with _lock:
        r = engine().render_run_sheet(pid)
        return Path(r["path"]).read_text(encoding="utf-8")


@app.get("/api/plans/{pid}/diff")
def diff(pid: int) -> dict:
    with _lock:
        e = engine(); e.lib.reopen()
        d = e.diff_plan(pid)
        return {"counts": d["counts"], "foreign": d["foreign"][:50], "uncoloured_foreign": d["uncoloured_foreign"]}


@app.post("/api/plans/{pid}/apply")
def apply(pid: int, dry_run: bool = Body(True, embed=True)) -> dict:
    with _lock:
        e = engine(); e.lib.reopen()
        if not dry_run and e.lib.rekordbox_running():
            raise HTTPException(409, "rekordbox is running; close it (File > Exit) before writing")
        r = e.apply_plan(pid, dry_run=dry_run)
        return {"counts": r["counts"]} if dry_run else public(r)


@app.post("/api/plans/{pid}/audition")
def audition(pid: int, body: dict = Body(default={})) -> dict:
    with _lock:
        e = engine(); e.lib.reopen()
        return public(e.stage_audition(pid, body.get("pairs"), int(body.get("limit", 10)),
                                       body.get("playlist_name", "Audition"), bool(body.get("dry_run", True))))


@app.post("/api/plans/{pid}/collect")
def collect(pid: int) -> dict:
    with _lock:
        e = engine(); e.lib.reopen()
        return e.collect_ratings(pid)


@app.post("/api/ratings")
def rate(req: RatingReq) -> dict:
    with _lock:
        e = engine()
        feats = None
        if req.plan_id:
            from ..engine.audition import _transitions
            feats = next((r["features"] for r in _transitions(e, e.store.plan(req.plan_id))
                          if r["a_id"] == req.a and r["b_id"] == req.b), None)
        e.store.add_rating(req.a, req.b, req.works, source="app", plan_id=req.plan_id, features=feats)
        return {"ok": True}


@app.post("/api/train")
def train(profile: str = Body("kpop", embed=True)) -> dict:
    with _lock:
        return engine().train(profile)


@app.post("/api/constraints/parse")
def parse_constraints(text: str = Body(..., embed=True), playlist_id: str = Body(..., embed=True)) -> dict:
    with _lock:
        return engine().parse_constraints(text, playlist_id)


@app.get("/api/plans/{pid}/export/{fmt}")
def export(pid: int, fmt: str) -> Any:
    with _lock:
        r = engine().export_plan(pid, fmt)
        return FileResponse(r["path"], filename=Path(r["path"]).name,
                            headers={"X-Export-Warnings": str(len(r["warnings"]))})


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("RBMCP_WEB_PORT", "8765")))


if __name__ == "__main__":
    main()
