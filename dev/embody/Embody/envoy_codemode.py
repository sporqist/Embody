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
import os
import sys
import contextlib
import time
import traceback
from collections import OrderedDict


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


# =============================================================================
# describe -- read-only text: the knowledge & structure entry point
# =============================================================================
#
# One tool, REQUIRED mode + target, NO smart dispatch (D3). Modes:
#   contract  (no target) -- the tk.* code-mode API surface. Read once.
#   node      (target=op path) -- one op in depth: params w/ live values +
#             defaults + is-expression, custom pars, connections, children.
#   network   (target=path, depth=N) -- topology; optional sparse dump (R4).
#   docs      (target=optype/class/func) -- FUSED live-introspection + wiki.

_DESCRIBE_MODES = ('contract', 'node', 'network', 'docs')

_CONTRACT_HELPERS = ('make', 'wire', 'layout', 'setp', 'find',
                     'settle', 'errors', 'checkpoint', 'report')


def describe(ext, mode, target=None, depth=1, dump=False):
    """Read-only text: the knowledge & structure entry point for code_mode.

    Required `mode`; `target` is required for every mode except 'contract'.
    No smart dispatch -- an ambiguous bare name is never auto-routed.
    """
    if mode not in _DESCRIBE_MODES:
        return {'error': f'describe: unknown mode {mode!r} -- '
                f'use one of {", ".join(_DESCRIBE_MODES)}'}
    if mode != 'contract' and not target:
        return {'error': f"describe(mode={mode!r}) requires target "
                f'(an op path for node/network, an optype/class/function '
                f'name for docs)'}
    try:
        if mode == 'contract':
            return _describeContract(ext)
        if mode == 'node':
            return _describeNode(ext, target)
        if mode == 'network':
            return _describeNetwork(ext, target, depth, dump)
        if mode == 'docs':
            return _describeDocs(ext, target)
    except Exception as e:
        ext._log(f'describe({mode}) failed: {e}', 'ERROR')
        return {'error': f'describe({mode}) failed: {e}'}


def _describeContract(ext):
    """The tk.* helper surface, generated from the live Toolkit so it never
    drifts from the code. Native td + op classes stay the substrate."""
    import inspect
    helpers = []
    for name in _CONTRACT_HELPERS:
        fn = getattr(_Toolkit, name, None)
        if fn is None:
            continue
        try:
            sig = str(inspect.signature(fn))
            sig = sig.replace('(self, ', '(').replace('(self)', '()')
        except Exception:
            sig = '(...)'
        doc = (fn.__doc__ or '').strip().splitlines()
        summary = doc[0].strip() if doc else ''
        helpers.append({
            'name': f'tk.{name}',
            'signature': f'tk.{name}{sig}',
            'summary': summary,
        })
    return {
        'mode': 'contract',
        'summary': 'The tk.* helper namespace injected into code_mode. Native '
                   'td + op classes remain the substrate; tk adds ergonomic '
                   'primitives only where native TD is a footgun or the '
                   'capability does not exist natively.',
        'fresh_globals': True,
        'substrate': 'op, ops, parent, root, me, td, and all td.* names '
                     '(operator type names, tdu, classes) are in scope.',
        'helpers': helpers,
        'notes': [
            'Fresh globals every call -- nothing persists between calls; keep '
            'persistent state in the TD project.',
            'code_mode auto-settles ~10 cook frames after your code and returns '
            'consolidated diagnostics; call tk.settle(n) mid-code to sample '
            'earlier.',
            'Return data with tk.report(obj); it rides back separate from '
            'stdout.',
        ],
    }


