# TD MCP — Revamp Design Notes

> Living doc. We are collecting decisions and their consequences before writing code.
> Status legend: **[DECIDED]** settled · **[OPEN]** needs a call · **[IMPLIED]** follows from a decision, confirm it

---

## Guiding principle

**Ergonomics for the LLM comes first.** The toolkit's job is to hand the model a small,
sharp, self-describing surface and a tight feedback loop — not a catalogue of verbs or a
snapshot of docs that drifts from reality. Fewer tools, one language, live truth.

---

## Prior art: Embody / Envoy (shipping, v6, `../Embody`)

A mature competitor already ships ~80% of our D2/D2a. Studied for context (2026-07-20). It
**validates our transport & threading choices** and gives proven patterns; it **diverges** on
philosophy (53-tool verb catalogue vs our 3-tool code-mode). Key facts (file refs in `../Embody`):

**Validated — patterns we can lift:**
- **Transport = FastMCP (`mcp` SDK) → `streamable_http_app()` → manually-managed `uvicorn.Server`
  on `127.0.0.1:9870/mcp`**, `stateless_http=True`, DNS-rebinding protection on. *Exactly our D2.*
- **Hosted off-main-thread inside TD's Thread Manager palette COMP** (`op.TDResources.ThreadManager`)
  as a standalone `TDTask` — the concrete "listener off-thread" mechanism for D2a.
- **Main-thread marshaling:** request `Queue` (worker→main) drained by the task's per-frame
  `RefreshHook`, **capped 5/frame**; dict-dispatch handlers; response `Queue` → checker thread →
  per-request `threading.Event` the tool thread blocks on (**30 s** timeout). Lift wholesale.
- **Docs (our D4) already built the same way:** live introspection (`inspect.getmembers(td)`,
  `pydoc.render_doc`) for API, **+ offline-wiki HTML parsing at the exact path we found**
  (`app.samplesFolder + '/Learn/OfflineHelp/https.docs.derivative.ca'`) with web fallback,
  section-splitting, HTML→markdown. **So offline docs is NOT a capability differentiator.**

