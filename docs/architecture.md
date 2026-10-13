# Architecture

```
            MCP client         rbmcp CLI          web UI (FastAPI)
                 \                 |                  /
                  └──────── engine.service.Engine ────┘
                     /        |          |          \
          rbcore.Library   Store     pure core      llm (optional)
          (rekordbox DB,  (engine.db) features
           ANLZ files)                planner / optimizer / qa / learn
```

## Layers

* **Adapters**: `rbcore.Library` (rekordbox) and `Store` (the engine's own SQLite). These
  are the only modules that do I/O on a library. Everything rekordbox-specific is here.
* **Pure core**: `features.compute`, `planner.plan`, `optimizer.*`, `qa.*`, `learn.fit`.
  These take arrays and dicts and return dicts. No DB, no files, no network. That is why
  most tests run on synthetic tracks on any OS, and why the planner can sit inside an
  optimizer loop.
* **Facade**: `Engine` turns stored pins into planner inputs and persists the results.
  MCP tools, the CLI and the web app are thin layers over it, so a feature is written once.

## Scaling

| Concern | Approach | Current numbers |
|---|---|---|
| Feature extraction | cached per track in `track_analysis`, keyed by analysis-file size+mtime and `ANALYSIS_VERSION`; re-analysing in rekordbox invalidates automatically | ~0.1 s/track cold, ~ms warm (76 tracks: 7.9 s cold) |
| Planning | pure, O(tracks × bars) per exit search | 76 tracks well under a second warm |
| Points DP | O(N × K² × bars) with K ≤ 4 landing candidates | seconds for a 76-track set |
| Order search | O(iters × N) annealing on a precomputed N×N pair matrix | 10 tracks × 3,000 iters in ~1 s; size `iters` to the crate |
| Store | SQLite WAL, one connection per thread, append-only migrations | — |
| Concurrency | rekordbox's DB is single-writer: every write goes through `write_cues` / `replace_playlist` (one guard, one backup, one commit). The web app takes a process lock. | — |
| Runtime LLM cost | none for planning; bulk jobs chunked to stay under Haiku's price step, Batch API for libraries | — |

For very large crates (1,000+ tracks), pre-filter with `search_tracks` / BPM range before
`optimize_set`. The pair matrix is quadratic.

## Extension points

* **Genres**: YAML profiles (`RBMCP_PROFILES`).
* **Export formats**: `@register("name")` in `engine/exporters/`.
* **Techniques**: `engine/techniques.py`.
* **Other DJ software as a source**: implement the small part of `rbcore.Library` the engine
  uses: `content`, `analysis` (grid + phrases), `_anlz_dir` / raw tags (or your own
  `features.compute` inputs), `cues`, `write_cues`, `playlist_tracks`, `replace_playlist`.
  The core never imports pyrekordbox.
* **Learned costs**: anything with the `planner.ExtraCost` signature can be plugged in.

## Safety invariants (tested)

1. Never delete a cue that isn't in the ledger.
2. Never write an uncoloured memory cue.
3. Never write while rekordbox is running (unless the DB is an explicit copy in tests).
4. Every write is preceded by a `PROTECTED-*` backup that rotation never deletes.
5. Every apply is verified by re-diffing.
