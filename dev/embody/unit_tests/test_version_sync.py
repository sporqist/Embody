"""
Test suite: version / minimum-build doc sync.

Guards the user-facing version statements that onProjectPreSave
(execute_src_ctrl.updateVersionDocs) keeps in lock-step on every save:
the README version badge must match par.Version, and the minimum-TD-build
statements in README.md, docs/index.md, and CONTRIBUTING.md must agree
with each other and with par.Touchbuild (the build of the last save --
which IS the support floor, since TD files do not open in older builds).

Also drives the pure line-rewriter directly so the anchored-substitution
logic is covered without touching files.
"""

import re
from pathlib import Path


runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


BUILD_RE = re.compile(r'\b\d{4}\.\d{3,6}\b')

# The last version upstream (dylanroscover/Embody) actually published before
# this fork diverged -- commit 2c980a8, dev/Embody-6.141.toe. Frozen: it says
# which upstream Embody/Envoy a build is compatible with, and a save must
# never advance it (see test_version_never_claims_an_upstream_number).
UPSTREAM_ANCHOR = '6.0.141'

README_ANCHOR = '**Requirements:** TouchDesigner'
DOCS_ANCHOR = '- **TouchDesigner '
CONTRIB_ANCHOR = '- **TouchDesigner '


class TestVersionSync(EmbodyTestCase):

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _repo_root(self):
        return Path(project.folder).resolve().parent

    def _read(self, *parts):
        path = self._repo_root().joinpath(*parts)
        self.assertTrue(path.is_file(), f'missing doc file: {path}')
        return path.read_text(encoding='utf-8')

    def _min_build(self, text, anchor, label):
        for line in text.splitlines():
            if line.startswith(anchor):
                match = BUILD_RE.search(line)
                self.assertIsNotNone(
                    match, f'{label}: anchored line has no build number: {line!r}')
                return match.group(0)
        self.fail(f'{label}: anchor line not found: {anchor!r}')

    def _src_ctrl(self):
        return opex('/embody/execute_src_ctrl').module

    # ------------------------------------------------------------------
    # Doc consistency (the tripwire)
    # ------------------------------------------------------------------

    def test_readme_badge_matches_par_version(self):
        text = self._read('README.md')
        # The value carries this fork's +cm.N build suffix (frozen upstream
        # anchor + our counter) -- the suffix must be part of the capture or
        # the comparison below trivially fails.
        match = re.search(r'badge/version-([0-9][0-9.]*(?:\+cm\.[0-9]+)?)-', text)
        self.assertIsNotNone(match, 'version badge missing from README')
        self.assertEqual(
            match.group(1), op.Embody.par.Version.eval(),
            'README version badge out of sync with par.Version -- '
            'updateVersionDocs should rewrite it on every save')

    def test_version_never_claims_an_upstream_number(self):
        """par.Version must keep the frozen upstream anchor + our counter.

        The regression this guards: the save hook used to bump the last dotted
        segment, walking the UPSTREAM triple forward on every save. Builds
        6.0.142-6.0.144 claimed upstream numbers this fork does not own, and a
        release shipped advertising an upstream 6.0.144 that does not exist.
        """
        v = op.Embody.par.Version.eval()
        m = re.match(r'^(\d+\.\d+\.\d+)\+cm\.(\d+)$', v)
        self.assertIsNotNone(
            m, f'par.Version {v!r} must be <upstream>+cm.<n>, e.g. 6.0.141+cm.4')
        self.assertEqual(
            m.group(1), UPSTREAM_ANCHOR,
            f'upstream anchor moved to {m.group(1)} -- it is frozen at '
            f'{UPSTREAM_ANCHOR} (the last release upstream actually published '
            f'before this fork) and must never be incremented by a save')

    def test_hook_bumps_only_the_build_counter(self):
        """The generator itself must never advance the upstream triple.

        Drives the PURE bump_version() -- version() writes par.Version, so
        calling it here would mutate the real project version.
        """
        m = self._src_ctrl()
        self.assertEqual(m.bump_version('6.0.141+cm.4'), '6.0.141+cm.5')
        self.assertEqual(m.bump_version('6.0.141+cm.9'), '6.0.141+cm.10')
        self.assertEqual(m.bump_version('6.0.141+cm.99'), '6.0.141+cm.100')
        # The upstream triple is untouched no matter how far the counter runs.
        for probe in ('6.0.141+cm.1', '6.0.141+cm.7'):
            self.assertTrue(m.bump_version(probe).startswith(UPSTREAM_ANCHOR + '+cm.'))
        # A bare upstream triple (the OLD scheme) is rejected, not bumped --
        # this is exactly the input that used to produce 6.0.145.
        self.assertIsNone(m.bump_version('6.0.144'))
        self.assertIsNone(m.bump_version('6.0.141+cm'))
        self.assertIsNone(m.bump_version(''))

    def test_min_build_consistent_across_docs(self):
        readme = self._min_build(self._read('README.md'), README_ANCHOR, 'README.md')
        docs = self._min_build(self._read('docs', 'index.md'), DOCS_ANCHOR, 'docs/index.md')
        contrib = self._min_build(self._read('CONTRIBUTING.md'), CONTRIB_ANCHOR, 'CONTRIBUTING.md')
        self.assertEqual(readme, docs, 'README vs docs/index.md minimum build drift')
        self.assertEqual(readme, contrib, 'README vs CONTRIBUTING.md minimum build drift')

    def test_min_build_matches_touchbuild(self):
        readme = self._min_build(self._read('README.md'), README_ANCHOR, 'README.md')
        self.assertEqual(
            readme, op.Embody.par.Touchbuild.eval(),
            'documented minimum build out of sync with par.Touchbuild '
            '(the build of the last save is the support floor)')

    # ------------------------------------------------------------------
    # Rewriter unit coverage (pure, no file writes)
    # ------------------------------------------------------------------

    def test_rewriter_updates_anchored_line_only(self):
        m = self._src_ctrl()
        text = (
            '**Requirements:** TouchDesigner **2025.11111 or later** blah\n'
            'history mentions TD 2025.22222 and must not change\n'
        )
        transforms = [(README_ANCHOR, lambda l: m.BUILD_RE.sub('2025.33333', l, count=1))]
        new_text, changed = m._rewriteText(text, transforms)
        self.assertTrue(changed)
        self.assertIn('2025.33333', new_text)
        self.assertIn('2025.22222', new_text,
                      'non-anchored line must never be rewritten')
        self.assertNotIn('2025.11111', new_text)

    def test_rewriter_reports_unchanged(self):
        m = self._src_ctrl()
        text = 'no anchored lines here\n'
        transforms = [(README_ANCHOR, lambda l: 'REWRITTEN\n')]
        new_text, changed = m._rewriteText(text, transforms)
        self.assertFalse(changed)
        self.assertEqual(new_text, text)

    def test_badge_regexes_hit_current_readme(self):
        # The anchors/regexes in updateVersionDocs must keep matching the
        # real README -- if the badge markup is ever restyled, this fails
        # instead of the bumper silently no-oping (the pre-2026 failure
        # mode this suite exists to prevent).
        text = self._read('README.md')
        lines = text.splitlines()
        self.assertTrue(
            any(l.startswith('[![Version](https://img.shields.io/badge/version-')
                for l in lines),
            'README version badge anchor no longer matches')
        self.assertTrue(
            any(l.startswith(README_ANCHOR) for l in lines),
            'README requirements anchor no longer matches')