**Reconsiderations — RESOLVED. DECISION: we are FORKING Embody** (`github.com/sporqist/Embody`,
the user's fork). So R1–R3 come **for free** — the fork already implements them:
- **R1 — STDIO bridge → ADOPT (free).** Fork already ships the raw-urllib **stdio↔HTTP bridge**
  (default) + direct-HTTP fallback; bridge serves meta-tools before TD is up and can launch/restart
  TD. Nothing to build — inherited. Supersedes D2's pure-Streamable-HTTP call.
- **R2 — deps via `uv` → ADOPT (free).** Fork already builds a project-local `.venv` with **`uv`**
  (`uv venv --python <TD python>` + `uv pip install mcp>=1.26 attrs<25 pyyaml [pywin32]`),
  off-thread, `stdin=DEVNULL`. Supersedes D2b's TDPyEnvManager. We'll **add `beautifulsoup4`** to
  the dep list for wiki parsing (though the fork already parses wiki HTML — confirm its method).
- **R3 — pragmatic watchdog → ADOPT (free) + discipline.** Fork already uses the pragmatic model
  (30 s waiter timeout + 5-req/frame cap + socket-liveness revive; runaway loops can freeze TD,
  accepted). We drop the trace-based deadline. **Discipline to enforce in our new tools:** never do
  UI/editor/viz work in the *same frame* as a mutation (the fork's `create_op` wedge → "viz gates"
  fix; our old auto-`home()`-after-execute was exactly this landmine).

**Confirmed differentiators (our edges hold):**
- **3-tool code-mode vs 53 schemas** — real context tax; they even added `batch_operations` to
  fight round-trips (partial admission of the problem). Our `code_mode` is strictly more expressive.
- **Docs *fusion*** — they expose introspection and wiki as *separate* tools; our `describe`
  **fuses** live truth + prose in one call (+ flags default mismatches, near-info surfacing).
  Narrower edge than "we have docs," but real: it's the *observability ergonomics*, not the data.
- **`view` relational data diff + reduction** vs their `capture_top` (pixels only) — genuine edge
  for *understanding* data flow.

**TDN (their YAML network format):** sparse, diffable, reconstructable — well-designed for
version-control/round-trip, which is **out of scope for our building-focused goal** (fair to call
ours more streamlined there). But for *comprehending a large existing network* in one read, a
TDN-like structured dump is ergonomic. **R4 — ADOPT [DECIDED]:** `describe(network)` gains an
optional mode emitting a **compact sparse dump** (non-default-only, in TDN's spirit) for
whole-network comprehension in one read — borrow the idea, skip the externalization/VC machinery.
Targeted introspection stays the default. Caveat: a full big network is many tokens (fork sample =
24 KB), so targeted reads can still beat a whole-network dump — the dump is an option, not default.
(The fork already has a TDN exporter in `TDNExt.py` we can reuse to produce the sparse dump.)

---

## Architecture decisions

### D1 — Streaming / long-running execution  **[DECIDED]**

Execution must **not** be a single blocking `exec` snapshot.

- Many TD errors/warnings surface only **after a few cook cycles / samples / frames**
  (GLSL compile logs, CHOP sample-dependent errors, delayed cooks). These MUST be caught
  **before the server responds to the LLM** — the model should never have to "poll again."
- Therefore `code_mode` internally: run code → let TD advance N frames / settle →
  collect diagnostics that appeared during that window → respond with a consolidated report.
- Long-running work needs **streaming / progress**, not just a final blob.

**[IMPLIED / confirm]**
- This wants a transport with progress + streaming (MCP progress notifications, or a
  network transport). Pure request/response stdio can do MCP progress tokens, but streaming
  intermediate output is cleaner over a persistent transport. → see D2 open question.
- **Settle policy [DECIDED]:** **fixed frame count, default 10**, caller-tunable. After running
  the code, advance ~10 cook frames, then collect diagnostics that appeared in that window.
- Need to sample `errors()`/`warnings()` **and** the GLSL/SPIR-V compiler `_info` DATs across
  the window, deduped, since that's exactly the delayed class.

### D2 — Single Python base, hosted in TD, over Streamable HTTP  **[DECIDED]**

Everything lives in **one Python codebase that drops into TD**. No JS-generating-Python.
No Node.js dependency.

- The `.tox` base COMP owns lifecycle controls: **Start / Stop / Autostart** the MCP server.
- All logic (execution, introspection, diagnostics, docs) is Python inside TD — one base to
  ship, one place to reason about.
- **Transport: TD hosts a Streamable-HTTP MCP server in its own Python.** The client connects
  to a URL; the base COMP starts/stops it. This is the modern MCP transport (replaced the old
  HTTP+SSE the previous README dropped) and gives D1's progress/streaming for free.
  Pure stdio is rejected — it can't satisfy "TD autostarts the server," since stdio requires
  the *client* to spawn and own the process.

**[IMPLIED]** MCP Python SDK (FastMCP) as the server framework. Node `td-mcp/` is retired.
Client config becomes a URL, not a spawn command; need a clean "bridge offline" error when TD
or the server isn't up.

**Multi-client + remote [DECIDED].** Serve **multiple clients**, and support **remote** access
(agent on another machine — normal for show/installation rigs where TD runs on a stage box).
Consequences:
- **Binding configurable:** `127.0.0.1` default, `0.0.0.0`/LAN opt-in via a base-COMP par.
- **Auth is mandatory once remote:** a **bearer token** (base-COMP par / generated). Remote +
  full-power `code_mode` = remote code execution, so the token is not optional — no token, no
  remote bind. **[note — security]**
- **Per-client state:** `view` diff baselines and any session state are keyed per client.
- **Primary client to validate against: Claude Code** (Streamable HTTP).

**Autosave [DECIDED].** Base COMP offers an autosave feature (interval par) — periodic
`project.save()` / `Backup/` snapshot, and a save before known-risky ops. Complements the
manual `checkpoint()` helper. Guards against a bad `code_mode` call trashing unsaved work.

#### D2a — Threading model  **[DECIDED, with a hard constraint]**

TD's Python API is **not thread-safe**: operator access, param sets, and cooks must run on the
**main cook thread**.

- **Listener / transport → off the main thread** (dedicated thread or async loop). Keeps the
  socket responsive and lets us **stream progress** to the model ("settling, frame 12/30…")
  while the main thread is busy advancing frames for D1's settle window.
- **`code_mode` execution → marshaled onto the main thread.** Requires a **watchdog /
  time-budget** with a default ceiling (caller-overridable, ~10s default / ~120s max), since
  long main-thread work stalls the TD UI regardless of transport. On breach: abort, return
  partial stdout + diagnostics + a clear "aborted by watchdog" flag. **[DECIDED]**
  - **Enforcement: trace-based coarse deadline [DECIDED].** Off-thread timers can't preempt
    Python holding the GIL on the main thread (a tight `while True` would freeze TD). So run
    user code under a `sys.settrace`/profile hook that checks the deadline every N lines and
    raises to abort. Coarse interval keeps overhead low while still killing runaways.
- **Engine COMP is NOT the default execution path.** An Engine COMP runs a *different* `.tox`
  in a separate process against *its own* network — it can't act on the user's main project.
  Parked as a **future `sandbox` mode** for running risky/generative patches in isolation. **[OPEN — future]**

#### D2b — Dependency delivery: TDPyEnvManager only (target **2025+**)  **[DECIDED]**

**Target 2025+ only.** No 2023 fallback — drop `--target ./Lib` and the version-straddling
code. One path, clean.

TD ships **Python 3.11**. Our server needs the MCP Python SDK (`mcp` / FastMCP) whose tree
includes **compiled wheels** (pydantic-core etc.) — deps must be **cp311 wheels matching TD's
build**, so hand-vendoring pure `.py` files is out.

- **`TDPyEnvManager` (ships in 2025 builds — confirmed present in `Samples/TDPyEnvManager` on
  the dev machine).** Creates a **project-local venv** (`<project>_vEnv`), pip-installs from a
  `requirements.txt`, and **auto-adds it to TD's search path**. **Programmatically drivable**
  via the `TDPyEnvManagerHelper` extension (+ standalone CLI) — the base COMP's first-run
  installs our `requirements.txt` with no terminal. Reproducible, isolated, one-click.
- **Ship a minimal `requirements.txt`.**

**Caveats [note]**
- **ABI/version match:** wheels must be cp311 for TD's exact 3.11 subversion.
- **numpy/opencv shadowing:** TD bundles numpy; the venv isolation handles this — don't force a
  conflicting numpy onto TD's path.

Sources: docs.derivative.ca/Python; TDPyEnvManager announcement (derivative.ca community post).

### D3 — Code-mode: collapse ~29 tools to 2–3  **[DECIDED]**

Throw out the verb catalogue. Model reasons in code, not through pre-baked tools.
Reference: Cloudflare / Grafana "code mode" — here the language is **Python** and the
sandbox is **TD itself**.

**Three tools.** A clean triad: **read-as-text / execute / see-as-pixels.**

| Tool | Role | Notes |
|------|------|-------|
| `describe` | **Read-only, text. One call, required `mode` + `target`.** The knowledge & structure entry point. `mode` is explicit — **no smart dispatch** (a bare name is too ambiguous to auto-route). Modes: `contract` / `docs` / `node` / `network` (see below). | see D4 |
| `code_mode` | **Execute.** Write Python, runs live in TD, with D1 streaming/settling + consolidated diagnostics. The one arbitrary-code path. | see D1 |
| `view` | **Read-only, "renders."** TOP → **inline image** (downscaled). CHOP/DAT → **structured render** (channel/table data), with **reduction options** (don't dump everything) and a **diff function** (op-vs-op + temporal). So the model can *see* both output and data cheaply. | closes the feedback loop with `code_mode` |

So the split is deliberate: **`describe` = structure + docs (text), `view` = renders
(pixels + reduced/diffed data), `code_mode` = change.** Everything the old 29 did
(`td_read_chop`, `td_state`, `td_operators`, `td_get_expressions`, `td_custom_params`,
`td_build_network`, `td_get_preview`, …) folds into these three.

**`describe` shape [DECIDED]:** one tool, **required `mode` + `target`, no smart dispatch.**
Four modes:
- `contract` (no target) — the `code_mode` API surface / helper layer (D3a). Read once before
  writing code. On-demand (not baked into the tool description, which would bloat context).
- `docs` (target = optype / class / expr-function name) — docs, tiered per D4.
- `node` (target = live op path) — one op in depth: params with **live** current values /
  defaults / is-expression, connections, custom pars, extensions.
- `network` (target = path, `depth=N`) — the graph: children, types, wiring. Breadth, not depth.

**Boundary with `view`:** `describe` gives *params + topology + prose* (structure); `view`
gives *actual processed data + pixels*. A CHOP's parameters → `describe(node)`; its channel
samples → `view`.

**`view` — CHOP/DAT renders [DECIDED]:**
- **Reduction options** — never blind-dump. Stats (min/max/mean/std), sampling
  (head/tail/stride/count), channel/row selection. Keeps big CHOPs & DAT tables token-cheap.
- **Diff function — two axes [DECIDED]:**
  - **Relational (primary intent):** diff data **between multiple ops** — e.g. input vs output
    of a chain, or stage A vs stage B — so the model *understands what an operator/chain does
    to the data it processes*. This is the headline feature: teach the model data-flow by
    showing the transformation, not just the endpoints.
  - **Temporal:** compare one target against a prior snapshot (what changed since the last
    view / since a pinned baseline). Server auto-remembers the last `view` per target; optional
    **pinned baseline** for "how far have I drifted from known-good." Bounded: last snapshot per
    target + LRU cap. Diff at the reduced level requested, plus structural diff (added/removed
    channels/rows).
- **[OPEN — future]** extend diff to TOP pixels (perceptual/pixel delta), so visual changes
  are also expressible as "what moved," not just a fresh image.

**`view` — TOP pixels [DECIDED]:**
- Return as an **inline image** (model sees it directly in the loop — not a file path).
- **Default downscale cap ~480p**; the model can **tune resolution up** when it needs detail.
- PNG; downscale happens server-side before the image is returned.

#### D3a — `code_mode` contract  **[DECIDED]**

- **Curated helper layer under a `tk.` namespace [DECIDED].** Native `td` + op classes stay in
  scope (the substrate); the toolkit adds ergonomic primitives under a single `tk` object — so
  zero namespace pollution/collision, and `describe('contract')` = "everything under `tk.*`."
  **Principle:** helpers exist only where native TD is a footgun or the capability doesn't exist
  natively. We do **not** re-wrap `op()`, `.par.x = …`, expressions — native is the substrate.
- **Fresh globals every call [DECIDED].** No REPL-style persistence between `code_mode` calls —
  clean namespace each time. No state leakage; keeps the watchdog/threading story clean. State
  that must persist lives in the TD project itself.
- **Structured return channel [DECIDED]:** inject `tk.report(obj)` so the model returns JSON-able
  data explicitly, separate from captured stdout (`exec` discards the last expression value, so
  without this everything smuggles through `print`).

**Helper set [DECIDED — all three tiers in]:**
- *Tier 1 — new capabilities:* `tk.settle(frames=10) -> diagnostics` (cook mid-call, return
  errors/warnings incl. GLSL `_info`/SPIR-V) · `tk.errors(target='/', recurse=True)` (on-demand
  diagnostics, no cook) · `tk.checkpoint(name=None) -> path` (snapshot to `Backup/`).
- *Tier 2 — footgun removers (the favorites):* `tk.make(optype, name=None, parent=None, **pars)
  -> op` (create + auto-name + **auto-layout** + optional param set) · `tk.wire(*ops)` (variadic
  chain connect, no `outputConnectors[0]`/index footguns).
- *Tier 3 — sugar:* `tk.find(pattern, type=None)` (glob + type over `findChildren`) ·
  `tk.setp(op, **pars)` (batch param set w/ clear errors on unknown pars).
- Plus `tk.report(obj)` (return channel, above).

### D4 — Docs: tiered, **fully offline** (live introspection + local wiki mirror)  **[DECIDED — disk-audited]**

Kill the static `wiki/data/` JSON snapshot. **The full offline wiki DOES ship** — I looked in the
wrong place at first (`Config/Help`); it lives at:

```
<install>/Samples/Learn/OfflineHelp/https.docs.derivative.ca/
```

Audit of `TouchDesigner.2025.33070` — this is a **complete local mirror of docs.derivative.ca**:
- **2,078 `.htm` pages** — every operator (`Noise_TOP.htm`), every Python class
  (`NoiseTOP_Class.htm`), concept pages (`3D_Parenting.htm`) + an `images/` dir. Verified real
  content: `Noise_TOP.htm` (86K) has Summary + parameter tables + param names, not stubs.
- **Predictable filenames** → resolve a page directly from an op type, no search:
  `<Operator>_<Family>.htm` and `<OpType><Family>_Class.htm`.
- **All internal links are local** (`<a href="…htm">`) → wiki-link traversal + near-info
  surfacing work **entirely offline**.
- It's a **per-build snapshot** → matches the running build's era better than a live web fetch.
- Also on disk: `Config/Help/exprhelp` (88K, **304 blocks**) — expression-function reference
  (cleaner than the wiki for those); `Config/Help/command.help` — legacy Tscript, low value.
- Note: no TD `.pyi` stubs ship (the 223 `.pyi` are third-party libs) — irrelevant now that the
  prose is local; structural truth comes from runtime introspection anyway.

So docs are a **tiered, offline-first** strategy:

1. **Live introspection = structural ground truth.** Params, defaults, min/max, menu options,
   connections, class members via the Python API. Matches the running build exactly. **Delivers
   "never guess parameters."** Powers `describe(node)`.
2. **Local wiki mirror = prose/narrative + parameter descriptions.** Parse the `.htm` for
   `describe(docs, <operator>)`: Summary, per-param descriptions, See-Also, concepts. Filename-
   resolved, link-traversable, **no internet**. Stitch operator page ⊕ `_Class` page.
3. **`exprhelp` = expression-language reference.** Parse the 304 blocks for expression functions.
4. **[fallback] Online fetch** only if the local mirror is absent (stripped install) or for
   info newer than the shipped snapshot. No longer the primary prose source.

**[IMPLIED / confirm]**
- HTML parser (`beautifulsoup4`) strips MediaWiki skin/nav chrome, extracts Summary/Parameters/
  See-Also sections → compact markdown. Small text parser for `exprhelp`.
- **Fusion (the "never guess" + "never hallucinate functionality" guarantee):** for a live node,
  params come from introspection (truth) and the wiki supplies the prose/description per param;
  flag when wiki default ≠ live default.
- "Near-info surfacing": bounded top-K via See-Also + category co-membership + local link graph.
- Auto-detect the `Samples/Learn/OfflineHelp/...` path per build; `TDOCS_PATH`-style override.

---

## What gets removed  **[greenfield — DECIDED]**

Clean slate. Drop it all now, reevaluate what to re-import later:
- Node.js `td-mcp/` server, all 29 JS tool files, the `utils/` JS layer.
- Static `wiki/data/` operator/Python JSON knowledge base.
- JS-side Python code generation and all string-escaping-into-Python machinery.
- The verb-catalogue tool surface.
- **Even `internal/shaders/` + `STYLE.md`** — parked, not carried in by default. Reevaluate
  re-importing the shader library / conventions once the new base stands.

## Open questions to resolve next

Design is essentially settled — remaining items are build-time details:
1. **Auth token mechanics** (D2) — generated vs user-set base-COMP par; header scheme.
2. **Online-wiki cache** (D4 tier 3) — cache location, invalidation, offline behavior when no net.
3. **`exprhelp` parser** (D4 tier 2) — map the 304-block text format → structured entries.
4. **First-run UX** — base COMP driving `TDPyEnvManagerHelper`; status readout; Claude Code
   `.mcp.json` (URL + token) snippet generation.
5. **Repo layout** — pure-Python structure once Node is gone (package name, base `.tox` build).

### Resolved
- ~~Transport/hosting~~ → **Streamable HTTP hosted in TD's Python** (D2).
- ~~Multi-client / remote~~ → **multi-client; remote opt-in; 127.0.0.1 default, mandatory bearer token when remote** (D2).
- ~~Threading~~ → **listener off-thread, execution on main thread + watchdog** (D2a).
- ~~Tool count~~ → **3: `describe` / `code_mode` / `view`** (D3).
- ~~Settle policy~~ → **fixed 10 frames, caller-tunable** (D1).
- ~~Watchdog~~ → **trace-based coarse deadline, ~10s default/~120s max; partial return on breach** (D2a).
- ~~`view` scope~~ → **TOP inline image (~480p default, tunable) + CHOP/DAT reduced renders + diff** (D3).
- ~~`view` diff~~ → **relational (op-vs-op, primary) + temporal (auto-last/pinned baseline)** (D3).
- ~~`describe` shape~~ → **one call, required `mode`+`target`, no smart dispatch; modes contract/docs/node/network** (D3).
- ~~`code_mode` contract~~ → **`tk.*` namespace, fresh globals, `tk.report()` return; helpers all 3 tiers** (D3a).
- ~~Docs strategy~~ → **tiered, FULLY OFFLINE: live introspection + local wiki mirror (`Samples/Learn/OfflineHelp/`, 2078 .htm) + `exprhelp`; online only as fallback** (D4).
- ~~Version scope~~ → **2025+ only** (D2b).
- ~~Dependency delivery~~ → **TDPyEnvManager venv, programmatic first-run install** (D2b).
- ~~Autosave~~ → **base-COMP interval autosave + save-before-risky** (D2).
- ~~Carry-overs~~ → **greenfield; shaders/STYLE parked for later reevaluation**.

---

## Carry-overs worth keeping from the current build

- `console.log`-safety mindset (protocol channel hygiene) — re-expressed for the Python server.
- The closed visual loop concept (`execute → preview → see → fix`) — now `code_mode` + `view`.
- `_info`-DAT / SPIR-V compiler-log scraping for GLSL errors — feed it into D1's diagnostics.
