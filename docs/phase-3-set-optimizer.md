# Phase 3: Set optimizer + genre profiles

**Status: built.** `engine/optimizer.py`, `engine/profiles/*.yaml`, `engine/constraints.py`.

## Goal

Go from a crate to a set: choose the order (or a subset), then every landing and exit,
under constraints like "open on X, peak late, end on drum & bass".

## Design

* **Points (fixed order), exact.**
  * DP over states (track, landing candidate, entered-by-blend).
  * Landing candidates: stored landing, refined hint, top `find_drops`.
  * The transition cost is the planner's own (`exit_score` + cut penalty + learned cost).
    So the DP optimum is never worse than the planner's greedy choice for the same candidates.
* **Order: simulated annealing.**
  * Moves: 2-opt, swap, move, and replace-from-crate (for subsets).
  * Cost: tempo gap (half price when blendable), downward tempo jumps, key compatibility
    (weighted less across cuts), energy drops, deviation from the drawn energy curve,
    optional BPM curve, region preferences ("house in the first third").
  * Pins: opener, closer, fixed positions, adjacent pairs, include and exclude. These are
    soft but huge, so the search can pass through them. Violations are reported.
* **Profiles** (`kpop`, `house`, `dnb`, `trap`; add your own via `RBMCP_PROFILES`):
  * phrase length, blend tempo limit, half/double-time matching
  * play-length window, blend lengths, the vocal-stack threshold
  * landing search, allowed techniques, brake and dead-stop rules
* **Constraints**:
  * One JSON schema (`constraints.SCHEMA`).
  * `parse_constraints` asks the Sonnet tier to fill it from plain English.
  * Validated, and every track reference resolved strictly against the crate.

## Steps (as built)

| Step | Model |
|---|---|
| Formulation and cost design | Fable 5.1 |
| DP and annealing | Opus 5.5 |
| Profiles, NL → constraints, tests | Sonnet 5.5 |
| Batch-evaluate random crates against the energy curve | Haiku 5.5 (reads the reports) |
| Review | Fable 5.1 |

## Verification (results)

* **Synthetic:**
  * DP plan cost ≤ planner plan cost.
  * DP cost equals the re-scored plan (`plan_cost`).
  * Fixed landings and exit pins hold.
  * Order pins, size, include and exclude all hold.
  * Annealing beats a shuffled start.
  * An upward energy curve puts high-energy tracks late.
* **Real tracks** (library copy, 10 tracks): with the order kept, the optimized plan costs
  ≤ the planner's. With a free order, opener and closer pins hold and every track appears once.
* `parse_constraints`: unit-tested with a fake client, and run against the live API (Sonnet 5.5,
  2026-10-13) on a 76-track crate. It returned valid constraints (opener and closer resolved, regions,
  energy and BPM curves) and noted what it couldn't express. That run is why tool calls use
  `tool_choice: auto` plus a retry: the API rejects forced tool choice on these models.

## Exit criteria

On a real crate the optimizer beats the hand plan on the shared cost and respects every
pin. Met for the points DP. Whether the order cost matches taste is unproven until
phase 4 ratings exist.

## Risks

* The pairwise order cost is a proxy. The real per-transition cost only exists after the DP,
  which is too slow to run inside annealing. The weights live in `DEFAULT_W` and can be tuned
  per call.
* Energy is low+mid level at the landing blended with tempo rank. It is crude for genres
  where energy isn't loudness.
