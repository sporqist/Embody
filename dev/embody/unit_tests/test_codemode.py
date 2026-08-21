"""
Test suite: code_mode handler + the tk namespace (envoy_codemode).

M1 of the code-mode surface (revamp/BUILD_BRIEF.md). Exercises _code_mode
directly on the main thread (the deterministic path -- no MCP transport),
mirroring test_mcp_code_execution.

Every test creates ops inside self.sandbox so the runner's tearDown reaps
them. tk.make is always given an explicit parent=<sandbox> so nothing lands
in the real network.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestCodeMode(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        self.sb = self.sandbox.path

    # --- basic execution + return channel ------------------------------------

    def test_report_channel(self):
        result = self.envoy._code_mode(code='tk.report({"answer": 42})')
        self.assertNotIn('error', result)
        self.assertTrue(result['success'])
        self.assertEqual(result['report'], {'answer': 42})

    def test_report_absent_when_not_called(self):
        result = self.envoy._code_mode(code='x = 1 + 1')
        self.assertTrue(result['success'])
        self.assertIsNone(result['report'])

    def test_stdout_captured(self):
        result = self.envoy._code_mode(code='print("hello codemode")')
        self.assertTrue(result['success'])
        self.assertIn('hello codemode', result['stdout'])

    def test_native_td_substrate_available(self):
        # Operator type names (noiseTOP) and op() are in scope like a real DAT.
        result = self.envoy._code_mode(
            code='tk.report(op("/").name)')
        self.assertTrue(result['success'])
        self.assertTrue(isinstance(result['report'], str))

    # --- the M1 proof: create + wire + settle + read in ONE call --------------

    def test_create_wire_settle_read(self):
        code = (
            "c = tk.make('constantCHOP', 'src', parent={sb!r})\n"
            "n = tk.make('nullCHOP', 'sink', parent={sb!r})\n"
            "wired = tk.wire(c, n)\n"
            "diag = tk.settle(5)\n"
            "tk.report({{\n"
            "    'value': (n.chans()[0].eval() if n.numChans else None),\n"
            "    'nchans': n.numChans,\n"
            "    'errors': diag['errorCount'],\n"
            "    'wired': wired,\n"
            "}})\n"
        ).format(sb=self.sb)
        result = self.envoy._code_mode(code=code)

        self.assertNotIn('error', result, result.get('error', ''))
        self.assertTrue(result['success'])
        # Both ops were created and tracked.
        self.assertLen(result['created'], 2)
        self.assertTrue(result['created'][0].endswith('/src'))
        self.assertTrue(result['created'][1].endswith('/sink'))
        # They actually exist in the network.
        self.assertIsNotNone(op(self.sb + '/src'))
        self.assertIsNotNone(op(self.sb + '/sink'))
        # The read propagated through the wire after settle; no errors.
        rep = result['report']
        # The constant->null chain cooked and produced a channel we could read.
        self.assertEqual(rep['nchans'], 1)
        self.assertEqual(rep['value'], 0.0)
        self.assertEqual(rep['errors'], 0)
        self.assertLen(rep['wired'], 1)
        # Consolidated diagnostics rode back clean.
        self.assertEqual(result['diagnostics']['errorCount'], 0)
        self.assertEqual(result['settled_frames'], 10)

    def test_make_positions_clear_of_sibling(self):
        # The FIRST op in an empty COMP legitimately lands at (0,0) -- there is
        # no sibling to avoid. Auto-layout must place a SECOND op clear of the
        # first (non-overlapping), which is the guarantee tk.make provides.
        code = (
            "a = tk.make('noiseTOP', 'noise_a', parent={sb!r})\n"
            "b = tk.make('noiseTOP', 'noise_b', parent={sb!r})\n"
            "tk.report({{'ax': a.nodeX, 'ay': a.nodeY, 'aw': a.nodeWidth,\n"
            "            'bx': b.nodeX, 'by': b.nodeY, 'bw': b.nodeWidth}})\n"
        ).format(sb=self.sb)
        result = self.envoy._code_mode(code=code)
        self.assertTrue(result['success'], result.get('error', ''))
        r = result['report']
        # b must not sit on top of a: their horizontal extents must not overlap
        # while sharing a row, OR they are on different rows.
        same_row = r['ay'] == r['by']
        x_overlap = (r['ax'] < r['bx'] + r['bw']) and (r['bx'] < r['ax'] + r['aw'])
        self.assertFalse(same_row and x_overlap,
                         'tk.make placed the second op overlapping the first')

    # --- tk.setp --------------------------------------------------------------

    def test_setp_sets_known_par(self):
        # timeslice is a real toggle par on a Null CHOP (bypass is a FLAG, not
        # a par -- verified live).
        code = (
            "b = tk.make('nullCHOP', 'byp', parent={sb!r})\n"
            "tk.setp(b, timeslice=1)\n"
            "tk.report(b.par.timeslice.eval())\n"
        ).format(sb=self.sb)
        result = self.envoy._code_mode(code=code)
        self.assertTrue(result['success'], result.get('error', ''))
        self.assertEqual(result['report'], 1)

    def test_setp_unknown_par_is_clear_error(self):
        code = (
            "b = tk.make('nullCHOP', 'byp2', parent={sb!r})\n"
            "tk.setp(b, Definitelynotapar=1)\n"
        ).format(sb=self.sb)
        result = self.envoy._code_mode(code=code)
        self.assertFalse(result['success'])
        self.assertIn('Definitelynotapar', result['error'])
        # The op it created before failing is still reported (fix-forward).
        self.assertLen(result['created'], 1)

    # --- tk.find --------------------------------------------------------------

    def test_find_by_pattern_and_type(self):
        code = (
            "tk.make('nullCHOP', 'findme_a', parent={sb!r})\n"
            "tk.make('nullCHOP', 'findme_b', parent={sb!r})\n"
            "tk.make('noiseTOP', 'other', parent={sb!r})\n"
            "hits = tk.find('findme_*', parent={sb!r})\n"
            "typed = tk.find('*', type='nullCHOP', parent={sb!r})\n"
            "tk.report({{'hits': len(hits), 'typed': len(typed)}})\n"
        ).format(sb=self.sb)
        result = self.envoy._code_mode(code=code)
        self.assertTrue(result['success'])
        self.assertEqual(result['report']['hits'], 2)
        self.assertEqual(result['report']['typed'], 2)

    # --- diagnostics on a broken op ------------------------------------------

    def test_diagnostics_surface_op_errors(self):
        # A GLSL TOP with an intentionally broken shader must surface a
        # diagnostic after settle -- proving the consolidated-error path works
        # (via op.errors and/or the GLSL info-DAT scrape).
        code = (
            "g = tk.make('glslTOP', 'broken_glsl', parent={sb!r})\n"
            "broke = False\n"
            "px = g.par.pixeldat.eval() if hasattr(g.par, 'pixeldat') else None\n"
            "if px is not None:\n"
            "    px.text = 'this is not valid glsl at all;;;'\n"
            "    broke = True\n"
            "diag = tk.settle(8)\n"
            "tk.report({{'errors': diag['errorCount'], 'broke': broke}})\n"
        ).format(sb=self.sb)
        result = self.envoy._code_mode(code=code)
        self.assertTrue(result['success'], result.get('error', ''))
        # When we actually broke the shader, a diagnostic MUST surface.
        if result['report'].get('broke'):
            self.assertTrue(
                result['diagnostics']['errorCount'] >= 1,
                'broken GLSL shader produced no diagnostic -- consolidated '
                'error path (op.errors + GLSL info scrape) missed it')

    # --- error handling / fix-forward ----------------------------------------

    def test_runtime_error_reported_with_diagnostics(self):
        result = self.envoy._code_mode(code='result = 1 / 0')
        self.assertFalse(result['success'])
        self.assertIn('ZeroDivisionError', result['error'])
        self.assertDictHasKey(result, 'traceback')
        # Diagnostics + stdout still ride along on failure.
        self.assertDictHasKey(result, 'diagnostics')
        self.assertDictHasKey(result, 'stdout')

    def test_syntax_error_reported(self):
        result = self.envoy._code_mode(code='def (broken')
        self.assertFalse(result['success'])
        self.assertDictHasKey(result, 'error')

    # --- fresh globals every call (no REPL persistence) -----------------------

    def test_fresh_globals_between_calls(self):
        first = self.envoy._code_mode(code='persisted = 123')
        self.assertTrue(first['success'])
        second = self.envoy._code_mode(code='tk.report(persisted)')
        self.assertFalse(second['success'])
        self.assertIn('NameError', second['error'])

    # --- report coercion ------------------------------------------------------

    def test_report_coerces_non_jsonable(self):
        # Returning a live op must not crash -- it is stringified.
        code = (
            "t = tk.make('nullCHOP', 'reportop', parent={sb!r})\n"
            "tk.report(t)\n"
        ).format(sb=self.sb)
        result = self.envoy._code_mode(code=code)
        self.assertTrue(result['success'])
        self.assertTrue(isinstance(result['report'], str))

    # --- auto-externalize integration (tk.make / tk.externalize) --------------

    def test_make_auto_externalize_runs_clean(self):
        # tk.make routes through the same AutoExternalizeNewOp chokepoint as
        # create_op. Inside the (externalized) test sandbox it correctly SKIPS
        # (ancestor .tdn already captures the subtree) -- so no error, no files.
        code = "tk.make('baseCOMP', 'axc', parent={sb!r})".format(sb=self.sb)
        result = self.envoy._code_mode(code=code)
        self.assertTrue(result['success'], result.get('error', ''))
        # skipped inside an externalized ancestor -> no 'externalized' key
        self.assertNotIn('externalized', result)

    def test_externalize_bad_target_errors(self):
        result = self.envoy._code_mode(
            code="tk.report(tk.externalize('/no/such/op'))")
        # tk.externalize resolves the target first -> raises -> code_mode error
        self.assertFalse(result['success'])
        self.assertIn('not found', result['error'])

    # --- settle_frames tunable ------------------------------------------------

    def test_settle_frames_respected(self):
        result = self.envoy._code_mode(code='pass', settle_frames=3)
        self.assertTrue(result['success'])
        self.assertEqual(result['settled_frames'], 3)

    # --- M2: make robustness / guards -----------------------------------------

    def test_make_refuses_bare_root(self):
        result = self.envoy._code_mode(code="tk.make('nullCHOP', 'x', parent='/')")
        self.assertFalse(result['success'])
        self.assertIn('off-limits', result['error'])

    def test_make_unknown_optype_is_clear(self):
        code = "tk.make('totallyFakeTOP', 'x', parent={sb!r})".format(sb=self.sb)
        result = self.envoy._code_mode(code=code)
        self.assertFalse(result['success'])
        self.assertIn('totallyFakeTOP', result['error'])
        self.assertIn('valid operator type', result['error'])

    # --- M2: tk.layout / wire layout ------------------------------------------

    def test_layout_forward_flow(self):
        code = (
            "a = tk.make('nullCHOP', 'la', parent={sb!r})\n"
            "b = tk.make('nullCHOP', 'lb', parent={sb!r})\n"
            "c = tk.make('nullCHOP', 'lc', parent={sb!r})\n"
            "tk.layout(a, b, c)\n"
            "tk.report({{'ax': a.nodeX, 'aw': a.nodeWidth, 'ay': a.nodeY,\n"
            "            'bx': b.nodeX, 'bw': b.nodeWidth, 'by': b.nodeY,\n"
            "            'cx': c.nodeX, 'cy': c.nodeY}})\n"
        ).format(sb=self.sb)
        result = self.envoy._code_mode(code=code)
        self.assertTrue(result['success'], result.get('error', ''))
        r = result['report']
        # Same row, strictly forward flow (each right edge left of the next).
        self.assertEqual(r['ay'], r['by'])
        self.assertEqual(r['by'], r['cy'])
        self.assertTrue(r['ax'] + r['aw'] <= r['bx'],
                        'op a overlaps/behind b after layout')
        self.assertTrue(r['bx'] + r['bw'] <= r['cx'],
                        'op b overlaps/behind c after layout')

    def test_wire_with_layout(self):
        code = (
            "a = tk.make('constantCHOP', 'wa', parent={sb!r})\n"
            "b = tk.make('nullCHOP', 'wb', parent={sb!r})\n"
            "tk.wire(a, b, layout=True)\n"
            "tk.report({{'ax': a.nodeX, 'aw': a.nodeWidth, 'ay': a.nodeY,\n"
            "            'bx': b.nodeX, 'by': b.nodeY}})\n"
        ).format(sb=self.sb)
        result = self.envoy._code_mode(code=code)
        self.assertTrue(result['success'], result.get('error', ''))
        r = result['report']
        self.assertEqual(r['ay'], r['by'])
        self.assertTrue(r['ax'] + r['aw'] <= r['bx'])

    # --- M2: checkpoint safe no-op on a non-TDN op ----------------------------

    def test_checkpoint_non_tdn_is_safe_noop(self):
        code = (
            "b = tk.make('nullCHOP', 'ckpt', parent={sb!r})\n"
            "tk.report(tk.checkpoint(b))\n"
        ).format(sb=self.sb)
        result = self.envoy._code_mode(code=code)
        self.assertTrue(result['success'], result.get('error', ''))
        self.assertFalse(result['report']['checkpointed'])


class TestCodeModeRealFrames(EmbodyTestCase):
    """The read-after-write fix: real_frames + tk.after_frames().

    The synchronous force-cook settle cannot advance real frames, so a read
    that depends on a callback firing / a reload landing / a loop evolving is
    stale in the same call. real_frames defers the response across real frames.

    The full deferred path (worker Event + a run(delayFrames=1) tick chain)
    needs real frames to pass, so these tests drive the pure machinery --
    cm_begin + a manual cm_advance loop standing in for the ticks -- against a
    fake pending Event. That deterministically covers the countdown, the
    callback invocation, delivery, and the error paths; the live end-to-end
    (real frames actually advancing) is verified by hand.
    """

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        self.cm = self.embody.op('envoy_codemode').module
        self._clear_defer_state()

    def tearDown(self):
        self._clear_defer_state()
        super().tearDown()

    def _clear_defer_state(self):
        import sys
        sys._envoy_pending_codemode = None
        sys._envoy_codemode_defer = None

    def _fake_pending(self):
        import threading
        holder = {}
        import sys
        sys._envoy_pending_codemode = {'event': threading.Event(),
                                       'holder': holder}
        return holder

    def _drive(self, code, settle_frames=0, real_frames=3):
        """Run the deferred machinery to completion; return the delivered
        result. Stands in for the EnvoyExt tick chain by calling cm_advance
        until it reports done (no real frames pass -- mechanics only)."""
        holder = self._fake_pending()
        deferring = self.cm.cm_begin(self.envoy, code, settle_frames, real_frames)
        if not deferring:
            # exec error delivered immediately in cm_begin
            return holder['result']
        guard = 0
        while self.cm.cm_advance(self.envoy):
            guard += 1
            self.assertLess(guard, real_frames + 5, 'cm_advance never finished')
        return holder['result']

    # --- tk.after_frames registration ----------------------------------------

    def test_after_frames_rejects_non_callable(self):
        result = self.envoy._code_mode(code='tk.after_frames(5)')
        self.assertFalse(result['success'])
        self.assertIn('after_frames', result.get('error', ''))

    def test_after_frames_runs_in_sync_path(self):
        # Registered without real_frames: still runs (post-settle), never
        # silently dropped -- its report wins.
        code = ("def rb():\n"
                "    tk.report({'ran': True})\n"
                "tk.after_frames(rb)\n")
        result = self.envoy._code_mode(code=code)
        self.assertTrue(result['success'], result.get('error', ''))
        self.assertEqual(result['report'], {'ran': True})

    # --- deferred machinery (cm_begin / cm_advance) --------------------------

    def test_deferred_delivers_callback_report(self):
        code = ("def rb():\n"
                "    tk.report({'from_callback': 99})\n"
                "tk.after_frames(rb)\n")
        result = self._drive(code, real_frames=3)
        self.assertTrue(result['success'], result.get('error', ''))
        self.assertEqual(result['report'], {'from_callback': 99})
        self.assertEqual(result['real_frames'], 3)

    def test_deferred_countdown_matches_real_frames(self):
        # remaining starts at N; cm_advance returns True (N-1) times then False.
        holder = self._fake_pending()
        self.assertTrue(self.cm.cm_begin(self.envoy, 'x = 1', 0, 3))
        trues = 0
        while self.cm.cm_advance(self.envoy):
            trues += 1
        self.assertEqual(trues, 2)  # 3 -> 2(True) -> 1(True) -> 0(False)
        self.assertIn('result', holder)

    def test_deferred_after_frames_error_captured(self):
        code = ("def rb():\n"
                "    raise ValueError('boom in read-back')\n"
                "tk.after_frames(rb)\n")
        result = self._drive(code, real_frames=2)
        self.assertFalse(result['success'])
        self.assertIn('boom in read-back', result.get('error', ''))

    def test_deferred_exec_error_delivers_immediately(self):
        result = self._drive('undefined_name_here', real_frames=5)
        self.assertFalse(result['success'])
        self.assertIn('NameError', result.get('error', ''))
        # No frames burned on a dead call.
        self.assertEqual(result.get('real_frames'), 0)

    def test_deferred_return_value_surfaced_without_report(self):
        # A callback that returns (not tk.report) still has its value surfaced.
        code = ("def rb():\n"
                "    return {'returned': 7}\n"
                "tk.after_frames(rb)\n")
        result = self._drive(code, real_frames=1)
        self.assertTrue(result['success'], result.get('error', ''))
        self.assertEqual(result.get('after_frames_return'), {'returned': 7})

    def test_real_frames_capped(self):
        import sys
        self._fake_pending()
        self.cm.cm_begin(self.envoy, 'x = 1', 0, 100000)
        state = sys._envoy_codemode_defer
        self.assertIsNotNone(state)
        self.assertLessEqual(state['total'], 300)


class TestCodeModeInertExecuteLint(EmbodyTestCase):
    """Field report 6b.2: a Parameter Execute DAT whose watch target is the
    COMP it lives in is inert (TD recursion guard) and fires nothing, with no
    error. code_mode's settle diagnostics now surface it as a warning."""

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        self.cm = self.embody.op('envoy_codemode').module

    def test_self_watching_parexec_warns(self):
        box = self.sandbox.create(baseCOMP, 'inert_host')
        pex = box.create(parameterexecuteDAT, 'watcher')
        pex.par.op = box            # watches its own parent -> inert
        pex.par.active = True
        warns = self.cm._inertExecuteWarnings([box])
        self.assertEqual(len(warns), 1, repr(warns))
        self.assertEqual(warns[0]['nodePath'], pex.path)
        self.assertIn('own parent', warns[0]['message'])
        self.assertEqual(warns[0]['source'], 'lint')

    def test_parexec_watching_other_op_is_ok(self):
        box = self.sandbox.create(baseCOMP, 'ok_host')
        other = box.create(nullCHOP, 'target')
        pex = box.create(parameterexecuteDAT, 'watcher')
        pex.par.op = other          # watches a different op -> fine
        pex.par.active = True
        self.assertEqual(self.cm._inertExecuteWarnings([box]), [])

    def test_inactive_parexec_not_warned(self):
        box = self.sandbox.create(baseCOMP, 'inactive_host')
        pex = box.create(parameterexecuteDAT, 'watcher')
        pex.par.op = box
        pex.par.active = False      # inactive -> not a live inert callback
        self.assertEqual(self.cm._inertExecuteWarnings([box]), [])

    def test_lint_surfaces_through_code_mode(self):
        code = (
            "b = tk.make('baseCOMP', 'lint_host', parent={sb!r})\n"
            "d = tk.make('parameterexecuteDAT', 'w', parent=b)\n"
            "d.par.op = b\n"
            "d.par.active = True\n"
        ).format(sb=self.sandbox.path)
        result = self.envoy._code_mode(code=code)
        self.assertTrue(result['success'], result.get('error', ''))
        msgs = [w['message'] for w in result['diagnostics']['warnings']]
        self.assertTrue(any('own parent' in m for m in msgs),
                        f'inert-parexec warning missing: {msgs}')
