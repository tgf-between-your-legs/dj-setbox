"""Genre profiles: every tunable number the planner and optimizer use, per genre.

Built-in profiles live in engine/profiles/*.yaml. Add your own by dropping a YAML file in a
folder named by $RBMCP_PROFILES (same keys; anything you leave out falls back to `kpop`).
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

BUILTIN_DIR = Path(__file__).parent / "profiles"
BASE = "kpop"


def _dirs() -> list[Path]:
    out = []
    env = os.environ.get("RBMCP_PROFILES")
    if env:
        out += [Path(p) for p in env.split(os.pathsep) if p]
    out.append(BUILTIN_DIR)
    return out


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read(name: str) -> dict:
    for d in _dirs():
        p = d / f"{name}.yaml"
        if p.exists():
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raise ValueError(f"unknown profile {name!r}; available: {available()}")


def available() -> list[str]:
    names: set[str] = set()
    for d in _dirs():
        if d.exists():
            names |= {p.stem for p in d.glob("*.yaml")}
    return sorted(names)


def load(name: str = BASE, overrides: dict | None = None) -> dict[str, Any]:
    prof = _read(BASE)
    if name != BASE:
        prof = _merge(prof, _read(name))
    if overrides:
        prof = _merge(prof, overrides)
    validate(prof)
    return prof


def validate(p: dict) -> None:
    from .techniques import TECHNIQUES
    need = {"phrase_bars", "tempo", "play", "closer", "blend", "exit", "landing", "techniques"}
    missing = need - set(p)
    if missing:
        raise ValueError(f"profile {p.get('name')} is missing {sorted(missing)}")
    bad = [t for t in p["techniques"]["allowed"] if t not in TECHNIQUES]
    if bad:
        raise ValueError(f"profile {p.get('name')}: unknown techniques {bad}")
    if not (p["play"]["min_sec"] < p["play"]["target_sec"] < p["play"]["max_sec"]):
        raise ValueError("play: need min_sec < target_sec < max_sec")
    if p["blend"]["overlap_short"] > p["blend"]["overlap_long"]:
        raise ValueError("blend: overlap_short must be <= overlap_long")
