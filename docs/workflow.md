# Build workflow: one phase, model-routed

`.claude/workflows/dj-build-phase.js` builds or reworks one phase from its doc.

```
Spec (Fable) → Build in parallel (Opus core, Sonnet wrappers/tests/UI/docs, own worktrees)
             → Verify (Sonnet runs pytest + golden; Haiku reads the logs)
             → Review (Fable, adversarial, against the doc's exit criteria)
             → Fix (Opus) → Verify again
```

* Run it from Claude Code with `args: {phase: N}`. It needs explicit opt-in: say
  "run the dj-build-phase workflow for phase N".
* Size: at most about 9 agents per run.
* Hard rules in every prompt:
  * Never touch the live `master.db`. Use the library copy fixture only.
  * The live apply is a manual step you approve, with rekordbox closed.
  * No personal library data in the repo.
* Agents: `.claude/agents/dj-architect.md` (Fable), `dj-engineer.md` (Opus),
  `dj-builder.md` (Sonnet), `dj-sweeper.md` (Haiku). Routing rationale: [models.md](models.md).
