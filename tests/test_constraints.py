import pytest

from rekordbox_mcp.engine import constraints as C

CRATE = [{"id": "1", "title": "Supernova", "artist": "aespa", "bpm": 120, "genre": "K-Pop"},
         {"id": "2", "title": "Magnetic", "artist": "ILLIT", "bpm": 130, "genre": "K-Pop"},
         {"id": "3", "title": "Turn Off The Lights", "artist": "Chris Lorenzo", "bpm": 126, "genre": "House"}]


class FakeBlock:
    type = "tool_use"

    def __init__(self, d):
        self.input = d


class FakeClient:
    def __init__(self, d):
        self.d = d
        self.messages = self
        self.last = None

    def create(self, **kw):
        self.last = kw
        return type("M", (), {"content": [FakeBlock(self.d)]})()


def test_validate_and_convert():
    d = {"opener": "Supernova", "closer": "aespa - Supernova", "adjacent": [{"a": "1", "b": "Magnetic"}],
         "energy_curve": [[0, .3], [1, 1]], "prefer": [{"match": {"genre": "house"}, "region": [0, .3]}], "minutes": 10}
    with pytest.raises(Exception):
        C.validate({"bogus": 1})
    oc = C.to_order_constraints(d, CRATE)
    assert oc.opener == "1" and oc.adjacent == [("1", "2")] and oc.prefer[0]["ids"] == ["3"] and oc.size == 8


def test_unknown_track_is_an_error():
    with pytest.raises(ValueError):
        C.resolve("Some Song Not There", CRATE)


def test_parse_uses_structure_tier_and_validates(monkeypatch):
    fake = FakeClient({"opener": "Magnetic", "energy_curve": [[0, .2], [1, .9]], "closer": None})
    d = C.parse("open on Magnetic and build", CRATE, client_=fake)
    assert d["opener"] == "2" and fake.last["model"] == "claude-sonnet-5-5"
    monkeypatch.setenv("RBMCP_MODEL_STRUCTURE", "my-model")
    C.parse("x", CRATE, client_=fake)
    assert fake.last["model"] == "my-model"


def test_parse_rejects_bad_model_output():
    with pytest.raises(Exception):
        C.parse("x", CRATE, client_=FakeClient({"energy_curve": [[0, 5]]}))
