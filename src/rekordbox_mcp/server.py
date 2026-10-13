"""rekordbox MCP server: read the library + analysis, write hot/memory cues and playlists.

Run:  python server.py            (stdio transport, for Claude Code / Desktop)
Env:  REKORDBOX_DB_PATH   override master.db location (default: rekordbox's own)
      REKORDBOX_BACKUP_DIR where pre-write backups go (default: ./backups)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

# pyrekordbox locates rekordbox via these Windows vars; MCP hosts sometimes spawn us with a
# stripped environment, so reconstruct sane defaults from where this file lives
# (falls back to the current user's home folder).
_HOME = Path(os.environ.get("USERPROFILE") or Path.home())
os.environ.setdefault("USERPROFILE", str(_HOME))
os.environ.setdefault("APPDATA", str(_HOME / "AppData" / "Roaming"))
os.environ.setdefault("LOCALAPPDATA", str(_HOME / "AppData" / "Local"))
os.environ.setdefault("ProgramFiles", r"C:\Program Files")
os.environ.setdefault("PROGRAMFILES", os.environ["ProgramFiles"])

from fastmcp import FastMCP

from . import barmap
from . import rbcore
from .rbcore import Library, public

if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

mcp = FastMCP(
    "rekordbox",
    instructions=(
        "Tools for a rekordbox 7 DJ library. Reads are safe anytime. Writes (set_hot_cue, "
        "set_memory_cue, delete_cue, playlist edits) require rekordbox to be CLOSED and take a "
        "backup of master.db first. Positions are seconds from track start; bars are 1-based from "
        "the beat grid's first downbeat. Hot cue slots are A-P (rekordbox shows A-H on the first "
        "pad page)."
    ),
)
lib = Library()


def _fresh() -> Library:
    """Reads reopen the DB so we never serve a stale snapshot while rekordbox edits."""
    lib.reopen()
    return lib


# ----------------------------------------------------------------------------- reads
@mcp.tool
def library_stats() -> dict[str, Any]:
    """Overview of the rekordbox library: track/playlist/cue counts, BPM range, top genres, whether rekordbox is running."""
    return _fresh().stats()


@mcp.tool
def search_tracks(query: str = "", bpm_min: Optional[float] = None, bpm_max: Optional[float] = None,
                  key: Optional[str] = None, genre: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Search tracks by free text (title/artist/album/comment) with optional BPM range, rekordbox key name (e.g. 'Am', 'F#m', 'Bb') and genre. Returns id, bpm, key, camelot code, length, cue counts."""
    return _fresh().search(query, bpm_min, bpm_max, key, genre, limit)


@mcp.tool
def get_track(track_id: str) -> dict[str, Any]:
    """Full detail for one track: metadata, all hot/memory cues with bar positions, mood, phrase structure (intro/verse/chorus/outro with start times, bars and energy) and beat-grid summary."""
    l = _fresh()
    c = l.content(track_id)
    a = l.analysis(c)
    d = l.track_summary(c)
    d["cues"] = l.cues(c, a["grid"])
    d["mood"] = a["mood"]
    d["bank"] = a["bank"]
    d["energy_overall"] = a.get("energy_overall")
    d["grid"] = public(a["grid"]) if a["grid"] else None
    d["phrases"] = a["phrases"]
    d["warnings"] = a["warnings"]
    return public(d)


@mcp.tool
def get_beat_grid(track_id: str, max_bars: int = 400) -> dict[str, Any]:
    """Downbeat times (seconds) for every bar of a track, plus BPM per segment if the tempo changes. Use to place cues on exact bars."""
    l = _fresh()
    c = l.content(track_id)
    a = l.analysis(c)
    g = a["grid"]
    if not g:
        return {"error": "no beat grid", "warnings": a["warnings"]}
    times = g["_times"]; db = g["_downbeats"]
    bars = [{"bar": i + 1, "sec": round(float(times[j]), 3)} for i, j in enumerate(db[:max_bars])]
    return {"track": f"{c.ArtistName} - {c.Title}", "bpm": (c.BPM or 0) / 100, "bars": g["bars"],
            "steady_tempo": g["steady_tempo"], "downbeats": bars}


@mcp.tool
def list_playlists() -> list[dict]:
    """All playlists and folders with ids, types and track counts."""
    return _fresh().playlists()


