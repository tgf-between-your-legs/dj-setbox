# Phase 5: Standalone app + exporters

**Status: local web app and exporters built; packaging decision open.**
`webapp/app.py`, `webapp/static/index.html`, `engine/exporters/`.

## Goal

Use the engine without an MCP client, and get plans into other software.

## Design

* **Local web app** (FastAPI, bound to 127.0.0.1, one writer lock):
  * crate picker, profile, opener and closer, a draggable energy curve, a plain-English box
  * Plan / Optimize
  * timeline strip (width = play time, colour = energy)
  * transition cards with flags and Works / Doesn't
  * Diff, two-step Write cues (a dry run first), two-step Audition, Collect ratings, Train,
    Export, Run sheet
* **Exporters** (registry, `fn(ExportSet) -> (filename, bytes)`):
  * `rekordbox_xml`: DJ_PLAYLISTS with `POSITION_MARK` memory cues. Local files only;
    streamed tracks are listed in warnings.
  * `m3u8`
  * `serato_crate`: running order only. Serato stores cues in the audio file's tags,
    which this tool never writes.
  * `json`
* **Not built: Engine DJ export.** Its database is SQLite, but every track must be a local
  file, and there was no Engine DJ install to verify against. Add it as one registered
  exporter when someone can test it.

## Steps (as built)

| Step | Model |
|---|---|
| Architecture | Fable 5.1 |
| Exporters and API | Opus 5.5 |
| UI | Sonnet 5.5 |
| Export round-trip sweeps | Haiku 5.5 |

## Verification (results)

* **API tests** (FastAPI TestClient on a library copy): health, playlists, crate with
  energy, plan, diff, dry-run apply, rating, run sheet, JSON export, JSON errors.
* **Exporter unit tests**: the XML parses back with the right `Location` and colours; the
  Serato crate has the right tags and UTF-16 paths; streamed tracks are skipped with warnings.
* **Not yet verified**: importing the XML into rekordbox, or the crate into Serato, on a real
  install.

## Decision gate (open)

How to ship to people who don't run Python:
* **Browser-only (current)**: `pip install` plus `setbox-web`. Zero extra code, but
  it needs Python.
* **Tauri shell**: a native window around the same web UI with a bundled Python. One
  download, but needs a signing and update story.
* **Recommendation**: stay browser-only until phases 1-3 have been used on real sets. A
  Tauri build doesn't change any engine code, so the choice can wait.

## Risks

Library writes from a web UI. Mitigated by binding to localhost only, a two-step write
with a dry run first, refusing writes while rekordbox is running, and the same ledger path
as MCP.
