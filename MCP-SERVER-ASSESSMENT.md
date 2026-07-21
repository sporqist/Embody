# Envoy MCP + TouchDesigner: an agent's field report

Written 2026-07-21 by Claude (Opus 4.8) after an extended build session on a
data-driven magnetosphere visualiser: ~270 operators across two scale stages,
14 GLSL shaders, live NOAA/NASA data ingestion, and a full recovery from a
TouchDesigner crash. Roughly 200 MCP tool calls.

This is written for the people improving the server. It is deliberately blunt
about friction, and specific about what cost me time. Where I hit something
that silently produced *wrong results* rather than an error, I've flagged it,
because those are the expensive ones.

---

## 1. What works genuinely well

**`code_mode` is the single best thing in this toolset.** Being able to run a
block of Python against the live network and return a structured `tk.report(...)`
turns what would be 15 round trips into one. Nearly every diagnostic in this
session — dependency scans, attribute audits, elevation-data probes, layout
verification — was one `code_mode` call. Keep this as the centrepiece. Most of
my suggestions below are about making it *sharper*, not replacing it.

**`capture_top` + reading the image back is the core creative loop.** For visual
work this is the difference between guessing and knowing. The quality heuristics
(`black_frame`, `fully_transparent`, `max_lum`, `std`) are excellent — they
caught several "I think this worked" moments where the frame was actually empty,
and the nudge to load a debug skill is well judged. This is a genuinely
thoughtful piece of design.

**Layout enforcement on `create_op` / `copy_op` / `set_op_position`.** Auto-hugging
docked DATs and auto-positioning removes an entire class of tedious bookkeeping.
The `LAYOUT WARNING` on `execute_python`-created ops is the right call: it catches
the (0,0) pileup that would otherwise ship.

**`get_op_errors` with `recurse=true`** is the fastest way to know if a build is
sound. I used it as a gate after every structural change.

**TDN export/import saved the project.** After the crash, `ImportNetworkFromFile`
restored 185 operators of structure in one call. That capability is the reason
this session had something to recover *to*.

---

## 2. Critical issues (silent data corruption or loss)

### 2.1 Non-ASCII characters are mangled in transit — HIGH SEVERITY

**Any non-ASCII character I send through the MCP layer arrives CP1252-decoded.**
An em dash (U+2014) becomes three characters; `Rₑ` (U+2091) becomes `Râ‚‘`.

This affects **both** `code_mode` source strings **and** `create_annotation`
parameters — so probably the whole transport, not one tool.

I proved TouchDesigner is *not* at fault: I wrote a probe string and dumped its
code points, and the corruption was already present *in the string Python
received*, before it ever reached a TD parameter. TD round-tripped it faithfully.

Why this is critical rather than cosmetic:

- It is **silent**. Nothing errors. The value looks fine in a JSON response until
  you inspect code points or a human notices in the UI.
- It had **already corrupted this project across earlier sessions** — parameter
  labels read `Orbit Distance (Râ‚‘)` and `Moon Distance (Râ‚‘)`. Nobody caught it
  until a user spotted an annotation title, weeks of work later.
- It corrupts **on write**, so it accumulates permanently in saved `.toe` / `.tdn`
  files.

Workaround I now use: build characters inside Python with `chr(0x2014)` — the
source stays ASCII in transit and Python produces the correct character.
`\uXXXX` escapes do **not** help (they get rendered to literal characters before
transmission). Repair is lossless via `s.encode('cp1252').decode('utf-8')`.

A second-order trap worth knowing: my first repair pass matched **zero** strings,
because the mojibake-detection characters *in my own source* were themselves
mangled. Guard characters have to be built with `chr()` too.

**Ask:** fix the encoding end-to-end (UTF-8 all the way). Failing that, validate
and reject/warn on non-ASCII rather than silently corrupting it.

### 2.2 `Embeddatsintdns` defaults to False — DATA-LOSS GRADE

This is an Embody setting rather than strictly MCP, but it caused the worst
event of this project. `/project1` was externalised with the TDN strategy, and
`Embeddatsintdns` was off. Every autosaved `project1.tdn` therefore contained the
complete network *structure* — 185 operators, parameters, wires, positions — and
**zero lines of code**. No shaders, no callbacks, no scripts.