def _describeNode(ext, target):
    """One op in depth: type/family, custom pars (all) + non-default built-in
    pars, each with live value + default + mode + expression, plus connections
    and children. The structure counterpart to `view` (which shows data)."""
    o = op(target)
    if o is None:
        return {'error': f'Operator not found: {target}'}

    def _par_entry(p):
        entry = {'name': p.name, 'label': p.label}
        try:
            entry['value'] = str(p.eval())
        except Exception:
            entry['value'] = 'N/A'
        try:
            entry['default'] = str(p.default)
        except Exception:
            entry['default'] = None
        try:
            entry['mode'] = p.mode.name
        except Exception:
            entry['mode'] = str(getattr(p, 'mode', ''))
        if entry['mode'] == 'EXPRESSION':
            try:
                entry['expr'] = p.expr
            except Exception:
                pass
        # Menu params: surface the friendly label + the valid options inline so
        # the model never guesses a token or index (value stays the token).
        try:
            if p.isMenu:
                names = list(p.menuNames)
                labels = list(p.menuLabels)
                idx = p.menuIndex
                entry['menu'] = {
                    'label': labels[idx] if 0 <= idx < len(labels) else None,
                    'options': [{'name': n, 'label': l}
                                for n, l in zip(names, labels)],
                }
        except Exception:
            pass
        return entry

    custom_pars = []
    builtin_nondefault = []
    for p in o.pars():
        try:
            is_custom = bool(p.isCustom)
        except Exception:
            is_custom = False
        if is_custom:
            custom_pars.append(_par_entry(p))
            continue
        # Built-in: keep only non-default (value or an active expression/bind).
        try:
            mode_name = p.mode.name
        except Exception:
            mode_name = 'CONSTANT'
        keep = mode_name != 'CONSTANT'
        if not keep:
            try:
                keep = p.val != p.default
            except Exception:
                keep = False
        if keep:
            builtin_nondefault.append(_par_entry(p))

    info = {
        'mode': 'node',
        'path': o.path,
        'name': o.name,
        'type': o.OPType,
        'family': o.family,
        'customPars': custom_pars,
        'nonDefaultPars': builtin_nondefault,
        'inputs': [i.path if i else None for i in o.inputs],
        'outputs': [i.path if i else None for i in o.outputs],
    }
    try:
        info['tags'] = sorted(o.tags)
    except Exception:
        pass
    if hasattr(o, 'children'):
        info['children'] = [c.name for c in o.children]
        info['childCount'] = len(info['children'])
    return info


def _describeNetwork(ext, target, depth=1, dump=False):
    """Topology of a COMP: children (path/type/family/inputs) walked to
    `depth`. With dump=True, also embed a sparse non-default-only TDN dict
    (via read_tdn) for whole-network comprehension in one read (R4). Breadth,
    not per-op depth -- use describe(node) for one op's params."""
    comp = op(target)
    if comp is None:
        return {'error': f'Operator not found: {target}'}
    # Non-COMPs also expose an (empty) .children, so hasattr is not a COMP
    # test -- use isCOMP.
    if not getattr(comp, 'isCOMP', False):
        return {'error': f'{target} is not a COMP ({comp.OPType})'}

    try:
        d = max(1, int(depth))
    except Exception:
        d = 1

    total = [0]

    def walk(c, remaining):
        out = []
        for child in c.children:
            total[0] += 1
            node = {
                'path': child.path,
                'name': child.name,
                'type': child.OPType,
                'family': child.family,
            }
            ins = [i.path for i in child.inputs if i]
            if ins:
                node['inputs'] = ins
            kids = getattr(child, 'children', None)
            if kids:
                node['childCount'] = len(kids)
                if remaining > 1:
                    node['children'] = walk(child, remaining - 1)
            out.append(node)
        return out

    operators = walk(comp, d)
    result = {
        'mode': 'network',
        'path': comp.path,
        'type': comp.OPType,
        'depth': d,
        'count': total[0],
        'operators': operators,
    }
    if dump:
        try:
            tdn = mod.envoy_read.read_tdn(ext, comp_path=comp.path,
                                          max_depth=d)
            if isinstance(tdn, dict) and 'error' in tdn:
                result['sparse_error'] = tdn['error']
            else:
                result['sparse'] = tdn
        except Exception as e:
            result['sparse_error'] = f'sparse dump failed: {e}'
    return result


