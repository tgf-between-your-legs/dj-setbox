"""Per-beat and per-bar track features from rekordbox's own analysis (no audio needed).

Inputs come from the track's ANLZ files: the beat grid (PQTZ), the 3-band detail waveform
(PWV7, 150 frames/s of (low, mid, high)), rekordbox's vocal detection (PVDI) and phrases
(PSSI). `compute()` is pure numpy so it can be tested on synthetic tracks; `load()` reads a
real track through rbcore and caches the result in the engine store.

Lessons encoded here (from planning a 76-track set by hand):
* Check landings beat by beat. A 1-bar break or riser right before a drop fools bar-level
  novelty, so `find_drops` reports the break and the beat where the change really happens.
* A grid can be half a bar off (every drop on beat 3). `check_grid` detects that.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

ANALYSIS_VERSION = 2
WAVE_FPS = 150
BASS_ON = 45        # bar-map low level (0-100) that counts as "bass playing"
VOCAL_ON = 40       # % of a bar rekordbox marks as voice (level >= 2)
W = np.array([1.0, 0.7, 0.5, 0.8])   # novelty weights: low, mid, high, vocal

# columns of TrackFeatures.rows
ROW_BAR, ROW_SEC, ROW_PHRASE, ROW_LOW, ROW_MID, ROW_HIGH, ROW_VOCAL, ROW_BASS, ROW_VOC = range(9)


@dataclass
class TrackFeatures:
    id: str
    bpm: float
    times: np.ndarray            # beat times (s)
    beats: np.ndarray            # beat number in bar (1-4) per beat
    beat: np.ndarray             # (n_beats, 4): low, mid, high (0-1, vs 95th pct), vocal fraction
    db: np.ndarray               # beat index of each downbeat
    rows: list[list]             # per bar: bar, sec, phrase, low, mid, high, vocal (0-100), bass flag, vocal flag
    phrases: list[dict]          # {name, start (fractional bar), bars}
    title: str = ""
    artist: str = ""
    key: Optional[str] = None
    camelot: Optional[str] = None
    length: float = 0.0
    mood: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    _bars: Optional[np.ndarray] = None
    _strength: Optional[np.ndarray] = None

    # -- derived -----------------------------------------------------------------
    @property
    def n_bars(self) -> int:
        return len(self.db)

    @property
    def bar_sec(self) -> float:
        return 240.0 / self.bpm if self.bpm else 2.0

    @property
    def bars(self) -> np.ndarray:
        """(n_bars, 4) mean of the beat features over each bar's 4 beats."""
        if self._bars is None:
            self._bars = np.array([self.beat[i:i + 4].mean(0) for i in self.db]) if len(self.db) else np.zeros((0, 4))
        return self._bars

    @property
    def strength(self) -> np.ndarray:
        """strength[b] for 1-based bar b: how hard the sound changes going into bar b."""
        if self._strength is None:
            self._strength = strength(self.bars)
        return self._strength

    def row(self, bar: int) -> list:
        return self.rows[bar - 1]

    def win(self, a: int, b: int, col: int) -> float:
        """Mean of a bar-map column over bars [a, b)."""
        rs = [self.rows[x - 1] for x in range(max(1, a), min(b, self.n_bars + 1))]
        return sum(r[col] for r in rs) / len(rs) if rs else 0.0

    def energy(self, a: int, b: int) -> float:
        """Mean low+mid over bars [a, b)."""
        B = self.bars[max(0, a - 1):max(a, b - 1)]
        return float((B[:, 0] + B[:, 1]).mean()) if len(B) else 0.0

    def bass_up(self, b: int) -> float:
        B = self.bars; k = b - 1
        if k < 1 or k + 2 > len(B):
            return 0.0
        return float(B[k:k + 2, 0].mean() - B[max(0, k - 2):k, 0].mean())

    def is_break(self, x: int) -> bool:
        """Bar x is a short break: its bass is well below the bars either side."""
        B = self.bars
        if x < 2 or x + 1 > len(B):
            return False
        lo = B[x - 1, 0]
        return bool(lo < 0.6 * B[x - 2, 0] and lo < 0.6 * B[x, 0] and B[x, 0] > 0.4)

    def change_beat(self, bar: int) -> int:
        """Beat offset (-3..3) from the bar's downbeat where the biggest single change happens."""
        k0 = self.db[bar - 1]; e = self.beat; best = None
        for off in range(-3, 4):
            k = k0 + off
            if k < 2 or k + 2 >= len(e):
                continue
            v = float((np.abs(e[k:k + 2].mean(0) - e[k - 2:k].mean(0)) * W).sum())
            if best is None or v > best[0]:
                best = (v, off)
        return best[1] if best else 0

    def chorus_starts(self) -> list[int]:
        out = []
        for p in self.phrases:
            s = int(round(p["start"]))
            if p["name"].startswith("Chorus") and s >= 5 and self.win(s, s + 4, ROW_BASS) >= .75:
                out.append(s)
        return out

    def time_at(self, bar: int, beat: int = 1) -> Optional[float]:
        if bar < 1 or bar > self.n_bars:
            return None
        i = int(self.db[bar - 1]) + beat - 1
        return float(self.times[min(i, len(self.times) - 1)])

    # -- serialisation -------------------------------------------------------------
    def to_json(self) -> dict:
        return dict(id=self.id, bpm=self.bpm, times=self.times.tolist(), beats=self.beats.tolist(),
                    beat=self.beat.tolist(), db=self.db.tolist(), rows=self.rows, phrases=self.phrases,
                    title=self.title, artist=self.artist, key=self.key, camelot=self.camelot,
                    length=self.length, mood=self.mood, warnings=self.warnings)

    @classmethod
    def from_json(cls, d: dict) -> "TrackFeatures":
        return cls(id=d["id"], bpm=d["bpm"], times=np.array(d["times"], float), beats=np.array(d["beats"], int),
                   beat=np.array(d["beat"], float).reshape(-1, 4), db=np.array(d["db"], int), rows=d["rows"],
                   phrases=d["phrases"], title=d.get("title", ""), artist=d.get("artist", ""), key=d.get("key"),
                   camelot=d.get("camelot"), length=d.get("length", 0.0), mood=d.get("mood"),
                   warnings=d.get("warnings", []))


