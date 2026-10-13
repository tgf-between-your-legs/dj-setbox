# Phase 1: Track memory + safe planner

**Status: built.** `engine/store.py`, `features.py`, `planner.py`, `qa.py`, `apply.py`,
`runsheet.py`, `service.py`, `tools.py`.

## Goal

Turn one-off planning scripts into tools: plan any playlist from rekordbox's own analysis,
remember what you verified by ear, and write cues without ever destroying hand-set ones.

## Why (lessons from planning a 76-track set by hand)

* **Throwaway scripts were keyed by playlist position.** Inserting 8 songs meant re-keying
  every override. The engine keys everything by rekordbox track id.
* **"Delete all cues, write the plan" removed hand-set hot cues.** The engine keeps a
  ledger and only deletes cues it wrote.
* **Bar-level novelty put cues on the 1-bar break before a drop**, one bar early.
  `find_drops` works beat by beat and reports the break.
* **A grid was half a bar off** (every drop on beat 3). `check_grid` flags it and
  `set_grid_fix` places cues on the real bar.
* **Uncoloured memory cues render red** and look like OUT cues. They are refused.
* **Plan play length first.** Tracks play hook to hook (about 1:00-1:30 in the K-pop
  profile); transitions are fitted around that, not the other way round.

## Scope / non-goals

In scope: features cache, landings, exit/pair/grid pins, the planner, QA flags, the
ledger-safe apply, the run sheet. Not in scope: choosing the order (phase 3) and audio
decoding (never; see phase 2).

## Design

* **Store** (`data/engine.db`, migrations in `MIGRATIONS`): `track_analysis` (cache keyed by
  analysis-file size+mtime), `track_name`, `landing` (source `ear | seed | hint | auto`),
  `exit_pin`, `grid_fix`, `pair_pin` (strict = yours, soft = imported), `plan`,
  `plan_track`, `plan_cue`, `cue_ledger`, `rating`.
* **Features** (`compute` is pure numpy): per-beat (low, mid, high, vocal), per-bar map,
  section-change `strength`, `bass_up`, `is_break`, `change_beat`, `chorus_starts`.
* **Planner** (`plan` is pure): landings (fixed or refined from hints), then exits in
  order. Each exit is searched over bars with `exit_score`:
  * play-length target
  * section strength at the exit
  * phrase alignment
  * no big change mid-blend
  * a blend unless both tracks sing
  Then techniques (pins first, then by fit, no repeats, at most N brakes, one dead stop),
  then cues.
* **Apply**: `diff_plan` → add / delete (ledger only) / adopt (matching cue already there) /
  keep / foreign (left alone). `apply_plan` writes everything with
  `rbcore.Library.write_cues`: one guard, one `PROTECTED-*` backup, one commit, then re-diff
  to verify.

## Steps (as built)

| Step | Model |
|---|---|
| Spec and schema | Fable 5.1 (1 call) |
| `features.py`, `planner.py`, `apply.py`, batch write in `rbcore` | Opus 5.5 |
| `store.py`, MCP wrappers, run sheet port, tests | Sonnet 5.5 |
| Golden-diff check of planner output vs the reference cues | Haiku 5.5 |
| Adversarial review | Fable 5.1 |

## Verification (results)

* **Golden test** (`tests/test_library_copy.py::test_golden_reference_plan`): with a reference
  set's seed (landings, exit pins, soft technique pins, one grid fix), the planner reproduces
  all **257 cues exactly**: same bar, beat, colour and comment on all 76 tracks. Plan rows
  (start, landing, exit, family, technique) are identical too.
* **Feature port**: per-bar rows equal the original bar map on all 76 tracks (0 mismatches).
* **Ledger safety**: on a library copy, `apply_plan` leaves every hand-set hot cue (slot and
  position) unchanged. A hand-made coloured memory cue survives a re-plan. A second apply is a
  no-op and takes no backup.
* **Grid check**: on the reference set it flags the known half-bar-off track (high confidence)
  plus two unverified ones (one labelled "probably a vocal pickup, check by ear").
* Unit tests on synthetic tracks: drop after a 1-bar break, half-bar grid, pins, grid-fix
  placement, profiles.

## Exit criteria

Golden test green · ledger tests green · every MCP tool callable · run sheet renders.
All met.

## Risks

* rekordbox schema changes (`contentCue` mirror, USN). Mitigation: one code path
  (`write_cues`), integration tests on a fresh copy after each rekordbox update.
* Mid-mood pop has weak section changes, so landings are less certain. `qa` flags `b_flat`
  and those go first in the audition queue.
