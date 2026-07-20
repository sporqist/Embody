"""Envoy code-mode surface (module DAT).

Module DAT (mod.envoy_codemode) called by EnvoyExt on the MAIN THREAD only.
Holds the implementation behind the new `code_mode` MCP tool -- the first of
the three-tool code-mode surface (`describe` / `code_mode` / `view`) described
in revamp/REVAMP.md + revamp/BUILD_BRIEF.md.

`code_mode` executes arbitrary Python live in TD with an ergonomic `tk.`
helper namespace injected on top of the native `td` substrate, then
auto-settles a few cook frames and returns CONSOLIDATED diagnostics (op errors
+ warnings + GLSL shader-compile logs) so the model never has to poll again.

Design contract (D1, D3a in REVAMP):
  - Fresh globals every call -- no REPL persistence between calls.
  - Native `td` + op classes stay in scope as the substrate; the toolkit adds
    ergonomic primitives ONLY where native TD is a footgun (`tk.make`/`tk.wire`)
    or the capability does not exist natively (`tk.settle`/`tk.report`).
  - Auto-settle after the call: force-cook the touched ops a fixed number of
    iterations (default 10), then collect the diagnostics that surfaced.
  - Consolidated return: stdout + `tk.report()` value + diagnostics + timing.

Threading: everything here runs on TD's MAIN thread (EnvoyExt marshals the
request through its per-frame RefreshHook). No module-level TD access; every
function reaches TD through the `ext` instance or the TD globals (op, ops,
root, ...) available inside function bodies at call time.

This surface reuses Embody/Envoy infrastructure (transport, main-thread
marshaling, get_op_errors, the layout helpers) -- see the repo NOTICE file for
attribution (MIT, (c) The Experiential Company, LLC).

NOTE (settle semantics): a code_mode call runs synchronously inside one
main-thread dispatch, so it CANNOT let real frames advance while it holds the
thread -- `tk.settle(frames=N)` force-cooks the touched ops N times instead.
Force-cook triggers shader compilation and surfaces the delayed error/warning
classes (GLSL, sample-dependent) that this feature exists to catch. It does
NOT advance feedback loops or movie-file reloads (those need real frames); a
later milestone may add a deferred real-frame settle. See td-python.md
(Cook Model) for why real-frame advancement is impossible on the held thread.
"""

from __future__ import annotations

import io
import contextlib
import time
import traceback


# =============================================================================
# tk -- the curated helper namespace injected into code_mode
# =============================================================================

