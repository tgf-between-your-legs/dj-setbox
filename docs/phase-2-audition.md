# Phase 2: Audition in rekordbox (no audio files)

**Status: built.** `engine/audition.py`, `rbcore.Library.replace_playlist`, run sheet rating buttons.

## Goal

Hear every risky transition on the real decks, quickly, and capture a works / doesn't
verdict per pair, without leaving rekordbox.

## Why

The tool never downloads or decodes audio. Streaming-only libraries have no files to render,
and copying copyrighted audio is out. So the decks are the renderer, and the engine makes
auditioning fast: the right pairs, in the right order, with cues already placed.

## Scope / non-goals

In scope: queue, staging, rating capture (rekordbox hot cues + run sheet), QA flags.
Non-goals: offline rendering or stems. A future `render_transition` could exist only for
files you legally own, and is not planned.

## Design

* `audition_queue(plan)`: every untested transition with its features, QA flags
  (`qa.flags_for`) and a risk score, highest first.
* `stage_audition(plan, pairs|limit, dry_run)`:
  * Writes those tracks' plan cues through the phase 1 ledger path.
  * Fills a playlist named "Audition" with A1, B1, A2, B2 … (one backup, one commit).
  * Returns deck steps for each pair (`techniques.deck_steps`).
* **Rating in rekordbox**: put a hot cue (any slot) within 4 bars of A's OUT DONE.
  Green, lime, sea green or teal = works. Red, orange, pink or magenta = doesn't.
  `collect_ratings` reads them, ignoring ledger cues and default-colour cues, and stores
  each rating with a feature snapshot.
* **Rating on the run sheet**: Works / Doesn't buttons save in the browser. "Copy ratings"
  gives JSON for `import_ratings`.

## Steps (as built)

| Step | Model |
|---|---|
| Spec for the audition and rating loop | Sonnet 5.5 |
| Staging, queue scoring, `collect_ratings` | Opus 5.5 |
| Library-copy tests, run sheet buttons | Sonnet 5.5 |
| Sweep staged output for missing cues and wrong order | Haiku 5.5 |
| Review | Opus 5.5 (no Fable: small, mostly plumbing) |

## Verification (results)

`tests/test_library_copy.py::test_audition_round_trip` on a library copy:
* The queue is sorted by risk.
* A dry run returns deck steps.
* Staging 2 pairs builds a 3-track playlist in order (the shared middle track is collapsed).
* A green hot cue placed at A's OUT DONE is read back as "works" with features.

The web app's rating endpoint has its own test.

## Exit criteria

Stage → play → rate → collect works end to end on a library copy. Met.
Not yet done with a person at real decks.

## Risks

* A rating cue placed far from OUT DONE (more than 4 bars) is missed. The tool says how
  many it read.
* Both colours near one OUT DONE count as a conflict and are reported, not stored.