# ----------------------------------------------------------------------------- pure maths
def strength(B: np.ndarray) -> np.ndarray:
    n = len(B); s = np.zeros(n + 2)
    for k in range(1, n):
        n1 = float((np.abs(B[k] - B[k - 1]) * W).sum())
        n2 = float((np.abs(B[k:k + 2].mean(0) - B[max(0, k - 2):k].mean(0)) * W).sum())
        s[k + 1] = 0.5 * n1 + n2
    return s


def _smooth_flags(v: list[bool]) -> list[bool]:
    out = list(v)
    for i in range(1, len(v) - 1):
        if v[i - 1] == v[i + 1] != v[i]:
            out[i] = v[i - 1]
    return out


def compute(track_id: str, bpm: float, times, beats, wave, voc, vfps: float, length: float,
            phrases: list[dict], **meta: Any) -> TrackFeatures:
    """Build features from raw analysis arrays.

    times/beats: beat grid; wave: (frames, 3) uint8 PWV7 at 150 fps; voc: PVDI bytes (0-4) at vfps;
    phrases: [{name, start (fractional bar), bars}].
    """
    t = np.asarray(times, float); bt = np.asarray(beats, int)
    wave = np.asarray(wave, float).reshape(-1, 3) if wave is not None and len(wave) else np.zeros((1, 3))
    v = np.asarray(voc if voc is not None else [], dtype=np.uint8)
    warnings = list(meta.pop("warnings", []))
    if len(t) < 8:
        raise ValueError(f"track {track_id}: beat grid too short ({len(t)} beats)")

    # per-beat features (ana.prep)
    ref = np.percentile(wave, 95, axis=0); ref[ref <= 0] = 1
    w = wave / ref
    ends = np.append(t[1:], t[-1] + (t[-1] - t[-2]))
    beat = []
    for t0, t1 in zip(t, ends):
        i0, i1 = int(t0 * WAVE_FPS), max(int(t1 * WAVE_FPS), int(t0 * WAVE_FPS) + 1)
        seg = w[i0:i1]
        vs = v[int(t0 * vfps):max(int(t1 * vfps), int(t0 * vfps) + 1)] if len(v) else v
        bands = list(seg.mean(0)) if len(seg) else [0.0, 0.0, 0.0]
        beat.append(bands + [float((vs >= 2).mean()) if len(vs) else 0])
    beat = np.array(beat, float)
    db = np.where(bt == 1)[0]

    # per-bar map (barmap.bar_map)
    starts = [float(t[j]) for j in db]
    bar_ends = starts[1:] + [float(length or (starts[-1] + 2))]
    raw_bands, raw_voc = [], []
    for t0, t1 in zip(starts, bar_ends):
        seg = wave[int(t0 * WAVE_FPS):max(int(t1 * WAVE_FPS), int(t0 * WAVE_FPS) + 1)]
        raw_bands.append(seg.mean(0) if len(seg) else np.zeros(3))
        if len(v):
            seg = v[int(t0 * vfps):max(int(t1 * vfps), int(t0 * vfps) + 1)]
            raw_voc.append(float((seg >= 2).mean() * 100) if len(seg) else 0.0)
    arr = np.vstack(raw_bands)
    bref = np.percentile(arr, 95, axis=0); bref[bref <= 0] = 1
    bands = np.clip(arr / bref * 100, 0, 100).round().astype(int)

    def phrase_at(bar: int) -> Optional[str]:
        for p in phrases:
            if p["start"] is not None and p["start"] <= bar < p["start"] + p["bars"]:
                return p["name"]
        return None

    rows = []
    for i, t0 in enumerate(starts):
        rows.append([i + 1, round(t0, 2), phrase_at(i + 1), int(bands[i][0]), int(bands[i][1]), int(bands[i][2]),
                     int(round(raw_voc[i])) if raw_voc else 0])
    bass = _smooth_flags([r[ROW_LOW] >= BASS_ON for r in rows])
    vocal = _smooth_flags([r[ROW_VOCAL] >= VOCAL_ON for r in rows])
    for r, b_, v_ in zip(rows, bass, vocal):
        r += [int(b_), int(v_)]
    if not len(v):
        warnings.append("no vocal detection data (PVDI)")
    return TrackFeatures(id=str(track_id), bpm=float(bpm), times=t, beats=bt, beat=beat, db=db, rows=rows,
                         phrases=phrases, warnings=warnings, length=float(length or 0), **meta)


