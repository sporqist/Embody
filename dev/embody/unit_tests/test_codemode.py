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
