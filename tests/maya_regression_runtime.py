"""Shared disposable Maya runtime for the migrated scene regressions.

Importing without Maya skips the suite. An already running Maya session is
also refused: these tests intentionally create and discard their own scenes.
"""
import atexit
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

try:
    import maya.standalone as standalone
    try:
        from PySide6 import QtWidgets
    except ImportError:
        from PySide2 import QtWidgets
except ImportError:
    raise unittest.SkipTest('Requires a disposable mayapy process and Qt')

_profile = Path(tempfile.mkdtemp(prefix='bake-master-regression-')).resolve()
_previous_profile = os.environ.get('MAYA_APP_DIR')
os.environ['MAYA_APP_DIR'] = str(_profile)
APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
_running = False


def shutdown():
    global _running
    if _running:
        APP.processEvents()
        standalone.uninitialize()
        _running = False
    if _previous_profile is None:
        os.environ.pop('MAYA_APP_DIR', None)
    else:
        os.environ['MAYA_APP_DIR'] = _previous_profile
    # Maya may hold mayaLog open until the process exits on Windows.
    assert _profile.parent == Path(tempfile.gettempdir()).resolve()
    assert _profile.name.startswith('bake-master-regression-')
    shutil.rmtree(str(_profile), ignore_errors=True)


try:
    standalone.initialize(name='python')
except RuntimeError:
    shutdown()
    raise unittest.SkipTest('Run in fresh mayapy; an existing Maya session must not be modified')
_running = True
atexit.register(shutdown)
import maya.cmds as cmds

# These are geometry/export regressions. Keep HTTP and update installation out
# of their temporary Maya profile; update behavior has its own Qt/end-to-end
# suites. No preference in the artist's Maya profile is touched.
cmds.optionVar(intValue=('BakeMasterAutoUpdate', 0))

REPO = Path(__file__).resolve().parents[1]
RUNTIME = REPO / 'src' / 'Bake_Groups'
sys.path.insert(0, str(RUNTIME))
_year = str(cmds.about(version=True))[:4]
for _candidate in (RUNTIME / 'bin' / _year, REPO / 'build' / 'native' / _year):
    if (_candidate / 'bg_math_core.pyd').is_file():
        sys.path.insert(0, str(_candidate))
        break
from bg_version import VERSION
