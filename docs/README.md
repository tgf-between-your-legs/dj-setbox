# dj-setbox roadmap

The engine was built in five phases. Each phase doc follows the same shape: goal · why ·
scope and non-goals · design · steps (each tagged with the model that does it) ·
verification · exit criteria · risks.

| Phase | Doc | Status |
|---|---|---|
| 1 | [Track memory + safe planner](phase-1-track-memory.md) | built; golden test reproduces a 257-cue reference set exactly |
| 2 | [Audition in rekordbox](phase-2-audition.md) | built; round trip verified on a library copy |
| 3 | [Set optimizer + genre profiles](phase-3-set-optimizer.md) | built; DP never scores worse than the planner, pins respected |
| 4 | [Learning](phase-4-learning.md) | built; waiting on real ratings (activates at 100) |
| 5 | [Standalone app + exporters](phase-5-standalone-app.md) | local web app + 4 exporters built; packaging decision open |

Also: [models.md](models.md) (which Claude model does what, and why) ·
[architecture.md](architecture.md) (how it scales, extension points) ·
[workflow.md](workflow.md) (the multi-agent build workflow).

## Dependencies

```
1 track memory ──> 2 audition ──> 4 learning
      │                                ▲
      └──────────> 3 optimizer ────────┘
                         │
                         └──> 5 app (needs 1-3)
```

* Phase 2 needs phase 1's ledger: auditioning writes cues, and those must be deletable later
  without touching yours.
* Phase 4 learns from phase 2's ratings and plugs into the planner and optimizer as an extra cost.
* Phase 5 is a UI and exporters over phases 1-3. It adds no new music logic.

## Definition of done (every phase)

* Unit tests on synthetic tracks pass on any OS (CI).
* Integration tests pass on a copy of a real library (never the live one).
* The phase doc's exit criteria hold. Anything verified only with fakes is marked as such.