# ----------------------------------------------------------------------------- detectors
def grid_phase_scores(f: TrackFeatures, top: int = 6) -> list[float]:
    """Mean of the `top` biggest bar-to-bar changes if bars started o beats after the grid's downbeat."""
    e = f.beat; res = []
    for o in range(4):
        idx = f.db + o; idx = idx[idx + 4 <= len(e)]
        bars = np.array([e[i:i + 4].mean(0) for i in idx])
        diff = np.abs(np.diff(bars, axis=0)).sum(1) if len(bars) > 1 else np.zeros(1)
        res.append(float(np.sort(diff)[::-1][:top].mean()))
    return res


def check_grid(f: TrackFeatures) -> dict:
    """Is the grid's downbeat where the music's sections change? Suggests a beat offset if not.

    Two signals: (1) bar-phase scores - how big section changes look if bars started 0-3 beats
    later; (2) votes - at every strong section change, on which beat of the bar the biggest
    single change happens. Votes are folded by phase (a change 2 beats early or late both mean
    "half a bar off"). Either signal clearly favouring a non-zero phase flags the grid.
    """
    sc = grid_phase_scores(f)
    best = int(np.argmax(sc))
    margin = sc[best] / sc[0] if sc[0] > 0 else float("inf")
    offs = [f.change_beat(b) for b in range(3, f.n_bars - 2) if f.strength[b] >= 0.6]
    votes = {o: offs.count(o) for o in set(offs)}
    phase_votes = [sum(v for o, v in votes.items() if o % 4 == ph) for ph in range(4)]
    n = sum(phase_votes)
    vbest = int(np.argmax(phase_votes))
    by_votes = (vbest != 0 and n >= 6 and phase_votes[vbest] >= 0.5 * n
                and phase_votes[vbest] >= 2.5 * max(1, phase_votes[0]))
    # the bar-phase score alone is noisy on real tracks; it only counts when the votes agree
    suspicious = by_votes and (best == vbest or margin < 1.15)
    phase = vbest
    offset = {0: 0, 1: 1, 2: -2, 3: -1}[phase] if suspicious else 0
    confidence = "high" if suspicious and phase == 2 and best == 2 else ("medium" if suspicious else None)
    note = None
    if suspicious and phase == 3:
        note = "changes land one beat early: often a vocal pickup or fill before the 1 rather than a bad grid; check by ear"
    return {
        "phase_scores": [round(x, 3) for x in sc],
        "best_phase": best,
        "margin": round(margin, 2) if margin != float("inf") else None,
        "change_beat_votes": {str(k): v for k, v in sorted(votes.items())},
        "phase_votes": phase_votes,
        "suspicious": bool(suspicious),
        "confidence": confidence,
        "note": note,
        "suggested_beat_offset": offset,
        "advice": ("grid looks right" if not suspicious else
                   f"sections change on beat {phase + 1} of the grid's bars: the downbeat is probably "
                   f"{abs(offset)} beat(s) {'late' if offset < 0 else 'early'}. Fix it in rekordbox's grid edit, "
                   f"or record set_grid_fix(beat_offset={offset}) so cues land on the real bar."),
    }


