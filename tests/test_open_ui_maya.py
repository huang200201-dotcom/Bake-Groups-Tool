"""Open-source workspace regression tests, run with a disposable mayapy.

The suite skips cleanly without Maya. It builds the real Qt UI, uses actual
scene operations, and never stubs authorization or the geometry processor.
"""
import importlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'src' / 'Bake_Groups'


class OpenUIRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import maya.standalone as standalone
            try:
                from PySide6 import QtWidgets, QtCore, QtGui
            except ImportError:
                from PySide2 import QtWidgets, QtCore, QtGui
        except ImportError:
            raise unittest.SkipTest('Requires Maya and its Qt runtime')
        cls.profile = Path(tempfile.mkdtemp(prefix='bake-master-open-ui-')).resolve()
        cls.previous_profile = os.environ.get('MAYA_APP_DIR')
        os.environ['MAYA_APP_DIR'] = str(cls.profile)
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        standalone.initialize(name='python')
        import maya.cmds as cmds
        cls.standalone = standalone
        cls.cmds, cls.QtWidgets, cls.QtCore, cls.QtGui = cmds, QtWidgets, QtCore, QtGui
        cls.previous_path = list(sys.path)
        sys.path.insert(0, str(RUNTIME))
        year = str(cmds.about(version=True))[:4]
        for candidate in (RUNTIME / 'bin' / year, ROOT / 'build' / 'native' / year):
            if (candidate / 'bg_math_core.pyd').is_file():
                sys.path.insert(0, str(candidate))
                break
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('Startup network request')):
            cls.main = importlib.import_module('bg_main_window')
        cls.l10n = importlib.import_module('bg_localization')
        cls.original_language = cls.l10n.current_language()
        cls.core = importlib.import_module('bg_core')
        cls.cage = importlib.import_module('bg_cage')
        cls.version = importlib.import_module('bg_version')

    @classmethod
    def tearDownClass(cls):
        cls.l10n.set_language(cls.original_language)
        cls.app.processEvents()
        cls.standalone.uninitialize()
        sys.path[:] = cls.previous_path
        if cls.previous_profile is None:
            os.environ.pop('MAYA_APP_DIR', None)
        else:
            os.environ['MAYA_APP_DIR'] = cls.previous_profile
        # Maya can keep its log open until process exit on Windows. Only try
        # to clean this test's own temporary profile, never a user profile.
        assert cls.profile.parent == Path(tempfile.gettempdir()).resolve()
        assert cls.profile.name.startswith('bake-master-open-ui-')
        shutil.rmtree(str(cls.profile), ignore_errors=True)

    def setUp(self):
        self.cmds.file(new=True, force=True)
        self.l10n.set_language('en')
        self.network_guard = mock.patch('urllib.request.urlopen', side_effect=AssertionError('UI network request'))
        self.network_request = self.network_guard.start()
        self.addCleanup(self.network_guard.stop)
        self.qt_errors = []
        exception_guard = mock.patch('sys.excepthook', side_effect=lambda *exc: self.qt_errors.append(exc))
        exception_guard.start()
        self.addCleanup(exception_guard.stop)
        self.updates = self.main.bg_update
        self.checker = self._patch_update('check_for_update', return_value=None)
        self.downloader = self._patch_update('download_and_stage', return_value={'version': '9.9.9', 'status': 'pending'})
        self.pending = self._patch_update('pending_update', return_value=None)
        self.restart = self._patch_update('requires_restart', return_value=False)
        self.messages = mock.patch.object(self.QtWidgets.QMessageBox, 'information', return_value=self.QtWidgets.QMessageBox.Ok)
        self.info = self.messages.start()
        self.addCleanup(self.messages.stop)
        warnings = mock.patch.object(self.QtWidgets.QMessageBox, 'warning', return_value=self.QtWidgets.QMessageBox.Ok)
        self.warning = warnings.start()
        self.addCleanup(warnings.stop)
        self.main._update_process_state()['auto_checked'] = False
        if self.cmds.optionVar(exists=self.main._AUTO_UPDATE_OPTION):
            self.cmds.optionVar(remove=self.main._AUTO_UPDATE_OPTION)
        self.ui = self.main.BakeManagerUI()
        self.startup_timer_remaining = self.ui.auto_update_timer.remainingTime()
        self.ui.auto_update_timer.stop()

    def _patch_update(self, name, **kwargs):
        patcher = mock.patch.object(self.updates, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(predicate(), 'Timed out waiting for background update')

    def tearDown(self):
        if not getattr(self, 'closed_early', False):
            self.QtWidgets.QWidget.close(self.ui)
        self.wait_for(lambda: not self.main._update_process_state()['workers'])
        self.network_request.assert_not_called()
        self.assertEqual(self.qt_errors, [], 'An exception escaped a Qt signal handler')

    def make_pair(self, base):
        hp = self.cmds.group(empty=True, name=base + '_HP')
        lp = self.cmds.group(empty=True, name=base + '_LP')
        pair = dict(id=base, base=base, hp_uuid=self.cmds.ls(hp, uuid=True)[0],
                    lp_uuid=self.cmds.ls(lp, uuid=True)[0], locked=[], book='Open UI')
        self.ui.root_pairs.append(pair)
        return pair, hp, lp

    def test_two_workspaces_without_licensing_and_three_localized_update_actions(self):
        self.assertEqual(self.ui.workspace_tabs.count(), 2)
        for language, expected in (
                ('en', ['Automatic Grouping', 'Asset Tasks']),
                ('zh-CN', ['自动分组', '资产任务'])):
            self.l10n.set_language(language)
            self.ui.refresh_localized_ui()
            self.assertEqual([self.ui.workspace_tabs.tabText(i) for i in range(2)], expected)
            labels = ['Automatic Updates', 'Manual Update', 'Visit Repository Website'] if language == 'en' else ['自动更新', '手动更新', '访问仓库网站']
            self.assertEqual([action.text() for action in self.ui.update_menu.actions()], labels)
        self.assertFalse(hasattr(self.ui, 'gt_widget'))
        for module in ('bg_license', 'bg_credentials', 'bg_gt_matcher'):
            self.assertNotIn(module, sys.modules)
        self.assertEqual(self.ui.lbl_runtime_version.text(), 'v' + self.version.VERSION)
        self.assertEqual(self.ui.windowTitle(), 'Bake Master ' + self.version.VERSION)

    def test_repository_action_opens_repo_root_and_auto_preference_persists(self):
        with mock.patch.object(self.QtGui.QDesktopServices, 'openUrl', return_value=True) as opened:
            self.ui.action_open_repository.trigger()
        opened.assert_called_once()
        self.assertEqual(opened.call_args[0][0].toString(), self.version.REPOSITORY_URL)
        self.assertTrue(self.ui.action_auto_update.isChecked())
        self.ui.action_auto_update.setChecked(False)
        self.assertEqual(self.cmds.optionVar(query=self.main._AUTO_UPDATE_OPTION), 0)
        other = self.main.BakeManagerUI()
        try:
            self.assertFalse(other.action_auto_update.isChecked())
            self.assertFalse(other.auto_update_timer.isActive())
        finally:
            self.QtWidgets.QWidget.close(other)
        self.ui.action_auto_update.setChecked(True)
        self.wait_for(lambda: self.checker.call_count == 1 and not self.main._update_process_state()['workers'])
        self.assertEqual(self.cmds.optionVar(query=self.main._AUTO_UPDATE_OPTION), 1)

    def test_automatic_check_is_delayed_off_thread_and_once_per_process(self):
        self.assertGreater(self.startup_timer_remaining, 1000)
        self.checker.assert_not_called()
        thread_ids = []
        self.checker.side_effect = lambda *args, **kwargs: thread_ids.append(threading.get_ident()) or None
        self.ui.auto_update_timer.start(1)
        self.wait_for(lambda: len(thread_ids) == 1 and not self.main._update_process_state()['workers'])
        self.assertNotEqual(thread_ids[0], threading.get_ident())
        self.info.assert_not_called()
        self.ui._start_automatic_update()
        self.app.processEvents()
        self.assertEqual(len(thread_ids), 1)
        self.ui.action_manual_update.trigger()
        self.wait_for(lambda: len(thread_ids) == 2 and not self.main._update_process_state()['workers'])
        self.info.assert_called_once()

    def test_manual_current_version_and_failure_feedback_automatic_failure_is_nonmodal(self):
        self.ui.action_manual_update.trigger()
        self.wait_for(lambda: not self.main._update_process_state()['workers'])
        self.assertIn('up to date', self.info.call_args[0][2])
        self.checker.side_effect = RuntimeError('offline fixture')
        self.ui.action_manual_update.trigger()
        self.wait_for(lambda: not self.main._update_process_state()['workers'])
        self.assertIn('offline fixture', self.warning.call_args[0][2])
        self.warning.reset_mock()
        self.ui.start_update_check(manual=False)
        self.wait_for(lambda: not self.main._update_process_state()['workers'])
        self.warning.assert_not_called()
        self.assertIn('offline fixture', self.ui._update_status)

    def test_download_stages_off_thread_busy_defers_and_native_requires_restart(self):
        self.checker.return_value = {'version': '9.9.9'}
        download_threads = []
        self.downloader.side_effect = lambda *args, **kwargs: download_threads.append(threading.get_ident()) or {'version': '9.9.9', 'status': 'pending'}
        self.ui._hp_task_finalized = False
        self.ui.start_update_check(manual=False)
        self.wait_for(lambda: self.ui._pending_update is not None and not self.main._update_process_state()['workers'])
        self.assertNotEqual(download_threads[0], threading.get_ident())
        import maya.utils
        with mock.patch.object(maya.utils, 'executeDeferred') as deferred:
            self.ui._try_apply_pending_update()
            deferred.assert_not_called()
            self.assertTrue(self.ui.pending_update_timer.isActive())
            self.ui._hp_task_finalized = True
            self.restart.return_value = True
            self.ui._try_apply_pending_update()
            deferred.assert_not_called()
            self.assertIn('Restart Maya', self.ui._update_status)
            self.restart.return_value = False
            self.ui._try_apply_pending_update()
            deferred.assert_called_once()
        self.ui.pending_update_timer.stop()
        self.ui._update_apply_scheduled = False

    def test_pending_reload_preserves_task_context_and_does_not_save_untouched_scene(self):
        pair, _hp, _lp = self.make_pair('Asset')
        self.ui.active_root_id = pair['id']
        self.ui.active_subgroup_name = 'Main'
        self.ui.workspace_tabs.setCurrentIndex(1)
        new_ui = mock.Mock(root_pairs=[pair])
        import launcher
        self.cmds.file(modified=False)
        with mock.patch.object(launcher, 'main', return_value=new_ui), mock.patch.object(self.core.BakeSessionModel, 'save') as save:
            self.ui._apply_pending_update_when_idle()
        save.assert_not_called()
        self.assertFalse(self.cmds.file(query=True, modified=True))
        self.assertEqual(new_ui.active_root_id, 'Asset')
        self.assertEqual(new_ui.active_subgroup_name, 'Main')
        new_ui.workspace_tabs.setCurrentIndex.assert_called_once_with(1)

    def test_close_and_disable_cancel_network_worker_without_destroying_running_thread(self):
        entered = threading.Event()
        cancelled = threading.Event()
        def block_until_cancelled(*args, **kwargs):
            entered.set()
            while not kwargs['cancelled']():
                time.sleep(0.005)
            cancelled.set()
            raise self.updates.UpdateCancelled('cancelled fixture')
        self.checker.side_effect = block_until_cancelled
        self.ui.start_update_check(manual=False)
        self.wait_for(entered.is_set)
        worker = self.ui.update_worker
        self.assertIn(worker, self.main._update_process_state()['workers'])
        self.ui.action_auto_update.setChecked(False)
        self.wait_for(lambda: cancelled.is_set() and not self.main._update_process_state()['workers'])
        entered.clear()
        cancelled.clear()
        release = threading.Event()
        self.addCleanup(release.set)
        self.checker.side_effect = None
        self.checker.return_value = {'version': '9.9.9'}
        def download_until_cancelled(*args, **kwargs):
            try:
                return block_until_cancelled(*args, **kwargs)
            finally:
                release.wait(timeout=2)
        self.downloader.side_effect = download_until_cancelled
        self.ui.start_update_check(manual=True)
        self.wait_for(entered.is_set)
        worker = self.ui.update_worker
        started = time.monotonic()
        self.assertTrue(self.QtWidgets.QWidget.close(self.ui))
        self.closed_early = True
        self.assertLess(time.monotonic() - started, 0.5)
        self.app.processEvents()
        self.assertTrue(worker.isRunning())
        self.assertIn(worker, self.main._update_process_state()['workers'])
        release.set()
        self.wait_for(lambda: cancelled.is_set() and not self.main._update_process_state()['workers'])
        self.warning.assert_not_called()

    def test_asset_task_switching_and_saved_custom_groups(self):
        first, _hp, _lp = self.make_pair('Asset')
        other, other_hp, _other_lp = self.make_pair('Other')
        mesh = self.cmds.polyCube(name='SavedHigh', constructionHistory=False)[0]
        mesh = self.cmds.parent(mesh, other_hp)[0]
        other['custom_grouping'] = {'saved_lp_constraint': self.cmds.ls(mesh, uuid=True)}
        self.core.BakeSessionModel.save(self.ui.root_pairs)
        self.ui.refresh_right_panel()
        self.ui.workspace_tabs.setCurrentIndex(1)
        for pair in (other, first, other):
            iterator = self.QtWidgets.QTreeWidgetItemIterator(self.ui.toc_tree)
            items = {}
            while iterator.value():
                item = iterator.value()
                items[item.data(0, self.QtCore.Qt.UserRole)] = item
                iterator += 1
            self.ui.toc_tree.itemClicked.emit(items[pair['id']], 0)
            self.assertEqual(self.ui.active_root_id, pair['id'])
            self.assertIn(pair['base'], self.ui.lbl_current_task.text())
        saved = next(p for p in self.core.BakeSessionModel.load() if p['id'] == other['id'])
        self.assertEqual(saved['custom_grouping'], other['custom_grouping'])

    def test_shared_group_final_view_and_cage_work_without_manual_workspace(self):
        pair, hp, lp = self.make_pair('Asset')
        self.ui.active_root_id = pair['id']
        self.ui.cb_keep_hp_structure.setChecked(False)
        self.ui.refresh_left_panel()
        raw = []
        for root in (hp, lp):
            cube = self.cmds.polyCube(constructionHistory=False)[0]
            raw.append(self.cmds.parent(cube, root)[0])
        self.cmds.select(raw, replace=True)
        self.ui.input_suffix.setText('panel_high_detail')
        self.ui.btn_create_subgroup.click()
        subgroup = '|Asset_LP|panel_high_detail_LP'
        self.assertTrue(self.cmds.objExists(subgroup))
        self.cmds.setAttr(subgroup + '.visibility', False)
        self.ui.btn_combine_bake.click()
        final = '|LP_Combine_BG|Asset|Asset_panel_high_detail_low'
        self.assertTrue(self.cmds.objExists(final))
        self.assertFalse(self.cmds.getAttr(subgroup + '.visibility'))
        self.cmds.setAttr(final + '.visibility', False)
        for _index in range(2):
            self.ui.btn_toggle_view.click()
            self.assertFalse(self.cmds.getAttr(final + '.visibility'))
            self.assertFalse(self.cmds.getAttr(subgroup + '.visibility'))
        if not self.ui.is_final_view:
            self.ui.btn_toggle_view.click()
        self.assertEqual([item['subgroup_name'] for item in self.ui.final_mesh_widgets],
                         ['panel_high_detail'])
        panel = self.ui.findChild(self.QtWidgets.QFrame, 'CagePanel')
        self.assertIsNotNone(panel)
        self.ui.rebuild_active_cage()
        cages = self.cage.CageManager.chapter_cages('Asset')
        self.assertEqual(len(cages), 1)
        self.assertGreater(self.cmds.polyEvaluate(cages[0], vertex=True), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
