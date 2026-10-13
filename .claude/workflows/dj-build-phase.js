export const meta = {
  name: 'dj-build-phase',
  description: 'Build or rework one dj-setbox phase from its doc: Fable spec, parallel Opus/Sonnet build, test + Haiku sweep, Fable review, Opus fix',
  whenToUse: 'Run with args {phase: 1..5} (optionally {phase, focus: "what to change"}) when a phase doc changes or a phase needs rework.',
  phases: [
    { title: 'Spec', detail: 'dj-architect (Fable) turns the phase doc into work units', model: 'fable' },
    { title: 'Build', detail: 'core units -> dj-engineer (Opus), the rest -> dj-builder (Sonnet), each in its own worktree' },
    { title: 'Verify', detail: 'dj-builder runs pytest; dj-sweeper (Haiku) reads the output' },
    { title: 'Review', detail: 'dj-architect (Fable) reviews against exit criteria + safety invariants', model: 'fable' },
    { title: 'Fix', detail: 'dj-engineer (Opus) fixes confirmed findings, then verify again', model: 'opus' },
  ],
}

const DOCS = {
  1: 'docs/phase-1-track-memory.md',
  2: 'docs/phase-2-audition.md',
  3: 'docs/phase-3-set-optimizer.md',
  4: 'docs/phase-4-learning.md',
  5: 'docs/phase-5-standalone-app.md',
}
const phaseNo = Number((args && args.phase) || 0)
if (!DOCS[phaseNo]) throw new Error('pass args {phase: 1..5}')
const doc = DOCS[phaseNo]
const focus = (args && args.focus) || 'bring the code in line with the doc and its exit criteria'

const RULES = [
  'Hard rules:',
  '- Never open or write the live rekordbox master.db. Tests that write use the live_engine fixture (a temp COPY).',
  '- Applying a plan to the live library is a manual step the owner approves with rekordbox closed. Do not do it.',
  '- No personal library data (real track ids, playlist ids, cue comments, user paths) in tracked files.',
  '- Music maths stays deterministic code; language models only for language jobs (engine/llm.py).',
].join('\n')

const SPEC = {
  type: 'object',
  properties: {
    units: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          kind: { type: 'string', enum: ['core', 'wrapper', 'test', 'ui', 'docs'] },
          files: { type: 'array', items: { type: 'string' } },
          task: { type: 'string' },
          tests: { type: 'array', items: { type: 'string' } },
        },
        required: ['id', 'kind', 'files', 'task'],
      },
    },
    exit_criteria: { type: 'array', items: { type: 'string' } },
  },
  required: ['units', 'exit_criteria'],
}
const RESULT = {
  type: 'object',
  properties: { summary: { type: 'string' }, files_changed: { type: 'array', items: { type: 'string' } }, tests_pass: { type: 'boolean' }, notes: { type: 'string' } },
  required: ['summary', 'tests_pass'],
}
const TESTRUN = {
  type: 'object',
  properties: { summary_line: { type: 'string' }, failures: { type: 'array', items: { type: 'string' } }, skipped: { type: 'array', items: { type: 'string' } }, output_tail: { type: 'string' } },
  required: ['summary_line', 'failures'],
}
const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: { file: { type: 'string' }, problem: { type: 'string' }, failure: { type: 'string' }, severity: { type: 'string', enum: ['high', 'medium', 'low'] } },
        required: ['file', 'problem', 'failure', 'severity'],
      },
    },
  },
  required: ['findings'],
}

phase('Spec')
const spec = await agent(
  `SPEC for phase ${phaseNo}. Read ${doc}, docs/architecture.md and the code it names. Goal: ${focus}.\n` +
  `Return file-level work units (kind core for algorithms/safety paths, otherwise wrapper|test|ui|docs) and the exit criteria.\n` +
  `Keep it to the units actually needed; at most 6.\n${RULES}`,
  { agentType: 'dj-architect', model: 'fable', effort: 'high', schema: SPEC, phase: 'Spec' })
if (!spec || !spec.units.length) { log('Spec returned no work units; nothing to build.'); return { spec } }
const units = spec.units.slice(0, 6)
if (spec.units.length > 6) log(`Spec had ${spec.units.length} units; building the first 6 (rerun with a focus for the rest).`)

phase('Build')
const built = await pipeline(units, u => agent(
  `Implement unit ${u.id} (${u.kind}) of phase ${phaseNo} (${doc}).\nFiles: ${u.files.join(', ')}\nTask: ${u.task}\n` +
  `Tests to add or keep green: ${(u.tests || []).join('; ') || 'as the task implies'}\n` +
  `Run python -m pytest -q before you finish and report honestly. Commit your work on this worktree's branch.\n${RULES}`,
  {
    agentType: u.kind === 'core' ? 'dj-engineer' : 'dj-builder',
    model: u.kind === 'core' ? 'opus' : 'sonnet',
    isolation: 'worktree', schema: RESULT, phase: 'Build', label: `build:${u.id}`,
  }))

async function verify(round) {
  const run = await agent(
    `Merge state check (round ${round}): in the main checkout, run python -m pytest -q, and the library-copy tests if testdb/live or RBMCP_TEST_LIBRARY exists. ` +
    `Return the exact summary line, each failure, each skip reason, and the last 60 lines of output.\n${RULES}`,
    { agentType: 'dj-builder', model: 'sonnet', schema: TESTRUN, phase: 'Verify', label: `pytest:${round}` })
  const sweep = await agent(
    `Sweep this test run and the current git diff for problems (failing/skipped tests, golden mismatches, personal data in tracked files, docs claiming unverified things):\n${JSON.stringify(run)}`,
    { agentType: 'dj-sweeper', model: 'haiku', effort: 'low', phase: 'Verify', label: `sweep:${round}` })
  return { run, sweep }
}

phase('Verify')
log(`Built ${built.filter(Boolean).length}/${units.length} units. Merge the worktree branches you accept before the review reads them.`)
const v1 = await verify(1)

phase('Review')
const review = await agent(
  `REVIEW phase ${phaseNo} against ${doc} exit criteria ${JSON.stringify(spec.exit_criteria)} and the safety invariants in docs/architecture.md.\n` +
  `Build results: ${JSON.stringify(built)}\nTest run: ${JSON.stringify(v1)}\n` +
  `Report only defects with a concrete input and wrong output or a violated invariant.\n${RULES}`,
  { agentType: 'dj-architect', model: 'fable', effort: 'high', schema: FINDINGS, phase: 'Review' })

const todo = ((review && review.findings) || []).filter(f => f.severity !== 'low')
if (!todo.length) return { spec, built, verify: v1, review, fixed: [] }

phase('Fix')
const fixed = await pipeline(todo, f => agent(
  `Fix this confirmed finding in phase ${phaseNo}: ${f.problem} (${f.file}). Failure: ${f.failure}. Add a test that fails before the fix.\n${RULES}`,
  { agentType: 'dj-engineer', model: 'opus', schema: RESULT, phase: 'Fix', label: `fix:${f.file}` }))
const v2 = await verify(2)
return { spec, built, verify: [v1, v2], review, fixed }