def _describeDocs(ext, target):
    """FUSE live introspection (truth) with offline-wiki prose (understanding)
    for an operator type. Live parameter names + defaults come from a transient
    probe instance (authoritative, matches the running build); the wiki page
    supplies the Summary + Parameters prose. This is the code-mode edge over
    the fork's separate introspection / get_docs tools.
    """
    live, live_err = _liveOptypeInfo(ext, target)
    wiki = _offlineWikiPage(ext, target)
    if not live and not wiki:
        note = f' ({live_err})' if live_err else ''
        return {'error': f'describe(docs): no live optype and no offline wiki '
                f'page matched {target!r}{note}'}

    result = {'mode': 'docs', 'target': target}
    if live:
        result['optype'] = live['optype']
        result['family'] = live['family']
        result['pythonClass'] = live['pythonClass']
        result['parameters'] = live['parameters']       # authoritative
        result['parameterSource'] = 'live introspection (running build)'
    elif live_err:
        result['live_note'] = f'no live instance introspected: {live_err}'

    if wiki:
        secs = wiki['sections']
        summary = (secs.get('summary') or '').strip()
        if summary:
            result['summary'] = summary[:1500]
        # Operator pages split parameters across multiple "Parameters - X Page"
        # sections (Noise/Transform/Output/Common), not a single 'Parameters'
        # one -- gather them all in page order.
        param_keys = sorted(k for k in secs if k.startswith('parameters'))
        params_prose = '\n\n'.join(secs[k].strip() for k in param_keys
                                   if secs[k].strip())
        if params_prose:
            result['wiki_parameters'] = params_prose[:5000]
            if len(params_prose) > 5000:
                result['wiki_parameters_truncated'] = True
        result['wiki'] = {
            'title': wiki['title'],
            'file': wiki['file'],
            'source': 'offline mirror',
            'sections_available': wiki['sections_available'],
        }
    else:
        result['wiki_note'] = 'no offline wiki page matched'

    result['fusion_note'] = (
        'Live parameters + defaults are authoritative (they match the running '
        'build); wiki prose is for understanding. Automated per-parameter '
        'wiki-vs-live default mismatch flagging is a planned enhancement.')
    return result


def _liveOptypeInfo(ext, optype):
    """Authoritative parameter list + defaults for an operator TYPE, read from
    a TRANSIENT probe instance (created in a scratch COMP, destroyed
    immediately). Returns (info, None) or (None, error_string).

    Uses raw .create() (NOT create_op) so it never auto-externalizes. The
    scratch holder is always torn down."""
    try:
        home = op.Embody.parent()
    except Exception:
        home = None
    if home is None or not hasattr(home, 'create'):
        return None, 'no scratch home to probe in'
    holder = None
    try:
        holder = home.create('baseCOMP')
        try:
            probe = holder.create(optype)
        except Exception as e:
            return None, f'{optype!r} is not a creatable operator type ({e})'
        pars = []
        for p in probe.pars():
            entry = {'name': p.name, 'label': p.label}
            try:
                entry['default'] = str(p.default)
            except Exception:
                entry['default'] = None
            try:
                entry['style'] = str(p.style)
            except Exception:
                pass
            try:
                if p.isMenu and p.menuNames:
                    # name token + friendly label per option -- the model reads
                    # both in one shot, never guessing a token or index.
                    entry['menu'] = [{'name': n, 'label': l} for n, l
                                     in zip(list(p.menuNames),
                                            list(p.menuLabels))]
            except Exception:
                pass
            pars.append(entry)
        return {
            'optype': probe.OPType,
            'family': probe.family,
            'pythonClass': type(probe).__name__,
            'parameters': pars,
        }, None
    except Exception as e:
        return None, str(e)
    finally:
        if holder is not None:
            try:
                holder.destroy()
            except Exception:
                pass


