"""Exercise reads + cue writes against a COPY of master.db (never the live one).

Usage: python test_write.py
Bypasses the rekordbox-running guard because it only touches the copy.
"""
import json
import os
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
TESTDIR = HERE / "testdb"
SRC = Path(os.environ["APPDATA"]) / "Pioneer" / "rekordbox"
TESTDIR.mkdir(exist_ok=True)
for name in ("master.db", "master.db-wal", "master.db-shm", "masterPlaylists6.xml"):
    p = SRC / name
    if p.exists():
        shutil.copy2(p, TESTDIR / name)

os.environ["REKORDBOX_DB_PATH"] = str(TESTDIR / "master.db")
os.environ["REKORDBOX_BACKUP_DIR"] = str(TESTDIR / "backups")

import pyrekordbox.db6.database as _dbmod
import rbcore

_dbmod.get_rekordbox_pid = lambda *a, **k: 0          # test-only: writes go to the copy
rbcore.get_rekordbox_pid = lambda *a, **k: 0

lib = rbcore.Library()
print("stats:", json.dumps(lib.stats(), indent=None)[:300])

hits = lib.search("fall in love again")
print("search:", [(h["id"], h["title"], h["bpm"], h["key"], h["camelot"]) for h in hits])
tid = next(h["id"] for h in hits if h["title"].startswith("p1harmony"))
print("other track warnings:", lib.analysis(lib.content(hits[0]["id"]))["warnings"])
c = lib.content(tid)
a = lib.analysis(c)
print("mood/bank:", a["mood"], a["bank"], "bars:", a["grid"]["bars"], "energy:", a["energy_overall"])
for p in a["phrases"][:6]:
    print("  phrase", p["n"], p["name"], p["start"], "bar", p["start_bar"], "len", p["bars"], "energy", p["energy"])
before = lib.cues(c, a["grid"])
print("cues before:", [(q["slot"], q["position"], q["bar"]) for q in before])

# --- write a hot cue in slot P at bar 17 (unlikely to be in use)
r = lib.set_cue(tid, slot="P", bar=17, comment="MCP test", color="orange")
print("set P:", r["slot"], r["position"], "bar", r["bar"], "replaced", r["replaced"])
# --- write again to same slot with replace (position_sec + snap to bar)
r2 = lib.set_cue(tid, slot="P", position_sec=61.0, snap="bar", comment="MCP test 2", color="#ff0000")
print("set P again:", r2["position"], "bar", r2["bar"], "replaced", r2["replaced"])
# --- loop hot cue in slot O, 4 bars
r3 = lib.set_cue(tid, slot="O", bar=33, loop_bars=4, comment="loop test")
print("set O loop:", r3["position"], "->", r3["loop_end_sec"])
# --- memory cue
r4 = lib.set_cue(tid, memory=True, bar=49, color="green", comment="mem test")
print("memory:", r4["slot"], r4["position"])

# --- verify DB state directly
from pyrekordbox.db6 import tables
db = lib.reopen()
rows = db.get_cue(ContentID=tid).all()
p_rows = [q for q in rows if q.Kind == rbcore.slot_to_kind("P")]
assert len(p_rows) == 1, f"expected 1 cue in slot P, got {len(p_rows)}"
q = p_rows[0]
assert q.InFrame == int(q.InMsec * 150 / 1000)
assert q.Comment == "MCP test 2" and q.ColorTableIndex == 42 and q.Color == -1, (q.Comment, q.ColorTableIndex, q.Color)
assert q.usn is None and q.rb_local_usn is None
o_rows = [q for q in rows if q.Kind == rbcore.slot_to_kind("O")]
assert o_rows[0].OutMsec > o_rows[0].InMsec
m_rows = [q for q in rows if q.Kind == 0]
assert m_rows and m_rows[0].Color == 4
cc = db.get_content_cue(ContentID=tid).first()
js = json.loads(cc.Cues)
assert len(js) == len(rows) == cc.rb_cue_count, (len(js), len(rows), cc.rb_cue_count)
assert {e["ID"] for e in js} == {q.ID for q in rows}
cnt = db.get_content(ID=tid)
print("CueUpdated now:", cnt.CueUpdated, "contentCue count:", cc.rb_cue_count, "json sample:", js[-1])
print("local usn now:", db.get_local_usn())

# --- delete
d = lib.delete_cue(tid, slot="P")
d2 = lib.delete_cue(tid, slot="O")
d3 = lib.delete_cue(tid, slot="MEMORY")
db = lib.reopen()
rows = db.get_cue(ContentID=tid).all()
assert not [q for q in rows if q.Kind in (0, 4, rbcore.slot_to_kind("O"), rbcore.slot_to_kind("P"))]
cc = db.get_content_cue(ContentID=tid).first()
orig_op = sum(1 for q in before if q["slot"] in ("O", "P"))
assert len(json.loads(cc.Cues)) == len(rows) == len(before) - orig_op, (len(rows), len(before), orig_op)
print("after delete, cues:", len(rows), "== before:", len(before))

# --- playlist round trip
pl = lib.create_playlist("MCP test playlist")
lib.add_to_playlist(pl["id"], [tid])
pts = lib.playlist_tracks(pl["id"])
assert pts and pts[0]["id"] == tid
lib.remove_from_playlist(pl["id"], [tid])
assert not lib.playlist_tracks(pl["id"])
print("playlist ok:", pl["name"], pl["id"])

# --- suggestions
import server
s = server.suggest_next_tracks.fn(tid, limit=5) if hasattr(server.suggest_next_tracks, "fn") else None
if s:
    print("suggest:", [(x["artist"], x["title"], x["bpm"], x["camelot"], x["key_match"], x["score"]) for x in s["candidates"]])
print("ALL OK")