@mcp.tool
def get_playlist_tracks(playlist_id: str) -> list[dict]:
    """Tracks in a playlist in order, with bpm/key/camelot so you can check the flow."""
    return _fresh().playlist_tracks(playlist_id)


@mcp.tool
def suggest_next_tracks(track_id: str, bpm_tolerance: float = 6.0, limit: int = 15,
                        allow_half_double_time: bool = True, playlist_id: Optional[str] = None) -> dict[str, Any]:
    """Rank library tracks that mix well after the given track: Camelot key compatibility, BPM distance (incl. half/double time), energy and phrase info. Optionally restrict candidates to one playlist."""
    l = _fresh()
    src = l.content(track_id)
    src_key = src.Key.ScaleName if src.Key else None
    src_bpm = (src.BPM or 0) / 100
    src_a = l.analysis(src)
    if playlist_id:
        cands = [r.Content for r in l.db.get_playlist_songs(PlaylistID=str(playlist_id)).all() if r.Content]
    else:
        cands = [c for c in l.db.get_content().all() if c.BPM and not (c.FolderPath or "").find("/rekordbox/Sampler/") >= 0]
    out = []
    for c in cands:
        if c.ID == src.ID or not c.BPM:
            continue
        bpm = c.BPM / 100
        ratio_note = ""
        diff = abs(bpm - src_bpm)
        if allow_half_double_time:
            for r, note in ((2.0, "double time"), (0.5, "half time")):
                d2 = abs(bpm * r - src_bpm)
                if d2 < diff:
                    diff, ratio_note = d2, note
        if diff > bpm_tolerance:
            continue
        kscore, kreason = rbcore.key_compatibility(src_key, c.Key.ScaleName if c.Key else None)
        score = kscore * 10 - diff * 1.5
        out.append({
            "id": c.ID, "title": c.Title, "artist": c.ArtistName, "bpm": bpm, "bpm_diff": round(diff, 2),
            "tempo_note": ratio_note or None, "key": c.Key.ScaleName if c.Key else None,
            "camelot": rbcore.camelot(c.Key.ScaleName if c.Key else None), "key_match": kreason,
            "score": round(score, 1), "hot_cues": len([q for q in c.Cues if q.Kind not in (0, 4)]),
        })
    out.sort(key=lambda x: -x["score"])
    out = out[:limit]
    # enrich the top picks with their intro phrase so a mix-in point is obvious
    for d in out:
        a = l.analysis(l.content(d["id"]))
        intro = next((p for p in a["phrases"] if p["name"].startswith("Intro")), None)
        first_chorus = next((p for p in a["phrases"] if "Chorus" in p["name"]), None)
        d["mood"] = a["mood"]
        d["intro"] = {"start": intro["start"], "bars": intro["bars"]} if intro else None
        d["first_chorus_at"] = first_chorus["start"] if first_chorus else None
        d["energy"] = a.get("energy_overall")
    outro = next((p for p in reversed(src_a["phrases"]) if "Outro" in p["name"]), None)
    return {
        "source": {"title": src.Title, "artist": src.ArtistName, "bpm": src_bpm, "key": src_key,
                   "camelot": rbcore.camelot(src_key), "mood": src_a["mood"],
                   "outro": {"start": outro["start"], "bars": outro["bars"]} if outro else None,
                   "length": src.Length},
        "candidates": out,
    }


@mcp.tool
def get_bar_map(track_id: str, include_bars: bool = True) -> dict[str, Any]:
    """Bar-by-bar map from rekordbox's own analysis: bass/mid/high level (0-100, relative to the track's loudest) and vocal % (rekordbox's vocal detection) per bar, plus sections grouped by bass on/off, vocal on/off and phrase. Use to find no-bass intros/outros, breakdowns, vocal-free windows and where the bass drops back in."""
    l = _fresh()
    c = l.content(track_id)
    m = barmap.bar_map(l, c)
    out = {"track": f"{c.ArtistName} - {c.Title}", "bpm": (c.BPM or 0) / 100,
           "sections": m["sections"], "warnings": m["warnings"]}
    if include_bars:
        out["columns"] = ["bar", "sec", "phrase", "low", "mid", "high", "vocal"]
        out["bars"] = [[r[k] for k in out["columns"]] for r in m["bars"]]
    return public(out)