class _Toolkit:
    """The `tk` object injected into every code_mode namespace.

    One instance per code_mode call (fresh state, no persistence). Native TD
    is the substrate -- these helpers exist only where native TD is a footgun
    (`make`/`wire`), or the capability does not exist natively (`settle`,
    `errors`, `report`, `checkpoint`).
    """

    def __init__(self, ext):
        self._ext = ext
        self._created = []      # ops this call created via tk.make (paths)
        self._watched = []      # ops to include in settle/diagnostics (ops)
        self._report = None     # structured return channel payload
        self._reported = False

    # ---- helpers -------------------------------------------------------------

    def _resolve(self, target, what='operator'):
        """Coerce an op / path-string into a live op, or raise clearly."""
        if target is None:
            raise ValueError(f'{what} is None')
        if isinstance(target, str):
            resolved = op(target)
            if resolved is None:
                raise ValueError(f'{what} not found: {target}')
            return resolved
        # Assume it is already an OP; validate.
        if not getattr(target, 'valid', False):
            raise ValueError(f'{what} is not a valid operator: {target!r}')
        return target

    def _watch(self, oper):
        if oper is not None and oper not in self._watched:
            self._watched.append(oper)

    # ---- Tier 2: footgun removers -------------------------------------------

    def make(self, optype, name=None, parent=None, **pars):
        """Create an operator, auto-position it clear of siblings, hug its
        docked companions, and optionally set parameters -- in one call.

        Args:
            optype: Operator type string ('noiseTOP', 'baseCOMP', ...) or the
                TD type object (noiseTOP).
            name:   Optional name; TD auto-names when omitted.
            parent: Parent COMP as an op or path string. Defaults to the
                container that holds the Embody COMP (the project's consistency
                anchor) when omitted.
            **pars: Parameter constants to set on the new op (see tk.setp).

        Returns: the created op. Tracked for auto-settle + diagnostics.

        Unlike the create_op MCP tool, tk.make does NOT auto-externalize -- it
        is a raw building primitive; externalize explicitly when you want a
        file on disk.
        """
        if parent is None:
            try:
                parent_comp = op.Embody.parent()
            except Exception:
                parent_comp = None
            if parent_comp is None:
                raise ValueError(
                    'tk.make: no parent given and op.Embody.parent() could not '
                    'be resolved -- pass parent=<comp or path>')
        else:
            parent_comp = self._resolve(parent, 'parent')
        if not hasattr(parent_comp, 'create'):
            raise ValueError(
                f'tk.make: parent {parent_comp.path} is not a COMP')
        # Never build at the bare root or in volatile /local (CLAUDE.md rule):
        # /local is not saved with the .toe, and the root '/' is not a home.
        ppath = parent_comp.path
        if ppath == '/' or ppath == '/local' or ppath.startswith('/local/'):
            raise ValueError(
                f'tk.make: refusing to create in {ppath} -- the bare root and '
                '/local are off-limits (/local is not saved with the .toe). '
                'Pass parent=<a real COMP>.')

        optype_name = (optype if isinstance(optype, str)
                       else getattr(optype, '__name__', str(optype)))
        try:
            new_op = (parent_comp.create(optype, name) if name
                      else parent_comp.create(optype))
        except Exception as e:
            raise ValueError(
                f'tk.make: could not create a {optype_name!r} in '
                f'{ppath} -- {e} (is {optype_name!r} a valid operator type?)')
        # Reuse Envoy's proven layout helpers (position clear of real siblings,
        # then hug any docked callback/shader/info DATs below the host).
        try:
            self._ext._find_non_overlapping_position(parent_comp, new_op)
            self._ext._placeDockedOps(new_op)
        except Exception as e:
            self._ext._log(f'tk.make layout for {new_op.path} failed: {e}',
                           'WARNING')
        if pars:
            self.setp(new_op, **pars)
        self._created.append(new_op.path)
        self._watch(new_op)
        return new_op

    def wire(self, *targets, source_index=0, dest_index=0, layout=False):
        """Chain-connect operators left to right in one call.

        tk.wire(a, b, c) connects a -> b -> c using each op's PRIMARY
        connectors, removing the outputConnectors[0]/inputConnectors[0] index
        footguns. Accepts ops or path strings. COMP-only operators (no
        data-flow connectors) fall back to their COMP connectors.

        layout=True also arranges the chain left-to-right (see tk.layout) so the
        wires read as forward flow.

        Returns a list of (source_path, dest_path) tuples actually connected.
        Raises a clear error if a pair cannot be wired.
        """
        ops = [self._resolve(t, 'wire target') for t in targets]
        if len(ops) < 2:
            raise ValueError('tk.wire needs at least two operators')
        made = []
        for src, dst in zip(ops, ops[1:]):
            self._connectPair(src, dst, source_index, dest_index)
            made.append((src.path, dst.path))
            self._watch(src)
            self._watch(dst)
        if layout:
            self.layout(*ops)
        return made

    def layout(self, *targets, dx_gap=200, row_y=None):
        """Arrange operators left-to-right in one row so a chain reads as
        forward flow (each op's right edge left of the next op's left edge).

        Positions are anchored at the FIRST op's current spot (or `row_y` for
        the shared Y). Each step = that op's nodeWidth + dx_gap, snapped up to
        the 200-unit grid (network-layout.md). Docked companions are re-hugged
        after each move. Accepts ops or path strings; returns the ops.
        """
        ops = [self._resolve(t, 'layout target') for t in targets]
        if not ops:
            return ops
        x = ops[0].nodeX
        y = ops[0].nodeY if row_y is None else row_y
        for o in ops:
            o.nodeX = x
            o.nodeY = y
            step = int(o.nodeWidth) + int(dx_gap)
            step = ((step + 199) // 200) * 200      # snap up to the 200 grid
            x += step
            try:
                self._ext._placeDockedOps(o)         # keep docks hugging
            except Exception:
                pass
            self._watch(o)
        return ops

    def _connectPair(self, src, dst, source_index, dest_index):
        out_conns = getattr(src, 'outputConnectors', None)
        in_conns = getattr(dst, 'inputConnectors', None)
        if out_conns and in_conns:
            if source_index >= len(out_conns):
                raise ValueError(
                    f'tk.wire: {src.path} has no output connector '
                    f'{source_index}')
            if dest_index >= len(in_conns):
                raise ValueError(
                    f'tk.wire: {dst.path} has no input connector {dest_index}')
            out_conns[source_index].connect(in_conns[dest_index])
            return
        # Fallback: COMP-family connectors (3D COMP wiring).
        out_comp = getattr(src, 'outputCOMPConnectors', None)
        in_comp = getattr(dst, 'inputCOMPConnectors', None)
        if out_comp and in_comp:
            out_comp[source_index].connect(in_comp[dest_index])
            return
        raise ValueError(
            f'tk.wire: cannot connect {src.path} -> {dst.path} '
            f'(no compatible connectors)')

    def setp(self, target, **pars):
        """Batch-set parameter constants on an op, with a clear error listing
        any unknown parameter names (native `setattr` on a typo fails silently
        or cryptically).

        Sets CONSTANT values. For expressions/bind, use native
        `op.par.X.expr = ...` on the substrate.

        Returns the op.
        """
        oper = self._resolve(target)
        unknown = [k for k in pars if not hasattr(oper.par, k)]
        if unknown:
            raise ValueError(
                f'tk.setp: unknown parameter(s) on {oper.path}: '
                f'{", ".join(sorted(unknown))}')
        for k, v in pars.items():
            setattr(oper.par, k, v)
        self._watch(oper)
        return oper

    # ---- Tier 3: sugar -------------------------------------------------------

    def find(self, pattern, type=None, parent=None, depth=None):
        """Glob operators by name (and optionally type) under a scope.

        Args:
            pattern: Name glob ('noise*', '*out*').
            type:    Optional TD op-type string ('noiseTOP') or class to filter.
            parent:  Scope op or path (defaults to root).
            depth:   Max search depth (defaults to unlimited).

        Returns a list of matching ops.
        """
        scope = root if parent is None else self._resolve(parent, 'find scope')
        kwargs = {}
        if depth is not None:
            kwargs['maxDepth'] = depth
        try:
            found = scope.findChildren(**kwargs)
        except Exception as e:
            raise ValueError(f'tk.find failed under {scope.path}: {e}')
        results = [o for o in found if tdu.match(pattern, [o.name])]
        if type is not None:
            type_str = type if isinstance(type, str) else getattr(
                type, '__name__', str(type))
            type_str = type_str.lower()
            results = [o for o in results
                       if o.type.lower() == type_str
                       or o.OPType.lower() == type_str]
        return results

    # ---- Tier 1: new capabilities -------------------------------------------

    def settle(self, frames=10, targets=None):
        """Force-cook the touched ops `frames` times, then return the
        consolidated diagnostics that surfaced.

        This is how delayed diagnostics (GLSL shader compile logs,
        sample-dependent CHOP errors) are caught before code_mode responds --
        the model never has to poll again. See the module note on why this is
        force-cook, not real-frame advancement.

        Args:
            frames:  Cook iterations (default 10, caller-tunable).
            targets: Optional explicit ops/paths to cook + diagnose. Defaults
                to everything tk touched this call (created + wired + setp'd).

        Returns the diagnostics dict (see _collectDiagnostics).
        """
        cook_ops = self._settleTargets(targets)
        n = max(0, int(frames))
        for _ in range(n):
            for oper in cook_ops:
                try:
                    oper.cook(force=True)
                except Exception:
                    pass
        return _collectDiagnostics(self._ext, cook_ops)

    def _settleTargets(self, targets):
        if targets is None:
            return [o for o in self._watched if getattr(o, 'valid', False)]
        if not isinstance(targets, (list, tuple)):
            targets = [targets]
        return [self._resolve(t, 'settle target') for t in targets]

    def errors(self, target='/', recurse=True):
        """On-demand diagnostics for a target (no cook). Consolidated errors +
        warnings + GLSL compile logs, like the settle report but without
        advancing anything."""
        oper = self._resolve(target)
        return _collectDiagnostics(self._ext, [oper], recurse=recurse)

    def checkpoint(self, target=None):
        """Snapshot a TDN-strategy COMP to disk (frame-cheap re-export).

        Delegates to Embody's per-COMP Checkpoint. Returns a dict describing
        what happened. A non-TDN target is a safe no-op (returns
        {'checkpointed': False, ...}). Requires a path/op that is a TDN
        boundary to actually write.
        """
        try:
            embody = op.Embody
        except Exception:
            return {'checkpointed': False,
                    'reason': 'Embody COMP not reachable'}
        path = None
        if target is not None:
            path = target if isinstance(target, str) else getattr(
                target, 'path', None)
        if not path:
            return {'checkpointed': False,
                    'reason': 'checkpoint needs a TDN COMP path/op target'}
        try:
            ok = embody.ext.Embody.Checkpoint(path)
        except Exception as e:
            return {'checkpointed': False, 'target': path, 'reason': str(e)}
        return {'checkpointed': bool(ok), 'target': path}

    def report(self, obj):
        """Return structured, JSON-able data to the model explicitly (exec
        discards the last expression value, so without this everything has to
        smuggle through print)."""
        self._report = obj
        self._reported = True
        return obj


# =============================================================================
# Diagnostics -- consolidated errors + warnings + GLSL compile logs
# =============================================================================

def _collectDiagnostics(ext, targets, recurse=True):
    """Merge op errors/warnings (via the proven get_op_errors parser) across
    every target, deduped, and append GLSL shader-compile diagnostics scraped
    from a temporary Info DAT. Never raises.

    Returns {errorCount, warningCount, errors, warnings} with each entry
    {nodePath, nodeName, opType, message, source}.
    """
    errors = []
    warnings = []
    seen_err = set()
    seen_warn = set()

    def _merge(items, out, seen, source):
        for it in items or []:
            key = (it.get('nodePath'), it.get('message'))
            if key in seen:
                continue
            seen.add(key)
            entry = dict(it)
            entry['source'] = source
            out.append(entry)

    for oper in targets:
        if not getattr(oper, 'valid', False):
            continue
        try:
            r = mod.envoy_read.get_op_errors(ext, oper.path, recurse)
        except Exception as e:
            ext._log(f'code_mode diagnostics failed for {oper.path}: {e}',
                     'WARNING')
            continue
        if isinstance(r, dict):
            _merge(r.get('errors'), errors, seen_err, 'op')
            _merge(r.get('warnings'), warnings, seen_warn, 'op')

    # GLSL shader-compile logs: op.errors() misses some (e.g. reserved-word
    # errors), so scrape an Info DAT pointed at each GLSL-family op in scope.
    for oper in _glslOpsInScope(targets, recurse):
        for entry in _glslCompileDiagnostics(ext, oper):
            key = (entry.get('nodePath'), entry.get('message'))
            if key not in seen_err:
                seen_err.add(key)
                errors.append(entry)

    return {
        'errorCount': len(errors),
        'warningCount': len(warnings),
        'errors': errors,
        'warnings': warnings,
    }


def _glslOpsInScope(targets, recurse):
    """GLSL-family ops among the targets (and descendants when recurse)."""
    found = []
    seen = set()

    def _add(o):
        try:
            if (o.valid and 'glsl' in o.type.lower()
                    and o.path not in seen):
                seen.add(o.path)
                found.append(o)
        except Exception:
            pass

    for oper in targets:
        if not getattr(oper, 'valid', False):
            continue
        _add(oper)
        if recurse and hasattr(oper, 'findChildren'):
            try:
                for child in oper.findChildren():
                    _add(child)
            except Exception:
                pass
    return found


def _glslCompileDiagnostics(ext, glsl_op):
    """Scrape shader-compile errors from an Info DAT pointed at a GLSL op.
    Returns a list of error entries. Never raises.

    Verified against live TD 2025.33070: a GLSL op with a compile error reports
    NOTHING via op.errors() -- only op.warnings() ("has compile errors (Use Info
    DAT to see details)"), and the actual "ERROR: <dat>:<line>: ..." text lives
    in the Info DAT as a SINGLE multi-line text cell (not [label, value] rows).
    So we read the Info DAT's whole .text and pull out the ERROR lines; a clean
    "Compiled Successfully" log contains no 'error' and yields nothing.

    A GLSL op already DOCKS its own info DAT (type 'info') -- prefer that (a
    read, no mutation) and only create a throwaway Info DAT as a fallback.
    """
    entries = []
    docked_info = _dockedInfoDat(glsl_op)
    temp = None
    try:
        info = docked_info
        if info is None:
            parent_comp = glsl_op.parent()
            if parent_comp is None or not hasattr(parent_comp, 'create'):
                return entries
            temp = parent_comp.create('infoDAT')
            try:
                temp.par.op = glsl_op.name   # sibling reference by name
            except Exception:
                return entries
            info = temp
        info.cook(force=True)
        text = info.text or ''
        low = text.lower()
        # The GLSL compile log names it a shader/compile error when it fails.
        if 'error' in low and ('shader' in low or 'compil' in low):
            err_lines = [ln.strip() for ln in text.splitlines()
                         if 'error' in ln.lower() and ln.strip()]
            msg = '; '.join(err_lines) if err_lines else text.strip()
            entries.append({
                'nodePath': glsl_op.path,
                'nodeName': glsl_op.name,
                'opType': glsl_op.OPType,
                'message': 'GLSL compile: ' + msg[:400],
                'source': 'glsl_info',
            })
    except Exception as e:
        ext._log(f'GLSL diagnostics scrape failed for {glsl_op.path}: {e}',
                 'DEBUG')
    finally:
        if temp is not None:
            try:
                temp.destroy()
            except Exception:
                pass
    return entries


def _dockedInfoDat(host):
    """The host op's docked Info DAT (type 'info'), or None. GLSL ops dock one
    already, so we can read compile results without creating anything."""
    try:
        for d in host.docked:
            if getattr(d, 'valid', False) and d.type == 'info':
                return d
    except Exception:
        pass
    return None


# =============================================================================
# code_mode -- the execute half of the code-mode surface
# =============================================================================

def _makeSafe(value, _depth=0):
    """Best-effort coercion of a tk.report() payload into JSON-able data."""
    if _depth > 6:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _makeSafe(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_makeSafe(v, _depth + 1) for v in value]
    # TD ops / pars / anything else -> a readable string.
    return str(value)


def code_mode(ext, code, settle_frames=10):
    """Execute Python live in TD with the `tk` namespace, auto-settle, and
    return consolidated diagnostics.

    Args:
        code:          Python source to exec on the main thread.
        settle_frames: Cook iterations for the auto-settle after the code runs
                       (default 10, 0 to skip). Also the default for
                       tk.settle().

    Returns a consolidated dict:
        success, stdout, report, diagnostics {errorCount, warningCount,
        errors, warnings}, created (paths), settled_frames, elapsed_ms.
        On an exception: success=False plus error + traceback (tail); stdout,
        created, and diagnostics still ride along so the model can fix forward
        (code_mode does NOT roll back created ops -- tk.make auto-positions, so
        there is no (0,0) pileup, and the partial state is the evidence).
    """
    preview = code[:200] + ('...' if len(code) > 200 else '')
    ext._log(f'code_mode: {preview}')

    tk = _Toolkit(ext)
    # Fresh globals every call. Native td + op classes are the substrate.
    namespace = {
        'op': op,
        'ops': ops,
        'parent': parent,
        'root': root,
        'me': ext.ownerComp,
        'tk': tk,
    }
    # Inject the td module contents (operator type names, TD classes, tdu, ...)
    # so native TD is fully usable inside code_mode, matching a real DAT.
    try:
        import td as _td
        namespace['td'] = _td
        for name in dir(_td):
            if not name.startswith('_'):
                namespace.setdefault(name, getattr(_td, name))
    except Exception:
        pass

    stdout = io.StringIO()
    t0 = time.perf_counter()
    error = None
    tb = None
    try:
        with contextlib.redirect_stdout(stdout):
            exec(code, namespace)
    except Exception as e:
        error = f'{type(e).__name__}: {e}'
        tb = traceback.format_exc()
        ext._log(f'code_mode failed: {error}', 'ERROR')

    # Auto-settle: force-cook the touched ops, collect diagnostics. Runs even
    # on error so the model sees what the partial code produced.
    settled = max(0, int(settle_frames))
    try:
        diagnostics = tk.settle(frames=settled)
    except Exception as e:
        ext._log(f'code_mode auto-settle failed: {e}', 'WARNING')
        diagnostics = {'errorCount': 0, 'warningCount': 0,
                       'errors': [], 'warnings': []}

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    result = {
        'success': error is None,
        'stdout': stdout.getvalue(),
        'report': _makeSafe(tk._report) if tk._reported else None,
        'diagnostics': diagnostics,
        'created': list(tk._created),
        'settled_frames': settled,
        'elapsed_ms': elapsed_ms,
    }
    if error is not None:
        result['error'] = error
        if tb:
            # Tail only -- the head is exec() plumbing.
            result['traceback'] = tb[-1500:]
    if error is None:
        ext._log('code_mode: completed successfully')
    return result
