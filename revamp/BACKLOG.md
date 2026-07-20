# Code-Mode Backlog / Known Gaps

Findings from the M1 assessment (2026-07-20). Tagged by where they belong.
This is the running list of deliberate simplifications + weak spots so nothing
gets lost between milestones. Update as items land.

## Deferred design decisions (bigger; from REVAMP D1/D2/D2a)

- **[settle] Real-frame deferred settle.** M1's `tk.settle` is synchronous
  force-cook -- it catches compile/cook-time errors (GLSL proven) but NOT
  feedback-loop or movie-reload delayed errors, which need real frames to pass.
  D1 wanted true frame advancement. Needs a deferred settle (return None from
  the handler, chain `run(delayFrames=1)`, push the response via the
  response_queue when done) -- the run_tests deferred pattern is the model.
- **[watchdog] Trace-based deadline (D2a).** NOT built. A `while True:` in
  `code_mode` blocks TD's main thread; the 30s transport timeout frees only the
  waiter, not the thread. Real TD-freeze risk. D2a wanted a `sys.settrace`
  coarse-deadline abort with a partial return + `aborted` flag (currently always
  False). Consider moving this UP -- it is the sharpest safety gap.
- **[streaming] Progress notifications (D1).** `code_mode` returns one final
  blob; no MCP progress/streaming for long settles.
- **[remote/auth] (D2).** Bearer token + configurable bind (0.0.0.0 opt-in) for
  remote code execution. Future.
- **[autosave] Base-COMP interval autosave (D2).** Complements tk.checkpoint.

## M2 scope (tk helper hardening) -- DONE 2026-07-20

- [x] **[docstring] Forward-reference** to describe() softened in the tool help.
- [x] **[tk.make] Default parent** now guards against the bare root and /local
  (clear error), validates optype with a clear "is it a valid operator type?"
  message. Covered by tests.
- [x] **[tk.errors] Richer messages** for unknown optype / off-limits parent.
- [x] **[glsl scrape]** now reuses the glsl op's already-docked `info` DAT
  (`_dockedInfoDat`), only creating a throwaway as a fallback -- no mutation in
  the common read path.
- [x] **[tk.layout]** added (M2's auto-layout goal): forward-flow row,
  grid-snapped; `tk.wire(..., layout=True)` invokes it.

## Test coverage still to add

- `tk.checkpoint` against a REAL TDN-strategy COMP (only the non-TDN no-op path
  is covered).
- `tk.wire` for 3D COMP connectors (camera/geo/light) via the COMP-connector
  fallback.
- Watchdog/timeout behavior (once the watchdog exists).

## Housekeeping

- **[persistence] `project.save()`** to bake the `envoy_codemode` DAT into the
  `.toe` (bumps 6.141 -> 6.142, re-exports the release `.tox`) so a fresh clone
  loads `code_mode`. The M1 commit is source-only by design.
- **[docs] "53 tools" is stale.** `mcp-tools-reference` skill + its template +
  the changelog omit `code_mode`. Update when the surface stabilizes (after M2,
  or at the Phase-2 consolidation decision).
