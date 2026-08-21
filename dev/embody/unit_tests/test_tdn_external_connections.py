"""
Test suite: external connection preservation across TDN strip/rebuild.

Issue #11 - wires from external siblings into a TDN-strategy BaseCOMP's
own input connectors (backed by inCHOP/inTOP/etc. inside the COMP) are
severed when the COMP's children are destroyed. The fix captures these
external wires before strip and restores them after rebuild.

Covered paths:
  - StripCompChildren + ImportNetwork(clear_first=True) via stored wires
    (simulates the Ctrl+S pre-save / post-save cycle and cold-open
    reconstruction).
  - ImportNetwork(clear_first=True) with live wires (simulates a user
    reload of a TDN COMP from disk).
  - Tolerance when the external sibling is deleted between capture and
    restore.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestTDNExternalConnections(EmbodyTestCase):

	def setUp(self):
		super().setUp()
		self.tdn = self.embody.ext.TDN

	# --------------------------------------------------------------
	# Helpers
	# --------------------------------------------------------------

	def _buildPassthroughBase(self, name='base1'):
		"""Create a BaseCOMP with an inCHOP -> outCHOP passthrough."""
		base = self.sandbox.create(baseCOMP, name)
		in_chop = base.create(inCHOP, 'in1')
		out_chop = base.create(outCHOP, 'out1')
		out_chop.inputConnectors[0].connect(in_chop.outputConnectors[0])
		return base

	def _countInputWires(self, op_obj, index=0):
		"""Count live wires on an input connector."""
		return len(op_obj.inputConnectors[index].connections)

	# --------------------------------------------------------------
	# Capture/restore via storage (save cycle + cold open)
	# --------------------------------------------------------------

	def test_strip_then_import_restores_input_wire(self):
		"""External source -> BaseCOMP input survives strip+rebuild."""
		src = self.sandbox.create(constantCHOP, 'src')
		base = self._buildPassthroughBase()
		# External wire: src -> base input connector
		src.outputConnectors[0].connect(base.inputConnectors[0])
		self.assertEqual(self._countInputWires(base), 1)

		# Export, strip, import (mirrors pre-save / post-save).
		result = self.tdn.ExportNetwork(root_path=base.path)
		self.assertTrue(result.get('success'))
		tdn_doc = result['tdn']

		self.embody.ext.Embody.StripCompChildren(base)
		self.assertEqual(len(base.children), 0)
		# External capture should have been stashed on the COMP itself.
		stashed = base.fetch('_tdn_external_wires', [], search=False)
		self.assertTrue(stashed,
			'StripCompChildren did not stash external wires')

		imp = self.tdn.ImportNetwork(
			target_path=base.path, tdn=tdn_doc, clear_first=True)
		self.assertTrue(imp.get('success'), f'Import failed: {imp}')
		self.assertEqual(imp.get('restored_external_connections'), 1)

		# Wire should be back on the rebuilt input connector.
		self.assertEqual(self._countInputWires(base), 1,
			'External input wire not restored after strip+rebuild')
		# Stash consumed.
		self.assertEqual(
			base.fetch('_tdn_external_wires', [], search=False), [])

	def test_strip_then_import_restores_output_wire(self):
		"""BaseCOMP output -> external dest survives strip+rebuild."""
		dst = self.sandbox.create(nullCHOP, 'dst')
		base = self._buildPassthroughBase()
		base.outputConnectors[0].connect(dst.inputConnectors[0])
		self.assertEqual(self._countInputWires(dst), 1)

		result = self.tdn.ExportNetwork(root_path=base.path)
		tdn_doc = result['tdn']

		self.embody.ext.Embody.StripCompChildren(base)
		imp = self.tdn.ImportNetwork(
			target_path=base.path, tdn=tdn_doc, clear_first=True)
		self.assertTrue(imp.get('success'))
		self.assertEqual(imp.get('restored_external_connections'), 1)
		self.assertEqual(self._countInputWires(dst), 1,
			'External output wire not restored after strip+rebuild')

	def test_strip_then_import_restores_both_directions(self):
		"""Issue #11 scenario: src -> base -> dst survives strip+rebuild."""
		src = self.sandbox.create(constantCHOP, 'src')
		dst = self.sandbox.create(nullCHOP, 'dst')
		base = self._buildPassthroughBase()
		src.outputConnectors[0].connect(base.inputConnectors[0])
		base.outputConnectors[0].connect(dst.inputConnectors[0])

		result = self.tdn.ExportNetwork(root_path=base.path)
		tdn_doc = result['tdn']

		self.embody.ext.Embody.StripCompChildren(base)
		imp = self.tdn.ImportNetwork(
			target_path=base.path, tdn=tdn_doc, clear_first=True)
		self.assertTrue(imp.get('success'))
		self.assertEqual(imp.get('restored_external_connections'), 2)

		self.assertEqual(self._countInputWires(base), 1,
			'Input wire not restored')
		self.assertEqual(self._countInputWires(dst), 1,
			'Output wire not restored')

	# --------------------------------------------------------------
	# Live-capture path (user reload)
	# --------------------------------------------------------------

	def test_import_live_reload_restores_wires(self):
		"""ImportNetwork(clear_first=True) on a live COMP preserves wires."""
		src = self.sandbox.create(constantCHOP, 'src')
		dst = self.sandbox.create(nullCHOP, 'dst')
		base = self._buildPassthroughBase()
		src.outputConnectors[0].connect(base.inputConnectors[0])
		base.outputConnectors[0].connect(dst.inputConnectors[0])

		result = self.tdn.ExportNetwork(root_path=base.path)
		tdn_doc = result['tdn']

		# No explicit strip - ImportNetwork should capture in-memory first.
		imp = self.tdn.ImportNetwork(
			target_path=base.path, tdn=tdn_doc, clear_first=True)
		self.assertTrue(imp.get('success'))
		self.assertEqual(imp.get('restored_external_connections'), 2)
		self.assertEqual(self._countInputWires(base), 1)
		self.assertEqual(self._countInputWires(dst), 1)

	# --------------------------------------------------------------
	# Tolerance
	# --------------------------------------------------------------

	def test_missing_remote_does_not_break_restore(self):
		"""Deleted remote op logs a warning; other wires still restored."""
		src = self.sandbox.create(constantCHOP, 'src')
		dst = self.sandbox.create(nullCHOP, 'dst')
		base = self._buildPassthroughBase()
		src.outputConnectors[0].connect(base.inputConnectors[0])
		base.outputConnectors[0].connect(dst.inputConnectors[0])

		result = self.tdn.ExportNetwork(root_path=base.path)
		tdn_doc = result['tdn']

		self.embody.ext.Embody.StripCompChildren(base)
		# Delete one remote between capture and restore.
		src.destroy()

		imp = self.tdn.ImportNetwork(
			target_path=base.path, tdn=tdn_doc, clear_first=True)
		self.assertTrue(imp.get('success'))
		# Only the output wire should be restored.
		self.assertEqual(imp.get('restored_external_connections'), 1)
		self.assertEqual(self._countInputWires(dst), 1)

	def test_empty_comp_capture_is_noop(self):
		"""BaseCOMP with no inputs/outputs: capture returns [], no store."""
		base = self.sandbox.create(baseCOMP, 'lonely')
		self.embody.ext.Embody.StripCompChildren(base)
		self.assertEqual(
			base.fetch('_tdn_external_wires', [], search=False), [])


