import sqlite3

from rekordbox_mcp.engine.store import MIGRATIONS, Store


def test_migrates_and_reopens(tmp_path):
    p = tmp_path / "e.db"
    s = Store(p); s.close()
    assert sqlite3.connect(p).execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    Store(p).close()   # idempotent


def test_pins_roundtrip(store):
    store.set_landing("1", 33, source="ear")
    store.set_exit_pin("1", 81)
    store.set_grid_fix("1", -2)
    store.set_pair_pin("1", "2", family="cut", technique="echo")
    assert store.landings()["1"]["bar"] == 33
    assert store.exit_pins() == {"1": 81}
    assert store.grid_fixes() == {"1": -2}
    assert store.pair_pins()[("1", "2")]["technique"] == "echo"
    store.set_exit_pin("1", None); store.set_grid_fix("1", 0); store.set_pair_pin("1", "2")
    assert store.exit_pins() == {} and store.grid_fixes() == {} and store.pair_pins() == {}


def test_bad_landing_source(store):
    import pytest
    with pytest.raises(ValueError):
        store.set_landing("1", 3, source="guess")


def test_plan_and_ledger(store):
    pid = store.save_plan(playlist_id="p", name="n", profile="kpop", params={}, summary={"cues": 1},
                          tracks=[dict(pos=1, track_id="1", s=1, land=9, e=33, play_sec=60, info={"title": "x"})],
                          cues=[dict(track_id="1", bar=9, beat=1, color="green", comment="IN")])
    p = store.plan(pid)
    assert p["tracks"][0]["info"]["title"] == "x" and p["cues"][0]["comment"] == "IN"
    store.ledger_add([{"cue_id": "c1", "track_id": "1", "plan_id": pid}])
    assert [r["cue_id"] for r in store.ledger(["1"])] == ["c1"]
    store.ledger_mark_deleted(["c1"])
    assert store.ledger(["1"]) == [] and len(store.ledger(["1"], live_only=False)) == 1
    store.mark_applied(pid, None)
    assert store.plan(pid)["status"] == "applied"


def test_ratings_upsert(store):
    store.add_rating("a", "b", True, "sheet", plan_id=1, features={"x": 1})
    store.add_rating("a", "b", False, "sheet", plan_id=1)
    r = store.ratings()
    assert len(r) == 1 and r[0]["verdict"] == 0


def test_concurrent_open_migrates_once(tmp_path):
    import threading
    p = tmp_path / "race.db"
    errs = []

    def open_it():
        try:
            Store(p).q("SELECT count(*) FROM plan")
        except Exception as e:  # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=open_it) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert errs == []
    assert sqlite3.connect(p).execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_default_paths_sit_next_to_the_checkout(monkeypatch):
    from rekordbox_mcp import paths, rbcore
    from rekordbox_mcp.engine import store as st
    monkeypatch.delenv("RBMCP_ENGINE_DB", raising=False)
    monkeypatch.delenv("REKORDBOX_BACKUP_DIR", raising=False)
    root = paths.home()
    assert (root / "pyproject.toml").exists()
    assert st.default_path() == root / "data" / "engine.db"
    assert rbcore.Library().backup_dir == root / "backups"
