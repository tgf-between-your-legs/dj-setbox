# dj-setbox

An MCP server and set-planning engine for **rekordbox 7**. (Python package: `rekordbox_mcp`; MCP server name: `rekordbox`.) It lets Claude (or any MCP
client, the CLI or a local web app) read your library, plan a whole DJ set from rekordbox's
own analysis, and write the plan back as coloured memory cues without touching the cues
you set by hand.

No audio files are needed. Everything comes from what rekordbox already stores for every
analysed track, streamed tracks included:

| Data | Where | Used for |
|---|---|---|
| Beat grid | `PQTZ` (`.DAT`) | bars, cue positions |
| Phrases | `PSSI` (`.EXT`) | intro/verse/chorus labels |
| 3-band waveform | `PWV7` (`.2EX`), 150 frames/s of low/mid/high | bass in/out, drops, breaks |
| Vocal detection | `PVDI` (`.2EX`), 0-4 per hop | vocal stacking, hand-offs |

> Not affiliated with AlphaTheta / Pioneer DJ. rekordbox is their trademark. This tool edits
> `master.db` directly; it refuses to write while rekordbox is running and backs up first,
> but keep your own backups too.

## What it does

1. **Track memory**: per-beat features, drop candidates and grid checks cached in its own
   SQLite store (`data/engine.db`), keyed by rekordbox track id.
2. **Planner**: for a running order, picks where each track lands (its hook or drop),
   where it leaves, blend or cut, and the technique. It never uses the same move twice in a
   row, and it writes green IN / red OUT memory cues.
3. **Safe writes**: a cue ledger records every cue the engine wrote. It only ever deletes
   its own cues; hand-set hot and memory cues are never touched. Each write is one batch:
   one backup, one commit, then a verify pass.
4. **Audition loop**: queues the riskiest transitions and builds an "Audition" playlist
   with deck steps. You rate each pair with a green or red hot cue, or on the run sheet.
5. **Optimizer**: annealing over the order (pins, energy curve, crate subset) plus an exact
   DP over every landing and exit. Genre profiles live in YAML.
6. **Learning**: logistic regression on your ratings and on weak labels from pasted
   tracklists. It only steers planning after it beats the hand-written score on held-out data.
7. **Exports and app**: rekordbox XML, M3U8, Serato crate, JSON, a printable run sheet, and a
   local web UI.

## Install

rekordbox's `master.db` is SQLCipher with a WAL. Run the server with the same OS's Python
that rekordbox runs on (on Windows, a Windows Python, not WSL).

```powershell
git clone https://github.com/tgf-between-your-legs/dj-setbox
cd dj-setbox
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[web,llm,dev]"
```

Register with Claude Code:

```bash
claude mcp add --scope user rekordbox -- <path-to>/.venv/Scripts/python.exe <path-to>/server.py
```

Claude Desktop (`claude_desktop_config.json`):

```json
{"mcpServers": {"rekordbox": {"command": "<path-to>\\.venv\\Scripts\\python.exe",
                              "args": ["<path-to>\\server.py"]}}}
```

Other entry points: `rbmcp --help` (CLI), `setbox-web` (http://127.0.0.1:8765).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `REKORDBOX_DB_PATH` | rekordbox's own | open a different `master.db` (for example a copy) |
| `REKORDBOX_BACKUP_DIR` | `./backups` | where pre-write backups go |
| `RBMCP_ENGINE_DB` | `./data/engine.db` | the engine's own store |
| `RBMCP_PROFILES` | — | extra folder(s) of genre profile YAML |
| `ANTHROPIC_API_KEY` | — | only for the optional language features |
| `RBMCP_MODEL_BULK` / `_STRUCTURE` / `_CRITIQUE` | Haiku / Sonnet / Opus 5.5 | override the model per job |

## A typical session

```text
plan_set(playlist_id)               -> plan_id, every transition explained
render_run_sheet(plan_id)           -> printable HTML with cue bars/times and QA flags
audition_queue(plan_id)             -> riskiest transitions first
stage_audition(plan_id, dry_run=False)   (rekordbox closed) -> "Audition" playlist + deck steps
  ... play each pair, drop a GREEN or RED hot cue near A's OUT DONE ...
collect_ratings(plan_id)
set_landing(track, bar, "ear") / pin_transition(a, b, ...)   fix what you heard
apply_plan(plan_id, dry_run=False)  (rekordbox closed) -> cues written, hand-set cues untouched
```

## Tools

The original library tools: `library_stats`, `search_tracks`, `get_track`, `get_beat_grid`,
`get_bar_map`, `plan_transition`, `list_playlists`, `get_playlist_tracks`,
`suggest_next_tracks`, `hot_cue_colors`, `set_hot_cue`, `set_memory_cue`, `delete_cue`,
`create_playlist`, `add_tracks_to_playlist`, `reorder_playlist`, `remove_tracks_from_playlist`.

The engine tools, grouped by phase:

| Phase | Tools |
|---|---|
| 1 track memory and planner | `analyze_track`, `check_grid`, `find_drops`, `set_landing`, `set_exit`, `set_grid_fix`, `pin_transition`, `score_transition`, `plan_set`, `list_plans`, `diff_plan`, `apply_plan`, `render_run_sheet`, `import_seed` |
| 2 audition | `audition_queue`, `stage_audition`, `collect_ratings`, `import_ratings` |
| 3 optimizer | `optimize_set`, `parse_constraints`, `list_profiles` |
| 4 learning | `train_model`, `import_tracklist`, `critique_plan` |
| 5 export | `export_plan` |

## How rekordbox 7 stores cues (verified on 7.2.18)

* Cues are only in `djmdCue`. ANLZ `PCOB`/`PCO2` tags stay empty until a USB export.
* `Kind`: 0 = memory cue, 4 = memory loop. Hot cues use 1, 2, 3, 5, 6 … 17 (slots 1-16, value 4 skipped).
* `InFrame = InMsec * 150 / 1000`.
* `contentCue` mirrors each track's cue list as JSON. rekordbox keeps it exactly in sync,
  so every write rebuilds it.
* Uncoloured memory cues are drawn **red**. The engine refuses to write them, so its red
  OUT cues stay unambiguous.
* PWV7 bytes are (low, mid, high). This was verified against decoded audio: correlation
  0.61 / 0.62 / 0.73 with energy below 150 Hz / 150 Hz-2.5 kHz / above 2.5 kHz.
* PVDI header after tag/len/len: u32 hop (1024), u16 sample rate (22050), u16, u32 count.
  Then 1 byte per hop: 0 = no voice, 4 = clear voice.

## Development

```bash
python -m pytest            # unit tests run anywhere (synthetic tracks)
```

Integration tests run against a **copy** of a real library. Put `master.db` (and its
`-wal`) in `testdb/live/` or point `RBMCP_TEST_LIBRARY` at a folder. Without a copy they
skip. See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/](docs/README.md).

## Restore

Close rekordbox. Copy the backup the write reported (`backups/PROTECTED-*.db`, plus its
`-wal` if present) over `master.db`, and delete any `master.db-shm`.

## License

MIT. See [LICENSE](LICENSE).
