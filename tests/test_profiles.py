import pytest

from rekordbox_mcp.engine import profiles


def test_builtins_load():
    for name in ("kpop", "house", "dnb", "trap"):
        p = profiles.load(name)
        assert p["name"] == name and p["play"]["min_sec"] < p["play"]["max_sec"]


def test_custom_profile_dir(tmp_path, monkeypatch):
    (tmp_path / "mine.yaml").write_text("name: mine\ndescription: x\nplay: {target_sec: 80}\n")
    monkeypatch.setenv("RBMCP_PROFILES", str(tmp_path))
    p = profiles.load("mine")
    assert p["play"]["target_sec"] == 80 and p["play"]["min_sec"] == 50   # rest inherited from kpop
    assert "mine" in profiles.available()


def test_bad_profile(tmp_path, monkeypatch):
    (tmp_path / "bad.yaml").write_text("name: bad\ntechniques: {allowed: [moonwalk]}\n")
    monkeypatch.setenv("RBMCP_PROFILES", str(tmp_path))
    with pytest.raises(ValueError):
        profiles.load("bad")
