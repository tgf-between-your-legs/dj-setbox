"""Synthetic tracks: rekordbox-shaped analysis arrays with known sections, for tests and CI.

A track is a list of sections (bars, low, mid, high, vocal 0..1). The generator builds a
steady beat grid, a 150 fps 3-band waveform (uint8) and a PVDI-like vocal track (0..4 at
~21.5 fps), then runs the real features.compute on them.
"""
from __future__ import annotations

import numpy as np

from rekordbox_mcp.engine.features import WAVE_FPS, compute

VFPS = 22050 / 1024

# (name, bars, low, mid, high, vocal)
POP = [("Intro", 8, .15, .4, .3, 0), ("Verse 1", 16, .3, .6, .4, .9), ("Bridge", 7, .2, .7, .6, .8),
       ("Break", 1, .02, .3, .5, 0), ("Chorus", 16, .95, .9, .8, .9), ("Verse 2", 16, .35, .6, .4, .9),
       ("Chorus", 16, .95, .9, .8, .9), ("Outro", 16, .3, .4, .3, 0)]
CLUB = [("Intro", 16, .2, .3, .5, 0), ("Up", 16, .3, .6, .7, 0), ("Break", 1, .02, .2, .6, 0),
        ("Chorus", 16, 1.0, .8, .8, 0), ("Down", 16, .15, .5, .5, .6), ("Chorus", 16, 1.0, .8, .8, 0),
        ("Outro", 16, .4, .3, .3, 0)]
VOCAL_CLUB = [(n, b, l, m, h, (.9 if n in ("Chorus", "Down") else v)) for n, b, l, m, h, v in CLUB]


def make(track_id: str, bpm: float, sections=POP, key: str = "Am", camelot: str = "8A", title: str | None = None,
         artist: str = "Synth", offset_beats: int = 0, mood: str = "high", seed: int = 0):
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    n_bars = sum(s[1] for s in sections)
    first = 0.05
    n_beats = n_bars * 4 + 8
    times = first + beat * np.arange(n_beats)
    beats = (np.arange(n_beats) % 4) + 1
    length = float(times[-1] + beat)
    frames = int(length * WAVE_FPS) + 1
    wave = np.zeros((frames, 3)); voc = np.zeros(int(length * VFPS) + 1)
    # sections start `offset_beats` beats after the grid downbeat (simulates a half-bar-off grid)
    cur = times[0] + offset_beats * beat
    phrases = []
    bar_no = 1
    for name, bars, lo, mi, hi, vo in sections:
        t1 = cur + bars * 4 * beat
        a, b = int(cur * WAVE_FPS), int(t1 * WAVE_FPS)
        wave[a:b] = np.array([lo, mi, hi]) * 200 + rng.normal(0, 4, (max(0, b - a), 3))
        va, vb = int(cur * VFPS), int(t1 * VFPS)
        voc[va:vb] = 4 if vo >= .5 else 0
        phrases.append({"name": name, "start": float(bar_no), "bars": bars})
        bar_no += bars; cur = t1
    wave = np.clip(wave, 0, 255).astype(np.uint8)
    return compute(track_id, bpm, times, beats, wave, voc.astype(np.uint8), VFPS, length, phrases,
                   title=title or f"Track {track_id}", artist=artist, key=key, camelot=camelot, mood=mood)


def crate(n: int = 8, start_bpm: float = 120.0, step: float = 2.0):
    keys = [("Am", "8A"), ("Em", "9A"), ("Bm", "10A"), ("F#m", "11A"), ("C#m", "12A"), ("G#m", "1A")]
    out = []
    for i in range(n):
        k, c = keys[i % len(keys)]
        out.append(make(f"t{i + 1}", start_bpm + step * i, [POP, CLUB, VOCAL_CLUB][i % 3], key=k, camelot=c,
                        title=f"Song {i + 1}", seed=i))
    return out
