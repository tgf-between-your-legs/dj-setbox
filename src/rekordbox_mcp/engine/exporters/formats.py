"""Built-in export formats: rekordbox XML, M3U8, Serato crate, JSON."""
from __future__ import annotations

import json
import re
import struct
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import PureWindowsPath, PurePosixPath

from . import ExportSet, register

RGB = {"pink": (255, 55, 111), "red": (230, 40, 40), "orange": (255, 140, 0), "yellow": (195, 175, 4),
       "green": (40, 226, 20), "aqua": (16, 177, 118), "blue": (48, 90, 255), "purple": (170, 114, 255)}


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "set"


def _file_uri(path: str) -> str:
    p = path.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", p):
        return "file://localhost/" + urllib.parse.quote(p, safe="/:")
    return "file://localhost" + urllib.parse.quote(p, safe="/")


def _is_local(loc: str | None) -> bool:
    return bool(loc) and not re.match(r"^[a-z]+:(?!/|\\)", loc or "") and "://" not in (loc or "")


@register("rekordbox_xml")
def rekordbox_xml(s: ExportSet) -> tuple[str, bytes]:
    """rekordbox's XML bridge format (File > Import Collection / rekordbox xml view).
    Streamed tracks have no file, so they're left out of the XML (listed in warnings)."""
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="dj-setbox", Version="0.2", Company="")
    local = [t for t in s.tracks if _is_local(t.location)]
    for t in s.tracks:
        if t not in local:
            s.warnings.append(f"{t.artist} - {t.title}: streamed track, not in the XML")
    coll = ET.SubElement(root, "COLLECTION", Entries=str(len(local)))
    for i, t in enumerate(local, 1):
        el = ET.SubElement(coll, "TRACK", TrackID=str(i), Name=t.title, Artist=t.artist, TotalTime=str(int(t.length_sec)),
                           AverageBpm=f"{t.bpm:.2f}", Tonality=t.key or "", Location=_file_uri(t.location))
        ET.SubElement(el, "TEMPO", Inizio=f"{t.first_beat_sec:.3f}", Bpm=f"{t.bpm:.2f}", Metro="4/4", Battito="1")
        for c in t.cues:
            r, g, b = RGB.get(c.color, (230, 40, 40))
            ET.SubElement(el, "POSITION_MARK", Name=c.comment, Type="0", Start=f"{c.sec:.3f}", Num="-1",
                          Red=str(r), Green=str(g), Blue=str(b))
    pls = ET.SubElement(root, "PLAYLISTS")
    node = ET.SubElement(pls, "NODE", Type="0", Name="ROOT", Count="1")
    pl = ET.SubElement(node, "NODE", Name=s.name, Type="1", KeyType="0", Entries=str(len(local)))
    for i, _ in enumerate(local, 1):
        ET.SubElement(pl, "TRACK", Key=str(i))
    ET.indent(root)
    return f"{_slug(s.name)}.xml", b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


@register("m3u8")
def m3u8(s: ExportSet) -> tuple[str, bytes]:
    lines = ["#EXTM3U", f"#PLAYLIST:{s.name}"]
    for t in s.tracks:
        lines.append(f"#EXTINF:{int(t.length_sec)},{t.artist} - {t.title}")
        lines.append(t.location if _is_local(t.location) else f"# streamed: {t.id} ({t.location or 'no file'})")
    return f"{_slug(s.name)}.m3u8", ("\n".join(lines) + "\n").encode("utf-8")


def _serato_tag(name: bytes, payload: bytes) -> bytes:
    return name + struct.pack(">I", len(payload)) + payload


@register("serato_crate")
def serato_crate(s: ExportSet) -> tuple[str, bytes]:
    """A Serato .crate (put it in _Serato_/Subcrates). Running order only: Serato keeps cues in the
    audio file's tags, which this tool never writes. Local files only."""
    out = _serato_tag(b"vrsn", "1.0/Serato ScratchLive Crate".encode("utf-16-be"))
    for t in s.tracks:
        if not _is_local(t.location):
            s.warnings.append(f"{t.artist} - {t.title}: streamed track, not in the crate")
            continue
        p = str(PureWindowsPath(t.location)) if re.match(r"^[A-Za-z]:", t.location) else str(PurePosixPath(t.location))
        p = re.sub(r"^[A-Za-z]:\\", "", p).replace("\\", "/").lstrip("/")
        out += _serato_tag(b"otrk", _serato_tag(b"ptrk", p.encode("utf-16-be")))
    return f"{_slug(s.name)}.crate", out


@register("json")
def as_json(s: ExportSet) -> tuple[str, bytes]:
    d = {"name": s.name, "tracks": [{"id": t.id, "title": t.title, "artist": t.artist, "bpm": t.bpm, "key": t.key,
                                     "length_sec": t.length_sec, "location": t.location,
                                     "cues": [{"sec": round(c.sec, 3), "color": c.color, "comment": c.comment} for c in t.cues]}
                                    for t in s.tracks], "warnings": s.warnings}
    return f"{_slug(s.name)}.json", json.dumps(d, ensure_ascii=False, indent=1).encode("utf-8")
