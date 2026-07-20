# M1 live-bootstrap: create the envoy_codemode module DAT, then verify.
#
# The code_mode surface is landed at the FILE level (envoy_codemode.py +
# EnvoyExt.py wiring + test_codemode.py). But envoy_codemode.py is a NEW module
# DAT that does not yet exist inside the .toe -- Embody syncs existing DATs from
# disk, it does not create missing ones. This snippet creates that DAT (loading
# its body from the on-disk file), externalizes it like its sibling modules, and
# is safe to re-run (idempotent).
#
# HOW TO RUN (pick one):
#   A) Fresh Claude Code session with Envoy MCP up:  paste this into an
#      execute_python call.
#   B) No MCP:  paste this into TouchDesigner's Textport.
#
# After it prints "envoy_codemode DAT ready", run the tests:
#   op.unit_tests.RunTestsSync(suite_name='test_codemode')   # focused
#   op.unit_tests.RunTestsSync()                              # full suite
# then project.save() to bake the new DAT into the .toe.

import os

emb = op.Embody
host = op('/embody/Embody')            # where the envoy_* module DATs live
assert host is not None, 'Embody module host /embody/Embody not found'

src_path = project.folder + '/embody/Embody/envoy_codemode.py'
assert os.path.isfile(src_path), 'envoy_codemode.py not found on disk: ' + src_path
with open(src_path, encoding='utf-8') as f:
    body = f.read()

dat = host.op('envoy_codemode')
if dat is None:
    dat = host.create(textDAT, 'envoy_codemode')
    # Tidy: extend the module column (siblings sit at x=-500, y=400..800).
    dat.nodeX = -500
    dat.nodeY = 900
dat.text = body

# Externalize as a .py module, matching envoy_ops/envoy_read/... so it is
# tracked, syncfile'd, and persists. Uses Embody's tag+externalize path.
try:
    emb.ext.Envoy._externalize_op('/embody/Embody/envoy_codemode', 'py')
except Exception as e:
    print('externalize warning (DAT still usable this session):', e)

# The code_mode MCP tool is registered from EnvoyExt.py at server start. On a
# FRESH TD launch it is already registered (the edit is on disk), so no restart
# is needed -- creating this DAT is enough for dispatch to resolve. If Envoy
# started BEFORE the EnvoyExt.py edit synced, cycle the server so the tool
# appears in tools/list:
try:
    emb.ext.Envoy.Stop()
    emb.ext.Envoy.Start()
except Exception as e:
    print('Envoy cycle note:', e)

# Sanity: the dispatch module now resolves.
assert mod.envoy_codemode is not None, 'mod.envoy_codemode did not resolve'
print('envoy_codemode DAT ready at', dat.path,
      '-- now run op.unit_tests.RunTestsSync(suite_name="test_codemode")')
result = dat.path
