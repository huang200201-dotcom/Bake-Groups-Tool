"""Export view regressions in a fresh standalone Maya process only.

Scene nodes, components, attributes, object sets and Qt controls are real Maya.
Standalone cannot create modelPanels; the isolated-panel test substitutes only
panel commands and MEL Auto Load preferences, retaining real Maya object sets.
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from maya_regression_runtime import APP, VERSION, cmds, QtWidgets, shutdown
import bg_scene_state as state
import bg_final_export


class ViewRegression(unittest.TestCase):
    def setUp(self):
        cmds.file(new=True, force=True)
        self.hp = cmds.group(empty=True, name='Asset_HP')
        self.lp = cmds.group(empty=True, name='Asset_LP')
        self.high = self.cube(self.hp, 'Asset_part_high_001')
        self.global_lp = cmds.group(empty=True, name='LP_Combine_BG')
        self.chapter = cmds.group(empty=True, name='Asset', parent=self.global_lp)
        self.low = self.cube(self.chapter, 'Asset_part_low')
        self.high_shape = cmds.listRelatives(self.high, shapes=True, fullPath=True)[0]
        self.low_shape = cmds.listRelatives(self.low, shapes=True, fullPath=True)[0]
        cmds.setAttr(self.high_shape + '.displaySmoothMesh', 2)
        cmds.setAttr(self.high_shape + '.smoothLevel', 2)
        pair = {'id': 'Asset', 'base': 'Asset'}
        self.ui = types.SimpleNamespace(root_pairs=[pair], active_root_id='Asset',
            core=types.SimpleNamespace(resolve_main_nodes=lambda pair: (self.hp, self.lp, 'Asset')),
            is_preview_active=True, is_final_low_visible=True,
            btn_preview=QtWidgets.QPushButton('Smooth ON'), btn_toggle_lp=QtWidgets.QPushButton('Low Visible'))
        self.ui.btn_preview.setCheckable(True)
        self.ui.btn_preview.setChecked(True)
        self.ui.btn_toggle_lp.setCheckable(True)
        self.ui.btn_toggle_lp.setChecked(True)
        self.ui._sync_final_view_ui = mock.Mock()
        self.ui._sync_final_low_visibility_ui = self.sync_low
        self.selection = [self.high + '.vtx[0:2]', self.low + '.f[1:2]']
        cmds.select(self.selection, replace=True)

    def tearDown(self):
        self.ui.btn_preview.deleteLater()
        self.ui.btn_toggle_lp.deleteLater()
        APP.processEvents()

    @staticmethod
    def cube(parent, name):
        node = cmds.polyCube(name=name, constructionHistory=False)[0]
        return cmds.ls(cmds.parent(node, parent)[0], long=True)[0]

    def sync_low(self, base):
        visible = bool(cmds.getAttr(self.global_lp + '.visibility') and
                       cmds.getAttr(self.chapter + '.visibility'))
        self.ui.is_final_low_visible = visible
        self.ui.btn_toggle_lp.setChecked(visible)
        self.ui.btn_toggle_lp.setText('Low Visible' if visible else 'Low Hidden')

    def change_view(self):
        for node in (self.global_lp, self.chapter, self.low, self.low_shape):
            cmds.setAttr(node + '.visibility', False)
        cmds.setAttr(self.high_shape + '.displaySmoothMesh', 0)
        cmds.setAttr(self.high_shape + '.smoothLevel', 0)
        self.ui.is_preview_active = False
        self.ui.is_final_low_visible = False
        self.ui.btn_preview.setText('Smooth View')
        self.ui.btn_toggle_lp.setText('Low Hidden')
        cmds.select(clear=True)

    def assert_restored(self):
        for node in (self.global_lp, self.chapter, self.low, self.low_shape):
            self.assertTrue(cmds.getAttr(node + '.visibility'))
        self.assertEqual(cmds.getAttr(self.high_shape + '.displaySmoothMesh'), 2)
        self.assertEqual(cmds.getAttr(self.high_shape + '.smoothLevel'), 2)
        self.assertTrue(self.ui.is_preview_active)
        self.assertTrue(self.ui.is_final_low_visible)
        self.assertEqual(self.ui.btn_preview.text(), 'Smooth ON')
        self.assertEqual(self.ui.btn_toggle_lp.text(), 'Low Visible')
        self.assertEqual(cmds.ls(selection=True, long=True), self.selection)
        self.assertEqual(self.ui._export_view_depth, 0)

    def test_success_and_mid_operation_cancel_restore_components_preview_and_visibility(self):
        for result in (True, False):
            @state.preserve_export_view
            def export(ui):
                self.change_view()
                return result
            self.assertEqual(export(self.ui), result)
            self.assert_restored()

    def test_early_dialog_cancel_does_not_rewrite_unchanged_attributes(self):
        @state.preserve_export_view
        def export(ui):
            return False
        with mock.patch.object(cmds, 'setAttr', wraps=cmds.setAttr) as writes:
            self.assertFalse(export(self.ui))
        self.assertEqual(writes.call_count, 0)
        self.assert_restored()

    def test_object_set_selection_remains_the_set_not_its_members(self):
        object_set = cmds.sets(self.high, name='ArtistSelectionSet')
        cmds.select(object_set, replace=True, noExpand=True)
        @state.preserve_export_view
        def export(ui):
            cmds.select(clear=True)
        export(self.ui)
        self.assertEqual(cmds.ls(selection=True), [object_set])

    def test_closed_ui_does_not_skip_selection_or_isolation_or_mask_export_error(self):
        self.ui._sync_final_view_ui.side_effect = RuntimeError('deleted Qt control')
        @state.preserve_export_view
        def export(ui):
            self.change_view()
            raise RuntimeError('original export failure')
        with mock.patch.object(state, 'restore_isolation', wraps=state.restore_isolation) as restore:
            with self.assertRaisesRegex(RuntimeError, 'original export failure'):
                export(self.ui)
        self.assertEqual(restore.call_count, 1)
        self.assertTrue(cmds.getAttr(self.global_lp + '.visibility'))
        self.assertEqual(cmds.ls(selection=True, long=True), self.selection)

    def test_fbx_failure_restores_view_and_propagates_original_error(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, 'Asset_LP.fbx')
            with open(target, 'wb') as stream:
                stream.write(b'previous file')
            def writer(path, **kwargs):
                with open(path, 'wb') as stream:
                    stream.write(b'partial FBX')
                raise RuntimeError('injected FBX failure')
            @state.preserve_export_view
            def export(ui):
                self.change_view()
                processor = bg_final_export.FinalExportProcessor
                processor._fbx_session_depth = 1
                try:
                    with mock.patch.object(cmds, 'file', side_effect=writer):
                        processor.export_selected_fbx(target)
                finally:
                    processor._fbx_session_depth = 0
            with self.assertRaisesRegex(RuntimeError, 'injected FBX failure'):
                export(self.ui)
            with open(target, 'rb') as stream:
                self.assertEqual(stream.read(), b'previous file')
            self.assertEqual(os.listdir(directory), ['Asset_LP.fbx'])
        self.assert_restored()

    def test_nested_batch_snapshots_once_and_restores_after_second_item_cancels(self):
        @state.preserve_export_view
        def item(ui, number):
            if number == 2:
                self.assertFalse(cmds.getAttr(self.global_lp + '.visibility'))
                return False
            self.change_view()
            return True
        @state.preserve_export_view
        def batch(ui):
            for number in (1, 2):
                if not item(ui, number):
                    return False
            return True
        with mock.patch.object(state, '_capture_view', wraps=state._capture_view) as snapshots:
            self.assertFalse(batch(self.ui))
        self.assertEqual(snapshots.call_count, 1)
        self.assert_restored()

    def test_uuid_restore_survives_rename_and_skips_deleted_selection(self):
        identifier = cmds.ls(self.high, uuid=True)[0]
        @state.preserve_export_view
        def export(ui):
            cmds.setAttr(self.high + '.visibility', False)
            cmds.rename(self.high, 'RenamedHigh')
            cmds.delete(self.low)
            cmds.select(clear=True)
        export(self.ui)
        renamed = cmds.ls(identifier, long=True)[0]
        self.assertTrue(cmds.getAttr(renamed + '.visibility'))
        self.assertEqual(cmds.ls(selection=True, long=True), [renamed + '.vtx[0:2]'])

    def test_changed_locked_or_connected_attributes_are_not_forced(self):
        driver = cmds.createNode('transform', name='visibilityDriver')
        cmds.select(self.selection, replace=True)
        @state.preserve_export_view
        def export(ui):
            cmds.setAttr(self.high + '.visibility', False)
            cmds.setAttr(self.high + '.visibility', lock=True)
            cmds.setAttr(driver + '.visibility', False)
            cmds.connectAttr(driver + '.visibility', self.low + '.visibility')
        export(self.ui)
        self.assertFalse(cmds.getAttr(self.high + '.visibility'))
        self.assertTrue(cmds.getAttr(self.high + '.visibility', lock=True))
        self.assertTrue(cmds.isConnected(driver + '.visibility', self.low + '.visibility'))
        self.assertFalse(cmds.getAttr(self.low + '.visibility'))

    def test_isolation_restores_exact_real_component_members_and_auto_add(self):
        # mayapy cannot instantiate modelPanels. Substitute panel APIs only;
        # Maya owns the set and resolves real renamed/component members.
        object_set = cmds.sets(self.selection, name='RegressionIsolationSet')
        panel = {'enabled': True, 'auto_add': True}
        class PanelAdapter:
            def __getattr__(self, name): return getattr(cmds, name)
            def getPanel(self, **kwargs): return ['regressionPanel']
            def modelPanel(self, name, **kwargs): return name == 'regressionPanel'
            def modelEditor(self, name, **kwargs): return ''
            def isolateSelect(self, name, **kwargs):
                if kwargs.get('query'):
                    return object_set if kwargs.get('viewObjects') else panel['enabled']
                if 'state' in kwargs:
                    panel['enabled'] = kwargs['state']
                    cmds.sets(clear=object_set)
                    if kwargs['state']:
                        selected = cmds.ls(selection=True, long=True) or []
                        if selected: cmds.sets(selected, addElement=object_set)
        def auto_add(name, value=None):
            if value is None: return panel['auto_add']
            panel['auto_add'] = value
        with mock.patch.object(state, 'cmds', PanelAdapter()), mock.patch.object(state, '_auto_add', side_effect=auto_add):
            with state.suspended_isolation():
                self.assertFalse(panel['enabled'])
                self.assertFalse(panel['auto_add'])
                cmds.select(self.lp)
                cmds.sets(self.lp, addElement=object_set)
            self.assertTrue(panel['enabled'])
            self.assertTrue(panel['auto_add'])
            self.assertEqual(set(cmds.ls(cmds.sets(object_set, query=True), long=True)), set(self.selection))
            self.assertEqual(cmds.ls(selection=True, long=True), self.selection)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        shutdown()
