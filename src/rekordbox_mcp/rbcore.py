"""Core library: read rekordbox 7 library + analysis files, write hot/memory cues.

Facts this module relies on (verified against rekordbox 7.2.18, Sep 2026):
- master.db is SQLCipher; pyrekordbox opens it (key bundled / auto-extracted).
- Cues live ONLY in the DB (djmdCue).  ANLZ PCOB/PCO2 tags are empty until USB export.
- djmdCue.Kind: 0 = memory cue, 4 = memory loop, hot cues use 1,2,3,5,6,...,17
  (slot index 1..16 with the value 4 skipped).  A=1 B=2 C=3 D=5 ... P=17.
- InFrame = InMsec * 150 / 1000.  InMpeg* fields are 0 for every row rekordbox 7 wrote.
- contentCue table mirrors every track's cue list as JSON (ID = content UUID);
  rekordbox keeps its count identical to djmdCue, so writes must update it too.
- djmdContent.CueUpdated is a string counter bumped on cue edits.
- rekordbox's own cue rows have usn / rb_local_usn = NULL.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Optional

import numpy as np
from pyrekordbox import Rekordbox6Database
from pyrekordbox.anlz import AnlzFile
from pyrekordbox.db6 import tables
import pyrekordbox.anlz.structs as _anlz_structs
from construct import Const as _Const, Int32ub as _Int32ub
from pyrekordbox.utils import get_rekordbox_pid
from sqlalchemy import func, or_

# pyrekordbox pins PQT2.u1 to 0x01000002 (one tempo segment).  Tracks with a
# dynamic beat grid carry 0x02000002, which made the whole .EXT parse fail and
# silently dropped the phrase (PSSI) data for those tracks.  Relax the constant.
def _relax_pqt2() -> None:
    pqt2 = _anlz_structs.PQT2
    for i, sub in enumerate(pqt2.subcons):
        inner = getattr(sub, "subcon", sub)
        if getattr(sub, "name", None) == "u1" and isinstance(inner, _Const):
            pqt2.subcons[i] = "u1" / _Int32ub
            break
_relax_pqt2()

# --------------------------------------------------------------------------- consts
HOTCUE_SLOTS = "ABCDEFGHIJKLMNOP"
KIND_MEMORY = 0
KIND_MEMORY_LOOP = 4

# rekordbox's 64-entry hot cue colour table (index -> RGB).  Source: CueGen ColorTable.
_COLOR_TABLE_RGB = [
    (0x28, 0xE2, 0x14), (0x30, 0x5A, 0xFF), (0x50, 0x73, 0xFF), (0x50, 0x8C, 0xFF),
    (0x50, 0xA0, 0xFF), (0x50, 0xB4, 0xFF), (0x50, 0xB0, 0xF2), (0x50, 0xAE, 0xE8),
    (0x45, 0xAC, 0xDB), (0x00, 0xE0, 0xFF), (0x19, 0xDA, 0xF0), (0x32, 0xD2, 0xE6),
    (0x21, 0xB4, 0xB9), (0x20, 0xAA, 0xA0), (0x1F, 0xA3, 0x92), (0x19, 0xA0, 0x8C),
    (0x14, 0xA5, 0x84), (0x14, 0xAA, 0x7D), (0x10, 0xB1, 0x76), (0x30, 0xD2, 0x6E),
    (0x37, 0xDE, 0x5A), (0x3C, 0xEB, 0x50), (0x28, 0xE2, 0x14), (0x7D, 0xC1, 0x3D),
    (0x8C, 0xC8, 0x32), (0x9B, 0xD7, 0x23), (0xA5, 0xE1, 0x16), (0xA5, 0xDC, 0x0A),
    (0xAA, 0xD2, 0x08), (0xB4, 0xC8, 0x05), (0xB4, 0xBE, 0x04), (0xBA, 0xB4, 0x04),
    (0xC3, 0xAF, 0x04), (0xE1, 0xAA, 0x00), (0xFF, 0xA0, 0x00), (0xFF, 0x96, 0x00),
    (0xFF, 0x8C, 0x00), (0xFF, 0x75, 0x00), (0xE0, 0x64, 0x1B), (0xE0, 0x46, 0x1E),
    (0xE0, 0x30, 0x1E), (0xE0, 0x28, 0x23), (0xE6, 0x28, 0x28), (0xFF, 0x37, 0x6F),
    (0xFF, 0x2D, 0x6F), (0xFF, 0x12, 0x7B), (0xF5, 0x1E, 0x8C), (0xEB, 0x2D, 0xA0),
    (0xE6, 0x37, 0xB4), (0xDE, 0x44, 0xCF), (0xDE, 0x44, 0x8D), (0xE6, 0x30, 0xB4),
    (0xE6, 0x19, 0xDC), (0xE6, 0x00, 0xFF), (0xDC, 0x00, 0xFF), (0xCC, 0x00, 0xFF),
    (0xB4, 0x32, 0xFF), (0xB9, 0x3C, 0xFF), (0xC5, 0x42, 0xFF), (0xAA, 0x5A, 0xFF),
    (0xAA, 0x72, 0xFF), (0x82, 0x72, 0xFF), (0x64, 0x73, 0xFF),
]
# The 16 colours rekordbox surfaces in its hot cue colour picker (name -> table index).
HOTCUE_COLORS: dict[str, int] = {
    "magenta": 49, "purple": 56, "violet": 60, "indigo": 62,
    "blue": 1, "sky": 5, "cyan": 9, "teal": 14,
    "seagreen": 18, "green": 22, "lime": 26, "olive": 30,
    "gold": 32, "orange": 38, "red": 42, "pink": 45,
}
# Memory cue colours (djmdCue.Color 0..7 when Kind == 0).
MEMORY_COLORS = ["pink", "red", "orange", "yellow", "green", "aqua", "blue", "purple"]

# Phrase names per mood (deep-symmetry PSSI analysis).
_PHRASE_NAMES = {
    1: {1: "Intro", 2: "Up", 3: "Down", 5: "Chorus", 6: "Outro"},                      # high
    2: {1: "Intro", 2: "Verse 1", 3: "Verse 2", 4: "Verse 3", 5: "Verse 4",
        6: "Verse 5", 7: "Verse 6", 8: "Bridge", 9: "Chorus", 10: "Outro"},           # mid
    3: {1: "Intro", 2: "Verse 1", 3: "Verse 1", 4: "Verse 1", 5: "Verse 2",
        6: "Verse 2", 7: "Verse 2", 8: "Bridge", 9: "Chorus", 10: "Outro"},           # low
}
_MOOD_NAMES = {1: "high", 2: "mid", 3: "low"}
_BANK_NAMES = {0: "default", 1: "cool", 2: "natural", 3: "hot", 4: "subtle",
               5: "warm", 6: "vivid", 7: "club1", 8: "club2"}

# Camelot wheel
_CAMELOT = {
    # major
    "C": "8B", "Db": "3B", "C#": "3B", "D": "10B", "Eb": "5B", "D#": "5B", "E": "12B",
    "F": "7B", "F#": "2B", "Gb": "2B", "G": "9B", "Ab": "4B", "G#": "4B", "A": "11B",
    "Bb": "6B", "A#": "6B", "B": "1B",
    # minor
    "Am": "8A", "Bbm": "3A", "A#m": "3A", "Bm": "10A", "Cm": "5A", "C#m": "12A", "Dbm": "12A",
    "Dm": "7A", "Ebm": "2A", "D#m": "2A", "Em": "9A", "Fm": "4A", "F#m": "11A", "Gbm": "11A",
    "Gm": "6A", "G#m": "1A", "Abm": "1A",
}


def camelot(key_name: Optional[str]) -> Optional[str]:
    if not key_name:
        return None
    return _CAMELOT.get(key_name.strip())


def key_compatibility(a: Optional[str], b: Optional[str]) -> tuple[int, str]:
    """Return (score 0..3, reason) for mixing key a into key b (Camelot rules)."""
    ca, cb = camelot(a), camelot(b)
    if not ca or not cb:
        return 0, "unknown key"
    na, la = int(ca[:-1]), ca[-1]
    nb, lb = int(cb[:-1]), cb[-1]
    if ca == cb:
        return 3, "same key"
    diff = (nb - na) % 12
    if la == lb and diff in (1, 11):
        return 3, "adjacent on wheel (+1/-1)"
    if la != lb and diff == 0:
        return 3, "relative major/minor"
    if la == lb and diff == 2:
        return 2, "+2 energy boost"
    if la != lb and diff in (1, 11):
        return 1, "diagonal (+1 with mode change)"
    if la == lb and diff == 7:
        return 1, "perfect fifth-ish (+7), use with care"
    return 0, "clash"


def slot_to_kind(slot: str | int) -> int:
    """Hot cue slot ('A'..'P' or 1..16) -> djmdCue.Kind."""
    if isinstance(slot, str):
        s = slot.strip().upper()
        if s.isdigit():
            idx = int(s)
        else:
            if len(s) != 1 or s not in HOTCUE_SLOTS:
                raise ValueError(f"hot cue slot must be A-P or 1-16, got {slot!r}")
            idx = HOTCUE_SLOTS.index(s) + 1
    else:
        idx = int(slot)
    if not 1 <= idx <= 16:
        raise ValueError(f"hot cue slot must be A-P or 1-16, got {slot!r}")
    return idx if idx < 4 else idx + 1


def kind_to_slot(kind: int) -> str:
    if kind == KIND_MEMORY:
        return "MEMORY"
    if kind == KIND_MEMORY_LOOP:
        return "MEMORY_LOOP"
    idx = kind if kind < 4 else kind - 1
    if 1 <= idx <= 16:
        return HOTCUE_SLOTS[idx - 1]
    return f"KIND{kind}"


def _color_name_for_cue(cue: tables.DjmdCue) -> Optional[str]:
    if cue.Kind == KIND_MEMORY or cue.Kind == KIND_MEMORY_LOOP:
        if cue.Color is not None and 0 <= cue.Color < 8:
            return MEMORY_COLORS[cue.Color]
        return None
    idx = cue.ColorTableIndex
    if idx is None or idx == 0:
        return "default"
    for name, i in HOTCUE_COLORS.items():
        if i == idx:
            return name
    if 0 <= idx < len(_COLOR_TABLE_RGB):
        r, g, b = _COLOR_TABLE_RGB[idx]
        return f"#{r:02x}{g:02x}{b:02x}"
    return None


def _fmt_time(sec: float) -> str:
    m, s = divmod(max(sec, 0.0), 60)
    return f"{int(m)}:{s:05.2f}"


# --------------------------------------------------------------------------- DB wrapper
class Library:
    """Thin wrapper around pyrekordbox with read helpers and safe cue writes."""

    def __init__(self, db_path: Optional[str] = None, backup_dir: Optional[str] = None):
        self.db_path = db_path or os.environ.get("REKORDBOX_DB_PATH") or None
        self.backup_dir = Path(backup_dir or os.environ.get("REKORDBOX_BACKUP_DIR")
                               or Path(__file__).parent / "backups")
        self._db: Optional[Rekordbox6Database] = None

    # -- lifecycle
    def open(self) -> Rekordbox6Database:
        if self._db is None:
            self._db = Rekordbox6Database(path=self.db_path) if self.db_path else Rekordbox6Database()
        return self._db

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None

    def reopen(self) -> Rekordbox6Database:
        self.close()
        return self.open()

    @property
    def db(self) -> Rekordbox6Database:
        return self.open()

    # -- generic helpers
    def rekordbox_running(self) -> bool:
        return bool(get_rekordbox_pid())

    def content(self, track_id: str | int) -> tables.DjmdContent:
        c = self.db.get_content(ID=str(track_id))
        if c is None:
            raise ValueError(f"no track with ID {track_id}")
        return c

    def track_summary(self, c: tables.DjmdContent) -> dict[str, Any]:
        key = c.Key.ScaleName if c.Key else None
        return {
            "id": c.ID,
            "title": c.Title,
            "artist": c.ArtistName,
            "album": c.AlbumName,
            "genre": c.GenreName,
            "bpm": (c.BPM or 0) / 100.0,
            "key": key,
            "camelot": camelot(key),
            "length_sec": c.Length,
            "length": _fmt_time(c.Length or 0),
            "rating": c.Rating,
            "color": c.Color.Commnt if c.Color else None,
            "play_count": c.DJPlayCount,
            "date_added": str(c.StockDate) if c.StockDate else None,
            "comment": c.Commnt,
            "analysed": bool(c.Analysed),
            "file": c.FolderPath,
            "hot_cues": sum(1 for q in c.Cues if q.Kind not in (KIND_MEMORY, KIND_MEMORY_LOOP)),
            "memory_cues": sum(1 for q in c.Cues if q.Kind in (KIND_MEMORY, KIND_MEMORY_LOOP)),
        }

    # -- reads
    def stats(self) -> dict[str, Any]:
        db = self.db
        n_tracks = db.get_content().count()
        genres = db.session.query(tables.DjmdGenre.Name, func.count(tables.DjmdContent.ID)) \
            .join(tables.DjmdContent, tables.DjmdContent.GenreID == tables.DjmdGenre.ID) \
            .group_by(tables.DjmdGenre.Name).order_by(func.count(tables.DjmdContent.ID).desc()).limit(15).all()
        bpm = db.session.query(func.avg(tables.DjmdContent.BPM), func.min(tables.DjmdContent.BPM),
                               func.max(tables.DjmdContent.BPM)).filter(tables.DjmdContent.BPM > 0).first()
        return {
            "tracks": n_tracks,
            "playlists": db.get_playlist().count(),
            "cues": db.get_cue().count(),
            "bpm_avg": round((bpm[0] or 0) / 100, 1),
            "bpm_min": (bpm[1] or 0) / 100, "bpm_max": (bpm[2] or 0) / 100,
            "top_genres": [{"genre": g, "tracks": n} for g, n in genres],
            "rekordbox_running": self.rekordbox_running(),
            "db_path": str(db.db_directory / "master.db"),
        }

    def search(self, query: str = "", bpm_min: Optional[float] = None, bpm_max: Optional[float] = None,
               key: Optional[str] = None, genre: Optional[str] = None, limit: int = 50) -> list[dict]:
        db = self.db
        C = tables.DjmdContent
        q = db.session.query(C)
        if query:
            like = f"%{query}%"
            q = q.outerjoin(tables.DjmdArtist, C.ArtistID == tables.DjmdArtist.ID) \
                 .outerjoin(tables.DjmdAlbum, C.AlbumID == tables.DjmdAlbum.ID) \
                 .filter(or_(C.Title.ilike(like), tables.DjmdArtist.Name.ilike(like),
                             tables.DjmdAlbum.Name.ilike(like), C.SearchStr.ilike(like),
                             C.Commnt.ilike(like)))
        if bpm_min is not None:
            q = q.filter(C.BPM >= int(bpm_min * 100))
        if bpm_max is not None:
            q = q.filter(C.BPM <= int(bpm_max * 100))
        if key:
            q = q.join(tables.DjmdKey, C.KeyID == tables.DjmdKey.ID).filter(tables.DjmdKey.ScaleName == key)
        if genre:
            q = q.join(tables.DjmdGenre, C.GenreID == tables.DjmdGenre.ID).filter(tables.DjmdGenre.Name.ilike(f"%{genre}%"))
        q = q.filter(~C.FolderPath.like("%/rekordbox/Sampler/%"))
        return [self.track_summary(c) for c in q.order_by(C.Title).limit(limit)]

    def _live_songs(self, playlist_id: str, **kw):
        """Playlist rows rekordbox still shows: it soft-deletes removed tracks
        (rb_local_deleted=1) instead of dropping the row."""
        return (self.db.get_playlist_songs(PlaylistID=str(playlist_id), **kw)
                .filter(tables.DjmdSongPlaylist.rb_local_deleted == 0))

    def playlists(self) -> list[dict]:
        out = []
        for p in self.db.get_playlist().order_by(tables.DjmdPlaylist.ParentID, tables.DjmdPlaylist.Seq):
            kind = {0: "playlist", 1: "folder", 4: "smart"}.get(p.Attribute, str(p.Attribute))
            n = self._live_songs(p.ID).count() if p.Attribute == 0 else None
            out.append({"id": p.ID, "name": p.Name, "type": kind, "parent_id": p.ParentID, "tracks": n})
        return out

    def playlist_tracks(self, playlist_id: str) -> list[dict]:
        db = self.db
        rows = self._live_songs(playlist_id).order_by(tables.DjmdSongPlaylist.TrackNo).all()
        out = []
        for r in rows:
            c = r.Content
            if c is None:
                continue
            d = self.track_summary(c)
            d["track_no"] = r.TrackNo
            d["song_id"] = r.ID
            out.append(d)
        return out

    # -- cues
    def cues(self, c: tables.DjmdContent, grid: Optional[dict] = None) -> list[dict]:
        out = []
        for q in sorted(c.Cues, key=lambda x: (x.Kind, x.InMsec)):
            d = {
                "cue_id": q.ID,
                "slot": kind_to_slot(q.Kind),
                "kind": q.Kind,
                "type": "memory" if q.Kind in (KIND_MEMORY, KIND_MEMORY_LOOP) else "hot",
                "position_sec": q.InMsec / 1000.0,
                "position": _fmt_time(q.InMsec / 1000.0),
                "loop_end_sec": q.OutMsec / 1000.0 if q.OutMsec and q.OutMsec > 0 else None,
                "comment": q.Comment,
                "color": _color_name_for_cue(q),
                "created": str(q.created_at)[:19],
            }
            if grid:
                d["bar"] = self.bar_at(grid, q.InMsec / 1000.0)
            out.append(d)
        return out

    # -- analysis
    def _share_dir(self) -> Path:
        env = os.environ.get("REKORDBOX_SHARE_DIR")
        if env:
            return Path(env)
        share = Path(self.db.share_directory)
        if share.exists():
            return share
        # DB was opened from a copy: fall back to rekordbox's real share folder
        from pyrekordbox.config import get_config
        for ver in ("rekordbox7", "rekordbox6"):
            try:
                cfg = get_config(ver)
            except Exception:
                continue
            if cfg and cfg.get("db_dir"):
                return Path(cfg["db_dir"]) / "share"
        return share

    def _anlz_dir(self, c: tables.DjmdContent) -> Optional[Path]:
        if not c.AnalysisDataPath:
            return None
        parts = [p for p in c.AnalysisDataPath.replace("\\", "/").split("/") if p]
        return self._share_dir().joinpath(*parts[:-1])

    def analysis(self, c: tables.DjmdContent) -> dict[str, Any]:
        """Parse ANLZ .DAT/.EXT for beat grid, phrases, waveform energy."""
        db = self.db
        result: dict[str, Any] = {"grid": None, "phrases": [], "mood": None, "bank": None, "warnings": []}
        d = self._anlz_dir(c)
        if d is None:
            result["warnings"].append("track has no AnalysisDataPath")
            return result
        if not Path(d).exists():
            result["warnings"].append(f"analysis folder missing: {d}")
            return result
        files = {p.suffix.upper(): p for p in Path(d).iterdir()}
        dat = files.get(".DAT")
        ext = files.get(".EXT")

        beats = bpms = times = None
        wave = None
        if dat:
            try:
                f = AnlzFile.parse_file(str(dat))
                for tag in f.tags:
                    n = tag.__class__.__name__
                    if n == "PQTZAnlzTag":
                        beats, bpms, times = tag.get()
                    elif n == "PWAVAnlzTag":
                        wave = np.asarray(tag.get(), dtype=float)
            except Exception as e:
                result["warnings"].append(f"DAT parse: {e}")
        if ext:
            try:
                f = AnlzFile.parse_file(str(ext))
                for tag in f.tags:
                    n = tag.__class__.__name__
                    if n == "PSSIAnlzTag":
                        s = tag.struct.content
                        result["mood"] = _MOOD_NAMES.get(s.mood, str(s.mood))
                        result["bank"] = _BANK_NAMES.get(s.bank, str(s.bank))
                        result["_pssi"] = [(e.index, e.beat, e.kind, e.k1, e.k2, e.k3, e.fill, e.beat_fill)
                                           for e in s.entries]
                        result["_end_beat"] = s.end_beat
                    elif n == "PWV3AnlzTag" and wave is None:
                        heights, _ = tag.get()
                        wave = np.asarray(heights, dtype=float)
            except Exception as e:
                result["warnings"].append(f"EXT parse: {e}")

        if times is None or len(times) == 0:
            result["warnings"].append("no beat grid (track not analysed?)")
            return result

        beats = np.asarray(beats); bpms = np.asarray(bpms); times = np.asarray(times, dtype=float)
        downbeat_idx = np.where(beats == 1)[0]
        grid = {
            "beats": int(len(times)),
            "bars": int(len(downbeat_idx)),
            "first_beat_sec": float(times[0]),
            "first_downbeat_sec": float(times[downbeat_idx[0]]) if len(downbeat_idx) else float(times[0]),
            "bpm_min": float(bpms.min()), "bpm_max": float(bpms.max()),
            "steady_tempo": bool(abs(bpms.max() - bpms.min()) < 0.05),
            "_times": times, "_beats": beats, "_downbeats": downbeat_idx,
        }
        result["grid"] = grid

        # waveform energy helper: wave is a per-frame height array; DAT PWAV is 400 entries
        # across the track, EXT PWV3 is ~150/sec.  Normalise to time.
        def energy(t0: float, t1: float) -> Optional[int]:
            if wave is None or len(wave) == 0 or c.Length in (None, 0):
                return None
            n = len(wave)
            i0 = int(max(0, min(n - 1, n * t0 / c.Length)))
            i1 = int(max(i0 + 1, min(n, n * t1 / c.Length)))
            seg = wave[i0:i1]
            if seg.size == 0:
                return None
            return int(round(float(seg.mean()) / 31.0 * 100))

        pssi = result.pop("_pssi", None)
        end_beat = result.pop("_end_beat", None)
        if pssi:
            mood_id = {"high": 1, "mid": 2, "low": 3}.get(result["mood"], 2)
            names = _PHRASE_NAMES.get(mood_id, _PHRASE_NAMES[2])
            phrases = []
            for i, (index, beat, kind, k1, k2, k3, fill, beat_fill) in enumerate(pssi):
                name = names.get(kind, f"kind{kind}")
                if mood_id == 1:  # high-mood variants
                    if kind == 1:
                        name = "Intro 1" if k1 == 1 else "Intro 2"
                    elif kind == 2:
                        name = "Up 3" if k2 == 1 else ("Up 2" if k3 == 1 else "Up 1")
                    elif kind == 5:
                        name = "Chorus 2" if k1 == 1 else "Chorus 1"
                    elif kind == 6:
                        name = "Outro 1" if k1 == 1 else "Outro 2"
                nb = pssi[i + 1][1] if i + 1 < len(pssi) else (end_beat or len(times))
                t0 = float(times[min(max(beat - 1, 0), len(times) - 1)])
                t1 = float(times[min(max(nb - 1, 0), len(times) - 1)])
                phrases.append({
                    "n": index, "name": name, "kind": kind,
                    "start_beat": int(beat), "end_beat": int(nb),
                    "start_bar": self.bar_at(grid, t0), "bars": max(1, round((nb - beat) / 4)),
                    "start_sec": round(t0, 3), "end_sec": round(t1, 3),
                    "start": _fmt_time(t0), "end": _fmt_time(t1),
                    "fill_at_beat": int(beat_fill) if fill else None,
                    "energy": energy(t0, t1),
                })
            result["phrases"] = phrases
        result["energy_overall"] = energy(0.0, float(c.Length or 0))
        return result

    @staticmethod
    def bar_at(grid: dict, sec: float) -> Optional[float]:
        """1-based bar number (fractional) for a time using the beat grid."""
        times = grid["_times"]; downbeats = grid["_downbeats"]
        if len(downbeats) == 0:
            return None
        dt = times[downbeats]
        if sec < dt[0]:
            return round(float(1 - (dt[0] - sec) / max(dt[1] - dt[0], 1e-6)) if len(dt) > 1 else 1.0, 2)
        i = int(np.searchsorted(dt, sec, side="right") - 1)
        if i >= len(dt) - 1:
            bar_len = dt[-1] - dt[-2] if len(dt) > 1 else 1.0
            return round(float(len(dt) + (sec - dt[-1]) / bar_len), 2)
        return round(float(i + 1 + (sec - dt[i]) / (dt[i + 1] - dt[i])), 2)

    @staticmethod
    def time_at_bar(grid: dict, bar: float, beat: int = 1) -> float:
        """Seconds for bar (1-based, may be fractional) and beat (1..4)."""
        times = grid["_times"]; downbeats = grid["_downbeats"]
        i = int(bar) - 1
        if i < 0 or len(downbeats) == 0:
            return float(times[0])
        if i >= len(downbeats):
            bar_len = float(times[downbeats[-1]] - times[downbeats[-2]]) if len(downbeats) > 1 else 2.0
            return float(times[downbeats[-1]]) + (bar - len(downbeats)) * bar_len
        idx = int(downbeats[i]) + (beat - 1)
        frac = bar - int(bar)
        base = float(times[min(idx, len(times) - 1)])
        if frac:
            nxt = float(times[downbeats[i + 1]]) if i + 1 < len(downbeats) else base + 2.0
            base += frac * (nxt - float(times[downbeats[i]]))
        return base

    @staticmethod
    def snap(grid: dict, sec: float, to: str) -> float:
        times = grid["_times"]
        if to == "bar":
            cand = times[grid["_downbeats"]]
        else:
            cand = times
        if len(cand) == 0:
            return sec
        j = int(np.abs(cand - sec).argmin())
        return float(cand[j])

    # -- writes ------------------------------------------------------------------
    def _guard_write(self) -> None:
        if self.rekordbox_running():
            raise RuntimeError("rekordbox is running. Close rekordbox (File > Exit) and retry; "
                               "writing while it is open can corrupt the library.")

    def master_path(self) -> Path:
        if self.db_path:
            return Path(self.db_path)
        from pyrekordbox.config import get_config
        for ver in ("rekordbox7", "rekordbox6"):
            try:
                cfg = get_config(ver)
            except Exception:
                continue
            if cfg and cfg.get("db_path"):
                return Path(cfg["db_path"])
        raise FileNotFoundError("could not locate master.db; set REKORDBOX_DB_PATH")

    def backup(self) -> str:
        """Copy master.db (+ WAL if present) before a write.  Call with no DB handle open."""
        self.close()
        src = self.master_path()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = self.backup_dir / f"master-{stamp}.db"
        shutil.copy2(src, dst)
        wal = src.with_name(src.name + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            try:
                shutil.copy2(wal, dst.with_name(dst.name + "-wal"))
            except PermissionError:
                pass  # locked by another handle; the main file still restores to last checkpoint
        olds = sorted(self.backup_dir.glob("master-*.db"))[:-25]
        for o in olds:
            for q in (o, o.with_name(o.name + "-wal")):
                q.unlink(missing_ok=True)
        return str(dst)

    @staticmethod
    def _json_ts(dt: _dt.datetime) -> str:
        """rekordbox stores local naive datetimes in the DB and UTC ISO strings in contentCue JSON."""
        utc = dt.astimezone(_dt.timezone.utc) if dt.tzinfo else dt.replace(tzinfo=None).astimezone(_dt.timezone.utc)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}+00:00"

    def _cue_json(self, q: tables.DjmdCue) -> dict:
        return {
            "ID": q.ID, "ContentID": q.ContentID, "ContentUUID": q.ContentUUID,
            "InMsec": q.InMsec, "InFrame": q.InFrame, "InMpegFrame": q.InMpegFrame or 0,
            "InMpegAbs": q.InMpegAbs or 0, "OutMsec": q.OutMsec if q.OutMsec is not None else -1,
            "OutFrame": q.OutFrame or 0, "OutMpegFrame": q.OutMpegFrame or 0, "OutMpegAbs": q.OutMpegAbs or 0,
            "Kind": q.Kind, "Color": q.Color if q.Color is not None else -1,
            "ColorTableIndex": q.ColorTableIndex or 0, "ActiveLoop": q.ActiveLoop or 0,
            "Comment": q.Comment or "", "BeatLoopSize": q.BeatLoopSize or 0, "CueMicrosec": q.CueMicrosec or 0,
            "UUID": q.UUID, "created_at": self._json_ts(q.created_at), "updated_at": self._json_ts(q.updated_at),
        }

    def _sync_content_cue(self, c: tables.DjmdContent, now: _dt.datetime) -> None:
        """Rebuild the contentCue JSON mirror + bump CueUpdated, like rekordbox does."""
        db = self.db
        db.flush()
        rows = db.get_cue(ContentID=c.ID).order_by(tables.DjmdCue.created_at).all()
        payload = json.dumps([self._cue_json(q) for q in rows], separators=(",", ":"), ensure_ascii=False)
        cc = db.get_content_cue(ContentID=c.ID).first()
        if cc is None:
            cc = tables.ContentCue.create(ID=c.UUID, ContentID=c.ID, Cues=payload, rb_cue_count=len(rows),
                                          UUID=str(uuid.uuid4()), rb_data_status=256,
                                          created_at=now, updated_at=now)
            db.add(cc)
        else:
            cc.Cues = payload
            cc.rb_cue_count = len(rows)
            cc.updated_at = now
        try:
            c.CueUpdated = str(int(c.CueUpdated or 0) + 1)
        except ValueError:
            c.CueUpdated = "1"
        c.updated_at = now

    def _commit(self, new_cues: list[tables.DjmdCue]) -> None:
        db = self.db
        # Let pyrekordbox bump the global local USN for every change, then blank the
        # per-row USN on cue rows because rekordbox itself leaves those NULL.
        db.registry.autoincrement_local_update_count(set_row_usn=True)
        with db.registry.disabled():
            for q in new_cues:
                q.rb_local_usn = None
                q.usn = None
        db.commit(autoinc=False)

    def _make_cue(self, c: tables.DjmdContent, kind: int, in_ms: int, out_ms: int, color_val: int,
                  color_idx: int, comment: Optional[str], now: _dt.datetime) -> tables.DjmdCue:
        db = self.db
        return tables.DjmdCue.create(
            ID=str(db.generate_unused_id(tables.DjmdCue, is_28_bit=False)),
            ContentID=c.ID, ContentUUID=c.UUID,
            InMsec=in_ms, InFrame=int(in_ms * 150 / 1000), InMpegFrame=0, InMpegAbs=0,
            OutMsec=out_ms, OutFrame=int(out_ms * 150 / 1000) if out_ms > 0 else 0,
            OutMpegFrame=0, OutMpegAbs=0,
            Kind=kind, Color=color_val, ColorTableIndex=color_idx, ActiveLoop=0,
            Comment=comment or "", BeatLoopSize=0, CueMicrosec=0,
            UUID=str(uuid.uuid4()), rb_data_status=256, rb_local_data_status=0,
            rb_local_deleted=0, rb_local_synced=0, created_at=now, updated_at=now,
        )

    def protected_backup(self, label: str) -> str:
        """One backup before a batch write. Named PROTECTED-* so the 25-file rotation never removes it."""
        self.close()
        src = self.master_path()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in label)[:60]
        dst = self.backup_dir / f"PROTECTED-{safe}-{stamp}.db"
        shutil.copy2(src, dst)
        wal = src.with_name(src.name + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            shutil.copy2(wal, dst.with_name(dst.name + "-wal"))
        return str(dst)

    def write_cues(self, adds: list[dict], delete_ids: list[str], label: str = "batch") -> dict:
        """Batch memory-cue write: one guard, one PROTECTED backup, one commit.

        adds: [{track_id, bar, beat?, color, comment}] or with position_sec instead of bar.
        Only coloured memory cues are accepted: rekordbox draws uncoloured memory cues red,
        which makes them indistinguishable from OUT markers.
        delete_ids: djmdCue IDs to remove (the caller decides which are safe to delete).
        """
        for a in adds:
            col = (a.get("color") or "").strip().lower()
            if col not in MEMORY_COLORS:
                raise ValueError(f"refusing memory cue without a valid colour ({a!r}); rekordbox shows those as red")
        if not adds and not delete_ids:
            return {"ok": True, "backup": None, "created": [], "deleted": []}
        self._guard_write()
        backup = self.protected_backup(label)
        db = self.open()
        now = _dt.datetime.now()
        touched: dict[str, tables.DjmdContent] = {}
        deleted = []
        for cid in delete_ids:
            q = db.get_cue().filter(tables.DjmdCue.ID == str(cid)).first()
            if q is None:
                continue
            deleted.append({"cue_id": str(cid), "track_id": str(q.ContentID)})
            touched[str(q.ContentID)] = self.content(q.ContentID)
            db.delete(q)
        db.flush()
        grids: dict[str, dict] = {}
        new_cues, created = [], []
        for a in adds:
            tid = str(a["track_id"])
            c = touched.get(tid) or self.content(tid)
            touched[tid] = c
            if a.get("position_sec") is not None:
                pos = float(a["position_sec"])
            else:
                if tid not in grids:
                    g = self.analysis(c)["grid"]
                    if g is None:
                        raise ValueError(f"track {tid} has no beat grid")
                    grids[tid] = g
                pos = self.time_at_bar(grids[tid], float(a["bar"]), int(a.get("beat", 1)))
            if pos < 0 or (c.Length and pos > c.Length + 1):
                raise ValueError(f"cue at {pos:.2f}s is outside track {tid} (length {c.Length}s)")
            in_ms = int(round(pos * 1000))
            q = self._make_cue(c, KIND_MEMORY, in_ms, -1, MEMORY_COLORS.index(a["color"].strip().lower()), 0,
                               a.get("comment"), now)
            db.add(q)
            db.flush()
            new_cues.append(q)
            created.append({"cue_id": str(q.ID), "track_id": tid, "position_ms": in_ms,
                            "color": a["color"].strip().lower(), "comment": a.get("comment") or ""})
        for c in touched.values():
            self._sync_content_cue(c, now)
        self._commit(new_cues)
        self.reopen()
        return {"ok": True, "backup": backup, "created": created, "deleted": deleted}

    def set_cue(self, track_id: str, slot: Optional[str] = None, position_sec: Optional[float] = None,
                bar: Optional[float] = None, beat: int = 1, snap: Optional[str] = None,
                comment: Optional[str] = None, color: Optional[str] = None,
                loop_end_sec: Optional[float] = None, loop_bars: Optional[float] = None,
                replace: bool = True, memory: bool = False) -> dict:
        """Create (or replace) a hot cue in `slot` or a memory cue (memory=True)."""
        self._guard_write()
        backup = self.backup()
        db = self.open()
        c = self.content(track_id)
        grid = None
        if bar is not None or snap or loop_bars is not None:
            grid = self.analysis(c)["grid"]
            if grid is None:
                raise ValueError("track has no beat grid; analyse it in rekordbox first or pass position_sec without snap")
        if bar is not None:
            position_sec = self.time_at_bar(grid, float(bar), int(beat))
        if position_sec is None:
            raise ValueError("give position_sec or bar")
        if snap in ("beat", "bar"):
            position_sec = self.snap(grid, position_sec, snap)
        if position_sec < 0 or (c.Length and position_sec > c.Length + 1):
            raise ValueError(f"position {position_sec}s is outside the track (length {c.Length}s)")

        if memory:
            kind = KIND_MEMORY
        else:
            if slot is None:
                raise ValueError("slot (A-P) is required for a hot cue")
            kind = slot_to_kind(slot)

        out_ms = -1
        if loop_bars is not None:
            end = self.time_at_bar(grid, self.bar_at(grid, position_sec) + float(loop_bars))
            out_ms = int(round(end * 1000))
        elif loop_end_sec is not None:
            out_ms = int(round(loop_end_sec * 1000))
        if out_ms > 0 and memory:
            kind = KIND_MEMORY_LOOP

        # colour
        color_val, color_idx = -1, 0
        if color:
            cn = color.strip().lower()
            if memory:
                if cn not in MEMORY_COLORS:
                    raise ValueError(f"memory cue colour must be one of {MEMORY_COLORS}")
                color_val = MEMORY_COLORS.index(cn)
            else:
                if cn.startswith("#") and len(cn) == 7:
                    rgb = tuple(int(cn[i:i + 2], 16) for i in (1, 3, 5))
                    color_idx = min(HOTCUE_COLORS.values(),
                                    key=lambda i: sum((a - b) ** 2 for a, b in zip(_COLOR_TABLE_RGB[i], rgb)))
                elif cn in HOTCUE_COLORS:
                    color_idx = HOTCUE_COLORS[cn]
                elif cn not in ("default", "none"):
                    raise ValueError(f"hot cue colour must be one of {list(HOTCUE_COLORS)} or #rrggbb")

        now = _dt.datetime.now()
        replaced = None
        if not memory:
            existing = db.get_cue(ContentID=c.ID, Kind=kind).all()
            if existing:
                if not replace:
                    raise ValueError(f"slot {kind_to_slot(kind)} already has a cue at "
                                     f"{_fmt_time(existing[0].InMsec / 1000)}; pass replace=True to overwrite")
                replaced = [_fmt_time(q.InMsec / 1000) for q in existing]
                for q in existing:
                    db.delete(q)
                db.flush()

        in_ms = int(round(position_sec * 1000))
        cue = self._make_cue(c, kind, in_ms, out_ms, color_val, color_idx, comment, now)
        db.add(cue)
        self._sync_content_cue(c, now)
        self._commit([cue])
        db = self.reopen()
        c = self.content(track_id)
        return {
            "ok": True, "track": f"{c.ArtistName} - {c.Title}", "slot": kind_to_slot(kind),
            "position": _fmt_time(position_sec), "position_sec": round(position_sec, 3),
            "bar": self.bar_at(grid, position_sec) if grid else None,
            "loop_end_sec": out_ms / 1000 if out_ms > 0 else None,
            "replaced": replaced, "backup": backup,
            "cues_now": self.cues(c, grid),
        }

    def delete_cue(self, track_id: str, slot: Optional[str] = None, cue_id: Optional[str] = None) -> dict:
        self._guard_write()
        backup = self.backup()
        db = self.open()
        c = self.content(track_id)
        if cue_id:
            rows = db.get_cue(ContentID=c.ID).filter(tables.DjmdCue.ID == str(cue_id)).all()
        elif slot:
            s = slot.strip().upper()
            if s == "MEMORY":
                rows = db.get_cue(ContentID=c.ID).filter(tables.DjmdCue.Kind.in_([KIND_MEMORY, KIND_MEMORY_LOOP])).all()
            else:
                rows = db.get_cue(ContentID=c.ID, Kind=slot_to_kind(s)).all()
        else:
            raise ValueError("give slot or cue_id")
        if not rows:
            return {"ok": False, "reason": "no matching cue", "cues_now": self.cues(c)}
        removed = [{"slot": kind_to_slot(q.Kind), "position": _fmt_time(q.InMsec / 1000)} for q in rows]
        now = _dt.datetime.now()
        for q in rows:
            db.delete(q)
        self._sync_content_cue(c, now)
        self._commit([])
        db = self.reopen()
        c = self.content(track_id)
        return {"ok": True, "removed": removed, "backup": backup, "cues_now": self.cues(c)}

    # -- playlists (write)
    def create_playlist(self, name: str, parent_id: Optional[str] = None) -> dict:
        self._guard_write()
        backup = self.backup()
        db = self.open()
        parent = db.get_playlist(ID=str(parent_id)) if parent_id else None
        pl = db.create_playlist(name, parent=parent)
        db.commit()
        return {"ok": True, "id": pl.ID, "name": pl.Name, "backup": backup}

    def add_to_playlist(self, playlist_id: str, track_ids: list[str]) -> dict:
        self._guard_write()
        backup = self.backup()
        db = self.open()
        pl = db.get_playlist(ID=str(playlist_id))
        if pl is None:
            raise ValueError(f"no playlist {playlist_id}")
        added = []
        for tid in track_ids:
            c = self.content(tid)
            db.add_to_playlist(pl, c)
            added.append(f"{c.ArtistName} - {c.Title}")
        db.commit()
        return {"ok": True, "playlist": pl.Name, "added": added, "backup": backup}

    def reorder_playlist(self, playlist_id: str, track_ids: list[str]) -> dict:
        """Set the full running order of an existing playlist in place (no new playlist).
        track_ids is the desired order of content IDs; any playlist tracks not listed keep
        their relative order after the listed ones."""
        self._guard_write()
        backup = self.backup()
        db = self.open()
        pl = db.get_playlist(ID=str(playlist_id))
        if pl is None:
            raise ValueError(f"no playlist {playlist_id}")
        songs = sorted(self._live_songs(pl.ID).all(), key=lambda s: s.TrackNo)
        by_content: dict[str, list] = {}
        for s in songs:
            by_content.setdefault(str(s.ContentID), []).append(s)
        want = [str(t) for t in track_ids]
        missing = [t for t in want if t not in by_content]
        if missing:
            raise ValueError(f"not in playlist: {missing}")
        if len(set(want)) != len(want):
            raise ValueError("track_ids contains duplicates")
        ordered = [by_content[t].pop(0) for t in want]
        ordered += [s for s in songs if s not in ordered]
        for pos, song in enumerate(ordered, start=1):
            if song.TrackNo != pos:
                db.move_song_in_playlist(pl, song, pos)
        db.commit()
        final = sorted(self._live_songs(pl.ID).all(), key=lambda s: s.TrackNo)
        return {"ok": True, "playlist": pl.Name, "backup": backup,
                "order": [f"{s.TrackNo}. {s.Content.ArtistName} - {s.Content.Title}" for s in final]}

    def find_playlist(self, name: str) -> Optional[tables.DjmdPlaylist]:
        for p in self.db.get_playlist().all():
            if p.Name == name and p.Attribute == 0:
                return p
        return None

    def replace_playlist(self, name: str, track_ids: list[str], label: str = "playlist") -> dict:
        """Create playlist `name` (or empty the existing one) and fill it with track_ids in order.
        One guard, one PROTECTED backup. Consecutive duplicates are collapsed."""
        self._guard_write()
        backup = self.protected_backup(label)
        db = self.open()
        pl = self.find_playlist(name)
        if pl is None:
            pl = db.create_playlist(name)
            db.flush()
        else:
            for song in self._live_songs(pl.ID).all():
                db.remove_from_playlist(pl, song)
            db.flush()
        added, last = [], None
        for tid in track_ids:
            if str(tid) == last:
                continue
            db.add_to_playlist(pl, self.content(tid))
            added.append(str(tid)); last = str(tid)
        db.commit()
        pid = pl.ID
        self.reopen()
        return {"ok": True, "playlist_id": str(pid), "name": name, "tracks": len(added), "backup": backup}

    def remove_from_playlist(self, playlist_id: str, track_ids: list[str]) -> dict:
        self._guard_write()
        backup = self.backup()
        db = self.open()
        pl = db.get_playlist(ID=str(playlist_id))
        if pl is None:
            raise ValueError(f"no playlist {playlist_id}")
        removed = []
        for tid in track_ids:
            for song in self._live_songs(pl.ID, ContentID=str(tid)).all():
                db.remove_from_playlist(pl, song)
                removed.append(str(tid))
        db.commit()
        return {"ok": True, "playlist": pl.Name, "removed": removed, "backup": backup}


def public(d: dict) -> dict:
    """Strip private numpy fields before returning JSON."""
    if isinstance(d, dict):
        return {k: public(v) for k, v in d.items() if not k.startswith("_")}
    if isinstance(d, list):
        return [public(x) for x in d]
    if isinstance(d, (np.floating, np.integer)):
        return d.item()
    return d
