# Code-Mode Backlog / Known Gaps

Running list of deferred work + known gaps for the Envoy code-mode surface.
Current as of build v6.0.142 (M1-M5 shipped). Update as items land.

## OPEN -- deferred design decisions (from REVAMP D1/D2/D2a)

- **[watchdog] Trace-based deadline (D2a) -- SHARPEST GAP.** Not built. A
  `while True:` in `code_mode` blocks TD's main thread; the 30s transport
  timeout frees only the waiter, not the thread -- real TD-freeze risk. D2a
  wanted a `sys.settrace` coarse-deadline abort with a partial return + an
  `aborted` flag (currently always False). Highest-value safety item.
- **[settle] Real-frame deferred settle.** `tk.settle` is synchronous
  force-cook -- catches compile/cook-time errors (GLSL proven) but NOT
  feedback-loop or movie-reload delayed errors, which need real frames. Needs a
  deferred settle (handler returns None, chain `run(delayFrames=1)`, push the
  response via the response_queue when done) -- the run_tests deferred pattern
  is the model.
- **[streaming] Progress notifications (D1).** `code_mode` returns one final
  blob; no MCP progress/streaming for long settles.
- **[remote/auth] (D2).** Bearer token + configurable bind (0.0.0.0 opt-in) for
  remote code execution. Future.
- **[autosave] Base-COMP interval autosave (D2).** Complements tk.checkpoint.

## OPEN -- next up

- **[#2 settle] Real-frame settle -- OPT-IN (its own focused pass).** Add an
  opt-in (`real_frames=N` / settle_mode) that DEFERS code_mode across N real
  frames (run(delayFrames=1) chain, deliver the response after) to catch
  feedback/movie-reload delayed errors; keep synchronous force-cook default.
  Deliberately deferred to a focused effort -- it touches the response-delivery
  machinery + the 30s watchdog + per-request state; not rushed at a session tail.
- **[#3 streaming] DEFERRED** until #2 lands (nothing to stream with force-cook).
- **[agent-contract vs codemode default] test_agent_contract expects the full
  56-tool inventory, but the surface now DEFAULTS to codemode (3 tools). The
  tier-1 contract client must SetToolSurface('full') before checking inventory
  (or expect the 3). Agent tier is opt-in / not in normal runs -- fix when next
  touching agent tests.

## OPEN -- accuracy / polish

- **[docs] mcp-tools-reference is stale** -- see #8 above.
- **[codemode-only gap] tk has no externalize.** In `codemode` tool-surface,
  new COMPs from tk.make are NOT auto-externalized (create_op is hidden), so
  file management regresses vs the 53-tool path. Close with a `tk.externalize()`
  helper and/or tk.make honoring the Autoexternalize preference.

## DROPPED

- **[#6 docs default-mismatch flag]** -- dropped. #7 made the LIVE default
  authoritative (fresh-probe eval); parsing wiki defaults is fragile and now
  low-value. Wiki is for understanding, not defaults.

## DONE

- **[#7 describe docs default]** describe(docs) now reports the fresh probe's
  eval() as the authoritative default (fixes the TD `Par.default` menu quirk:
  outTOP filtertype now reports 'nearest', matching a fresh op). Node-side
  non-default detection left consistent with TD/TDN (isDefault agrees).

## OPEN -- test coverage to add

- `tk.checkpoint` against a REAL TDN-strategy COMP (only the non-TDN no-op path
  is covered).
- `tk.wire` for 3D COMP connectors (camera/geo/light) via the COMP-connector
  fallback.
- Watchdog/timeout behavior (once the watchdog exists).
- More per-optype `describe(node)` summarizers (only math/constant/null tuned;
  select/noise/transform/level would sharpen large-network mapping).

## DONE (shipped in v6.0.142)

- M1 code_mode; M2 tk hardening (root/local guard, optype validation, clear
  errors) + tk.layout + docked-GLSL-info reuse.
- M3 describe (contract/node/network/docs, live+wiki fusion).
- M4 view (TOP image, CHOP/DAT reduction, relational + temporal diff) + menu
  transparency (label + options inline).
- Summary-first describe(node) + sequence collapse (op.seq).
- M5 tool-surface flag (SetToolSurface 'full'|'codemode').
- project.save() -> build 6.142 (envoy_codemode baked in); changelog + README.
