"""Set constraints: a JSON shape the optimizer understands, plus plain English -> that shape.

The model (Sonnet tier) only translates words into this schema. Every track reference is
then resolved against the crate by code, and the result is validated before use, so a
model mistake can't reach the planner.
"""
from __future__ import annotations

import difflib
import json
from typing import Any, Optional

import jsonschema

from . import llm
from .optimizer import OrderConstraints

TRACK_REF = {"type": "string", "description": "track id, or 'title' / 'artist - title' as shown in the crate"}
POINT = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "profile": {"type": "string", "description": "genre profile name, e.g. kpop, house, dnb, trap"},
        "size": {"type": ["integer", "null"], "minimum": 2, "description": "number of tracks to pick from the crate"},
        "minutes": {"type": ["number", "null"], "minimum": 5, "description": "target set length in minutes"},
        "opener": {**TRACK_REF, "type": ["string", "null"]},
        "closer": {**TRACK_REF, "type": ["string", "null"]},
        "fixed": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                             "properties": {"track": TRACK_REF, "position": {"type": "integer", "minimum": 1}},
                                             "required": ["track", "position"]}},
        "adjacent": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                "properties": {"a": TRACK_REF, "b": TRACK_REF}, "required": ["a", "b"]}},
        "exclude": {"type": "array", "items": TRACK_REF},
        "include": {"type": "array", "items": TRACK_REF},
        "energy_curve": {"type": "array", "items": POINT,
                         "description": "[[position 0..1, energy 0..1], ...] e.g. [[0,0.4],[0.7,1],[1,0.6]]"},
        "bpm_curve": {"type": "array", "items": POINT, "description": "[[position 0..1, bpm], ...]"},
        "prefer": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"match": {"type": "object", "additionalProperties": False,
                                     "properties": {"genre": {"type": "string"}, "artist": {"type": "string"},
                                                    "text": {"type": "string"}, "bpm_min": {"type": "number"},
                                                    "bpm_max": {"type": "number"}}},
                           "region": POINT, "weight": {"type": "number", "minimum": 0}},
            "required": ["match", "region"]}},
        "notes": {"type": "string", "description": "anything you could not express in the fields"},
    },
}

SYSTEM = ("You turn a DJ's plain-English request into set constraints. Use only the fields in the tool schema. "
          "Positions run 0 (start) to 1 (end). 'Peak' means roughly 0.6-0.85. Refer to tracks exactly as they "
          "appear in the crate list. If something can't be expressed, put it in notes; never guess track names "
          "that aren't in the crate.")


def validate(d: dict) -> dict:
    jsonschema.validate(d, SCHEMA)
    for k in ("energy_curve",):
        for x, y in d.get(k) or []:
            if not (0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError(f"{k} points must be within 0..1")
    for x0, x1 in [p["region"] for p in d.get("prefer") or []]:
        if not (0 <= x0 <= x1 <= 1):
            raise ValueError("prefer.region must be [start, end] within 0..1")
    return d


def resolve(ref: Optional[str], crate: list[dict]) -> Optional[str]:
    """Track id for a reference (id, title, or 'artist - title'), fuzzy but strict."""
    if ref is None:
        return None
    ref = str(ref).strip()
    ids = {str(t["id"]) for t in crate}
    if ref in ids:
        return ref
    keys = {}
    for t in crate:
        for k in (t.get("title") or "", f"{t.get('artist') or ''} - {t.get('title') or ''}"):
            keys[k.lower()] = str(t["id"])
    if ref.lower() in keys:
        return keys[ref.lower()]
    m = difflib.get_close_matches(ref.lower(), list(keys), n=1, cutoff=0.75)
    if m:
        return keys[m[0]]
    raise ValueError(f"{ref!r} doesn't match any track in the crate")


def _match(t: dict, m: dict) -> bool:
    if "genre" in m and m["genre"].lower() not in (t.get("genre") or "").lower():
        return False
    if "artist" in m and m["artist"].lower() not in (t.get("artist") or "").lower():
        return False
    if "text" in m and m["text"].lower() not in f"{t.get('artist')} {t.get('title')} {t.get('comment') or ''}".lower():
        return False
    if "bpm_min" in m and (t.get("bpm") or 0) < m["bpm_min"]:
        return False
    if "bpm_max" in m and (t.get("bpm") or 0) > m["bpm_max"]:
        return False
    return True


def to_order_constraints(d: dict, crate: list[dict], avg_play_sec: float = 72.0) -> OrderConstraints:
    validate(d)
    size = d.get("size")
    if not size and d.get("minutes"):
        size = max(2, round(d["minutes"] * 60 / avg_play_sec))
    return OrderConstraints(
        opener=resolve(d.get("opener"), crate), closer=resolve(d.get("closer"), crate),
        fixed={resolve(f["track"], crate): int(f["position"]) for f in d.get("fixed") or []},
        adjacent=[(resolve(p["a"], crate), resolve(p["b"], crate)) for p in d.get("adjacent") or []],
        exclude={resolve(x, crate) for x in d.get("exclude") or []},
        include={resolve(x, crate) for x in d.get("include") or []},
        size=size,
        energy_curve=[tuple(p) for p in d.get("energy_curve") or []],
        bpm_curve=[tuple(p) for p in d.get("bpm_curve") or []],
        prefer=[{"ids": [str(t["id"]) for t in crate if _match(t, p["match"])], "region": p["region"],
                 "weight": p.get("weight", 1.0)} for p in d.get("prefer") or []],
    )


def parse(text: str, crate: list[dict], client_: Any = None) -> dict:
    """Plain English -> validated constraints dict (Sonnet tier)."""
    lines = "\n".join(f"{t['id']}\t{t.get('artist') or ''} - {t.get('title') or ''}\t{t.get('bpm')}\t{t.get('genre') or ''}"
                      for t in crate[:600])
    d = llm.tool_call(SYSTEM, f"Crate (id, artist - title, bpm, genre):\n{lines}\n\nRequest: {text}",
                      "set_constraints", SCHEMA, job="structure", client_=client_)
    d = {k: v for k, v in d.items() if v is not None or k in ("opener", "closer", "size", "minutes")}
    validate(d)
    for f in ("opener", "closer"):
        if d.get(f):
            d[f] = resolve(d[f], crate)
    return d


def dumps(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False, indent=1)
