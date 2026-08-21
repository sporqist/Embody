"""
Test suite: describe -- the read-only knowledge & structure tool.

M3 of the code-mode surface. Exercises the four modes (contract / node /
network / docs) directly via env._describe (deterministic, no MCP transport),
plus required mode+target validation. Ops are created in self.sandbox so the
runner's tearDown reaps them.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestDescribe(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    # --- contract -------------------------------------------------------------

    def test_contract_lists_tk_helpers(self):
        r = self.envoy._describe('contract')
        self.assertEqual(r['mode'], 'contract')
        names = [h['name'] for h in r['helpers']]
        for expected in ('tk.make', 'tk.wire', 'tk.layout', 'tk.settle',
                         'tk.report'):
            self.assertIn(expected, names)
        self.assertTrue(r['fresh_globals'])
        # signatures are generated from the live Toolkit, so they carry args
        make = next(h for h in r['helpers'] if h['name'] == 'tk.make')
        self.assertIn('optype', make['signature'])

    def test_contract_needs_no_target(self):
        r = self.envoy._describe('contract')
        self.assertNotIn('error', r)

    # --- node -----------------------------------------------------------------

    def test_node_summary_first_default(self):
        # Default is summary-first: a one-line summary + connections, NO param
        # fan-out (that is behind full=True).
        n = self.sandbox.create(nullCHOP, 'nsum')
        r = self.envoy._describe('node', n.path)
        self.assertIn('summary', r)
        self.assertIn('inputs', r)
        self.assertNotIn('nonDefaultPars', r)
        self.assertIn('hint', r)

    def test_node_math_summary(self):
        m = self.sandbox.create(mathCHOP, 'msum')
        m.par.gain = 2
        r = self.envoy._describe('node', m.path)
        self.assertIn('*2', r['summary'])

    def test_node_full_shape(self):
        n = self.sandbox.create(nullCHOP, 'ndesc')
        r = self.envoy._describe('node', n.path, full=True)
        self.assertEqual(r['type'], 'nullCHOP')
        self.assertEqual(r['family'], 'CHOP')
        for key in ('customPars', 'nonDefaultPars', 'inputs', 'outputs'):
            self.assertIn(key, r)

    def test_node_nondefault_par_appears(self):
        n = self.sandbox.create(nullCHOP, 'nd2')
        n.par.timeslice = 1
        r = self.envoy._describe('node', n.path, full=True)
        names = [p['name'] for p in r['nonDefaultPars']]
        self.assertIn('timeslice', names)
        tp = next(p for p in r['nonDefaultPars'] if p['name'] == 'timeslice')
        self.assertIn('default', tp)
        # A toggle's eval() stringifies to 'True'/'1' depending on TD -- either
        # is the non-default (on) value.
        self.assertIn(tp['value'], ('1', 'True'))

    def test_node_sequence_collapse(self):
        # A Constant CHOP's const sequence collapses into blocks, not fanned
        # out as const0value / const1value.
        c = self.sandbox.create(constantCHOP, 'cseq')
        c.par.const0value = 5
        c.par.const1value = 9
        r = self.envoy._describe('node', c.path, full=True)
        self.assertIn('sequences', r)
        seq = next(s for s in r['sequences'] if s['sequence'] == 'const')
        self.assertTrue(seq['numBlocks'] >= 2)
        # sequence-member pars must NOT appear in the flat list
        flat = [p['name'] for p in r['nonDefaultPars']]
        self.assertNotIn('const0value', flat)

    def test_node_custom_par_appears(self):
        c = self.sandbox.create(baseCOMP, 'cp')
        page = c.appendCustomPage('X')
        page.appendFloat('Speed')
        r = self.envoy._describe('node', c.path, full=True)
        names = [p['name'] for p in r['customPars']]
        self.assertIn('Speed', names)

    def test_node_menu_shows_label_and_options(self):
        # A non-default menu par surfaces the friendly label + option list so
        # the model never guesses a token/index (value stays the token).
        g = self.sandbox.create(geometryCOMP, 'gmenu')
        g.par.xord = 'rst'
        r = self.envoy._describe('node', g.path, full=True)
        xord = next(p for p in r['nonDefaultPars'] if p['name'] == 'xord')
        self.assertEqual(xord['value'], 'rst')          # token, not an index
        self.assertIn('menu', xord)
        self.assertEqual(xord['menu']['label'], 'Rotate Scale Translate')
        names = [o['name'] for o in xord['menu']['options']]
        self.assertIn('srt', names)

    def test_node_missing_target_errors(self):
        r = self.envoy._describe('node')
        self.assertIn('error', r)

    def test_node_bad_path_errors(self):
        r = self.envoy._describe('node', '/no/such/op')
        self.assertIn('error', r)

    # --- network --------------------------------------------------------------

    def test_network_topology(self):
        self.sandbox.create(nullCHOP, 'na')
        self.sandbox.create(nullCHOP, 'nb')
        r = self.envoy._describe('network', self.sandbox.path)
        self.assertEqual(r['mode'], 'network')
        self.assertTrue(r['count'] >= 2)
        names = [o['name'] for o in r['operators']]
        self.assertIn('na', names)
        self.assertIn('nb', names)

    def test_network_sparse_dump(self):
        self.sandbox.create(nullCHOP, 'nd')
        r = self.envoy._describe('network', self.sandbox.path, 1, True)
        self.assertIn('sparse', r)
        self.assertIsInstance(r['sparse'], dict)

    def test_network_non_comp_errors(self):
        n = self.sandbox.create(nullCHOP, 'notacomp')
        r = self.envoy._describe('network', n.path)
        self.assertIn('error', r)

    # --- docs (live introspection (+) offline-wiki fusion) --------------------

    def test_docs_live_params_authoritative(self):
        r = self.envoy._describe('docs', 'nullCHOP')
        self.assertEqual(r.get('optype'), 'nullCHOP')
        params = r.get('parameters') or []
        self.assertTrue(len(params) > 0)
        self.assertIn('name', params[0])
        self.assertIn('default', params[0])
        self.assertEqual(r.get('parameterSource'),
                         'live introspection (running build)')
        # menu params carry name + friendly label pairs
        menu_par = next((p for p in params if 'menu' in p), None)
        if menu_par is not None:
            opt = menu_par['menu'][0]
            self.assertIn('name', opt)
            self.assertIn('label', opt)

    def test_docs_wiki_fused_when_mirror_present(self):
        r = self.envoy._describe('docs', 'noiseTOP')
        self.assertEqual(r.get('optype'), 'noiseTOP')  # live always present
        # The offline mirror may be absent on some installs -- assert the wiki
        # fusion only when it resolved a page.
        if 'wiki' in r:
            self.assertTrue(bool(r.get('summary')))
            self.assertIn('sections_available', r['wiki'])

    def test_docs_unknown_optype_errors(self):
        r = self.envoy._describe('docs', 'totallyFakeXYZ123')
        self.assertIn('error', r)

    def test_docs_missing_target_errors(self):
        r = self.envoy._describe('docs')
        self.assertIn('error', r)

    # --- validation -----------------------------------------------------------

    def test_unknown_mode_errors(self):
        r = self.envoy._describe('nonsense')
        self.assertIn('error', r)
        self.assertIn('unknown mode', r['error'])


class TestDescribeSequenceAccess(EmbodyTestCase):
    """Field report 6b.1: sequence pages are 0-indexed (vec0name, ...) and
    describe(node, full) must SPELL OUT the enumeration so an agent reads the
    origin instead of guessing it."""

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    def test_sequence_access_hint_is_zero_indexed(self):
        box = self.sandbox.create(baseCOMP, 'seq_host')
        g = box.create(glslmultiTOP, 'shader')
        g.seq.vec.numBlocks = 2
        g.seq.vec[0].par.name = 'uAlt'
        g.seq.vec[1].par.name = 'uTime'

        d = self.envoy._describe('node', g.path, full=True)
        seqs = {s['sequence']: s for s in (d.get('sequences') or [])}
        self.assertIn('vec', seqs, 'populated vec sequence must be described')
        vec = seqs['vec']
        self.assertEqual(vec['numBlocks'], 2)
        self.assertIn('0-indexed', vec['access'])
        self.assertIn('op.seq.vec[i]', vec['access'])
        # Blocks enumerate from 0, carrying the authored names.
        self.assertEqual(vec['blocks'][0]['index'], 0)
        self.assertEqual(vec['blocks'][0]['pars'].get('name'), 'uAlt')
        self.assertEqual(vec['blocks'][1]['index'], 1)
