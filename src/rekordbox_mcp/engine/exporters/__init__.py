"""Exporters: turn a saved plan into files other DJ software (or people) can load.

Each exporter is `fn(ExportSet) -> (filename, bytes)` registered in EXPORTERS. Add a format by
writing one function and registering it; nothing else in the engine changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ExportCue:
    sec: float
    color: str            # memory colour name (green, red, ...)
    comment: str


@dataclass
class ExportTrack:
    id: str
    title: str
    artist: str
    bpm: float
    key: Optional[str]
    length_sec: float
    location: Optional[str]     # local file path, or None for streamed tracks
    first_beat_sec: float = 0.0
    cues: list[ExportCue] = field(default_factory=list)


@dataclass
class ExportSet:
    name: str
    tracks: list[ExportTrack]
    warnings: list[str] = field(default_factory=list)


Exporter = Callable[[ExportSet], tuple[str, bytes]]
EXPORTERS: dict[str, Exporter] = {}


def register(name: str):
    def deco(fn: Exporter) -> Exporter:
        EXPORTERS[name] = fn
        return fn
    return deco


from . import formats as _formats  # noqa: E402,F401  (registers the built-in exporters)
