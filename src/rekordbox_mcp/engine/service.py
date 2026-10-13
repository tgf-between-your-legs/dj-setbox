"""Engine facade: the one object MCP tools, the web app and the CLI talk to.

It owns a rbcore.Library (rekordbox) and a Store (engine DB), loads cached features, turns
stored pins into PlanInputs, runs the planner/optimizer and persists results.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .. import rbcore
from . import apply as _apply
from . import features as _features
from . import planner as _planner
from . import profiles as _profiles
from .store import Store


class Engine:
    def __init__(self, lib: Optional[rbcore.Library] = None, store: Optional[Store] = None):
        self.lib = lib or rbcore.Library()
        self.store = store or Store()

    # ------------------------------------------------------------------ features
    def features(self, track_id: str, refresh: bool = False) -> _features.TrackFeatures:
        return _features.load(self.lib, self.store, str(track_id), refresh=refresh)

    def features_many(self, track_ids: list[str], refresh: bool = False) -> list[_features.TrackFeatures]:
        return [self.features(t, refresh) for t in track_ids]

    def playlist_ids(self, playlist_id: str) -> list[str]:
        return [str(r["id"]) for r in self.lib.playlist_tracks(str(playlist_id))]

    def analyze_track(self, track_id: str, refresh: bool = False) -> dict:
        f = self.features(track_id, refresh)
        land = self.store.landings().get(str(track_id))
        return {"id": f.id, "title": f.title, "artist": f.artist, "bpm": f.bpm, "camelot": f.camelot, "mood": f.mood,
                "bars": f.n_bars, "bar_sec": round(f.bar_sec, 3), "phrases": f.phrases,
                "chorus_starts": f.chorus_starts(), "drops": _features.find_drops(f),
                "grid": _features.check_grid(f), "landing": land,
                "grid_fix": self.store.grid_fixes().get(str(track_id), 0), "warnings": f.warnings}

    # ------------------------------------------------------------------ inputs
    def inputs(self, order: list[str], pins: Optional[dict] = None) -> _planner.PlanInputs:
        """Stored landings/pins for these tracks, with per-call overrides on top.

        pins (all optional): {landings: {id: bar}, exits: {id: bar}, hints: {id: bar},
                              pairs: [{a, b, family?, technique?}]}
        """
        pins = pins or {}
        lands = self.store.landings()
        fixed = {t: r["bar"] for t, r in lands.items() if r["source"] in ("ear", "seed") and t in order}
        hints = {t: r["bar"] for t, r in lands.items() if r["source"] in ("auto", "hint") and t in order}
        fixed.update({str(k): int(v) for k, v in (pins.get("landings") or {}).items()})
        hints.update({str(k): int(v) for k, v in (pins.get("hints") or {}).items()})
        exits = {t: b for t, b in self.store.exit_pins().items() if t in order}
        exits.update({str(k): int(v) for k, v in (pins.get("exits") or {}).items()})
        pairs = {k: {"family": v["family"], "technique": v["technique"], "strict": bool(v["strict"])}
                 for k, v in self.store.pair_pins().items()}
        for p in pins.get("pairs") or []:
            pairs[(str(p["a"]), str(p["b"]))] = {"family": p.get("family"), "technique": p.get("technique"),
                                                 "strict": True}
        names = {t: (r["title"], r["artist"]) for t, r in self.store.names().items()}
        return _planner.PlanInputs(order=list(order), landings=fixed, hints=hints, exit_pins=exits, pair_pins=pairs,
                                   grid_fix=self.store.grid_fixes(), names=names)

    # ------------------------------------------------------------------ planning
    def plan_set(self, playlist_id: Optional[str] = None, order: Optional[list[str]] = None,
                 profile: str = "kpop", opener: Optional[str] = None, closer: Optional[str] = None,
                 pins: Optional[dict] = None, name: Optional[str] = None, save: bool = True,
                 use_model: bool = True) -> dict:
        if order is None:
            if not playlist_id:
                raise ValueError("give playlist_id or order")
            order = self.playlist_ids(playlist_id)
        order = [str(t) for t in order]
        if opener:
            order = [str(opener)] + [t for t in order if t != str(opener)]
        if closer:
            order = [t for t in order if t != str(closer)] + [str(closer)]
        prof = _profiles.load(profile)
        fs = self.features_many(order)
        inp = self.inputs(order, pins)
        extra = self._model_cost(profile) if use_model else None
        p = _planner.plan(fs, inp, prof, extra=extra)
        if save:
            pid = self.store.save_plan(playlist_id=playlist_id, name=name or f"plan {profile}", profile=profile,
                                       params={"pins": pins or {}, "opener": opener, "closer": closer,
                                               "model": bool(extra)},
                                       summary=p["summary"] | {"warnings": p["warnings"]},
                                       tracks=p["tracks"], cues=p["cues"])
            p["plan_id"] = pid
        return p

    def _model_cost(self, profile: str):
        from . import learn
        return learn.active_cost(self.store, profile)

    def score_transition(self, a_id: str, b_id: str, profile: str = "kpop",
                         a_land: Optional[int] = None, b_land: Optional[int] = None) -> dict:
        from .qa import transition_report
        prof = _profiles.load(profile)
        fa, fb = self.features(a_id), self.features(b_id)
        inp = self.inputs([str(a_id), str(b_id)])
        La = a_land or inp.landings.get(str(a_id)) or _planner.refine_landing(fa, _planner.first_land(fa), prof)
        Lb = b_land or inp.landings.get(str(b_id)) or _planner.refine_landing(fb, _planner.first_land(fb), prof)
        return transition_report(fa, fb, La, Lb, prof)

    # ------------------------------------------------------------------ apply
    def diff_plan(self, plan_id: int) -> dict:
        return _apply.diff_plan(self.lib, self.store, plan_id)

    def apply_plan(self, plan_id: int, dry_run: bool = True) -> dict:
        return _apply.apply_plan(self.lib, self.store, plan_id, dry_run=dry_run)

    # ------------------------------------------------------------------ seed import
    def import_seed(self, path: str | Path, playlist_id: Optional[str] = None, adopt: bool = True) -> dict:
        """Load a seed file (landings, pins, names, a reference plan and its cues) into the store.

        Format (JSON): {order, names: {id: {title, artist}}, landing: {id: bar}, exit_pin: {id: bar},
        family_pin: [[a, b, fam]], tech_pin: [[a, b, tech]], half_grid: [id] | grid_fix: {id: offset},
        plan: {id: {pos, s, L, E, fam, tech, ov}}, cues: {id: {cues: [[bar, beat, color, comment]]}}}
        """
        seed = json.loads(Path(path).read_text(encoding="utf-8"))
        st = self.store
        for tid, nm in (seed.get("names") or {}).items():
            st.set_name(tid, nm.get("title"), nm.get("artist"), source="seed")
        for tid, bar in (seed.get("landing") or {}).items():
            st.set_landing(tid, int(bar), source="seed")
        for tid, bar in (seed.get("exit_pin") or {}).items():
            st.set_exit_pin(tid, int(bar), note="seed")
        for tid in seed.get("half_grid") or []:
            st.set_grid_fix(tid, -2, note="grid half a bar off (seed)")
        for tid, off in (seed.get("grid_fix") or {}).items():
            st.set_grid_fix(tid, int(off), note="seed")
        fam = {(a, b): f for a, b, f in seed.get("family_pin") or []}
        tech = {(a, b): t for a, b, t in seed.get("tech_pin") or []}
        for k in set(fam) | set(tech):
            # imported technique choices are soft (kept only where they still suit); family pins are kept
            st.set_pair_pin(k[0], k[1], family=fam.get(k), technique=tech.get(k), note="seed", strict=False)
        out: dict[str, Any] = {"names": len(seed.get("names") or {}), "landings": len(seed.get("landing") or {}),
                               "exit_pins": len(seed.get("exit_pin") or {}), "pair_pins": len(set(fam) | set(tech))}
        if seed.get("plan") and seed.get("cues"):
            order = seed["order"]
            tracks = []
            for tid in order:
                r = seed["plan"][tid]
                tracks.append(dict(pos=r["pos"], track_id=tid, s=r["s"], land=r["L"], e=r["E"], family=r.get("fam"),
                                   technique=r.get("tech"), overlap=r.get("ov")))
            cues = [dict(track_id=tid, bar=c[0], beat=c[1], color=c[2], comment=c[3])
                    for tid, v in seed["cues"].items() for c in v["cues"]]
            pid = st.save_plan(playlist_id=playlist_id, name="imported reference plan", profile="kpop",
                               params={"imported": str(Path(path).name)}, summary={"cues": len(cues)},
                               tracks=tracks, cues=cues)
            out["reference_plan_id"] = pid
            if adopt:
                out["ledger"] = _apply.adopt_matching(self.lib, st, pid)
                st.mark_applied(pid, None)
        return out

    # ------------------------------------------------------------------ run sheet
    def render_run_sheet(self, plan_id: int, path: Optional[str] = None, title: Optional[str] = None,
                         with_flags: bool = True) -> dict:
        from . import runsheet
        from .audition import _transitions
        plan = self.store.plan(plan_id)
        times = {}
        for t in plan["tracks"]:
            f = self.features(t["track_id"])
            times[t["track_id"]] = [float(f.times[j]) for j in f.db]
        flags = {}
        if with_flags:
            flags = {(r["a_id"], r["b_id"]): r["flags"] for r in _transitions(self, plan)}
        page = runsheet.render(plan, times=times, title=title, flags=flags)
        out = Path(path) if path else self.store.path.parent / "runsheets" / f"plan-{plan_id}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page, encoding="utf-8")
        return {"path": str(out), "bytes": len(page)}

    # ------------------------------------------------------------------ audition (phase 2)
    def audition_queue(self, plan_id: int, limit: Optional[int] = None) -> dict:
        from . import audition
        return audition.audition_queue(self, plan_id, limit)

    def stage_audition(self, plan_id: int, pairs: Optional[list] = None, limit: int = 10,
                       playlist_name: str = "Audition", dry_run: bool = True) -> dict:
        from . import audition
        return audition.stage_audition(self, plan_id, pairs, limit, playlist_name, dry_run)

    def collect_ratings(self, plan_id: int) -> dict:
        from . import audition
        return audition.collect_ratings(self, plan_id)

    def import_ratings(self, payload: str | dict) -> dict:
        from . import audition
        return audition.import_ratings(self, payload)

    # ------------------------------------------------------------------ optimizer (phase 3)
    def crate(self, playlist_id: str) -> list[dict]:
        return [{"id": str(r["id"]), "title": r["title"], "artist": r["artist"], "bpm": r["bpm"], "genre": r["genre"],
                 "camelot": r["camelot"], "comment": r["comment"]} for r in self.lib.playlist_tracks(str(playlist_id))]

    def optimize_set(self, playlist_id: Optional[str] = None, crate_ids: Optional[list[str]] = None,
                     constraints: Optional[dict] = None, profile: Optional[str] = None, keep_order: bool = False,
                     free_landings: bool = False, respect_exit_pins: bool = True, iters: Optional[int] = None,
                     seed: int = 7, save: bool = True, name: Optional[str] = None) -> dict:
        """Choose order (unless keep_order) and every landing/exit, then build the full plan."""
        from . import constraints as _cons
        from . import optimizer as _opt
        constraints = dict(constraints or {})
        profile = profile or constraints.get("profile") or "kpop"
        prof = _profiles.load(profile)
        crate = self.crate(playlist_id) if playlist_id else [
            {"id": str(t), **{k: v for k, v in self.lib.track_summary(self.lib.content(t)).items() if k != "id"}}
            for t in (crate_ids or [])]
        ids = [c["id"] for c in crate]
        fs = {f.id: f for f in self.features_many(ids)}
        cons = _cons.to_order_constraints(constraints, crate, prof["play"]["target_sec"]) if constraints else _opt.OrderConstraints()
        inp0 = self.inputs(ids)
        if keep_order:
            order = [i for i in ids if i not in cons.exclude]
            ores = {"order": order, "cost": None, "violations": []}
        else:
            ores = _opt.optimize_order(list(fs.values()), prof, cons, landings=inp0.landings, iters=iters, seed=seed)
            order = ores["order"]
        inp = self.inputs(order)
        if free_landings:
            inp.hints.update({k: v for k, v in inp.landings.items()
                              if (self.store.landings().get(k) or {}).get("source") != "ear"})
            inp.landings = {k: v for k, v in inp.landings.items()
                            if (self.store.landings().get(k) or {}).get("source") == "ear"}
        extra = self._model_cost(profile)
        flist = [fs[t] for t in order]
        pts = _opt.optimize_points(flist, inp, prof, respect_exit_pins=respect_exit_pins, extra=extra)
        final = _planner.PlanInputs(order=order, landings=pts["landings"], hints={}, exit_pins=pts["exits"],
                                    pair_pins={**inp.pair_pins, **{k: {**(inp.pair_pins.get(k) or {}), "family": v}
                                                                   for k, v in pts["families"].items()}},
                                    grid_fix=inp.grid_fix, names=inp.names)
        p = _planner.plan(flist, final, prof, extra=extra)
        p["optimizer"] = {"order_cost": ores["cost"], "violations": ores["violations"], "points_cost": pts["cost"],
                          "plan_cost": _planner.plan_cost(flist, p, prof)}
        if save:
            p["plan_id"] = self.store.save_plan(
                playlist_id=playlist_id, name=name or f"optimized {profile}", profile=profile,
                params={"constraints": constraints, "keep_order": keep_order, "free_landings": free_landings},
                summary=p["summary"] | {"optimizer": p["optimizer"], "warnings": p["warnings"]},
                tracks=p["tracks"], cues=p["cues"])
        return p

    def plan_cost(self, plan_id: int) -> float:
        plan = self.store.plan(plan_id)
        fs = self.features_many([t["track_id"] for t in plan["tracks"]])
        return _planner.plan_cost(fs, plan, _profiles.load(plan["profile"]))

    def parse_constraints(self, text: str, playlist_id: str, client_=None) -> dict:
        from . import constraints as _cons
        return _cons.parse(text, self.crate(playlist_id), client_=client_)

    # ------------------------------------------------------------------ learning (phase 4)
    def train(self, profile: str = "kpop", force_active: bool = False) -> dict:
        from . import learn
        return learn.train(self.store, profile, force_active)

    def import_tracklist(self, text: str, profile: str = "kpop", name: str = "", dj: str = "", url: str = "",
                         use_llm: bool = False, playlist_id: Optional[str] = None, client_=None) -> dict:
        from . import tracklist
        if playlist_id:
            library = self.crate(playlist_id)
        else:
            library = [{"id": str(c.ID), "title": c.Title, "artist": c.ArtistName}
                       for c in self.lib.db.get_content().all() if c.Title]
        return tracklist.import_tracklist(self.store, text, library, self.features, _profiles.load(profile), name=name,
                                          dj=dj, url=url, use_llm=use_llm, client_=client_)

    def clean_names(self, playlist_id: str, apply: bool = False, client_=None) -> dict:
        from . import llm
        rows = llm.clean_names(self.crate(playlist_id), client_=client_)
        if apply:
            for r in rows:
                self.store.set_name(r["id"], r["title"], r["artist"], source="llm")
        return {"tracks": rows, "applied": apply}

    def critique(self, plan_id: int, client_=None) -> str:
        from . import llm
        plan = self.store.plan(plan_id)
        lines = []
        for t in plan["tracks"]:
            i = t["info"]; o = i.get("out") or {}
            lines.append(f"{t['pos']}. {i.get('artist')} - {i.get('title')} ({i.get('bpm')} BPM, {i.get('camelot')}) "
                         f"bars {t['s']}-{t['e']}, lands {t['land']}"
                         + (f" -> {o.get('technique')} ({o.get('family')}), {o.get('note')}" if o else ""))
        return llm.critique_plan("\n".join(lines), client_=client_)

    # ------------------------------------------------------------------ export (phase 5)
    def export_plan(self, plan_id: int, fmt: str = "rekordbox_xml", path: Optional[str] = None) -> dict:
        from .exporters import EXPORTERS, ExportCue, ExportSet, ExportTrack
        if fmt not in EXPORTERS:
            raise ValueError(f"unknown format {fmt!r}; available: {sorted(EXPORTERS)}")
        plan = self.store.plan(plan_id)
        by_track: dict[str, list] = {}
        for q in plan["cues"]:
            by_track.setdefault(q["track_id"], []).append(q)
        tracks = []
        for t in plan["tracks"]:
            c = self.lib.content(t["track_id"]); f = self.features(t["track_id"])
            cues = []
            for q in sorted(by_track.get(t["track_id"], []), key=lambda q: (q["bar"], q["beat"])):
                sec = f.time_at(q["bar"], q["beat"])
                if sec is not None:
                    cues.append(ExportCue(sec, q["color"], q["comment"]))
            tracks.append(ExportTrack(id=f.id, title=t["info"].get("title") or f.title, artist=t["info"].get("artist") or f.artist,
                                      bpm=f.bpm, key=f.key, length_sec=f.length, location=c.FolderPath,
                                      first_beat_sec=float(f.times[0]), cues=cues))
        s = ExportSet(name=plan.get("name") or f"plan {plan_id}", tracks=tracks)
        fname, data = EXPORTERS[fmt](s)
        out = Path(path) if path else self.store.path.parent / "exports" / fname
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        return {"path": str(out), "bytes": len(data), "format": fmt, "warnings": s.warnings}

    # ------------------------------------------------------------------ names
    def resolve_names(self, playlist_id: str, fetch=None, overwrite: bool = False) -> dict:
        """Fetch readable titles for streamed tracks whose rekordbox title is encrypted ($A7:...).

        Uses Spotify's public oEmbed endpoint (title only; no account, no audio). Stored as display
        names in the engine store (source 'oembed'); rekordbox itself is not changed.
        """
        import urllib.parse
        import urllib.request
        from .features import ENCRYPTED_PREFIX, streaming_ref

        def _get(url: str) -> dict:
            with urllib.request.urlopen(url, timeout=10) as r:   # noqa: S310 (fixed https host)
                return json.loads(r.read().decode("utf-8"))
        fetch = fetch or _get
        have = self.store.names()
        done, failed = [], []
        for r in self.lib.playlist_tracks(str(playlist_id)):
            tid = str(r["id"])
            if not (r["title"] or "").startswith(ENCRYPTED_PREFIX) or (tid in have and not overwrite):
                continue
            ref = streaming_ref(r["file"])
            if not ref or ref[0] != "spotify":
                failed.append({"id": tid, "reason": "not a Spotify track"}); continue
            url = "https://open.spotify.com/oembed?url=" + urllib.parse.quote(f"https://open.spotify.com/track/{ref[1]}", safe="")
            try:
                title = fetch(url).get("title")
            except Exception as ex:  # network errors are reported, not raised
                failed.append({"id": tid, "reason": str(ex)[:120]}); continue
            if title:
                artist = r["artist"] if r["artist"] and not r["artist"].startswith(ENCRYPTED_PREFIX) else ""
                self.store.set_name(tid, title, artist, source="oembed")
                done.append({"id": tid, "title": title})
        return {"resolved": done, "failed": failed,
                "note": "Spotify oEmbed returns titles only; add artists with set_display_name if you want them."}
