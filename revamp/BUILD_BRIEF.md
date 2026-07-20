# Code-Mode Build Brief — Envoy → 3-Tool Code-Mode Surface

> **Read `revamp/REVAMP.md` first** — it holds the full rationale for every decision below.
> This brief maps those decisions onto *this repo's actual code* and defines the build order.
> **Work directly on `main`.** **Repo:** `github.com/sporqist/Embody` (our fork of Embody v6).

---

## Mission

Evolve Envoy from a **53-tool verb catalogue** into a **3-tool code-mode surface**
(`describe` / `code_mode` / `view`) that is more LLM-ergonomic, **while keeping all of
Embody's proven infrastructure** (transport, threading, venv, bridge, docs, TDN, tests).
This is **additive first, consolidation later** — do NOT rip out the 53 tools in step one;
they are load-bearing and covered by 2,171 tests.

Why fork instead of greenfield: the fork already ships ~80% of our target architecture and
solved the hard TD-side problems. Three of our four reconsiderations (R1 stdio bridge, R2 `uv`
deps, R3 pragmatic watchdog) are **already implemented here** — inherited free.

---

## What we INHERIT (reuse, do not rebuild)

All paths under `dev/embody/`. **Line numbers are from a summary — verify against live code.**

| Capability | Where | Reuse for |
|---|---|---|
| MCP server: FastMCP → `streamable_http_app()` → uvicorn on `127.0.0.1:9870/mcp` | `Embody/EnvoyExt.py` (`run()` ~2100, `_register_tools()` ~520) | Register our 3 tools the same way (`@self.mcp.tool()`) |
| Main-thread marshaling: request `Queue` → per-frame `RefreshHook` (`_onRefresh` ~3951, cap 5/frame) → dispatch table (`_execute_operation` ~4168) → response `Queue` → `Event` | `Embody/EnvoyExt.py` (`_execute_in_td` ~452) | Every code-mode op routes through this — DO NOT touch operators off the main thread |
| STDIO↔HTTP bridge + launch/restart TD (R1) | `envoy_bridge.py` | Inherited; no work |
| `uv` venv bootstrap, off-thread (R2) | `Embody/EmbodyExt.py` (`_installDependencies` ~508, dep list ~332) | **Add `beautifulsoup4`** to the dep list |
| Pragmatic watchdog: 30s waiter timeout + socket-liveness revive (R3) | `Embody/EnvoyExt.py` | Inherited; no trace-hook needed |
| Live introspection: `get_td_classes`/`get_td_class_details`/`get_module_help` (`inspect`, `pydoc`) | `Embody/envoy_read.py` (~311/330/372) | Feed `describe(node)` / `describe(docs)` |
| Offline-wiki HTML parsing at `app.samplesFolder + '/Learn/OfflineHelp/https.docs.derivative.ca'` + web fallback, section split | `Embody/EnvoyExt.py` (`_get_docs_roots` ~5089, `_docsHtmlToText` ~2047, `_docsSplitSections` ~2081) | Feed `describe(docs)` prose |
| TDN export/import (sparse, non-default-only YAML) | `Embody/TDNExt.py` (`ExportNetwork` ~1003) | Reuse to produce `describe(network)` sparse dump (R4) |
| TOP capture | `capture_top` tool + handler | Base for `view` TOP-pixels (cap ~480p) |
| Existing code-exec path | `Embody/execute.py`, `execute_python` tool | Base for `code_mode` main-thread exec |
| Test runner (2,171 tests, sandboxed) | `dev/embody/unit_tests/`, `op.unit_tests.RunTests()` | Keep green; add suites per milestone |

---

## What we BUILD (net-new — the philosophy layer)

Full specs in `revamp/REVAMP.md` (D1, D3, D3a, D4). Condensed:

