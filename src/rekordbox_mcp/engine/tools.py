"""MCP tools for the set-planning engine (registered on the main rekordbox server).

Read/plan tools are safe while rekordbox is open. Tools that write to rekordbox
(apply_plan, stage_audition with dry_run=False) need rekordbox CLOSED and take one
PROTECTED backup per call.
"""
from __future__ import annotations

from typing import Any, Optional

from ..rbcore import public
from .service import Engine

_engine: Optional[Engine] = None


def register(mcp, lib) -> None:
    def eng() -> Engine:
        global _engine
        if _engine is None:
            _engine = Engine(lib=lib)
        lib.reopen()
        return _engine

    # ---------------------------------------------------------------- phase 1: track memory + safe planner
    @mcp.tool
    def analyze_track(track_id: str, refresh: bool = False) -> dict[str, Any]:
        """Per-beat analysis of one track from rekordbox's own data: drop/landing candidates (with 1-bar-break and off-beat warnings), grid check, chorus starts, stored landing. Cached in the engine store."""
        return public(eng().analyze_track(track_id, refresh))

    @mcp.tool
    def check_grid(track_id: str) -> dict[str, Any]:
        """Is the beat grid's downbeat where the music's sections change? Flags grids that are a beat or half a bar off and suggests a beat offset."""
        from .features import check_grid as cg
        return cg(eng().features(track_id))

    @mcp.tool
    def find_drops(track_id: str, top: int = 6) -> list[dict]:
        """Landing candidates (drops / hook starts) ranked by how hard the sound changes and the bass comes in."""
        from .features import find_drops as fd
        return fd(eng().features(track_id), top=top)

    @mcp.tool
    def set_landing(track_id: str, bar: int, verified_by: str = "ear", note: Optional[str] = None) -> dict[str, Any]:
        """Store the bar a track should land on (its hook/drop). verified_by: 'ear' (you listened), 'hint' (approximate; the planner may move it a few bars to the strongest change)."""
        eng().store.set_landing(track_id, bar, source=verified_by, note=note)
        return {"ok": True, "track_id": track_id, "bar": bar, "source": verified_by}

    @mcp.tool
    def set_exit(track_id: str, bar: Optional[int] = None, note: Optional[str] = None) -> dict[str, Any]:
        """Pin the bar a track must leave on (OUT DONE). bar=None removes the pin."""
        eng().store.set_exit_pin(track_id, bar, note)
        return {"ok": True, "track_id": track_id, "bar": bar}

    @mcp.tool
    def set_grid_fix(track_id: str, beat_offset: int, note: Optional[str] = None) -> dict[str, Any]:
        """Record that a track's real bars start `beat_offset` beats from rekordbox's grid bars (e.g. -2 when every drop lands on beat 3). Cues are then placed on the real bar."""
        eng().store.set_grid_fix(track_id, beat_offset, note)
        return {"ok": True, "track_id": track_id, "beat_offset": beat_offset}

    @mcp.tool
    def pin_transition(a_id: str, b_id: str, family: Optional[str] = None, technique: Optional[str] = None,
                       note: Optional[str] = None) -> dict[str, Any]:
        """Force how A goes into B: family 'blend' or 'cut', and/or a technique (vocalover, handoff, bassswap, filter, tease, echo, wash, roll, cut, brake, stop). Both None removes the pin."""
        eng().store.set_pair_pin(a_id, b_id, family, technique, note)
        return {"ok": True}

    @mcp.tool
    def score_transition(a_id: str, b_id: str, profile: str = "kpop", a_land: Optional[int] = None,
                         b_land: Optional[int] = None) -> dict[str, Any]:
        """Best way from A into B (exit bar, blend or cut), with QA flags: vocal stacking, bass overlap, landing on a break, weak/flat landing, key clash, tempo too wide, grid problems, plus a risk score."""
        return public(eng().score_transition(a_id, b_id, profile, a_land, b_land))

    @mcp.tool
    def plan_set(playlist_id: str, profile: str = "kpop", opener: Optional[str] = None, closer: Optional[str] = None,
                 pins: Optional[dict] = None, name: Optional[str] = None) -> dict[str, Any]:
        """Plan every landing, exit, technique and cue for a playlist in its current order, using stored landings/pins. Saves the plan and returns plan_id, summary, transitions. Read-only for rekordbox."""
        p = eng().plan_set(playlist_id=playlist_id, profile=profile, opener=opener, closer=closer, pins=pins, name=name)
        return public({"plan_id": p["plan_id"], "summary": p["summary"], "warnings": p["warnings"],
                       "transitions": [{k: r[k] for k in ("a", "b", "family", "technique", "out_done", "b_land", "note")}
                                       for r in p["transitions"]]})

    @mcp.tool
    def list_plans(playlist_id: Optional[str] = None) -> list[dict]:
        """Saved plans, newest first."""
        return eng().store.plans(playlist_id)

    @mcp.tool
    def diff_plan(plan_id: int) -> dict[str, Any]:
        """What apply_plan would change: cues to add, engine-owned cues to delete, matching cues to adopt, and the foreign (hand-set) cues it will leave alone."""
        d = eng().diff_plan(plan_id)
        return {"counts": d["counts"], "add": d["add"][:40], "delete": d["delete"][:40], "foreign": d["foreign"][:40],
                "uncoloured_foreign": d["uncoloured_foreign"]}

    @mcp.tool
    def apply_plan(plan_id: int, dry_run: bool = True) -> dict[str, Any]:
        """Write a plan's memory cues to rekordbox. Only cues in the engine's ledger are ever deleted; hand-set cues are never touched. dry_run=False needs rekordbox CLOSED; takes one PROTECTED backup, writes in one batch, then verifies."""
        d = eng().apply_plan(plan_id, dry_run=dry_run)
        if dry_run:
            return {"dry_run": True, "counts": d["counts"]}
        return d

    @mcp.tool
    def render_run_sheet(plan_id: int, path: Optional[str] = None, title: Optional[str] = None) -> dict[str, Any]:
        """Write a self-contained HTML run sheet for a plan (BPM arc, technique legend, a card per transition with cue bars/times, QA flags and Works/Doesn't rating buttons)."""
        return eng().render_run_sheet(plan_id, path, title)

    @mcp.tool
    def import_seed(path: str, playlist_id: Optional[str] = None, adopt: bool = True) -> dict[str, Any]:
        """Load a seed JSON (names, landings, pins, a reference plan) into the engine store and adopt matching library cues into the ledger. No rekordbox writes."""
        return eng().import_seed(path, playlist_id, adopt)

    @mcp.tool
    def resolve_names(playlist_id: str, overwrite: bool = False) -> dict[str, Any]:
        """Readable titles for streamed tracks rekordbox stores encrypted ($A7:...), via Spotify's public oEmbed. Saved as display names in the engine store; rekordbox is not changed."""
        return eng().resolve_names(playlist_id, overwrite=overwrite)

    @mcp.tool
    def set_display_name(track_id: str, title: str, artist: str = "") -> dict[str, Any]:
        """Set the name the engine uses for a track in cue comments, run sheets and the app (rekordbox is not changed)."""
        eng().store.set_name(track_id, title, artist, source="manual")
        return {"ok": True}

    # ---------------------------------------------------------------- phase 2: audition
    @mcp.tool
    def audition_queue(plan_id: int, limit: Optional[int] = None) -> dict[str, Any]:
        """Untested transitions of a plan, riskiest first (QA flags + tempo gap)."""
        return eng().audition_queue(plan_id, limit)

    @mcp.tool
    def stage_audition(plan_id: int, pairs: Optional[list] = None, limit: int = 10, playlist_name: str = "Audition",
                       dry_run: bool = True) -> dict[str, Any]:
        """Set up pairs to audition on the decks: writes their plan cues (ledger-safe) and fills an 'Audition' playlist A1,B1,A2,B2...; returns deck steps per pair. pairs: transition numbers or [a_id, b_id]; default = riskiest `limit`. dry_run=False needs rekordbox CLOSED."""
        return public(eng().stage_audition(plan_id, pairs, limit, playlist_name, dry_run))

    @mcp.tool
    def collect_ratings(plan_id: int) -> dict[str, Any]:
        """Read your verdicts from rekordbox: a GREEN hot cue near A's OUT DONE = works, RED = doesn't. Stores them as ratings."""
        return eng().collect_ratings(plan_id)

    @mcp.tool
    def import_ratings(payload: str) -> dict[str, Any]:
        """Import the JSON copied from the run sheet's 'Copy ratings' button."""
        return eng().import_ratings(payload)

    # ---------------------------------------------------------------- phase 3: optimizer
    @mcp.tool
    def optimize_set(playlist_id: str, constraints: Optional[dict] = None, profile: Optional[str] = None,
                     keep_order: bool = False, free_landings: bool = False, iters: Optional[int] = None,
                     name: Optional[str] = None) -> dict[str, Any]:
        """Optimise a crate into a set: order (simulated annealing with pins and an energy curve) then every landing/exit (exact DP). constraints follows the parse_constraints schema (opener, closer, fixed, adjacent, exclude, include, size/minutes, energy_curve, bpm_curve, prefer). keep_order=True only optimises the points. Saves a plan."""
        p = eng().optimize_set(playlist_id=playlist_id, constraints=constraints, profile=profile, keep_order=keep_order,
                               free_landings=free_landings, iters=iters, name=name)
        return public({"plan_id": p.get("plan_id"), "summary": p["summary"], "optimizer": p["optimizer"],
                       "order": [t["info"]["title"] for t in p["tracks"]], "warnings": p["warnings"]})

    @mcp.tool
    def parse_constraints(text: str, playlist_id: str) -> dict[str, Any]:
        """Turn a plain-English request ("open on X, peak at 160 in the last third, no two PinkPantheress back to back") into the constraints JSON for optimize_set. Uses the Sonnet tier; needs ANTHROPIC_API_KEY."""
        return eng().parse_constraints(text, playlist_id)

    @mcp.tool
    def list_profiles() -> dict[str, Any]:
        """Genre profiles available to plan_set / optimize_set."""
        from . import profiles
        return {p: profiles.load(p)["description"] for p in profiles.available()}

    # ---------------------------------------------------------------- phase 4: learning
    @mcp.tool
    def train_model(profile: str = "kpop", force_active: bool = False) -> dict[str, Any]:
        """Fit transition weights from your ratings (+ weak tracklist labels). The model only steers planning once it has 100+ ratings and beats the hand score on held-out data."""
        return eng().train(profile, force_active)

    @mcp.tool
    def import_tracklist(text: str, profile: str = "kpop", name: str = "", dj: str = "", url: str = "",
                         use_llm: bool = False, playlist_id: Optional[str] = None) -> dict[str, Any]:
        """Learn from a pro mix's tracklist with start times (one 'mm:ss Artist - Title' per line): matches lines to your library, estimates play lengths and exits from the grid, stores weak training labels. use_llm=True asks the Haiku tier to match leftovers."""
        return eng().import_tracklist(text, profile, name, dj, url, use_llm, playlist_id)

    @mcp.tool
    def critique_plan(plan_id: int) -> str:
        """An Opus-tier DJ critique of a saved plan (needs ANTHROPIC_API_KEY)."""
        return eng().critique(plan_id)

    # ---------------------------------------------------------------- phase 5: export
    @mcp.tool
    def export_plan(plan_id: int, fmt: str = "rekordbox_xml", path: Optional[str] = None) -> dict[str, Any]:
        """Export a plan: rekordbox_xml (local files + memory cues), m3u8, serato_crate (order only), json."""
        return eng().export_plan(plan_id, fmt, path)
