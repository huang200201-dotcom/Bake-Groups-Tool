"""Actual Maya/native hot reload from a disposable installed release package.

Set BG_UPDATE_TEST_PACKAGE to an extracted, complete release package. Networking
is replaced by in-memory release bytes; filesystem, Qt lifecycle and C++ loading
remain real. Only Maya's dock placement is replaced for standalone operation.
"""
import hashlib
import importlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


class InstalledHotReloadTests(unittest.TestCase):
    def test_loaded_native_survives_real_python_update_and_scene_is_preserved(self):
        package_path = os.environ.get('BG_UPDATE_TEST_PACKAGE')
        if not package_path:
            self.skipTest('Set BG_UPDATE_TEST_PACKAGE to a complete extracted release')
        try:
            import maya.standalone as standalone
            from PySide6 import QtWidgets
        except ImportError:
            self.skipTest('Requires Maya 2027 with PySide6')
        root = Path(tempfile.mkdtemp(prefix='bake-update-maya-'))
        self.addCleanup(lambda: shutil.rmtree(str(root), ignore_errors=True))
        os.environ['MAYA_APP_DIR'] = str(root / 'maya-profile')
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        standalone.initialize(name='python')
        self.addCleanup(standalone.uninitialize)
        import maya.cmds as cmds
        from maya.app.general.mayaMixin import MayaQWidgetDockableMixin
        cmds.optionVar(intValue=('BakeMasterAutoUpdate', 0))

        package = Path(package_path)
        spec = importlib.util.spec_from_file_location('hot_reload_installer', package / '安装到Maya.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        runtime = Path(installer.install_package(str(package), str(root / 'scripts'))['target'])
        sys.path.insert(0, str(runtime))
        self.addCleanup(lambda: sys.path.remove(str(runtime)))
        launcher = importlib.import_module('launcher')

        def standalone_show(window, *args, **kwargs):
            # Standalone has no Maya dock hierarchy. Construction, signals,
            # shutdown, imports and scene state are exercised below.
            return None

        qt_errors = []
        with mock.patch.object(MayaQWidgetDockableMixin, 'show', standalone_show), \
                mock.patch('sys.excepthook', side_effect=lambda *error: qt_errors.append(error)):
            ui = launcher.main()
            updater = importlib.import_module('bg_update')
            core = importlib.import_module('bg_core')
            native = importlib.import_module('bg_math_core')
            version = importlib.import_module('bg_version').VERSION
            parts = [int(part) for part in version.split('.')]
            parts[-1] += 1
            next_version = '.'.join(map(str, parts))
            binary = Path(native.__file__)
            old_binary_bytes = binary.read_bytes()
            old_binary_mtime = binary.stat().st_mtime_ns
            hp = cmds.group(empty=True, name='ReloadAsset_HP')
            lp = cmds.group(empty=True, name='ReloadAsset_LP')
            mesh = cmds.polyCube(name='ReloadMesh', constructionHistory=False)[0]
            mesh = cmds.parent(mesh, lp)[0]
            cmds.setAttr(mesh + '.visibility', False)
            cmds.select(mesh, replace=True)
            pair = dict(id='ReloadAsset', base='ReloadAsset',
                        hp_uuid=cmds.ls(hp, uuid=True)[0], lp_uuid=cmds.ls(lp, uuid=True)[0],
                        locked=[], book='Hot reload')
            ui.root_pairs = [pair]
            core.BakeSessionModel.save(ui.root_pairs)
            ui.active_root_id = pair['id']
            ui.workspace_tabs.setCurrentIndex(1)
            before = {'nodes': set(cmds.ls(long=True)), 'selection': cmds.ls(selection=True, long=True),
                      'data': core.BakeSessionModel.load()}

            # Change only Python source in a complete, checksum-correct future
            # fixture. No fixture version is published or installed for the user.
            contents = {}
            for path in package.rglob('*'):
                if path.is_file() and path.name != 'SHA256.txt' and '__pycache__' not in path.parts:
                    name = path.relative_to(package).as_posix()
                    data = path.read_bytes()
                    if name == 'Bake_Groups/bg_version.py':
                        data = data.replace(version.encode('ascii'), next_version.encode('ascii'))
                    contents[name] = data
            manifest = ''.join('{}  {}\n'.format(hashlib.sha256(data).hexdigest(), name)
                               for name, data in sorted(contents.items()))
            contents['SHA256.txt'] = manifest.encode('utf-8')
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zipped:
                for name, data in contents.items():
                    zipped.writestr(name, data)
            archive_bytes = buffer.getvalue()
            archive_name = 'Bake_Master_{}_Windows_x64.zip'.format(next_version)
            checksum = '{}  {}\n'.format(hashlib.sha256(archive_bytes).hexdigest(), archive_name).encode('ascii')

            def download(url, destination=None, limit=None, cancelled=None):
                data = checksum if url.endswith('.sha256') else archive_bytes
                if destination:
                    Path(destination).write_bytes(data)
                    return len(data)
                return data

            with mock.patch.object(updater, '_download', side_effect=download):
                pending = updater.download_and_stage({'version': next_version}, str(runtime))
            self.assertFalse(updater.requires_restart(str(runtime)))
            ui._pending_update = pending
            ui._apply_pending_update_when_idle()
            app.processEvents()
            updated_module = sys.modules['bg_main_window']
            updated_ui = updated_module.bake_manager_ui
            self.addCleanup(lambda: QtWidgets.QWidget.close(updated_ui))
            self.assertIsNot(updated_ui, ui)
            self.assertEqual(sys.modules['bg_version'].VERSION, next_version)
            self.assertIs(sys.modules['bg_math_core'], native)
            self.assertEqual(binary.read_bytes(), old_binary_bytes)
            self.assertEqual(binary.stat().st_mtime_ns, old_binary_mtime)
            self.assertEqual(updated_ui.workspace_tabs.count(), 2)
            self.assertEqual(updated_ui.workspace_tabs.currentIndex(), 1)
            self.assertEqual(updated_ui.active_root_id, pair['id'])
            self.assertEqual(sys.modules['bg_core'].BakeSessionModel.load(), before['data'])
            self.assertEqual(set(cmds.ls(long=True)), before['nodes'])
            self.assertEqual(cmds.ls(selection=True, long=True), before['selection'])
            self.assertFalse(cmds.getAttr(mesh + '.visibility'))
            self.assertIsNone(sys.modules['bg_update'].pending_update(str(runtime)))
            self.assertAlmostEqual(native.calculate_avg_distance([0., 0., 0.], [1., 0., 0.]), 1.)
            QtWidgets.QWidget.close(updated_ui)
            app.processEvents()
            self.assertEqual(qt_errors, [], 'A Qt signal or virtual method raised an exception')


if __name__ == '__main__':
    unittest.main(verbosity=2)
