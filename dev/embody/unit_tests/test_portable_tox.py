"""
Test suite: ExportPortableTox ship-at-default preference reset.

A release .tox is exported from the live dev COMP, so any user-preference
parameter left at a non-default DEV value would ship that preference to every
user (e.g. Clipboardautopaste=False on the dev machine silently disabled
auto-paste for everyone who installed the release). ExportPortableTox now
resets the ship-at-default params to their default for the exported artifact
and restores the live value afterward. Ports the concept of upstream
v6.0.252's Clipboardautopaste release-leak fix.
"""

import os

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestPortableToxShipDefaults(EmbodyTestCase):

    def _tmp_path(self, name):
        d = os.path.join(project.folder, 'embody', 'unit_tests', '_test_temp')
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, name)

    def _comp_with_pref(self, name, default, value):
        """A COMP carrying a custom Clipboardautopaste toggle."""
        box = self.sandbox.create(baseCOMP, name)
        page = box.appendCustomPage('Test')
        par = page.appendToggle('Clipboardautopaste')[0]
        par.default = default
        par.val = value
        return box

    def test_shipped_tox_carries_the_default_not_the_dev_value(self):
        box = self._comp_with_pref('ship_a', default=True, value=False)
        path = self._tmp_path('ship_a.tox')
        try:
            self.embody.ExportPortableTox(target=box, save_path=path)
            dest = self.sandbox.create(baseCOMP, 'ship_a_loaded')
            loaded = dest.loadTox(path)
            self.assertIsNotNone(loaded)
            self.assertTrue(
                loaded.par.Clipboardautopaste.eval(),
                'the release .tox must ship the DEFAULT (True), not the dev '
                'machine preference (False)')
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_live_dev_value_is_restored_after_export(self):
        box = self._comp_with_pref('ship_b', default=True, value=False)
        path = self._tmp_path('ship_b.tox')
        try:
            self.embody.ExportPortableTox(target=box, save_path=path)
            # The dev machine keeps its own preference; only the artifact
            # shipped the default.
            self.assertFalse(box.par.Clipboardautopaste.eval(),
                             'the live dev value must be restored after export')
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_at_default_pref_is_untouched(self):
        # Nothing to reset when the pref is already at its default.
        box = self._comp_with_pref('ship_c', default=True, value=True)
        path = self._tmp_path('ship_c.tox')
        try:
            self.embody.ExportPortableTox(target=box, save_path=path)
            self.assertTrue(box.par.Clipboardautopaste.eval())
        finally:
            if os.path.exists(path):
                os.remove(path)
