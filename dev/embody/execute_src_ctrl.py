# me - this DAT
#
# frame - the current frame
# state - True if the timeline is paused
#
# Make sure the corresponding toggle is enabled in the Execute DAT.

import re
from pathlib import Path

comp = op.Embody
root = Path(project.folder).parents[0]

# TD build pattern, e.g. 2025.32820. Only ever substituted on ANCHORED lines
# (startswith match below) -- never file-wide -- so changelog/version-history
# mentions of older builds are left alone.
BUILD_RE = re.compile(r'\b\d{4}\.\d{3,6}\b')


# This fork's version is a FROZEN upstream anchor plus our own build counter:
#
#     6.0.141+cm.4
#     ^^^^^^^ ^^^^
#     |       |
#     |       our build counter -- the ONLY part that ever moves
#     upstream Embody (dylanroscover/Embody) this fork diverged from
#
# 6.0.141 is the last version upstream actually published before the fork
# (commit 2c980a8, dev/Embody-6.141.toe); it states which upstream
# Embody/Envoy a build is compatible with and must never be incremented here.
#
# Why parsed strictly instead of "bump the last dotted segment": the previous
# implementation did exactly that, so every save walked the UPSTREAM triple
# forward. Builds 6.0.142-6.0.144 silently claimed upstream numbers this fork
# does not own, and a release was published advertising compatibility with an
# upstream 6.0.144 that does not exist. Matching the shape explicitly makes
# that class of mistake structurally impossible rather than merely unlikely.
VERSION_RE = re.compile(r'^(?P<base>\d+\.\d+\.\d+)\+cm\.(?P<build>\d+)$')


def bump_version(version):
    """Pure: next version string, or None if the shape is unrecognized.

    Kept side-effect free (like _rewriteText) so the whole increment rule is
    unit-testable without writing to par.Version.
    """
    match = VERSION_RE.match(str(version).strip())
    if not match:
        return None
    return f"{match.group('base')}+cm.{int(match.group('build')) + 1}"


def version(version):
    """Bump ONLY the fork build counter; never the upstream anchor.

    On an unrecognized shape the version is left untouched and the problem is
    reported, rather than guessed at -- a wrong version number is worse than a
    stale one, and this must never abort the user's save.
    """
    new_version = bump_version(version)
    if new_version is None:
        debug(f'par.Version {version!r} is not <upstream>+cm.<n> -- leaving it '
              f'unchanged. Set it to e.g. 6.0.141+cm.1 to restore versioning.')
        return version
    comp.par.Version.val = new_version
    return new_version


def _rewriteText(text, transforms):
    """Pure line rewriter: transforms is [(startswith_anchor, fn), ...] where
    fn(line) -> new line. The first matching anchor wins per line. Returns
    (new_text, changed)."""
    lines = text.splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        for anchor, fn in transforms:
            if line.startswith(anchor):
                new_line = fn(line)
                if new_line != line:
                    lines[i] = new_line
                    changed = True
                break
    return ''.join(lines), changed


def _rewriteFile(path, transforms):
    """Apply _rewriteText to a file in place. UTF-8 pinned (README contains
    emoji; locale-default codecs crash on Windows). Returns True if written."""
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    new_text, changed = _rewriteText(text, transforms)
    if changed:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_text)
    return changed


def updateVersionDocs(build, new_version):
    """Keep every user-facing version / minimum-build statement in lock-step
    with this save. The build we save with IS the minimum supported build
    (TD files do not open in older builds), so README, docs/index.md, and
    CONTRIBUTING.md restate the running build; the README badges restate
    par.Version and the build's year. Each file is guarded so a missing or
    unwritable doc can never abort a project save."""
    year = str(build).split('.')[0]
    targets = [
        (root / 'README.md', [
            # The value may carry our +cm.N build suffix -- match it, or the
            # rewrite silently stops working after the first transition
            # (`[0-9.]*` halts at the '+', so the trailing '-' never matches).
            ('[![Version](https://img.shields.io/badge/version-',
             lambda l: re.sub(r'version-[0-9][0-9.]*(?:\+cm\.[0-9]+)?-',
                              f'version-{new_version}-', l, count=1)),
            ('[![TouchDesigner](https://img.shields.io/badge/TouchDesigner-',
             lambda l: re.sub(r'TouchDesigner-\d{4}-', f'TouchDesigner-{year}-', l, count=1)),
            ('**Requirements:** TouchDesigner',
             lambda l: BUILD_RE.sub(build, l, count=1)),
        ]),
        (root / 'docs' / 'index.md', [
            ('- **TouchDesigner ',
             lambda l: BUILD_RE.sub(build, l, count=1)),
        ]),
        (root / 'CONTRIBUTING.md', [
            ('- **TouchDesigner ',
             lambda l: BUILD_RE.sub(build, l, count=1)),
        ]),
    ]
    for path, transforms in targets:
        try:
            _rewriteFile(path, transforms)
        except Exception as e:
            debug(f'version-doc bump skipped for {path.name}: {e}')


def onStart():
    return

def onCreate():
    return

def onExit():
    return

def onFrameStart(frame):
    return

def onFrameEnd(frame):
    return

def onPlayStateChange(state):
    return

def onDeviceChange():
    return

def onProjectPreSave():
    # set page for component
    comp.currentPage = 'Embody'

    # app.build, not project.saveBuild: pre-save, saveBuild still reports the
    # PREVIOUS save's build, so after a TD upgrade it would understate the
    # floor. The running build is what this save writes.
    build = str(app.build)
    old_version = comp.par.Version.val
    new_version = version(old_version)

    # version up (dev): README badges + minimum-build statements in
    # README / docs/index.md / CONTRIBUTING.md
    updateVersionDocs(build, new_version)

    # update build
    comp.par.Touchbuild = build

    # Release artifacts are fork-branded so it is always clear this is the
    # sporqist codemode fork built on the upstream Embody build line (the
    # version number IS the upstream lineage). See docs/changelog.md v6.0.144.
    RELEASE_PREFIX = f"{comp.name}-codemode"

    # try to delete last release
    try:
        old_release = Path(project.folder).parents[0] / 'release' / f"{RELEASE_PREFIX}-v{old_version}.tox"
        old_release.unlink()
    except Exception as e:
        # You might want to log the exception for debugging purposes
        # print(f"Error deleting old release: {e}")
        pass

    # Clear TDN UI pars so the baked .tox doesn't carry stale paths
    comp.par.Tdnfile = ''
    comp.par.Networkpath = ''

    # save out self-contained portable .tox (strips external file references)
    save_path = Path(project.folder).parents[0] / 'release' / f"{RELEASE_PREFIX}-v{new_version}.tox"
    comp.ExportPortableTox(save_path=str(save_path))

def onProjectPostSave():
    return