def find_drops(f: TrackFeatures, top: int = 6, min_bar: int = 5) -> list[dict]:
    """Landing candidates: bars where the sound changes hard and the bass comes in, best first."""
    s = f.strength; out = []
    for b in range(max(2, min_bar), f.n_bars - 7):
        up = f.bass_up(b)
        score = s[b] + 1.5 * max(0.0, up)
        if score < 0.3:
            continue
        if b > 2 and s[b - 1] > s[b] and s[b - 1] + 1.5 * max(0.0, f.bass_up(b - 1)) > score:
            continue
        if b + 1 < len(s) and s[b + 1] + 1.5 * max(0.0, f.bass_up(b + 1)) > score:
            continue
        notes = []
        if f.is_break(b - 1):
            notes.append(f"bar {b - 1} is a 1-bar break; the drop is {b}, not the break")
        if f.is_break(b):
            notes.append(f"bar {b} itself is a break; check {b + 1}")
        cb = f.change_beat(b)
        if cb:
            notes.append(f"the biggest change is {abs(cb)} beat(s) {'after' if cb > 0 else 'before'} the downbeat")
        row = f.row(b)
        out.append({"bar": b, "time": round(float(f.times[f.db[b - 1]]), 2), "score": round(float(score), 3),
                    "bass_up": round(float(up), 2), "phrase": row[ROW_PHRASE], "vocal": row[ROW_VOCAL],
                    "confidence": round(float(min(1.0, score / 1.5)), 2), "notes": notes})
    out.sort(key=lambda d: -d["score"])
    return out[:top]


# ----------------------------------------------------------------------------- library IO
def raw_tags(path: Path) -> dict[str, tuple[bytes, bytes]]:
    """{tag: (header, body)} for every tag in an ANLZ file, including ones pyrekordbox skips."""
    b = path.read_bytes()
    i = struct.unpack(">I", b[4:8])[0]
    out: dict[str, tuple[bytes, bytes]] = {}
    while i + 12 <= len(b):
        name = b[i:i + 4].decode("latin1")
        h, n = struct.unpack(">II", b[i + 4:i + 12])
        if n <= 0:
            break
        out.setdefault(name, (b[i:i + h], b[i + h:i + n]))
        i += n
    return out


