import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

LIVE_COPY = Path(os.environ.get("RBMCP_TEST_LIBRARY", ROOT / "testdb" / "live"))
GOLDEN_SEED = Path(os.environ.get("RBMCP_GOLDEN_SEED", ROOT / "local" / "legacy" / "seed_v6.json"))


@pytest.fixture
def store(tmp_path):
    from rekordbox_mcp.engine.store import Store
    s = Store(tmp_path / "engine.db")
    yield s
    s.close()


@pytest.fixture
def kpop():
    from rekordbox_mcp.engine import profiles
    return profiles.load("kpop")


@pytest.fixture
def live_engine(tmp_path, monkeypatch):
    """Engine over a throwaway COPY of a real rekordbox library (testdb/live by default).

    Skipped when no copy exists (CI, contributors). Writes go to the copy only; the
    running-rekordbox guard is disabled for the copy.
    """
    if not (LIVE_COPY / "master.db").exists():
        pytest.skip("no library copy at testdb/live (set RBMCP_TEST_LIBRARY)")
    dst = tmp_path / "lib"
    dst.mkdir()
    for name in ("master.db", "master.db-wal", "masterPlaylists6.xml"):
        if (LIVE_COPY / name).exists():
            shutil.copy2(LIVE_COPY / name, dst / name)
    monkeypatch.setenv("REKORDBOX_DB_PATH", str(dst / "master.db"))
    import pyrekordbox.db6.database as dbmod
    from rekordbox_mcp import rbcore
    monkeypatch.setattr(dbmod, "get_rekordbox_pid", lambda *a, **k: 0)
    monkeypatch.setattr(rbcore, "get_rekordbox_pid", lambda *a, **k: 0)
    from rekordbox_mcp.engine.service import Engine
    from rekordbox_mcp.engine.store import Store
    lib = rbcore.Library(db_path=str(dst / "master.db"), backup_dir=str(tmp_path / "backups"))
    e = Engine(lib=lib, store=Store(tmp_path / "engine.db"))
    yield e
    e.lib.close()
    e.store.close()
