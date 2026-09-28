"""Real Maya/Qt regressions for non-destructive final-view visibility.

Run in disposable mayapy against the single source runtime.
Imports the shared real UI fixture, not its test suite. The export restoration
test writes real FBX files into a temporary directory.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

import test_maya_final_groups_regression as fixture
from maya_regression_runtime import APP, VERSION, cmds, shutdown
import bg_final_export


class VisibilityRegression(unittest.TestCase):
    setUp = fixture.FinalGroupWorkflowRegression.setUp
    tearDown = fixture.FinalGroupWorkflowRegression.tearDown
    make_pair = staticmethod(fixture.FinalGroupWorkflowRegression.make_pair)
    cube = staticmethod(fixture.FinalGroupWorkflowRegression.cube)
    create_group = fixture.FinalGroupWorkflowRegression.create_group
    normal_rows = fixture.FinalGroupWorkflowRegression.normal_rows
    final_items = fixture.FinalGroupWorkflowRegression.final_items
    combine = fixture.FinalGroupWorkflowRegression.combine

    def chapters(self):
        for name in ('Visible', 'Hidden'):
            self.create_group(name)
        self.combine()
        active = self.pair, self.hp, self.lp
        other, other_hp, other_lp = self.make_pair('Other')
        self.ui.root_pairs.append(other)
        self.pair, self.hp, self.lp = other, other_hp, other_lp
        self.ui.active_root_id = other['id']
        for name in ('OtherVisible', 'OtherHidden'):
            self.create_group(name)
        self.combine()
        self.pair, self.hp, self.lp = active
        self.ui.active_root_id = self.pair['id']
        self.ui.is_final_view = False
        self.ui.refresh_left_panel()
        # Establish deliberate existing user state after the initial build.
        for node, visible in (
            ('|Asset_HP|Visible_HP', 1), ('|Asset_HP|Hidden_HP', 0),
            ('|Asset_LP|Visible_LP', 1), ('|Asset_LP|Hidden_LP', 0),
            ('|LP_Combine_BG', 1), ('|LP_Combine_BG|Asset', 1),
            ('|LP_Combine_BG|Other', 1),
            ('|LP_Combine_BG|Asset|Asset_Visible_low', 1),
            ('|LP_Combine_BG|Asset|Asset_Hidden_low', 0),
            ('|LP_Combine_BG|Other|Other_OtherVisible_low', 1),
            ('|LP_Combine_BG|Other|Other_OtherHidden_low', 0),
        ):
            cmds.setAttr(node + '.visibility', visible)

    @staticmethod
    def effective_visibility(node):
        node = cmds.ls(node, long=True)[0]
        while node:
            if cmds.attributeQuery('visibility', node=node, exists=True):
                if not cmds.getAttr(node + '.visibility'):
                    return False
            parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
            node = parents[0] if parents else None
        return True

    @staticmethod
    def plug_state(node):
        result = {}
        for attr in ('visibility', 'overrideEnabled', 'overrideVisibility', 'overrideDisplayType'):
            plug = node + '.' + attr
            if cmds.objExists(plug):
                result[attr] = (cmds.getAttr(plug), cmds.getAttr(plug, lock=True),
                                tuple(cmds.listConnections(plug, source=True,
                                                           destination=False, plugs=True) or []))
        return result

    def uuid_states(self, nodes):
        return {cmds.ls(node, uuid=True)[0]: self.plug_state(node) for node in nodes}

    def assert_uuid_states(self, states):
        for identifier, expected in states.items():
            nodes = cmds.ls(identifier, long=True) or []
            self.assertEqual(len(nodes), 1, identifier)
            self.assertEqual(self.plug_state(nodes[0]), expected, nodes[0])

    def low_states(self):
        nodes = cmds.listRelatives('|LP_Combine_BG', allDescendents=True,
                                   type='transform', fullPath=True) or []
        return {node: self.plug_state(node) for node in nodes + ['|LP_Combine_BG']}

    def test_combine_hides_generated_chapter_but_keeps_sources_and_other_chapters(self):
        self.chapters()
        parent = cmds.group(empty=True, name='UserHiddenAncestor')
        self.hp = cmds.parent(self.hp, parent, absolute=True)[0]
        cmds.setAttr(parent + '.visibility', 0)
        layer = cmds.createDisplayLayer(name='UserHiddenLayer', empty=True)
        cmds.editDisplayLayerMembers(layer, '|Asset_LP|Hidden_LP', noRecurse=True)
        cmds.setAttr(layer + '.visibility', 0)
        nodes = [parent, self.hp, self.lp, layer]
        for root in (self.hp, self.lp):
            nodes.extend(cmds.listRelatives(root, allDescendents=True, fullPath=True) or [])
        sources = self.uuid_states(nodes)
        finals = self.low_states()
        self.ui.combine_all_subgroups_ui()
        self.assert_uuid_states(sources)
        finals['|LP_Combine_BG|Asset']['visibility'] = (False, False, ())
        self.assertEqual(self.low_states(), finals)
        self.assertFalse(self.effective_visibility(self.hp))
        self.assertFalse(self.effective_visibility('|LP_Combine_BG|Asset|Asset_Visible_low'))
        self.assertTrue(self.effective_visibility('|LP_Combine_BG|Other|Other_OtherVisible_low'))

    def test_repeated_combine_hides_result_each_time_and_keeps_individual_mesh_choices(self):
        self.chapters()
        cmds.setAttr('|LP_Combine_BG|Asset.visibility', 0)
        shape = cmds.listRelatives('|LP_Combine_BG|Asset|Asset_Visible_low',
                                   shapes=True, noIntermediate=True, fullPath=True)[0]
        cmds.setAttr(shape + '.visibility', 0)
        states = self.low_states()
        for _repeat in range(2):
            cmds.setAttr('|LP_Combine_BG|Asset.visibility', 1)
            self.ui.combine_all_subgroups_ui()
            self.assertEqual(self.low_states(), states)
            current_shape = cmds.listRelatives('|LP_Combine_BG|Asset|Asset_Visible_low',
                                               shapes=True, noIntermediate=True, fullPath=True)[0]
            self.assertFalse(cmds.getAttr(current_shape + '.visibility'))

    def test_enter_exit_final_keeps_locked_and_connected_visibility(self):
        self.chapters()
        cmds.setAttr('|Asset_HP|Hidden_HP.visibility', lock=True)
        driver = cmds.createNode('network', name='UserVisibilityDriver')
        cmds.addAttr(driver, longName='show', attributeType='bool', defaultValue=True)
        cmds.connectAttr(driver + '.show', '|Asset_LP|Visible_LP.visibility', force=True)
        nodes = cmds.ls(type='transform', long=True) or []
        before = self.uuid_states(nodes)
        for expected in (True, False, True, False):
            self.ui.toggle_final_view()
            self.assertEqual(self.ui.is_final_view, expected)
            self.assert_uuid_states(before)

    def test_view_switch_keeps_isolation_membership_with_standalone_panel_adapter(self):
        self.chapters()
        # mayapy's modelPanel/isolateSelect UI commands return False/None.
        # Adapt only that missing viewport handle. The objectSet and all
        # membership edits are real Maya scene operations, not mocked data.
        isolation = cmds.sets('|Asset_LP|Visible_LP', name='UserIsolateSet')
        before = sorted(cmds.sets(isolation, query=True) or [])
        panel = 'StandaloneIsolationPanel'
        original_get_panel = cmds.getPanel
        original_isolate = cmds.isolateSelect

        def get_panel(*args, **kwargs):
            if kwargs.get('type') == 'modelPanel':
                return [panel]
            return original_get_panel(*args, **kwargs)

        def isolate(requested, *args, **kwargs):
            if requested != panel:
                return original_isolate(requested, *args, **kwargs)
            if kwargs.get('state') and (kwargs.get('query') or kwargs.get('q')):
                return True
            if kwargs.get('viewObjects'):
                return isolation
            if kwargs.get('addDagObject'):
                cmds.sets(kwargs['addDagObject'], addElement=isolation)

        with mock.patch.object(cmds, 'getPanel', side_effect=get_panel):
            with mock.patch.object(cmds, 'isolateSelect', side_effect=isolate) as viewport:
                for _step in range(2):
                    self.ui.toggle_final_view()
                    self.assertEqual(sorted(cmds.sets(isolation, query=True) or []), before)
                viewport.assert_not_called()

    def test_low_toggle_only_changes_active_chapter_with_visible_global_root(self):
        self.chapters()
        other_nodes = ['|LP_Combine_BG|Other'] + (cmds.listRelatives(
            '|LP_Combine_BG|Other', allDescendents=True, fullPath=True) or [])
        other_state = self.uuid_states(other_nodes)
        source_state = self.uuid_states([self.hp, self.lp, '|Asset_LP|Visible_LP', '|Asset_LP|Hidden_LP'])
        self.ui.is_final_view = True
        self.ui.set_final_low_visibility('Asset', False)
        self.assertFalse(self.effective_visibility('|LP_Combine_BG|Asset|Asset_Visible_low'))
        self.assertTrue(cmds.getAttr('|LP_Combine_BG.visibility'))
        self.assert_uuid_states(other_state)
        self.assert_uuid_states(source_state)
        self.assertFalse(self.ui.btn_toggle_lp.isChecked())
        self.ui.set_final_low_visibility('Asset', True)
        self.assertTrue(self.effective_visibility('|LP_Combine_BG|Asset|Asset_Visible_low'))
        self.assert_uuid_states(other_state)
        self.assert_uuid_states(source_state)
        self.assertTrue(self.ui.btn_toggle_lp.isChecked())

    def test_show_active_under_hidden_global_never_exposes_other_chapter(self):
        self.chapters()
        cmds.setAttr('|LP_Combine_BG.visibility', 0)
        other = '|LP_Combine_BG|Other|Other_OtherVisible_low'
        self.assertFalse(self.effective_visibility(other))
        self.ui.is_final_view = True
        self.ui.set_final_low_visibility('Asset', True)
        self.assertTrue(self.effective_visibility('|LP_Combine_BG|Asset|Asset_Visible_low'))
        self.assertFalse(self.effective_visibility(other))
        self.assertTrue(self.ui.btn_toggle_lp.isChecked())

    def test_low_toggle_handles_locked_or_driven_container_without_forcing_plugs(self):
        self.chapters()
        chapter = '|LP_Combine_BG|Asset'
        self.ui.is_final_view = True
        cmds.setAttr(chapter + '.visibility', 0)
        cmds.setAttr(chapter + '.visibility', lock=True)
        locked = self.plug_state(chapter)
        self.ui.set_final_low_visibility('Asset', True)
        self.assertEqual(self.plug_state(chapter), locked)
        self.assertFalse(self.ui.btn_toggle_lp.isChecked())
        cmds.setAttr(chapter + '.visibility', lock=False)
        driver = cmds.createNode('network', name='UserFinalVisibilityDriver')
        cmds.addAttr(driver, longName='show', attributeType='bool', defaultValue=False)
        cmds.connectAttr(driver + '.show', chapter + '.visibility', force=True)
        connected = self.plug_state(chapter)
        self.ui.set_final_low_visibility('Asset', True)
        self.assertEqual(self.plug_state(chapter), connected)
        self.assertFalse(self.ui.btn_toggle_lp.isChecked())

    def test_low_button_reads_effective_scene_visibility_not_stale_flag(self):
        self.chapters()
        chapter = '|LP_Combine_BG|Asset'
        for root_vis, chapter_vis, mesh_vis, expected in (
            (1, 1, 1, True), (0, 1, 1, False),
            (1, 0, 1, False), (1, 1, 0, True),
        ):
            cmds.setAttr('|LP_Combine_BG.visibility', root_vis)
            cmds.setAttr(chapter + '.visibility', chapter_vis)
            cmds.setAttr(chapter + '|Asset_Visible_low.visibility', mesh_vis)
            cmds.setAttr(chapter + '|Asset_Hidden_low.visibility', 0)
            self.ui.is_final_view = False
            self.ui.is_final_low_visible = not expected
            states = self.low_states()
            self.ui.toggle_final_view()
            self.assertEqual(self.low_states(), states)
            self.assertEqual(self.ui.btn_toggle_lp.isChecked(), expected)

    def test_hidden_global_migration_is_atomic_when_other_chapter_is_protected(self):
        self.chapters()
        cmds.setAttr('|LP_Combine_BG.visibility', 0)
        other = '|LP_Combine_BG|Other'
        cmds.setAttr(other + '.visibility', lock=True)
        self.ui.is_final_view = True
        before = self.low_states()
        self.ui.set_final_low_visibility('Asset', True)
        self.assertEqual(self.low_states(), before)
        self.assertFalse(self.ui.btn_toggle_lp.isChecked())
        cmds.setAttr(other + '.visibility', lock=False)
        driver = cmds.createNode('network', name='OtherVisibilityDriver')
        cmds.addAttr(driver, longName='show', attributeType='bool', defaultValue=True)
        cmds.connectAttr(driver + '.show', other + '.visibility', force=True)
        before = self.low_states()
        self.ui.set_final_low_visibility('Asset', True)
        self.assertEqual(self.low_states(), before)
        self.assertFalse(self.ui.btn_toggle_lp.isChecked())

    def test_ui_export_preserves_view_and_component_selection_on_all_paths(self):
        self.chapters()
        items = self.final_items()
        self.ui.chk_cage_export.setChecked(False)
        self.ui._save_active_cage_settings()
        items['Visible']['combo'].setCurrentIndex(1)
        items['Hidden']['combo'].setCurrentIndex(2)
        self.ui.toggle_preview_smoothing()
        self.assertTrue(self.ui.is_preview_active)
        self.ui._sync_final_low_visibility_ui('Asset')
        selected_mesh = items['Visible']['hp_nodes'][0]
        cmds.select(selected_mesh + '.vtx[0:2]', replace=True)
        component_selection = cmds.ls(selection=True, long=True)
        self.assertTrue(any('.vtx[' in item for item in component_selection))
        nodes = (cmds.ls(type='transform', long=True) or []) + (cmds.ls(type='mesh', long=True) or [])
        visibility = self.uuid_states(nodes)
        preview_attributes = {}
        for node in cmds.ls(type='mesh', long=True) or []:
            preview_attributes[cmds.ls(node, uuid=True)[0]] = (
                cmds.getAttr(node + '.displaySmoothMesh'), cmds.getAttr(node + '.smoothLevel'))
        self.assertTrue(any(value[0] == 2 for value in preview_attributes.values()))
        flags = (self.ui.is_preview_active, self.ui.is_final_low_visible)
        preview_button = (self.ui.btn_preview.text(), self.ui.btn_preview.styleSheet())
        transforms = set(cmds.ls(type='transform', long=True) or [])

        def assert_restored():
            self.assert_uuid_states(visibility)
            self.assertEqual(cmds.ls(selection=True, long=True), component_selection)
            self.assertEqual((self.ui.is_preview_active, self.ui.is_final_low_visible), flags)
            self.assertEqual((self.ui.btn_preview.text(), self.ui.btn_preview.styleSheet()), preview_button)
            self.assertEqual(set(cmds.ls(type='transform', long=True) or []), transforms)
            for identifier, expected in preview_attributes.items():
                current = cmds.ls(identifier, long=True)
                self.assertEqual(len(current), 1)
                self.assertEqual((cmds.getAttr(current[0] + '.displaySmoothMesh'),
                                  cmds.getAttr(current[0] + '.smoothLevel')), expected)

        with tempfile.TemporaryDirectory(prefix='bakemaster-view-restore-') as directory:
            success = os.path.join(directory, 'success')
            failed = os.path.join(directory, 'failed')
            os.makedirs(success)
            os.makedirs(failed)
            with self.subTest(path='success'):
                with mock.patch.object(cmds, 'fileDialog2', return_value=[success]):
                    self.ui.export_final_group_ui(mode='separate')
                self.assertEqual(sorted(os.listdir(success)), ['Asset_HP.fbx', 'Asset_LP.fbx'])
                self.assertTrue(all(os.path.getsize(os.path.join(success, name)) > 1000
                                    for name in os.listdir(success)))
                assert_restored()
            with self.subTest(path='cancelled_directory'):
                with mock.patch.object(cmds, 'fileDialog2', return_value=[]):
                    self.ui.export_final_group_ui(mode='separate')
                assert_restored()
            with self.subTest(path='writer_failure'):
                # Only the final writer fails; actual UI, snapshot, preparation,
                # temporary meshes, exporter cleanup and view guard still run.
                with mock.patch.object(cmds, 'fileDialog2', return_value=[failed]):
                    with mock.patch.object(bg_final_export.FinalExportProcessor, 'export_selected_fbx',
                                           side_effect=RuntimeError('Injected FBX writer failure')) as writer:
                        self.ui.export_final_group_ui(mode='separate')
                    self.assertEqual(writer.call_count, 2)
                self.assertEqual(os.listdir(failed), [])
                assert_restored()


if __name__ == '__main__':
    try:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(VisibilityRegression)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        print('BAKE_MASTER_VISIBILITY_REGRESSION', cmds.about(version=True), VERSION,
              'PASS' if result.wasSuccessful() else 'FAIL')
    finally:
        shutdown()
    sys.exit(0 if result.wasSuccessful() else 1)