class TestTDNSparseInputConnections(EmbodyTestCase):
	"""A wire on a non-zero input index must keep that index across a TDN
	round-trip. OP.inputs is COMPACTED, so a wire on input 1 (input 0 empty)
	reads as index 0 there -- the old exporter collapsed it onto input 0 (a
	crossTOP's right input silently moved to the left). Ported from upstream
	v6.0.241; the exporter now enumerates positional inputConnectors."""

	def setUp(self):
		super().setUp()
		self.tdn = self.embody.ext.TDN

	def test_export_preserves_gap_before_wire(self):
		box = self.sandbox.create(baseCOMP, 'sparse_box')
		src = box.create(noiseTOP, 'src')
		x = box.create(crossTOP, 'x')
		# Wire src -> input index 1, leaving input 0 empty.
		src.outputConnectors[0].connect(x.inputConnectors[1])
		exported = self.tdn._exportConnections(x)
		self.assertEqual(exported, [None, 'src'],
			'a wire on input 1 must export as [null, "src"], not ["src"]')

	def test_roundtrip_keeps_wire_on_input_1(self):
		box = self.sandbox.create(baseCOMP, 'sparse_rt')
		src = box.create(noiseTOP, 'src')
		x = box.create(crossTOP, 'x')
		src.outputConnectors[0].connect(x.inputConnectors[1])

		result = self.tdn.ExportNetwork(root_path=box.path)
		self.assertTrue(result.get('success'))
		dest = self.sandbox.create(baseCOMP, 'sparse_dest')
		self.tdn.ImportNetwork(dest.path, result['tdn'], clear_first=True)

		dx = dest.op('x')
		self.assertIsNotNone(dx)
		self.assertEqual(len(dx.inputConnectors[0].connections), 0,
			'input 0 must stay empty')
		self.assertEqual(len(dx.inputConnectors[1].connections), 1,
			'the wire must land back on input 1, not collapse to input 0')

	def test_contiguous_inputs_still_export(self):
		# Regression guard: the common (no-gap) case is unchanged.
		box = self.sandbox.create(baseCOMP, 'contig')
		a = box.create(noiseTOP, 'a')
		b = box.create(noiseTOP, 'b')
		x = box.create(crossTOP, 'x')
		a.outputConnectors[0].connect(x.inputConnectors[0])
		b.outputConnectors[0].connect(x.inputConnectors[1])
		self.assertEqual(self.tdn._exportConnections(x), ['a', 'b'])
