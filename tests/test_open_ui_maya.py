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
        self.ui = self.main.BakeManagerUI()

    def tearDown(self):
        self.ui.close()
        self.app.processEvents()
        self.network_request.assert_not_called()
        self.assertEqual(self.qt_errors, [], 'An exception escaped a Qt signal handler')

    def make_pair(self, base):
        hp = self.cmds.group(empty=True, name=base + '_HP')
        lp = self.cmds.group(empty=True, name=base + '_LP')
        pair = dict(id=base, base=base, hp_uuid=self.cmds.ls(hp, uuid=True)[0],
                    lp_uuid=self.cmds.ls(lp, uuid=True)[0], locked=[], book='Open UI')
        self.ui.root_pairs.append(pair)
        return pair, hp, lp

    def test_two_workspaces_without_licensing_or_background_updater(self):
        self.assertEqual(self.ui.workspace_tabs.count(), 2)
        for language, expected in (
                ('en', ['Automatic Grouping', 'Asset Tasks']),
                ('zh-CN', ['自动分组', '资产任务'])):
            self.l10n.set_language(language)
            self.ui.refresh_localized_ui()
            self.assertEqual([self.ui.workspace_tabs.tabText(i) for i in range(2)], expected)
        for attribute in ('gt_widget', 'update_timer', 'update_check_worker',
                          'update_install_worker', 'update_menu'):
            self.assertFalse(hasattr(self.ui, attribute), attribute)
        for module in ('bg_license', 'bg_credentials', 'bg_update', 'bg_gt_matcher'):
            self.assertNotIn(module, sys.modules)
        self.assertEqual(self.ui.lbl_runtime_version.text(), 'v1.0')
        self.assertEqual(self.ui.windowTitle(), 'Bake Master 1.0')

    def test_releases_button_only_opens_public_download_page(self):
        with mock.patch.object(self.QtGui.QDesktopServices, 'openUrl', return_value=True) as opened:
            self.ui.btn_releases.click()
        opened.assert_called_once()
        self.assertEqual(opened.call_args[0][0].toString(), self.version.RELEASES_URL)
        self.assertIsNone(self.ui.btn_releases.menu())
        self.assertTrue(self.ui.btn_releases.toolTip())

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
