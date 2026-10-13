import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from test_library_copy import _playlist  # noqa: E402


@pytest.fixture
def client(live_engine):
    from rekordbox_mcp.webapp import app as web
    web._engine = live_engine
    yield TestClient(web.app)
    web._engine = None


def test_index_and_health(client):
    assert "Set planner" in client.get("/").text
    h = client.get("/api/health").json()
    assert h["ok"] and "rekordbox_running" in h


def test_plan_flow(client, live_engine):
    pl = _playlist(live_engine)
    assert any(p["id"] == pl for p in client.get("/api/playlists").json())
    crate = client.get(f"/api/playlists/{pl}/tracks").json()
    assert crate and "energy" in crate[0]
    p = client.post("/api/plan", json={"playlist_id": pl}).json()
    assert p["plan_id"] and len(p["transitions"]) == len(crate) - 1
    d = client.get(f"/api/plans/{p['plan_id']}/diff").json()
    assert "add" in d["counts"]
    dry = client.post(f"/api/plans/{p['plan_id']}/apply", json={"dry_run": True}).json()
    assert dry["counts"]["add"] == d["counts"]["add"]
    r = client.post("/api/ratings", json={"a": p["transitions"][0]["a_id"], "b": p["transitions"][0]["b_id"],
                                          "works": True, "plan_id": p["plan_id"]})
    assert r.json()["ok"]
    assert client.get(f"/api/plans/{p['plan_id']}").json()["transitions"][0]["rating"] is True
    sheet = client.get(f"/api/plans/{p['plan_id']}/runsheet")
    assert sheet.status_code == 200 and 'class="tx"' in sheet.text
    x = client.get(f"/api/plans/{p['plan_id']}/export/json")
    assert x.status_code == 200 and x.content.startswith(b"{")


def test_errors_are_json(client):
    r = client.get("/api/plans/999999")
    assert r.status_code == 400 and "no plan" in r.json()["error"]
