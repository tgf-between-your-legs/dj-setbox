# Which model does what

Researched 2026-10-12 from Anthropic's models overview
(platform.claude.com/docs/en/about-claude/models/overview) and anthropic.com/claude/fable.

| Model | API id | $ in / out per MTok | Latency | Default effort | Anthropic's positioning |
|---|---|---|---|---|---|
| Fable 5.1 | `claude-fable-5-1` | 10 / 50 | slower | high | demanding reasoning, long-horizon agentic work, ambitious coding, code review |
| Opus 5.5 | `claude-opus-5-5` | 4 / 20 | moderate | medium | recommended default; long-running agentic coding and knowledge work |
| Sonnet 5.5 | `claude-sonnet-5-5` | 2 / 10 | fast | high | best balance of speed and intelligence |
| Haiku 5.5 | `claude-haiku-5-5` | from 0.10 / 0.50 | fastest | medium | high-volume classification, extraction, routing |

* All four have a 1M-token context, 128K output and a June 2026 knowledge cutoff.
* The Batch API is 50% off.
* Haiku's low price applies up to 100K-token prompts. Third-party reports put longer
  prompts at about 5×, so bulk jobs are chunked (`llm.HAIKU_CHUNK = 150` items).

## Rule zero: the music maths is code

Beat analysis, drop detection, scoring, the DP and annealing are deterministic Python.
No model runs per bar or per beat. That makes plans reproducible and testable (the golden
test), cheap, and fast enough for an optimizer loop. Models design, write, review and judge
the code. At runtime they only handle language.

## Building the engine (who writes what)

| Role | Model | Agent file | Used for |
|---|---|---|---|
| Architect | Fable 5.1 | `.claude/agents/dj-architect.md` | the phase spec (1 call) and the adversarial review at the end (1 call). At most 2 calls per phase: slow and expensive, so only where depth pays. |
| Engineer | Opus 5.5 | `.claude/agents/dj-engineer.md` | core algorithms (features, planner, apply/ledger, audition loop, optimizer), fixes after review, orchestration |
| Builder | Sonnet 5.5 | `.claude/agents/dj-builder.md` | MCP wrappers, schema/store, tests, run sheet and web UI, docs, refactors that follow a spec |
| Sweeper | Haiku 5.5 | `.claude/agents/dj-sweeper.md` | reading test logs and diffs for failures, golden-diff checks, consistency sweeps |

Phase 2 skips Fable: it is small and mostly plumbing, so Sonnet writes the spec and Opus reviews.

## Inside the product (runtime)

`src/rekordbox_mcp/engine/llm.py` routes each job to a tier:

| Job | Tier | Default | Why this tier |
|---|---|---|---|
| clean names / tag genre and language for every library track | bulk | Haiku 5.5 (Batch API capable) | thousands of tiny extraction calls; cost dominates |
| match pasted tracklist lines to library tracks | bulk | Haiku 5.5 | fuzzy string matching does most of it; Haiku takes the leftovers |
| plain English -> constraints JSON | structure | Sonnet 5.5 | needs judgment about intent; output is schema-validated |
| critique a full set | critique | Opus 5.5 | rare, long context, quality matters |

Override with `RBMCP_MODEL_BULK`, `RBMCP_MODEL_STRUCTURE`, `RBMCP_MODEL_CRITIQUE`. In
Claude Code, override an agent's model in its frontmatter (`model:`) or per call (Agent `model:`).
Nothing here needs a key unless you use these jobs (`ANTHROPIC_API_KEY`).

## Cost notes

* A phase build is roughly 2 Fable calls, 4-6 Opus agents, 3-5 Sonnet agents and 1-2 Haiku sweeps.
  Fable is about 2.5× Opus per token, so keeping it to spec and review keeps a phase near the
  price of an Opus-only build while getting the deeper review.
* Runtime cost is close to zero for planning (no model calls). Name clean-up of a 5,000-track
  library is about 34 Haiku requests, half price through the Batch API.
