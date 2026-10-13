# Phase 4: Learning

**Status: built; inactive until there's data.** `engine/learn.py`, `engine/tracklist.py`.

## Goal

Replace hand-set cost weights with ones learned from what actually worked for you.

## Design

* **Features**: `qa.FEATURE_NAMES`:
  * tempo gap, key score and clash, blend and overlap
  * both vocals, vocal stack, bass overlap
  * exit and landing strength, landing on a break, flat track
  * play-length deviation, off-phrase exit, BPM delta, technique one-hot
* **Model**: L2 logistic regression in numpy (no new dependency), standardised features,
  sample weights.
* **Evaluation**: k-fold AUC on strong labels only, learned probability vs the hand-written
  risk score.
* **Activation**:
  * A trained model is stored inactive.
  * It becomes active (per profile) only with ≥ 100 strong ratings AND a higher held-out
    AUC than the hand score.
  * Active models add `2 × (0.5 − P(works))` to every candidate exit in the planner and the DP.
* **Tracklists** (the stand-in for mix mining, which would need audio):
  1. Parse `mm:ss Artist - Title` lines.
  2. Match them by normalised string similarity. The Haiku tier matches only the leftovers,
     and only ids from the candidate list are accepted.
  3. Estimate each track's exit from the next timestamp and the grid, snapped to the phrase.
  4. Store consecutive pairs as weak "works" labels (weight 0.2).
  5. Report the median play length (a hint for profile tuning).

## Steps (as built)

| Step | Model |
|---|---|
| Evaluation design and leakage review | Fable 5.1 |
| Tracklist timing estimator | Opus 5.5 |
| Feature code, training, reports | Sonnet 5.5 |
| Tracklist matching and data-quality sweeps | Haiku 5.5 (Batch API capable) |

## Verification (results)

* **Synthetic ratings** with a hidden rule (bass overlap decides): the model's held-out AUC
  beats the hand score, and the model activates.
* **Timing estimator**: timestamps generated from a synthetic plan recover every exit within
  one phrase.
* **Line parsing and matching**: covered by tests.
* **Not yet verified**: anything on real ratings (there are none yet), and a real pro tracklist.

## Exit criteria

Learned weights beat the hand weights on your held-out ratings. Pending data.

## Risks

* Weak labels only say "a pro did this", never "this failed". They carry low weight and are
  excluded from evaluation.
* Leakage: the same pair rated twice in different plans. Folds are by rating row. Group by
  pair if ratings accumulate per pair.