@mcp.tool
def plan_transition(from_track_id: str, to_track_id: str, overlap_bars: Optional[int] = None,
                    top: int = 3) -> dict[str, Any]:
    """Plan the mix from track A into track B using bar maps (bass, vocals, phrases), tempo gap and key. Within ~6% tempo it plans a beatmatched blend (A window, B in-point, bass-swap bar, cut, where B lands); wider gaps get an echo-out/cut on a phrase. Returns ranked plans with the reasons and ready-to-write memory cues (green IN/NEXT, red SWAP/CUT). Read-only: write the cues with set_memory_cue."""
    return public(barmap.plan_transition(_fresh(), from_track_id, to_track_id, overlap_bars, top))


@mcp.tool
def hot_cue_colors() -> dict[str, Any]:
    """The colour names accepted by set_hot_cue / set_memory_cue."""
    return {"hot_cue": list(rbcore.HOTCUE_COLORS), "memory_cue": rbcore.MEMORY_COLORS,
            "note": "hot cues also accept #rrggbb (nearest of rekordbox's 64-colour table is used)"}


# ----------------------------------------------------------------------------- writes
@mcp.tool
def set_hot_cue(track_id: str, slot: str, position_sec: Optional[float] = None, bar: Optional[float] = None,
                beat: int = 1, snap: Optional[str] = None, comment: Optional[str] = None,
                color: Optional[str] = None, loop_bars: Optional[float] = None,
                replace: bool = True) -> dict[str, Any]:
    """Set a hot cue (slot A-P) on a track. Give either position_sec or bar (+beat 1-4). snap='bar'|'beat' aligns to the grid. loop_bars makes it a loop hot cue. Requires rekordbox to be closed; backs up master.db first. Replaces an existing cue in that slot unless replace=False."""
    return public(lib.set_cue(track_id, slot=slot, position_sec=position_sec, bar=bar, beat=beat, snap=snap,
                              comment=comment, color=color, loop_bars=loop_bars, replace=replace))


@mcp.tool
def set_memory_cue(track_id: str, position_sec: Optional[float] = None, bar: Optional[float] = None,
                   beat: int = 1, snap: Optional[str] = None, comment: Optional[str] = None,
                   color: Optional[str] = None, loop_bars: Optional[float] = None) -> dict[str, Any]:
    """Add a memory cue (or memory loop with loop_bars) to a track. Requires rekordbox to be closed; backs up master.db first."""
    return public(lib.set_cue(track_id, position_sec=position_sec, bar=bar, beat=beat, snap=snap,
                              comment=comment, color=color, loop_bars=loop_bars, memory=True))


@mcp.tool
def delete_cue(track_id: str, slot: Optional[str] = None, cue_id: Optional[str] = None) -> dict[str, Any]:
    """Delete a cue by hot cue slot (A-P), 'MEMORY' (all memory cues) or a specific cue_id from get_track. Requires rekordbox to be closed."""
    return public(lib.delete_cue(track_id, slot=slot, cue_id=cue_id))


@mcp.tool
def create_playlist(name: str, parent_id: Optional[str] = None) -> dict[str, Any]:
    """Create an empty playlist (optionally inside a folder). Requires rekordbox to be closed."""
    return lib.create_playlist(name, parent_id)


@mcp.tool
def add_tracks_to_playlist(playlist_id: str, track_ids: list[str]) -> dict[str, Any]:
    """Append tracks to a playlist in the given order. Requires rekordbox to be closed."""
    return lib.add_to_playlist(playlist_id, track_ids)


@mcp.tool
def reorder_playlist(playlist_id: str, track_ids: list[str]) -> dict[str, Any]:
    """Reorder an EXISTING playlist in place: track_ids is the complete desired running order (content ids). Tracks not listed are kept after them. Requires rekordbox to be closed; backs up master.db first."""
    return lib.reorder_playlist(playlist_id, track_ids)


@mcp.tool
def remove_tracks_from_playlist(playlist_id: str, track_ids: list[str]) -> dict[str, Any]:
    """Remove tracks from a playlist. Requires rekordbox to be closed."""
    return lib.remove_from_playlist(playlist_id, track_ids)


from .engine import tools as _engine_tools  # noqa: E402  (registers the engine tools)

_engine_tools.register(mcp, lib)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