def _source_key(d: Optional[Path]) -> str:
    if d is None or not d.exists():
        return "none"
    parts = []
    for p in sorted(d.iterdir()):
        if p.suffix.upper() in (".DAT", ".EXT", ".2EX"):
            st = p.stat()
            parts.append(f"{p.suffix}:{st.st_size}:{int(st.st_mtime)}")
    return "|".join(parts)


ENCRYPTED_PREFIX = "$A7:"


def streaming_ref(path: Optional[str]) -> Optional[tuple[str, str]]:
    """('spotify', id) / ('soundcloud', id) ... for a streamed track's FolderPath, else None."""
    if not path or ":" not in path or "/" in path.split(":", 1)[0] or "\\" in path[:3]:
        return None
    parts = path.split(":")
    if len(parts) >= 3 and parts[0].isalpha() and len(parts[0]) > 1:
        return parts[0].lower(), parts[-1]
    return None


def display_name(title: Optional[str], artist: Optional[str], path: Optional[str]) -> tuple[str, str]:
    """rekordbox stores some streaming services' titles encrypted ($A7:v1:...). Show something readable;
    `Engine.resolve_names` can fetch the real ones."""
    t, a = title or "", artist or ""
    ref = streaming_ref(path)
    if t.startswith(ENCRYPTED_PREFIX):
        t = f"{ref[0].title()} track {ref[1][:8]}" if ref else "encrypted title"
    if a.startswith(ENCRYPTED_PREFIX):
        a = ""
    return t, a


def extract(lib, c) -> TrackFeatures:
    """Read one rekordbox track (rbcore.Library + DjmdContent) into TrackFeatures."""
    from .. import rbcore
    a = lib.analysis(c)
    g = a["grid"]
    if not g:
        raise ValueError(f"track {c.ID} has no beat grid: {a['warnings']}")
    d = lib._anlz_dir(c)
    tags = raw_tags(d / "ANLZ0000.2EX") if d and (d / "ANLZ0000.2EX").exists() else {}
    warnings = list(a["warnings"])
    if "PWV7" in tags:
        wave = np.frombuffer(tags["PWV7"][1], np.uint8).reshape(-1, 3)
    else:
        wave = None
        warnings.append("no 3-band waveform (PWV7); re-analyse the track in rekordbox 6.6+")
    voc, vfps = None, 1.0
    if "PVDI" in tags:
        hdr, body = tags["PVDI"]
        hop, sr = struct.unpack(">IH", hdr[12:18])
        voc, vfps = np.frombuffer(body, np.uint8), sr / hop
    key = c.Key.ScaleName if c.Key else None
    title, artist = display_name(c.Title, c.ArtistName, c.FolderPath)
    return compute(str(c.ID), (c.BPM or 0) / 100.0, g["_times"], g["_beats"], wave, voc, vfps, float(c.Length or 0),
                   [dict(name=p["name"], start=p["start_bar"], bars=p["bars"]) for p in a["phrases"]],
                   title=title, artist=artist, key=key, camelot=rbcore.camelot(key),
                   mood=a.get("mood"), warnings=warnings)


def load(lib, store, track_id: str, refresh: bool = False) -> TrackFeatures:
    """Features for a library track, from the store's cache when the analysis files haven't changed."""
    c = lib.content(track_id)
    key = _source_key(lib._anlz_dir(c))
    if store is not None and not refresh:
        hit = store.get_analysis(str(track_id), key, ANALYSIS_VERSION)
        if hit:
            return TrackFeatures.from_json(hit)
    f = extract(lib, c)
    if store is not None:
        store.put_analysis(str(track_id), key, ANALYSIS_VERSION, f.to_json())
    return f