def _offlineWikiPage(ext, query):
    """Resolve + parse the offline-wiki page for `query` on the MAIN thread
    (the ext's _docsOffline round-trips through _execute_in_td and would
    deadlock here). Returns a parsed page dict or None. Normalization strips
    non-alphanumerics + lowercases, so an optype ('moviefileinTOP') matches its
    wiki file ('Movie_File_In_TOP.htm')."""
    # The offline-wiki text helpers are static methods on the EnvoyMCPServer
    # class (worker-side), NOT on the EnvoyExt instance we get here -- reach
    # them via the sibling module's class. _get_docs_roots IS on EnvoyExt.
    try:
        srv = mod.EnvoyExt.EnvoyMCPServer
    except Exception:
        return None
    try:
        roots = (ext._get_docs_roots() or {}).get('roots', [])
    except Exception:
        return None
    root = next((r for r in roots if os.path.isdir(r)), None)
    if not root:
        return None
    try:
        index = {}
        for fn in os.listdir(root):
            if not fn.lower().endswith(('.htm', '.html')):
                continue
            key = srv._docsNormalize(os.path.splitext(fn)[0])
            if key and key not in index:
                index[key] = fn
    except Exception:
        return None
    key = srv._docsNormalize(query)
    if not key:
        return None
    fn = index.get(key)
    if fn is None:
        cands = [f for k, f in index.items() if key in k or k in key]
        if len(cands) == 1:
            fn = cands[0]
    if fn is None:
        return None
    try:
        with open(os.path.join(root, fn), encoding='utf-8',
                  errors='replace') as f:
            html = f.read()
        text = srv._docsHtmlToText(html)
        available, sections = srv._docsSplitSections(text)
        return {'title': os.path.splitext(fn)[0].replace('_', ' '),
                'file': fn, 'sections_available': available,
                'sections': sections}
    except Exception:
        return None


# =============================================================================
# view -- read-only "renders": TOP pixels, CHOP/DAT reduced data, diffs
# =============================================================================
#
# Closes the feedback loop with code_mode: SEE the output (TOP -> inline image)
# and the DATA (CHOP/DAT -> reduced render), cheaply. Two diff axes (D3):
#   relational (op-vs-op, PRIMARY) -- what a chain does to its data.
#   temporal   -- vs this client's last view / a pinned baseline.
#
# The handler returns a dict on the MAIN thread; the worker-side `view` tool
# turns a TOP result's image_b64 into an inline image. CHOP/DAT/diff stay dicts.

_VIEW_SNAP_CAP = 64          # LRU cap on remembered temporal snapshots


def view(ext, target, resolution=480, other=None, channels=None,
         head=8, tail=0, stats=True, rows=16, cols=None, pin=False, sid=None):
    """Render an operator's output for the model to see.

    TOP -> inline image (downscaled to `resolution`, default 480 longest edge).
    CHOP -> per-channel stats + head/tail samples (never blind-dump).
    DAT  -> header + head/tail rows.
    `other` (a second op path) -> RELATIONAL diff of the two ops' reduced data.
    Otherwise a CHOP/DAT view auto-diffs vs this client's last view of the same
    target (temporal); pin=True stores the current view as the baseline.
    """
    o = op(target)
    if o is None:
        return {'error': f'Operator not found: {target}'}

    if other:
        return _viewRelational(ext, o, other, resolution, channels,
                               head, tail, stats, rows, cols)

    fam = o.family
    if fam == 'TOP':
        return _viewTop(ext, o, resolution)
    if fam == 'CHOP':
        snap = _reduceChop(o, channels, head, tail, stats)
        result = {'kind': 'chop', 'path': o.path, **snap}
        _attachTemporal(result, sid, o.path, snap, pin)
        return result
    if fam == 'DAT':
        snap = _reduceDat(o, head, tail, cols)
        result = {'kind': 'dat', 'path': o.path, **snap}
        _attachTemporal(result, sid, o.path, snap, pin)
        return result
    return {'error': f'view does not support family {fam!r} yet '
            f'({o.OPType}) -- use TOP, CHOP, or DAT (describe(node) for '
            f'params, describe(network) for structure).'}


