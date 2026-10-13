"""Per-bar bass/mid/high + vocal map from rekordbox's own analysis, and a transition planner.

Data sources (all in the track's ANLZ0000.2EX, present for streamed tracks too):
  PWV7  3-band detail waveform, 150 entries/sec, 3 bytes each = (low, mid, high).
        Byte order verified 2026-10-12 against the decoded audio of a local mp3:
        corr(low<150Hz, byte0)=0.61, corr(mid, byte1)=0.62, corr(high>2.5kHz, byte2)=0.73.
  PVDI  rekordbox's vocal detection (the vocal bar above the waveform). pyrekordbox does not
        parse it.  Header after tag/len/len: u32 hop (1024), u16 sample rate (22050), u16 ?,
        u32 count; then one byte per hop, 0 (no voice) .. 4 (clear voice).
Both are read raw; nothing here writes to the library.
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, Optional

import numpy as np

from . import rbcore

WAVE_FPS = 150
BASS_ON = 45      # normalised low-band level that counts as "bass playing"
VOCAL_ON = 40     # % of a bar's frames rekordbox marks as voice (level >= 2)
BLEND_MAX_PCT = 6.0


def _raw_tags(path: Path) -> dict[str, tuple[bytes, bytes]]:
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


def _smooth_flags(v: list[bool]) -> list[bool]:
    """Flip single bars that disagree with two agreeing neighbours (one-bar blips aren't sections)."""
    out = list(v)
    for i in range(1, len(v) - 1):
        if v[i - 1] == v[i + 1] != v[i]:
            out[i] = v[i - 1]
    return out


def bar_map(lib: rbcore.Library, c, a: Optional[dict] = None) -> dict[str, Any]:
    a = a or lib.analysis(c)
    g = a["grid"]
    res: dict[str, Any] = {"bars": [], "sections": [], "warnings": list(a["warnings"])}
    if not g:
        return res
    d = lib._anlz_dir(c)
    tags = _raw_tags(d / "ANLZ0000.2EX") if d and (d / "ANLZ0000.2EX").exists() else {}
    if "PWV7" not in tags:
        res["warnings"].append("no 3-band waveform (PWV7); re-analyse the track in rekordbox 6.6+")
    if "PVDI" not in tags:
        res["warnings"].append("no vocal detection data (PVDI)")

    wave = np.frombuffer(tags["PWV7"][1], np.uint8).reshape(-1, 3).astype(float) if "PWV7" in tags else None
    voc = vfps = None
    if "PVDI" in tags:
        hdr, body = tags["PVDI"]
        hop, sr = struct.unpack(">IH", hdr[12:18])
        voc, vfps = np.frombuffer(body, np.uint8), sr / hop

    times = g["_times"]
    starts = [float(times[j]) for j in g["_downbeats"]]
    ends = starts[1:] + [float(c.Length or (starts[-1] + 2))]
    phrases = a["phrases"]

    def phrase_at(bar: int) -> Optional[str]:
        for p in phrases:
            if p["start_bar"] is not None and p["start_bar"] <= bar < p["start_bar"] + p["bars"]:
                return p["name"]
        return None

    raw_bands, raw_voc = [], []
    for t0, t1 in zip(starts, ends):
        if wave is not None:
            seg = wave[int(t0 * WAVE_FPS):max(int(t1 * WAVE_FPS), int(t0 * WAVE_FPS) + 1)]
            raw_bands.append(seg.mean(0) if len(seg) else np.zeros(3))
        if voc is not None:
            seg = voc[int(t0 * vfps):max(int(t1 * vfps), int(t0 * vfps) + 1)]
            raw_voc.append(float((seg >= 2).mean() * 100) if len(seg) else 0.0)

    bands = None
    if raw_bands:
        arr = np.vstack(raw_bands)
        ref = np.percentile(arr, 95, axis=0)
        ref[ref <= 0] = 1
        bands = np.clip(arr / ref * 100, 0, 100).round().astype(int)

    rows = []
    for i, t0 in enumerate(starts):
        bar = i + 1
        rows.append({
            "bar": bar, "sec": round(t0, 2), "phrase": phrase_at(bar),
            "low": int(bands[i][0]) if bands is not None else None,
            "mid": int(bands[i][1]) if bands is not None else None,
            "high": int(bands[i][2]) if bands is not None else None,
            "vocal": int(round(raw_voc[i])) if raw_voc else None,
        })
    bass = _smooth_flags([(r["low"] or 0) >= BASS_ON for r in rows])
    vocal = _smooth_flags([(r["vocal"] or 0) >= VOCAL_ON for r in rows])
    for r, b, v in zip(rows, bass, vocal):
        r["_bass"], r["_vocal"] = b, v
    res["bars"] = rows

    # contiguous runs of the same (bass, vocal) state, also split at phrase changes
    sections: list[dict] = []
    for r in rows:
        key = (r["_bass"], r["_vocal"], r["phrase"])
        if sections and sections[-1]["_key"] == key:
            sections[-1]["to_bar"] = r["bar"]
        else:
            sections.append({"_key": key, "from_bar": r["bar"], "to_bar": r["bar"], "start": rbcore._fmt_time(r["sec"]),
                             "phrase": r["phrase"], "bass": r["_bass"], "vocal": r["_vocal"]})
    for s in sections:
        s["bars"] = s["to_bar"] - s["from_bar"] + 1
        s["label"] = {(False, False): "no bass, no vocal", (True, False): "bass, no vocal",
                      (False, True): "vocal, no bass", (True, True): "bass + vocal"}[(s["bass"], s["vocal"])]
        del s["_key"]
    res["sections"] = sections
    return res


# ----------------------------------------------------------------------------- planner
def _tempo(bpm_a: float, bpm_b: float) -> tuple[float, float, str]:
    """(pct gap after best half/double ratio, ratio, note)."""
    best = (abs(bpm_b - bpm_a) / bpm_a * 100, 1.0, "")
    for r, note in ((2.0, "B at double time"), (0.5, "B at half time")):
        pct = abs(bpm_b * r - bpm_a) / bpm_a * 100
        if pct < best[0]:
            best = (pct, r, note)
    return best


def _phrase_starts(a: dict) -> set[int]:
    return {int(round(p["start_bar"])) for p in a["phrases"] if p["start_bar"] is not None}


def _on_phrase(bar: int, starts: set[int]) -> bool:
    """A phrase start, or a multiple of 8 bars into a phrase (rekordbox phrases are often 16-32 bars)."""
    prior = [p for p in starts if p <= bar]
    return bool(prior) and (bar - max(prior)) % 8 == 0


def plan_transition(lib: rbcore.Library, from_id: str, to_id: str, overlap_bars: Optional[int] = None,
                    top: int = 3) -> dict[str, Any]:
    ca, cb = lib.content(from_id), lib.content(to_id)
    aa, ab = lib.analysis(ca), lib.analysis(cb)
    ma, mb = bar_map(lib, ca, aa), bar_map(lib, cb, ab)
    A, B = ma["bars"], mb["bars"]
    if not A or not B:
        return {"error": "missing beat grid / analysis", "warnings": ma["warnings"] + mb["warnings"]}
    na, nb = len(A), len(B)
    bpm_a, bpm_b = (ca.BPM or 0) / 100, (cb.BPM or 0) / 100
    key_a = ca.Key.ScaleName if ca.Key else None
    key_b = cb.Key.ScaleName if cb.Key else None
    kscore, kreason = rbcore.key_compatibility(key_a, key_b)
    pct, ratio, tnote = _tempo(bpm_a, bpm_b)
    style = "blend" if pct <= BLEND_MAX_PCT else "cut"
    pa, pb = _phrase_starts(aa), _phrase_starts(ab)
    title_b = cb.Title[:28] if cb.Title else to_id
    title_a = ca.Title[:28] if ca.Title else from_id

    def lvl(rows, i, k):
        return (rows[i][k] or 0) if 0 <= i < len(rows) else 0

    plans = []
    a_from = max(1, int(na * 0.4))
    if style == "blend":
        for L in ([overlap_bars] if overlap_bars else [32, 16, 8]):
            for s in range(a_from, na - L + 2):
                if (s - 1) % 4:
                    continue
                for b in sorted({1} | {x for x in pb if x <= max(1, nb * 0.3)}):
                    if b + L > nb:
                        continue
                    ov = range(L)
                    voc_clash = np.mean([min(lvl(A, s - 1 + i, "vocal"), lvl(B, b - 1 + i, "vocal")) for i in ov]) / 100
                    bass_clash = np.mean([min(lvl(A, s - 1 + i, "low"), lvl(B, b - 1 + i, "low")) for i in ov]) / 100
                    a_on_phrase = _on_phrase(s, pa) or s + L in pa or s + L > na
                    b_on_phrase = _on_phrase(b, pb) or b == 1
                    land = b + L
                    land_phrase = _on_phrase(land, pb)
                    land_bass = lvl(B, land - 1, "low") >= BASS_ON
                    skipped = max(0, na - (s + L) + 1)
                    score = (100 - 70 * voc_clash - 8 * bass_clash + 10 * a_on_phrase + 8 * b_on_phrase
                             + 8 * land_phrase + 6 * land_bass - 0.6 * max(0, skipped - 8) + 4 * kscore + {8: 0, 16: 5, 32: 3}.get(L, 0))
                    swap_off = next((i for i in ov if lvl(B, b - 1 + i, "low") >= BASS_ON), L)
                    swap_off = min(L, max(L // 2, swap_off // 4 * 4)) if swap_off < L else L
                    plans.append({
                        "score": round(float(score), 1), "style": "blend", "overlap_bars": L,
                        "a_start_bar": s, "a_cut_bar": s + L, "b_in_bar": b, "swap_bar_a": s + swap_off,
                        "b_landing_bar": land,
                        "why": {"vocal_clash_pct": round(float(voc_clash) * 100), "bass_clash_pct": round(float(bass_clash) * 100),
                                "a_window_on_phrase": a_on_phrase, "b_in_on_phrase": b_on_phrase,
                                "b_lands_on_phrase": land_phrase, "b_lands_with_bass": land_bass,
                                "a_bars_skipped": skipped},
                    })
    else:
        for c_bar in range(a_from, na + 1):
            if (c_bar - 1) % 4:
                continue
            tail_voc = np.mean([lvl(A, c_bar - 1 - k, "vocal") for k in (1, 2)]) / 100
            at_voc = lvl(A, c_bar - 1, "vocal") / 100
            a_on_phrase = _on_phrase(c_bar, pa) or c_bar > na
            skipped = max(0, na - c_bar + 1)
            for b in sorted({1} | pb):
                if b > nb * 0.6:
                    continue
                land_bass = lvl(B, b - 1, "low") >= BASS_ON
                b_on_phrase = _on_phrase(b, pb) or b == 1
                score = (100 - 35 * tail_voc - 25 * at_voc + 12 * a_on_phrase + 8 * b_on_phrase
                         + 10 * land_bass - 0.6 * max(0, skipped - 8) + 4 * kscore - 0.3 * max(0, b - 1))
                plans.append({
                    "score": round(float(score), 1), "style": "cut", "a_cut_bar": c_bar, "b_in_bar": b,
                    "why": {"a_vocal_in_last_2_bars_pct": round(float(tail_voc) * 100),
                            "a_vocal_at_cut_pct": round(float(at_voc) * 100), "a_cut_on_phrase": a_on_phrase,
                            "b_in_on_phrase": b_on_phrase, "b_enters_with_bass": land_bass, "a_bars_skipped": skipped},
                })

    plans.sort(key=lambda p: -p["score"])
    picked: list[dict] = []
    for p in plans:  # keep the top N that are not near-duplicates of each other
        if all(abs(p["a_cut_bar"] - q["a_cut_bar"]) >= 8 or abs(p["b_in_bar"] - q["b_in_bar"]) >= 4 for q in picked):
            picked.append(p)
        if len(picked) >= top:
            break

    ga, gb = aa["grid"], ab["grid"]

    def t(grid, bar):
        return round(lib.time_at_bar(grid, float(bar)), 3) if bar <= (len(grid["_downbeats"])) else None

    for p in picked:
        cues = []
        if p["style"] == "blend":
            cues += [
                {"track": "A", "track_id": from_id, "bar": p["a_start_bar"], "color": "green",
                 "comment": f"NEXT: {title_b} bar {p['b_in_bar']} in"},
                {"track": "A", "track_id": from_id, "bar": p["swap_bar_a"], "color": "red", "comment": "SWAP bass"},
                {"track": "A", "track_id": from_id, "bar": p["a_cut_bar"], "color": "red",
                 "comment": f"CUT -> {title_b} bar {p['b_landing_bar']}"},
                {"track": "B", "track_id": to_id, "bar": p["b_in_bar"], "color": "green",
                 "comment": f"IN: over {title_a} bar {p['a_start_bar']}"},
            ]
            lo = p["a_start_bar"]
            p["overlap"] = [{"a_bar": lo + i, "b_bar": p["b_in_bar"] + i,
                             "a": f"low {lvl(A, lo - 1 + i, 'low')} voc {lvl(A, lo - 1 + i, 'vocal')}",
                             "b": f"low {lvl(B, p['b_in_bar'] - 1 + i, 'low')} voc {lvl(B, p['b_in_bar'] - 1 + i, 'vocal')}"}
                            for i in range(0, p["overlap_bars"], 4)]
        else:
            cues += [
                {"track": "A", "track_id": from_id, "bar": max(1, p["a_cut_bar"] - 8), "color": "green",
                 "comment": f"NEXT: {title_b} ready, cue bar {p['b_in_bar']}"},
                {"track": "A", "track_id": from_id, "bar": p["a_cut_bar"], "color": "red",
                 "comment": f"ECHO/CUT -> {title_b} bar {p['b_in_bar']}"},
                {"track": "B", "track_id": to_id, "bar": p["b_in_bar"], "color": "green",
                 "comment": f"IN: on {title_a} cut (bar {p['a_cut_bar']})"},
            ]
        for q in cues:
            q["sec"] = t(ga if q["track"] == "A" else gb, q["bar"])
            q["time"] = rbcore._fmt_time(q["sec"]) if q["sec"] is not None else "end of track"
        p["cues"] = cues

    return {
        "a": {"id": from_id, "title": ca.Title, "bpm": bpm_a, "key": key_a, "camelot": rbcore.camelot(key_a), "bars": na},
        "b": {"id": to_id, "title": cb.Title, "bpm": bpm_b, "key": key_b, "camelot": rbcore.camelot(key_b), "bars": nb},
        "tempo_gap_pct": round(pct, 1), "tempo_note": tnote or None,
        "key_match": kreason, "style": style,
        "style_note": ("tempo gap is small enough to beatmatch and blend" if style == "blend" else
                       f"tempo gap {pct:.1f}% is too wide to beatmatch cleanly; plan is an echo-out / cut on a phrase"),
        "plans": picked,
        "warnings": ma["warnings"] + mb["warnings"],
        "legend": "low/vocal are 0-100: low is relative to the track's loudest bass (95th pct); vocal is % of the bar rekordbox marks as voice.",
    }