1. **`code_mode`** (D1, D3a) — execute Python on the main thread with:
   - **`tk.` helper namespace** injected (not bare): `tk.make(optype,name,parent,**pars)` (create +
     auto-name + **auto-layout** + pars), `tk.wire(*ops)` (variadic chain), `tk.setp(op,**pars)`,
     `tk.find(pattern,type=None)`, `tk.settle(frames=10)` (advance frames, return diagnostics),
     `tk.errors(target,recurse)`, `tk.checkpoint(name)`, `tk.report(obj)` (structured return channel).
     Native `td` + op classes stay in scope as the substrate.
   - **Fresh globals every call** (no REPL persistence).
   - **Auto-settle after the call**: advance ~10 cook frames, then collect diagnostics that surfaced
     (reuse `get_op_errors` **+ GLSL `_info`/SPIR-V compiler-log scraping** — see REVAMP carry-overs),
     deduped, and return them consolidated so the LLM never has to poll.
   - Consolidated return: stdout + `tk.report()` value + diagnostics + watchdog/abort flag.
2. **`describe`** (D3, D4) — read-only text; **required `mode` + `target`, no smart dispatch**:
   - `contract` — the `tk` API surface (so the model reads the code-mode contract once).
   - `docs` — **FUSE** live introspection (truth) ⊕ offline-wiki prose (understanding); flag when
     wiki default ≠ live default. This fusion is our edge — the fork exposes these separately.
   - `node` — live op: params/values/defaults/is-expression, connections, custom pars.
   - `network` — topology + **optional sparse dump** (R4, via `TDNExt`) for one-read comprehension.
3. **`view`** (D3) — read-only renders:
   - TOP → **inline image**, ~480p default, LLM-tunable up.
   - CHOP/DAT → **structured render** with **reduction** (stats/sampling/selection) + **diff**:
     **relational (op-vs-op, primary)** and temporal (auto-last / pinned baseline).

**Discipline (from R3):** never do UI/editor/viz work in the *same frame* as a mutation — that is
the real main-thread freeze cause (the fork's `create_op` wedge → viz-gates fix). Our old
auto-`home()`-after-execute was exactly this landmine. Keep `code_mode`/`view` mutation-and-read
separated across frames (the settle loop already advances frames).

---

## Integration points (where new code wires in)

- **Register** 3 `@self.mcp.tool()` functions inside `_register_tools()` (`EnvoyExt.py` ~520),
  each forwarding to `self._execute_in_td('<op>', {...})`.
- **Dispatch**: add 3 entries to the handlers dict in `_execute_operation` (`EnvoyExt.py` ~4168),
  each pointing at a wrapper delegating to a new module.
- **New module**: `dev/embody/Embody/envoy_codemode.py` — the `tk` helper layer, the settle loop,
  and the describe/view builders. Follow the existing `mod.envoy_ops` / `mod.execute` pattern.
- **Deps**: add `beautifulsoup4` in `EmbodyExt.py` dep list (~332).

## Fate of the 53 existing tools

**Phase 1 (this branch): additive.** Land `describe`/`code_mode`/`view` alongside the existing 53.
Do not delete or break anything; all existing tests stay green. **Phase 2 (later, separate
decision):** evaluate hiding/consolidating the verb catalogue behind the code-mode surface (e.g. a
server flag to expose only the 3 tools). Not now.

---

## First milestones (ordered; each ends with tests green)

- **M0 — docs landed on `code-mode`** (this brief + `REVAMP.md`). *(done)*
- **M1 — `code_mode` end-to-end**: register tool → `_execute_in_td` → main-thread exec with `tk`
  namespace + auto-settle(10) + consolidated diagnostics + `tk.report()`. New suite
  `test_codemode.py`. Prove the whole path with a create+wire+settle+read in one call.
- **M2 — `tk` helper layer** fleshed out (make/wire/setp/find/checkpoint), auto-layout, error clarity.
- **M3 — `describe`**: contract/node/network first (introspection + TDN sparse dump), then `docs`
  with the introspection⊕wiki **fusion** + default-mismatch flag.
- **M4 — `view`**: TOP inline image (≤480p, tunable) + CHOP/DAT reduction + relational/temporal diff.
- **M5 — consolidation review** (Phase 2 decision on the 53 tools).

## Handoff protocol

You are the repo-local agent. Work directly on `main`. Source of truth = `revamp/REVAMP.md`
(rationale) + this brief (build order). **Verify all line numbers against the live files** before
editing (the table is from a summary and may have drifted). Keep Embody's existing tests green;
add a suite per milestone. Checkpoint after each milestone: what changed, what's verified, what's
left. Fail loud — never report a milestone done if any test was skipped.
