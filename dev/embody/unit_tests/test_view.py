"""
Test suite: view -- read-only renders (TOP image / CHOP-DAT reduction / diff).

M4 of the code-mode surface. Exercises env._view directly (deterministic, no
MCP transport / no inline-image packaging -- that is the worker-side wrapper).
Ops are created in self.sandbox so the runner's tearDown reaps them.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestView(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    # --- TOP -> image ---------------------------------------------------------

    def test_top_returns_image(self):
        t = self.sandbox.create(noiseTOP, 'vt')
        t.par.resolutionw = 128
        t.par.resolutionh = 128
        r = self.envoy._view(t.path)
        self.assertEqual(r['kind'], 'top')
        self.assertTrue(r.get('image_b64'))
        self.assertEqual(r['format'], 'png')
        # Noise is not a black frame -> quality passes.
        self.assertTrue((r.get('quality') or {}).get('pass'))

    def test_top_resolution_cap(self):
        t = self.sandbox.create(noiseTOP, 'vt2')
        t.par.resolutionw = 512
        t.par.resolutionh = 512
        r = self.envoy._view(t.path, resolution=128)
        self.assertTrue(max(r['width'], r['height']) <= 128)

    def test_non_av_family_errors(self):
        c = self.sandbox.create(baseCOMP, 'vcomp')
        r = self.envoy._view(c.path)
        self.assertIn('error', r)
        self.assertIn('does not support', r['error'])

    def test_bad_target_errors(self):
        r = self.envoy._view('/no/such/op')
        self.assertIn('error', r)

    # --- CHOP -> reduced render ----------------------------------------------

    def test_chop_stats_and_head(self):
        c = self.sandbox.create(constantCHOP, 'vc')
        c.par.const0value = 5
        r = self.envoy._view(c.path)
        self.assertEqual(r['kind'], 'chop')
        ch0 = r['channels'][0]
        self.assertEqual(ch0['name'], 'chan1')
        self.assertEqual(ch0['stats']['mean'], 5.0)
        self.assertEqual(ch0['head'], [5.0])

    def test_chop_channel_filter(self):
        c = self.sandbox.create(constantCHOP, 'vc2')
        r = self.envoy._view(c.path, channels='nomatch*')
        self.assertEqual(r['shownChannels'], 0)
        self.assertTrue(r['numChannels'] >= 1)

    # --- DAT -> reduced render -----------------------------------------------

    def test_dat_rows(self):
        d = self.sandbox.create(tableDAT, 'vd')
        d.clear()
        d.appendRow(['a', 'b'])
        d.appendRow(['1', '2'])
        r = self.envoy._view(d.path)
        self.assertEqual(r['kind'], 'dat')
        self.assertEqual(r['numRows'], 2)
        self.assertEqual(r['rows'][0], ['a', 'b'])

    def test_dat_col_selection(self):
        d = self.sandbox.create(tableDAT, 'vd2')
        d.clear()
        d.appendRow(['a', 'b', 'c'])
        d.appendRow(['1', '2', '3'])
        r = self.envoy._view(d.path, cols=[0, 2])
        self.assertEqual(r['rows'][0], ['a', 'c'])

    # --- relational diff (op vs op) ------------------------------------------

    def test_relational_diff(self):
        a = self.sandbox.create(constantCHOP, 'va')
        a.par.const0value = 5
        b = self.sandbox.create(constantCHOP, 'vb')
        b.par.const0value = 9
        r = self.envoy._view(a.path, other=b.path)
        self.assertEqual(r['kind'], 'diff')
        self.assertEqual(r['axis'], 'relational')
        changed = r['diff']['changedMeans']
        self.assertEqual(changed[0]['channel'], 'chan1')
        self.assertEqual(changed[0]['delta'], 4.0)

    def test_relational_diff_family_mismatch_errors(self):
        a = self.sandbox.create(constantCHOP, 'vam')
        b = self.sandbox.create(noiseTOP, 'vbm')
        r = self.envoy._view(a.path, other=b.path)
        self.assertIn('error', r)

    # --- temporal diff (vs last view / pinned) -------------------------------

    def test_temporal_diff_vs_last_view(self):
        # The snapshot store persists on sys per (sid, path); the sandbox path
        # repeats across runs, so use a run-unique sid (sandbox.id) to isolate.
        sid = 'temporal-%d' % self.sandbox.id
        c = self.sandbox.create(constantCHOP, 'vtemp')
        c.par.const0value = 5
        # first view: no prior snapshot -> no temporal_diff
        first = self.envoy._view(c.path, sid=sid)
        self.assertNotIn('temporal_diff', first)
        # change + re-view -> temporal_diff appears
        c.par.const0value = 7
        second = self.envoy._view(c.path, sid=sid)
        self.assertIn('temporal_diff', second)
        self.assertEqual(second['temporal_diff']['changedMeans'][0]['delta'], 2.0)
        self.assertEqual(second['temporal_baseline'], 'last_view')

    def test_pin_baseline(self):
        sid = 'pin-%d' % self.sandbox.id
        c = self.sandbox.create(constantCHOP, 'vpin')
        c.par.const0value = 1
        self.envoy._view(c.path, sid=sid, pin=True)
        c.par.const0value = 4
        r = self.envoy._view(c.path, sid=sid)
        self.assertEqual(r['temporal_baseline'], 'pinned')
        self.assertEqual(r['temporal_diff']['changedMeans'][0]['delta'], 3.0)
