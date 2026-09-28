"""Single-button HP grouping -> LP matching in a disposable real Maya scene."""
import sys
import time
import unittest
from unittest import mock

import test_maya_final_groups_regression as fixture
from maya_regression_runtime import APP, cmds, shutdown
import bg_final_groups
import bg_mixins


class AutoGroupingRegression(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.FinalGroupWorkflowRegression('runTest')
        self.fixture.setUp()
        self.ui = self.fixture.ui
        self.pair, self.hp, self.lp = self.fixture.pair, self.fixture.hp, self.fixture.lp
        self.ui._mark_chapter_pre_checked(self.pair['id'])
        self.ui.cb_color_subgroups.setChecked(False)
        self.ui.set_grouping_mode('conflict_safe')
        self.qt_errors = []
        errors = mock.patch('sys.excepthook', side_effect=lambda *exc: self.qt_errors.append(exc))
        errors.start()
        self.addCleanup(errors.stop)

    def tearDown(self):
        self.fixture.tearDown()
        self.assertEqual(self.qt_errors, [], 'A Qt signal handler raised')

    def wait_done(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            APP.processEvents()
            if (self.ui._auto_grouping_context is None and
                    self.ui.hp_worker is None and self.ui.lp_worker is None):
                break
            time.sleep(0.005)
        self.assertIsNone(self.ui._auto_grouping_context)
        self.assertIsNone(self.ui.hp_worker)
        self.assertIsNone(self.ui.lp_worker)
        self.assertTrue(self.ui._hp_task_finalized)
        self.assertTrue(self.ui._lp_task_finalized)
        self.assertTrue(self.ui.btn_run_hp.isEnabled())
        self.assertTrue(self.ui.toc_tree.isEnabled())

    def cubes(self):
        high = self.fixture.cube(self.hp, 'RawHigh')
        low = self.fixture.cube(self.lp, 'RawLow')
        return cmds.ls(high, uuid=True)[0], cmds.ls(low, uuid=True)[0]

    def assert_matched(self, high_id, low_id):
        high = cmds.ls(high_id, long=True)[0]
        low = cmds.ls(low_id, long=True)[0]
        high_group = cmds.listRelatives(high, parent=True, fullPath=True)[0]
        low_group = cmds.listRelatives(low, parent=True, fullPath=True)[0]
        self.assertNotEqual(high_group, '|Asset_HP')
        self.assertNotEqual(low_group, '|Asset_LP')
        self.assertEqual(bg_final_groups.subgroup_name(high_group, 'HP'),
                         bg_final_groups.subgroup_name(low_group, 'LP'))

    def test_one_click_runs_real_hp_then_real_lp_once(self):
        high_id, low_id = self.cubes()
        self.assertFalse(hasattr(self.ui, 'btn_run_lp'))
        with mock.patch.object(self.ui, 'run_lp_matching', wraps=self.ui.run_lp_matching) as matching:
            self.ui.btn_run_hp.click()
            self.assertIsNotNone(self.ui._auto_grouping_context)
            self.assertTrue(self.ui._update_ui_busy())
            self.assertFalse(self.ui.run_auto_grouping())
            self.wait_done()
        self.assertEqual(matching.call_count, 1, self.ui.log_output.toPlainText())
        self.assert_matched(high_id, low_id)

    def test_keep_hp_structure_continues_to_real_lp_matching(self):
        self.fixture.create_group('Existing')
        high = cmds.listRelatives('|Asset_HP|Existing_HP', children=True, type='transform', fullPath=True)[0]
        low = cmds.listRelatives('|Asset_LP|Existing_LP', children=True, type='transform', fullPath=True)[0]
        high_id, low_id = cmds.ls(high, uuid=True)[0], cmds.ls(low, uuid=True)[0]
        self.ui.cb_keep_hp_structure.setChecked(True)
        with mock.patch.object(self.ui, 'run_lp_matching', wraps=self.ui.run_lp_matching) as matching:
            self.ui.btn_run_hp.click()
            self.wait_done()
        self.assertEqual(matching.call_count, 1)
        self.assert_matched(high_id, low_id)
        self.assertTrue(cmds.objExists('|Asset_HP|Existing_HP'))

    def test_cancel_hp_never_starts_lp_and_allows_another_run(self):
        self.cubes()
        with mock.patch.object(self.ui, 'run_lp_matching', wraps=self.ui.run_lp_matching) as matching:
            self.ui.btn_run_hp.click()
            self.ui._cancel_hp_analysis()
            self.wait_done()
            matching.assert_not_called()
            self.ui.btn_run_hp.click()
            self.wait_done()
            self.assertEqual(matching.call_count, 1)

    def test_failed_hp_worker_never_starts_lp(self):
        self.cubes()
        with mock.patch.object(bg_mixins.HPGroupingWorker, '_run_impl', side_effect=RuntimeError('injected HP failure')):
            with mock.patch.object(self.ui, 'run_lp_matching', wraps=self.ui.run_lp_matching) as matching:
                self.ui.btn_run_hp.click()
                self.wait_done()
                matching.assert_not_called()

    def test_failed_lp_keeps_completed_hp_stage(self):
        high_id, low_id = self.cubes()
        with mock.patch.object(bg_mixins.LPMatchingWorker, '_run_impl', side_effect=RuntimeError('injected LP failure')):
            self.ui.btn_run_hp.click()
            self.wait_done()
        self.assertNotEqual(cmds.listRelatives(cmds.ls(high_id, long=True)[0], parent=True, fullPath=True)[0], '|Asset_HP')
        self.assertEqual(cmds.listRelatives(cmds.ls(low_id, long=True)[0], parent=True, fullPath=True)[0], '|Asset_LP')

    def test_new_scene_cancels_chain_before_lp(self):
        self.cubes()
        with mock.patch.object(self.ui, 'run_lp_matching', wraps=self.ui.run_lp_matching) as matching:
            self.ui.btn_run_hp.click()
            cmds.file(new=True, force=True)
            self.wait_done()
            matching.assert_not_called()
        self.assertFalse(cmds.objExists('Asset_HP'))

    def test_close_between_keep_hp_and_lp_invalidates_queued_step(self):
        self.fixture.create_group('Existing')
        self.ui.cb_keep_hp_structure.setChecked(True)
        with mock.patch.object(self.ui, 'run_lp_matching', wraps=self.ui.run_lp_matching) as matching:
            self.ui.btn_run_hp.click()
            self.assertEqual(self.ui._auto_grouping_context['phase'], 'lp_queued')
            self.assertTrue(self.ui._update_ui_busy())
            self.assertTrue(self.ui.shutdown_for_reload())
            APP.processEvents()
            matching.assert_not_called()
            self.assertIsNone(self.ui._auto_grouping_context)

    def test_asset_task_is_pinned_even_if_active_id_changes_programmatically(self):
        high_id, low_id = self.cubes()
        other, hp, lp = self.fixture.make_pair('Other')
        self.ui.root_pairs.append(other)
        with mock.patch.object(self.ui, 'run_lp_matching', wraps=self.ui.run_lp_matching) as matching:
            self.ui.btn_run_hp.click()
            self.ui.activate_root(other)
            self.assertEqual(self.ui.active_root_id, self.pair['id'])
            self.ui.active_root_id = other['id']
            self.wait_done()
        self.assertEqual(matching.call_args[1]['pair_id'], self.pair['id'])
        self.assert_matched(high_id, low_id)
        self.assertFalse(cmds.listRelatives(hp, children=True))
        self.assertFalse(cmds.listRelatives(lp, children=True))

    def test_session_pair_replacement_rejects_queued_hp_result(self):
        self.cubes()
        with mock.patch.object(self.ui, 'on_hp_finished', wraps=self.ui.on_hp_finished) as commit:
            self.ui.btn_run_hp.click()
            self.ui.root_pairs = [dict(self.pair)]
            self.wait_done()
            commit.assert_not_called()

    def test_root_rename_and_replacement_rejects_queued_hp_result(self):
        self.cubes()
        with mock.patch.object(self.ui, 'on_hp_finished', wraps=self.ui.on_hp_finished) as commit:
            self.ui.btn_run_hp.click()
            cmds.rename(self.hp, 'MovedAsset_HP')
            replacement = cmds.group(empty=True, name='Asset_HP')
            untouched = self.fixture.cube(replacement, 'UserMesh')
            unchanged = cmds.ls(untouched, uuid=True)[0]
            self.wait_done()
            commit.assert_not_called()
        self.assertEqual(cmds.ls(unchanged, long=True), ['|Asset_HP|UserMesh'])

    def test_empty_validation_cancel_and_all_locked_release_workflow(self):
        with mock.patch.object(self.ui, 'run_lp_matching', wraps=self.ui.run_lp_matching) as matching:
            self.ui.btn_run_hp.click()
            self.wait_done()
            with mock.patch.object(self.ui, 'validate_frozen_transforms', return_value=False):
                self.ui.btn_run_hp.click()
                self.wait_done()
            self.ui._hp_structure_checked_chapters.clear()
            with mock.patch.object(bg_mixins.QtWidgets.QMessageBox, 'exec_', return_value=None):
                self.ui.btn_run_hp.click()
                self.wait_done()
            self.ui._mark_chapter_pre_checked(self.pair['id'])
            self.fixture.create_group('Locked')
            self.pair['locked'] = ['Locked']
            before = sorted(cmds.ls('|Asset_HP|Locked_HP', '|Asset_LP|Locked_LP', uuid=True))
            for keep in (False, True):
                self.ui.cb_keep_hp_structure.setChecked(keep)
                self.ui.btn_run_hp.click()
                self.wait_done()
            self.assertEqual(sorted(cmds.ls('|Asset_HP|Locked_HP', '|Asset_LP|Locked_LP', uuid=True)), before)
            matching.assert_not_called()

    def test_keep_preparation_error_reverts_only_its_unique_chunk(self):
        group = cmds.group(empty=True, name='Loose', parent=self.hp)
        self.fixture.cube(group, 'Detail')
        self.fixture.cube(self.lp, 'RawLow')
        sentinel = cmds.group(empty=True, name='UserEdit')
        cmds.setAttr(sentinel + '.tx', 7)
        self.ui.cb_keep_hp_structure.setChecked(True)
        with mock.patch.object(self.ui, 'prepare_meshes', side_effect=RuntimeError('injected preparation failure')):
            self.ui.btn_run_hp.click()
            self.wait_done()
        self.assertTrue(cmds.objExists('|Asset_HP|Loose'))
        self.assertFalse(cmds.objExists('|Asset_HP|Loose_HP'))
        self.assertEqual(cmds.getAttr(sentinel + '.tx'), 7)

    def test_empty_failed_apply_does_not_undo_previous_user_command(self):
        self.cubes()
        sentinel = cmds.group(empty=True, name='UserEdit')
        original = self.ui.on_hp_finished
        def fail_before_first_mutation(*args, **kwargs):
            cmds.setAttr(sentinel + '.tx', 7)
            with mock.patch.object(bg_mixins.cmds, 'listRelatives', side_effect=RuntimeError('injected empty apply failure')):
                return original(*args, **kwargs)
        with mock.patch.object(self.ui, 'on_hp_finished', side_effect=fail_before_first_mutation):
            with mock.patch.object(self.ui, 'run_lp_matching', wraps=self.ui.run_lp_matching) as matching:
                self.ui.btn_run_hp.click()
                self.wait_done()
                matching.assert_not_called()
        self.assertEqual(cmds.getAttr(sentinel + '.tx'), 7)


if __name__ == '__main__':
    try:
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(AutoGroupingRegression))
    finally:
        shutdown()
    sys.exit(0 if result.wasSuccessful() else 1)