# ---- TOP: inline image ------------------------------------------------------

def _viewTop(ext, o, resolution):
    try:
        res = int(resolution) if resolution else 480
    except Exception:
        res = 480
    cap = mod.envoy_read.capture_top(ext, o.path, format='png',
                                     max_resolution=res)
    if isinstance(cap, dict) and 'error' in cap:
        return cap
    return {
        'kind': 'top',
        'path': o.path,
        'image_b64': cap.get('image_b64'),
        'format': cap.get('format', 'png'),
        'width': cap.get('width'),
        'height': cap.get('height'),
        'original_width': cap.get('original_width'),
        'original_height': cap.get('original_height'),
        'size_bytes': cap.get('size_bytes'),
        'quality': cap.get('quality'),
    }


# ---- CHOP / DAT reduction ---------------------------------------------------

def _round(v):
    try:
        return round(float(v), 6)
    except Exception:
        return None


def _reduceChop(o, channels, head, tail, stats):
    try:
        o.cook(force=True)
    except Exception:
        pass
    arr = None
    try:
        import numpy as np
        arr = o.numpyArray()   # (numChans, numSamples)
    except Exception:
        arr = None
    all_chans = list(o.chans())
    out = []
    for idx, ch in enumerate(all_chans):
        if channels and not tdu.match(channels, [ch.name]):
            continue
        try:
            nsamp = len(ch)
        except Exception:
            nsamp = o.numSamples
        entry = {'name': ch.name, 'numSamples': nsamp}
        row = None
        try:
            if arr is not None and idx < arr.shape[0]:
                row = arr[idx]
        except Exception:
            row = None
        if row is not None and len(row):
            if stats:
                entry['stats'] = {
                    'min': _round(row.min()), 'max': _round(row.max()),
                    'mean': _round(row.mean()), 'std': _round(row.std()),
                }
            n = max(0, int(head))
            entry['head'] = [_round(v) for v in row[:n]]
            if tail:
                entry['tail'] = [_round(v) for v in row[-int(tail):]]
        else:
            try:
                entry['value'] = _round(ch.eval())
            except Exception:
                pass
        out.append(entry)
    return {
        'numChannels': len(all_chans),
        'shownChannels': len(out),
        'numSamples': o.numSamples,
        'sampleRate': getattr(o, 'rate', None),
        'channels': out,
    }


def _reduceDat(o, head, tail, cols):
    try:
        o.cook(force=True)
    except Exception:
        pass
    nr, nc = o.numRows, o.numCols
    col_idx = None
    if cols:
        # cols: list of indices or header names.
        header = [o[0, c].val for c in range(nc)] if nr else []
        col_idx = []
        for c in cols:
            if isinstance(c, int):
                if 0 <= c < nc:
                    col_idx.append(c)
            else:
                if c in header:
                    col_idx.append(header.index(c))

    def _row(r):
        idxs = col_idx if col_idx is not None else range(nc)
        return [o[r, c].val for c in idxs]

    n_head = min(nr, max(0, int(head)))
    rows_out = [_row(r) for r in range(n_head)]
    tail_out = None
    if tail and nr > n_head:
        t = min(nr - n_head, int(tail))
        tail_out = [_row(r) for r in range(nr - t, nr)]
    result = {
        'numRows': nr,
        'numCols': nc,
        'rows': rows_out,
        'truncated': nr > n_head,
    }
    if tail_out:
        result['tailRows'] = tail_out
    if col_idx is not None:
        result['columns'] = col_idx
    return result


# ---- Relational diff (op vs op) ---------------------------------------------

