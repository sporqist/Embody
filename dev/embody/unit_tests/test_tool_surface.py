"""
Test suite: tool-surface flag (M5) -- hide the 53 verb tools behind the 3
code-mode tools.

Tests the prune logic on a THROWAWAY FastMCP instance (never the live server --
toggling that would restart Envoy mid-test) plus the pref accessor + the
SetToolSurface validation guard.
"""

import sys

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestToolSurface(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = op.Embody.ext.Envoy
        self.srv_cls = op.Embody.op('EnvoyExt').module.EnvoyMCPServer

    def _fastmcp_with(self, names):
        from mcp.server.fastmcp import FastMCP
        m = FastMCP('test_surface')

        def make(nm):
            def f() -> str:
                "probe tool"
                return nm
            f.__name__ = nm
            return f
        for nm in names:
            m.tool()(make(nm))
        return m

    def _prune_stub(self, mcp, surface):
        class Stub:
            pass
        s = Stub()
        s.mcp = mcp
        s._CODEMODE_TOOLS = self.srv_cls._CODEMODE_TOOLS
        saved = getattr(sys, '_envoy_tool_surface', '<unset>')
        try:
            sys._envoy_tool_surface = surface
            self.srv_cls._applyToolSurface(s)
        finally:
            if saved == '<unset>':
                if hasattr(sys, '_envoy_tool_surface'):
                    del sys._envoy_tool_surface
            else:
                sys._envoy_tool_surface = saved
        return set(mcp._tool_manager._tools.keys())

    # --- prune logic ----------------------------------------------------------

    def test_codemode_keeps_only_three(self):
        m = self._fastmcp_with(
            ['code_mode', 'describe', 'view', 'create_op', 'get_op',
             'delete_op', 'set_parameter'])
        remaining = self._prune_stub(m, 'codemode')
        self.assertEqual(remaining, {'code_mode', 'describe', 'view'})

    def test_full_keeps_everything(self):
        names = ['code_mode', 'describe', 'view', 'create_op', 'get_op']
        m = self._fastmcp_with(names)
        remaining = self._prune_stub(m, 'full')
        self.assertEqual(remaining, set(names))

    def test_codemode_tools_constant(self):
        self.assertEqual(self.srv_cls._CODEMODE_TOOLS,
                         frozenset({'code_mode', 'describe', 'view'}))

    # --- pref accessor + guard ------------------------------------------------

    def test_pref_default_is_codemode(self):
        # The code-mode surface is the default; SetToolSurface('full') opts back.
        prev = op.Embody.fetch('_tool_surface', None, search=False)
        try:
            op.Embody.unstore('_tool_surface')
            self.assertEqual(self.envoy._toolSurfacePref(), 'codemode')
        finally:
            if prev is not None:
                op.Embody.store('_tool_surface', prev)

    def test_pref_reads_stored_value(self):
        prev = op.Embody.fetch('_tool_surface', None, search=False)
        try:
            op.Embody.store('_tool_surface', 'codemode')
            self.assertEqual(self.envoy._toolSurfacePref(), 'codemode')
        finally:
            if prev is None:
                op.Embody.unstore('_tool_surface')
            else:
                op.Embody.store('_tool_surface', prev)

    def test_set_tool_surface_rejects_bad_mode(self):
        # Bad mode returns an error BEFORE storing/restarting -- safe to call.
        r = self.envoy.SetToolSurface('bogus')
        self.assertIn('error', r)