When TouchDesigner crashed, the structure survived and every line of code was
gone. The file header records `include_dat_content: false`, but nothing surfaces
this: the externalisation UI showed a healthy green "Saved" timestamp the whole
time.

Compounding it: DATs inside a TDN-strategy COMP **cannot** be individually
externalised — `applyTagToOperator` refuses them ("DAT tags can only be applied
to supported DAT types"). So the TDN embed is the *only* persistence path for
that project's code, and it was off by default.

> **Correction (verified 2026-07-21, Embody 6.0.144).** This last paragraph is
> mistaken, and the mistake matters because it points at the wrong remedy.
> DATs inside a TDN-strategy COMP **can** be externalised: tagging one live
> inside a `tdn`-tagged COMP succeeds ("Tag 'py' applied"). The refusal quoted
> above is a *type* check, not a TDN-containment rule — `supported_dat_types`
> is `{text, table, execute, parexec, pargroupexec, chopexec, datexec, opexec,
> panelexec}`, and the DAT that was refused would have been an Info DAT
> (`glsl1_info`), which is auto-generated and correctly excluded. A GLSL
> shader's own DATs (`glsl1_pixel`, `glsl1_compute`) are plain `text` DATs and
> were externalisable the whole time; they simply were not tagged.
>
> So the embed toggle was never the only persistence path — and a `.py` on
> disk is the *better* one (diffable, editable, greppable). Fixed in two
> layers: newly-created DATs under a non-embedding `.tdn` are now
> auto-externalised to their own file, and any authored DAT content that still
> has no file is embedded rather than dropped. See changelog v6.0.145.

**Ask:** default this to true, or refuse to report a TDN externalisation as
"saved" when it contains DATs whose content was dropped. A one-line warning
("N DATs excluded — code not saved") would have prevented hours of loss.

### 2.3 TDN round-trip inserts blank rows into table DATs

After the TDN restore, **every** restored table DAT came back with blank rows
interleaved between the real ones. This failed silently and far from the cause:

- `latest` / `baked` → `now_callbacks` threw `float('')`, so the entire space
  weather CHOP had **zero channels** and every downstream shader read 0.
- `sprite_falloff_keys` → the Ramp TOP's key table was garbage, so **every point
  sprite in the project rendered as a hard square**. This had been on the
  project's TODO list for days as "colormap not rounding" — it was this.

**Ask:** strip empty rows on TDN import, or round-trip table DATs losslessly.

### 2.4 Parameter mode silently reverting EXPRESSION -> CONSTANT

I set `Orbitradius` to an expression, verified it evaluated correctly, and later
found it as `ParMode.CONSTANT` holding a stale literal — silently breaking the
control mapping it drove. I could not reproduce it deliberately, and I can't
tell you whether the culprit was a save, the TDN export, or the parameter-binding
work. But it happened, and it is the kind of thing that quietly invalidates
downstream behaviour.

**Ask:** worth investigating. If a save/export path can flatten expressions to
constants, that is a correctness bug.

---

## 3. Ergonomic friction (ranked by time cost)

### 3.1 `settle_frames` runs *after* execution — no read-after-write

This cost me more round trips than anything else. `settle_frames` lets frames
advance *after* the script, but there is no way to say "change this, let the
graph settle, **then** read". So:

```python
top.par.file = 'new.png'
arr = top.numpyArray()   # returns the PREVIOUS texture, silently
```

I hit this hard with Movie File In (documented TD behaviour, but the tool shape
makes it worse) and again with `choptoPOP` attribute changes. Every occurrence
became two or three calls plus a confused debugging detour, because the stale
data *looks* plausible — I nearly concluded a perfectly good 8k elevation dataset
was broken.

**Ask:** a `settle_then` / second-phase block that runs after the settle:

```python
tk.after_settle(lambda: tk.report({'arr': top.numpyArray().mean()}))
```

or simply `code_mode(setup=..., settle_frames=N, report=...)`.

### 3.2 Exceptions abort mid-script, leaving partial mutations

`code_mode` runs to first exception. When the script is mutating the network,
that leaves it half-changed. Deleting 13 operators, my loop died partway because
destroying a GLSL op also destroys its docked DATs, invalidating a reference I
still held. The network was left in a half-deleted state I had to reconcile.

**Ask:** (a) an opt-in continue-on-error mode returning per-statement results;
(b) make `destroy()` on a host return/handle its docked companions, or at least
make stale references raise something catchable and specific rather than a
generic `tdError`.

### 3.3 Performance timings are too noisy to act on

`timing.frameTimeMs` swung non-monotonically across A/B tests: LOD *off* measured
faster than LOD *on*; the magnetosphere fully culled measured *slower* than with
it drawn. Both physically impossible. Per-op `gpuCookTime` is per-cook and varies
with camera angle, so single reads are meaningless.

The result: **I could not honestly report the impact of my own optimisations.**
I had to tell the user "I can't stand behind these numbers", which is the right
answer but a bad outcome for a performance tool.

**Ask:** `get_project_performance(sample_frames=N)` returning mean / median /
p95 / stddev over N frames. Same for `get_op_performance`. This single addition
would move perf work from anecdote to evidence. A scene-freeze helper (pin
time/camera for a fair A/B) would be the perfect companion.

### 3.4 Sequence parameters don't behave like parameters

`op.par.attr = 2` on a `choptoPOP` **silently does nothing**. The real API is
`op.seq.attr.numBlocks = 2`. Reading `op.par.attr` afterwards returns the value
you'd expect from the sequence, so a naive round-trip check *passes* while the
blocks were never created. I lost a debugging cycle to a "successful" write that
had no effect.

**Ask:** make sequence-length parameter writes either work or raise.

### 3.5 `create_op` accepts `node_x` / `node_y` and ignores them

I passed explicit coordinates; the returned op was at completely different ones.
The tool auto-positions, which is good — but silently ignoring supplied
coordinates is worse than either honouring them or rejecting them. I then had to
reposition everything in a follow-up `code_mode` call anyway.

**Ask:** honour them, or drop the parameters from the schema.

### 3.6 Inconsistent parameter access idioms

- `op.par.doesnotexist` raises `tdAttributeError`
- `op.par['doesnotexist']` returns `None`

Both are defensible; having both means every probing script needs the bracket
form, and forgetting it aborts the script (see 3.2). Worth documenting loudly in
the tool description, since agents write a lot of speculative introspection.

### 3.7 Family-name asymmetry between similar operators

Small things that each cost a failed call:

- `glslPOP` has `vecNtype` (vec4 etc.); `glslTOP` does **not**.
- `glslPOP` auto-declares Vectors uniforms; `glslTOP` does **not** — you must
  write `uniform vec4 uCtl;` yourself, or get an undeclared-identifier error.
- `sphereSOP` with `type='poly'` ignores `rows`/`cols` and uses `freq`. I set
  rows=512/cols=1024 and got 9,002 points, wondering why my displacement test
  showed nothing.
- `moviefileinTOP` has no `loadonstart`.

None are server bugs — they're TD surface area. But an agent can't discover them
without failing first. See 4.1.

### 3.8 `findChildren` recurses by default

`findChildren(type=annotateCOMP, includeUtility=True)` from `/project1` also
returned annotations *inside* child COMPs. My "move the annotations" loop
therefore dragged a nested annotation out of its network. I caught it in the same
turn, but a less careful pass would have shipped a broken layout.

Not wrong, but `maxDepth` defaulting to unlimited surprises; worth a prominent
note.

### 3.9 Stock-component errors pollute the error gate

`/project1/stage_global/sun` is a **stock TouchDesigner Light COMP**. Its internal
`merge1..3` / `merge_pointLight` POPs report `No input POP` permanently. Every
single `get_op_errors` call for the rest of the session returned those four,
forever, with no way to acknowledge or filter them.

This degrades the error gate: I had to eyeball and mentally subtract known-noise
every time, which is exactly how a real error eventually gets missed.

**Ask:** a `ignore_paths` / `ignore_known_stock` option, or suppress errors
originating inside unmodified palette/stock component internals.

---

## 4. Missing capabilities I wanted repeatedly

### 4.1 Operator-type parameter introspection without instantiation

There is no "what parameters does a `choptoPOP` have, with types, menus, and
defaults?" I resorted to creating an op and dumping `.pars()` grouped by page —
which works, but means creating and deleting throwaway operators to answer a
documentation question.

`get_td_class_details` is great for Python classes; the equivalent for operator
parameters would be used constantly.

### 4.2 A dependency query

"What references this operator?" I hand-rolled this twice (before deleting the
ground-curtain chain, and when auditing dead nodes) by walking every op, every
par, checking `.expr` and OP-reference values. It's ~15 lines and easy to get
subtly wrong (I nearly missed wired inputs, which don't appear in parameters at
all — a `crossTOP` input is a wire, not a par).

`find_references(op_path)` returning `{parameters: [...], wires: [...], render_lists: [...]}`
would make destructive edits far safer. Deleting operators is exactly when an
agent most wants certainty.

### 4.3 Region / crop capture

`capture_top` always captures the whole TOP. To inspect terrain detail on an 8k
texture I had to fly the *scene camera* to a region — which conflates "inspect
the data" with "change the project state". A `region=[x,y,w,h]` or `zoom` param
would let me verify texture content without touching the scene.

### 4.4 Parameter-mode audit

After discovering the silent EXPRESSION -> CONSTANT revert (2.4), I wanted "show
me every custom parameter in this network whose mode is not what I last set".
I hand-rolled a scan. A built-in drift check would be valuable given 2.4 exists.

### 4.5 Scene state snapshot / restore

For benchmarking and for A/B captures I repeatedly needed "save these N parameter
values, change them, restore them". I hand-rolled it with `store()`/`fetch()`
each time, and once left a camera frozen because I forgot to restore an
expression. A scoped snapshot/restore helper would prevent a real class of
agent-caused mess.

---

## 5. Notes on the human-facing side

Two observations that aren't bugs but shaped the session:

**The project benefited enormously from a control surface, and I'd suggest
Embody encourage this pattern.** Mid-session the user asked for one container
holding the master copy of every hand-tunable parameter, BIND-linked to the real
ones, plus a live table of "every value that differs from its default". On first
cook it surfaced 17 hand-tuned values — including a 45 -> 15.8 degree field of
view that had been silently breaking my framing arithmetic for hours, and sprite
sizes at 4x default that I had misdiagnosed as a rendering bug. Two mysteries
resolved instantly.

Agents cannot see a human's UI interactions. A convention (or generated COMP)
that makes manual state legible is worth more than several new tools.

**Skills-before-acting works.** The project's CLAUDE.md requires loading specific
skills before certain operations. It felt heavy at first and repeatedly paid off:
the POP skill's warning that `Color` must be `float4` (not `Cd`), the layout
rules' docked-DAT formula, the parameter-design help-text requirement. The
`/visual-aesthetics` rubric in particular changed what I built, not just how I
documented it.

---

## 6. Summary of asks, ranked

| # | Ask | Severity | Why |
|---|-----|----------|-----|
| 1 | Fix non-ASCII encoding end-to-end | **Critical** | Silent, permanent, already corrupted this project across sessions |
| 2 | Default `Embeddatsintdns` on, or warn when DATs are dropped | **Critical** | Caused total code loss on crash |
| 3 | Strip blank rows on TDN table import | **High** | Silent failures far from the cause; cost days on a mystery sprite bug |
| 4 | Investigate EXPRESSION -> CONSTANT reverts | **High** | Silently invalidates behaviour |
| 5 | Read-after-settle in `code_mode` | **High** | Biggest single source of wasted round trips |
| 6 | `sample_frames=N` on performance tools | **High** | Perf tooling currently can't support honest claims |
| 7 | `find_references(op_path)` | Medium | Makes destructive edits safe |
| 8 | Sequence-param writes work or raise | Medium | Silent no-op that passes round-trip checks |
| 9 | Continue-on-error / partial results | Medium | Avoids half-mutated networks |
| 10 | Operator-type parameter introspection | Medium | Removes throwaway-op discovery |
| 11 | Filter stock-component errors | Medium | Protects the error gate from noise blindness |
| 12 | `create_op` honour or drop `node_x/y` | Low | Silent ignore is the worst option |
| 13 | Region capture on `capture_top` | Low | Inspect textures without moving the scene |

---

## 7. Closing

The core loop here — write Python against the live network, capture the render,
look at it, iterate — is genuinely good, and better than any TouchDesigner
tooling I'm aware of. Most of what I've listed is polish on a design that is
already right.

The exception is the encoding bug and the TDN DAT-embedding default. Both are
silent, both corrupt or destroy work without surfacing anything, and both had
already damaged this project before anyone noticed. I'd fix those two before
anything else on the list.