def _viewRelational(ext, o, other_path, resolution, channels,
                    head, tail, stats, rows, cols):
    other = op(other_path)
    if other is None:
        return {'error': f'view(other=...): operator not found: {other_path}'}
    if o.family != other.family:
        return {'error': f'relational diff needs same-family ops: '
                f'{o.path} is {o.family}, {other.path} is {other.family}'}
    fam = o.family
    if fam == 'CHOP':
        a = _reduceChop(o, channels, head, tail, stats)
        b = _reduceChop(other, channels, head, tail, stats)
        return {'kind': 'diff', 'axis': 'relational', 'family': 'CHOP',
                'a': o.path, 'b': other.path,
                'diff': _diffChop(a, b), 'a_view': a, 'b_view': b}
    if fam == 'DAT':
        a = _reduceDat(o, head, tail, cols)
        b = _reduceDat(other, head, tail, cols)
        return {'kind': 'diff', 'axis': 'relational', 'family': 'DAT',
                'a': o.path, 'b': other.path,
                'diff': _diffDat(a, b), 'a_view': a, 'b_view': b}
    if fam == 'TOP':
        return {'kind': 'diff', 'axis': 'relational', 'family': 'TOP',
                'a': o.path, 'b': other.path,
                'note': 'TOP-vs-TOP pixel diff is a planned enhancement; view '
                        'each TOP separately for now.'}
    return {'error': f'relational diff unsupported for family {fam!r}'}


def _diffChop(a, b):
    an = {c['name']: c for c in a.get('channels', [])}
    bn = {c['name']: c for c in b.get('channels', [])}
    added = sorted(set(bn) - set(an))
    removed = sorted(set(an) - set(bn))
    changed = []
    for name in sorted(set(an) & set(bn)):
        sa = (an[name].get('stats') or {}).get('mean')
        sb = (bn[name].get('stats') or {}).get('mean')
        if sa is not None and sb is not None and sa != sb:
            changed.append({'channel': name, 'mean_a': sa, 'mean_b': sb,
                            'delta': _round(sb - sa)})
    return {'addedChannels': added, 'removedChannels': removed,
            'changedMeans': changed,
            'sampleCount': {'a': a.get('numSamples'), 'b': b.get('numSamples')}}


def _diffDat(a, b):
    return {'rowCount': {'a': a.get('numRows'), 'b': b.get('numRows')},
            'colCount': {'a': a.get('numCols'), 'b': b.get('numCols')},
            'rowDelta': (b.get('numRows', 0) - a.get('numRows', 0)),
            'colDelta': (b.get('numCols', 0) - a.get('numCols', 0))}


# ---- Temporal diff (vs this client's last view / pinned baseline) -----------

def _viewSnapStore():
    store = getattr(sys, '_envoy_view_snapshots', None)
    if store is None:
        store = OrderedDict()
        sys._envoy_view_snapshots = store
    return store


def _attachTemporal(result, sid, path, snap, pin):
    """Auto-diff a CHOP/DAT view vs this client's last view of the same target,
    then remember the current one. pin=True stores it as a sticky baseline that
    later views diff against until re-pinned."""
    store = _viewSnapStore()
    who = sid or '_anon'
    live_key = (who, path, 'last')
    pin_key = (who, path, 'pin')

    baseline = store.get(pin_key) or store.get(live_key)
    if baseline is not None:
        kind = result.get('kind')
        try:
            if kind == 'chop':
                result['temporal_diff'] = _diffChop(baseline, snap)
            elif kind == 'dat':
                result['temporal_diff'] = _diffDat(baseline, snap)
            result['temporal_baseline'] = ('pinned'
                                           if store.get(pin_key) else 'last_view')
        except Exception:
            pass

    # Update the rolling "last" snapshot (LRU), and the pin if requested.
    store[live_key] = snap
    store.move_to_end(live_key)
    if pin:
        store[pin_key] = snap
        store.move_to_end(pin_key)
        result['pinned'] = True
    while len(store) > _VIEW_SNAP_CAP:
        store.popitem(last=False)
