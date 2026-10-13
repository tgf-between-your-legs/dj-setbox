# Contributing

## Ground rules

* **Never write to anyone's live library in a test.** Integration tests copy a library into
  a temp folder (`tests/conftest.py::live_engine`). New tests that write must use that fixture.
* **The engine only deletes cues in its ledger.** Any change to `engine/apply.py` needs a
  test showing hand-set hot cues and foreign memory cues survive (see `tests/test_library_copy.py`).
* **No personal data in the repo.** Track ids, playlists, cue comments and seed files from
  your own library belong in `local/` (git-ignored) or `data/`.
* **Music maths is deterministic code.** Language models only translate words to
  structured data, clean names, match tracklists and write critiques (`engine/llm.py`).

## Layout

```
src/rekordbox_mcp/
  rbcore.py          rekordbox library access + batch cue writes (pyrekordbox)
  barmap.py          per-bar map + single-transition planner (original tools)
  server.py          MCP server (library tools) + engine tool registration
  cli.py             `rbmcp` command line
  engine/
    store.py         engine SQLite store + migrations
    features.py      per-beat features, find_drops, check_grid (pure numpy)
    planner.py       landings, exits, families, techniques, cues (pure)
    qa.py            transition QA flags + feature vector
    apply.py         ledger-safe diff/apply
    audition.py      phase 2 audition loop
    optimizer.py     phase 3 DP + annealing
    constraints.py   constraints schema, NL -> constraints
    learn.py         phase 4 model
    tracklist.py     phase 4 tracklist import
    llm.py           model routing for runtime language jobs
    runsheet.py      HTML run sheet
    exporters/       export formats (registry)
    profiles/*.yaml  genre profiles
    service.py       Engine facade used by MCP, CLI and web
    tools.py         MCP tool definitions
  webapp/            FastAPI + single-page UI
tests/               synthetic + library-copy tests
docs/                roadmap, models, phase docs, architecture
```

## Adding things

* **A genre**: add `engine/profiles/<name>.yaml`. Keys you leave out inherit from `kpop`.
* **An export format**: write `fn(ExportSet) -> (filename, bytes)` and decorate it with
  `@register("name")` in `engine/exporters/`.
* **A technique**: add it to `engine/techniques.py` (`TECHNIQUES` and, if the planner should
  offer it, `options`).
* **A schema change**: append a migration to `engine/store.py::MIGRATIONS`. Never edit an
  existing one.

## Running

```bash
python -m pytest -q
RBMCP_TEST_LIBRARY=/path/to/copy python -m pytest -q tests/test_library_copy.py
```
