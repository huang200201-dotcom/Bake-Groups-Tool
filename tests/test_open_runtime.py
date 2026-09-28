"""Exercise open-source startup without Maya, network access, or a native DLL."""
import ast
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'src' / 'Bake_Groups'


class OpenRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bake-open-runtime-')
        self.addCleanup(self.temporary.cleanup)
        self.saved_path = list(sys.path)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), self.saved_path))
        self.cmds = types.ModuleType('maya.cmds')
        self.cmds.about = mock.Mock(return_value='2027')
        self.cmds.workspaceControl = mock.Mock(return_value=False)
        self.cmds.warning = mock.Mock()
        maya = types.ModuleType('maya')
        maya.cmds = self.cmds
        self.app = mock.Mock()
        qt = types.ModuleType('PySide6')
        qt.QtWidgets = types.SimpleNamespace(
            QApplication=types.SimpleNamespace(instance=lambda: self.app))
        qt.QtCore = types.SimpleNamespace(
            QEventLoop=types.SimpleNamespace(AllEvents=0))
        self.modules = mock.patch.dict(sys.modules, {
            'maya': maya, 'maya.cmds': self.cmds, 'PySide6': qt,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location(
            'open_runtime_under_test', str(RUNTIME / 'launcher.py'))
        self.launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.launcher)
        for name in self.launcher.RUNTIME_MODULES + ('bg_math_core',):
            sys.modules.pop(name, None)
        self.runtime = Path(self.temporary.name) / 'installed' / 'Bake_Groups'
        self.runtime.mkdir(parents=True)
        self.launcher.__file__ = str(self.runtime / 'launcher.py')

    def make_binary(self, year='2027', runtime=None):
        path = (runtime or self.runtime) / 'bin' / year / 'bg_math_core.pyd'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'path-selection-fixture-only')
        return path

    def make_ui(self):
        ui = mock.Mock()
        ui.objectName.return_value = 'BakeManagerUI'
        ui._shutdown_for_reload_done = False
        return ui

    def test_missing_matching_year_fails_before_ui_shutdown(self):
        self.make_binary('2026')
        with mock.patch.object(self.launcher, '_shutdown_existing_bake_groups_ui') as shutdown:
            with mock.patch.object(self.launcher.importlib, 'import_module') as imported:
                with self.assertRaisesRegex(RuntimeError, 'Maya 2027 is missing'):
                    self.launcher.main()
        shutdown.assert_not_called()
        imported.assert_not_called()

    def test_wrong_abi_import_failure_keeps_existing_ui(self):
        self.make_binary()
        old_ui = self.make_ui()
        sys.modules['bg_main_window'] = types.SimpleNamespace(
            __file__=str(self.runtime / 'bg_main_window.py'), bake_manager_ui=old_ui)
        with mock.patch.object(self.launcher, '_shutdown_existing_bake_groups_ui') as shutdown:
            with mock.patch.object(self.launcher.importlib, 'import_module',
                                   side_effect=ImportError('fixture wrong Python ABI')):
                with self.assertRaisesRegex(ImportError, 'wrong Python ABI'):
                    self.launcher.main()
        shutdown.assert_not_called()
        old_ui.close.assert_not_called()

    def test_already_loaded_foreign_binary_requires_restart(self):
        self.make_binary()
        sys.modules['bg_math_core'] = types.SimpleNamespace(
            __file__=str(Path(self.temporary.name) / 'other' / 'bg_math_core.pyd'))
        with mock.patch.object(self.launcher, '_shutdown_existing_bake_groups_ui') as shutdown:
            with mock.patch.object(self.launcher.importlib, 'import_module') as imported:
                with self.assertRaisesRegex(RuntimeError, 'Restart Maya'):
                    self.launcher.main()
        shutdown.assert_not_called()
        imported.assert_not_called()

    def test_checkout_uses_matching_build_directory(self):
        checkout = Path(self.temporary.name) / 'repo'
        runtime = checkout / 'src' / 'Bake_Groups'
        runtime.mkdir(parents=True)
        binary = checkout / 'build' / 'native' / '2027' / 'bg_math_core.pyd'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'path-selection-fixture-only')
        self.launcher.__file__ = str(runtime / 'launcher.py')
        with mock.patch.object(self.launcher.importlib, 'import_module') as imported:
            result = self.launcher._prepare_runtime()
        self.assertEqual(result, str(runtime))
        self.assertEqual(sys.path[:2], [os.path.realpath(str(binary.parent)), str(runtime)])
        imported.assert_called_once_with('bg_math_core')

    def test_first_launch_imports_native_before_building_ui(self):
        self.make_binary()
        ui = self.make_ui()
        module = types.SimpleNamespace(bake_manager_ui=ui, main=mock.Mock())
        events = []

        def imported(name):
            events.append(name)
            return module if name == 'bg_main_window' else types.SimpleNamespace()

        with mock.patch.object(self.launcher, '_shutdown_existing_bake_groups_ui',
                               side_effect=lambda: events.append('shutdown')):
            with mock.patch.object(self.launcher.importlib, 'import_module', side_effect=imported):
                self.assertIs(self.launcher.main(), ui)
        self.assertEqual(events, ['bg_math_core', 'shutdown', 'bg_main_window'])
        module.main.assert_called_once_with()

    def test_repeated_launch_reuses_ui_and_keeps_loaded_modules(self):
        binary = self.make_binary()
        native = types.SimpleNamespace(__file__=str(binary))
        ui = self.make_ui()
        window_module = types.SimpleNamespace(
            __file__=str(self.runtime / 'bg_main_window.py'), bake_manager_ui=ui)
        core_module = types.SimpleNamespace()
        sys.modules.update(bg_math_core=native, bg_main_window=window_module, bg_core=core_module)
        with mock.patch.object(self.launcher, '_shutdown_existing_bake_groups_ui') as shutdown:
            with mock.patch.object(self.launcher.importlib, 'import_module', return_value=native) as imported:
                self.assertIs(self.launcher.main(), ui)
                self.assertIs(self.launcher.main(), ui)
        shutdown.assert_not_called()
        self.assertEqual(imported.call_args_list, [mock.call('bg_math_core')] * 2)
        self.assertIs(sys.modules['bg_main_window'], window_module)
        self.assertIs(sys.modules['bg_core'], core_module)
        self.assertEqual(ui.show.call_count, 2)
        self.assertEqual(sys.path.count(str(self.runtime)), 1)
        self.assertEqual(sys.path.count(os.path.realpath(str(binary.parent))), 1)

    def test_closed_ui_is_not_reused(self):
        ui = self.make_ui()
        ui._shutdown_for_reload_done = True
        sys.modules['bg_main_window'] = types.SimpleNamespace(
            __file__=str(self.runtime / 'bg_main_window.py'), bake_manager_ui=ui)
        self.assertFalse(self.launcher._restore_existing_ui(str(self.runtime)))
        ui.show.assert_not_called()

    def test_staged_update_is_applied_before_native_import(self):
        self.make_binary()
        events = []
        with mock.patch.object(self.launcher, '_apply_ready_update',
                               side_effect=lambda path: events.append('apply') or True):
            with mock.patch.object(self.launcher, '_launch_updated_runtime',
                                   side_effect=lambda path: events.append('updated_launcher') or 'new_ui'):
                with mock.patch.object(self.launcher, '_prepare_runtime') as prepare:
                    self.assertEqual(self.launcher.main(), 'new_ui')
        self.assertEqual(events, ['apply', 'updated_launcher'])
        prepare.assert_not_called()

    def _fake_updater(self, restart=False):
        update_dir = self.runtime.parent / 'Bake_Groups.update'
        update_dir.mkdir()
        (update_dir / 'pending.json').write_text('{}')
        (self.runtime / 'bg_update.py').write_text('')
        updater = types.ModuleType('_fake_updater')
        updater.pending_update = mock.Mock(return_value={'version': '1.0.2'})
        updater.requires_restart = mock.Mock(return_value=restart)
        updater.apply_pending = mock.Mock(return_value={'status': 'applied', 'version': '1.0.2'})
        loader = mock.Mock()
        spec = types.SimpleNamespace(loader=loader)
        patches = [mock.patch.object(self.launcher.importlib.util, 'spec_from_file_location', return_value=spec),
                   mock.patch.object(self.launcher.importlib.util, 'module_from_spec', return_value=updater)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return updater

    def test_loaded_native_change_waits_without_closing_ui(self):
        updater = self._fake_updater(restart=True)
        with mock.patch.object(self.launcher, '_shutdown_existing_bake_groups_ui') as shutdown:
            self.assertFalse(self.launcher._apply_ready_update(str(self.runtime)))
        shutdown.assert_not_called()
        updater.apply_pending.assert_not_called()
        self.cmds.warning.assert_called_once()

    def test_worker_shutdown_must_succeed_before_applying_update(self):
        updater = self._fake_updater()
        with mock.patch.object(self.launcher, '_shutdown_existing_bake_groups_ui',
                               side_effect=RuntimeError('worker still stopping')):
            self.assertFalse(self.launcher._apply_ready_update(str(self.runtime)))
        updater.apply_pending.assert_not_called()
        self.cmds.warning.assert_called_once()

    def test_python_update_closes_old_ui_before_file_replacement(self):
        updater = self._fake_updater()
        events = []
        updater.apply_pending.side_effect = lambda path: events.append('apply') or {'status': 'applied'}
        with mock.patch.object(self.launcher, '_shutdown_existing_bake_groups_ui',
                               side_effect=lambda: events.append('shutdown')):
            self.assertTrue(self.launcher._apply_ready_update(str(self.runtime)))
        self.assertEqual(events, ['shutdown', 'apply'])

    def test_apply_failure_reports_error_and_allows_normal_startup(self):
        updater = self._fake_updater()
        updater.apply_pending.side_effect = RuntimeError('disk full, transaction restored')
        with mock.patch.object(self.launcher, '_shutdown_existing_bake_groups_ui'):
            self.assertFalse(self.launcher._apply_ready_update(str(self.runtime)))
        self.assertIn('disk full', self.cmds.warning.call_args[0][0])

    def test_new_launcher_code_runs_without_applying_same_update_twice(self):
        (self.runtime / 'launcher.py').write_text(
            'def main(_skip_update=False):\n    return ("new launcher", _skip_update)\n')
        self.assertEqual(self.launcher._launch_updated_runtime(str(self.runtime)),
                         ('new launcher', True))

    def test_unfinished_rollback_blocks_import_of_mixed_runtime(self):
        updater = self._fake_updater()
        journal = self.runtime.parent / 'Bake_Groups.update' / 'transaction.json'
        journal.write_text('{"state":"applying"}')
        updater.apply_pending.side_effect = RuntimeError('recovery remains pending')
        with mock.patch.object(self.launcher, '_shutdown_existing_bake_groups_ui'):
            with mock.patch.object(self.launcher, '_prepare_runtime') as prepare:
                with self.assertRaisesRegex(RuntimeError, 'recovery is incomplete'):
                    self.launcher.main()
        prepare.assert_not_called()

    def test_native_recovery_requiring_restart_keeps_existing_ui(self):
        updater = self._fake_updater(restart=True)
        journal = self.runtime.parent / 'Bake_Groups.update' / 'transaction.json'
        journal.write_text('{"state":"applying"}')
        with mock.patch.object(self.launcher, '_shutdown_existing_bake_groups_ui') as shutdown:
            with self.assertRaisesRegex(RuntimeError, 'recovery is incomplete'):
                self.launcher.main()
        shutdown.assert_not_called()
        updater.apply_pending.assert_not_called()

    def test_completed_transaction_cleanup_failure_does_not_block_startup(self):
        updater = self._fake_updater()
        journal = self.runtime.parent / 'Bake_Groups.update' / 'transaction.json'
        journal.write_text('{"state":"committed"}')
        updater.apply_pending.side_effect = RuntimeError('old backup cleanup failed')
        with mock.patch.object(self.launcher, '_shutdown_existing_bake_groups_ui'):
            self.assertFalse(self.launcher._apply_ready_update(str(self.runtime)))

    def test_source_tree_keeps_network_confined_to_public_updater(self):
        forbidden = {
            'bg_license', 'bg_credentials',
            'urllib', 'http', 'requests', 'socket', 'websocket',
        }
        for path in sorted(RUNTIME.rglob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
            for node in ast.walk(tree):
                imported = []
                if isinstance(node, ast.Import):
                    imported = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = [node.module]
                for name in imported:
                    if path.name == 'bg_update.py' and name.split('.')[0] in ('urllib', 'http', 'socket'):
                        continue
                    self.assertNotIn(name.split('.')[0], forbidden,
                                     '{} imports {}'.format(path.name, name))


if __name__ == '__main__':
    unittest.main(verbosity=2)
