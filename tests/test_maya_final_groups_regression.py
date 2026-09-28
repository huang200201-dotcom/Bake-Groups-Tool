"""Final-group workflow regressions in a disposable standalone Maya scene.

Run in fresh mayapy against src/Bake_Groups. This constructs the real Qt UI and
executes Create Group, Combine Fin, final actions and rename without mocking
the scene or the grouping implementation. The export regression writes actual
FBX files into a temporary directory and reimports them to verify geometry.
"""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

from maya_regression_runtime import APP, VERSION, cmds, QtWidgets, shutdown
import bg_main_window
import bg_ui_widgets
import bg_final_export
import bg_cage
import bg_localization


class FinalGroupWorkflowRegression(unittest.TestCase):
    def setUp(self):
        cmds.file(new=True, force=True)
        bg_localization.set_language('en')
        self.ui = bg_main_window.BakeManagerUI()
        self.pair, self.hp, self.lp = self.make_pair('Asset')
        self.ui.root_pairs = [self.pair]
        self.ui.active_root_id = self.pair['id']
        self.ui.cb_keep_hp_structure.setChecked(False)

    def tearDown(self):
        self.assertTrue(self.ui.shutdown_for_reload())
        # MayaQWidgetDockableMixin.close() is a no-op for the parentless
        # windows used by standalone. Invoke Qt's close path explicitly.
        QtWidgets.QWidget.close(self.ui)
        APP.processEvents()
        bg_final_export.FinalExportProcessor._owned_export_uuids.clear()

    @staticmethod
    def make_pair(base):
        hp = cmds.group(empty=True, name=base + '_HP')
        lp = cmds.group(empty=True, name=base + '_LP')
        pair = dict(id=base, base=base, hp_uuid=cmds.ls(hp, uuid=True)[0],
                    lp_uuid=cmds.ls(lp, uuid=True)[0], locked=[], book='Regression')
        return pair, hp, lp

    @staticmethod
    def cube(parent, name):
        node = cmds.polyCube(name=name, constructionHistory=False)[0]
        node = cmds.parent(node, parent, absolute=True)[0]
        return cmds.ls(node, long=True)[0]

    def create_group(self, name):
        high = self.cube(self.hp, 'RawHigh')
        low = self.cube(self.lp, 'RawLow')
        cmds.select([high, low], replace=True)
        self.ui.input_suffix.setText(name)
        self.ui.create_subgroup_pair()

    def normal_rows(self):
        self.ui.is_final_view = False
        self.ui.refresh_left_panel()
        result = []
        for index in range(self.ui.subgroups_layout.count()):
            widget = self.ui.subgroups_layout.itemAt(index).widget()
            if widget and widget.objectName() == 'SubgroupRow':
                result.append(widget.findChild(bg_ui_widgets.SubgroupButton).text())
        return sorted(result)

    def final_items(self):
        self.ui.is_final_view = True
        self.ui.refresh_left_panel()
        return {item['subgroup_name']: item for item in self.ui.final_mesh_widgets}

    def combine(self):
        # Use the real user action, including its structural preflight.
        # A failed preflight must assert rather than open a blocking dialog.
        hp_names = self.ui.combine_fin_subgroup_names(self.hp, '_HP', 'HP')
        lp_names = self.ui.combine_fin_subgroup_names(self.lp, '_LP', 'LP')
        self.assertEqual(hp_names, lp_names)
        self.ui.combine_all_subgroups_ui()

    def assert_rows(self, names):
        self.assertEqual(self.normal_rows(), sorted(names))
        self.assertEqual(sorted(self.final_items()), sorted(names))

    def test_tokens_and_repeated_chapter_prefix_remain_four_real_groups(self):
        names = ['Main', 'brace_low_insert', 'panel_high_detail', 'rear_Asset_label']
        for name in names:
            self.create_group(name)
        self.assertEqual(self.normal_rows(), sorted(names))
        self.combine()
        self.assert_rows(names)
        items = self.final_items()
        for name in names:
            self.assertEqual(items[name]['full_prefix'], 'Asset_' + name)
            self.assertTrue(items[name]['hp_nodes'])

    def test_orphans_intermediate_shapes_and_other_chapters_do_not_make_rows(self):
        self.create_group('Main')
        self.combine()
        other_pair, other_hp, _other_lp = self.make_pair('Other')
        self.ui.root_pairs.append(other_pair)
        self.cube(other_hp, 'Other_Main_high_001')
        self.cube(self.hp, 'Asset_NotMain_high_001')
        self.cube(self.hp, 'Asset_Ghost_high_001')
        history = self.cube(self.hp, 'Asset_History_high_001')
        for shape in cmds.listRelatives(history, shapes=True, fullPath=True):
            cmds.setAttr(shape + '.intermediateObject', True)
        self.assert_rows(['Main'])
        item = self.final_items()['Main']
        self.assertEqual(item['hp_nodes'], ['|Asset_HP|Main_HP|Asset_Main_high_001'])
        self.ui.select_final_hp_nodes('Main')
        self.assertEqual(cmds.ls(selection=True, long=True), item['hp_nodes'])

    def test_preview_and_visibility_stay_inside_exact_active_subgroup(self):
        for name in ['Main', 'NotMain']:
            self.create_group(name)
        self.combine()
        other_pair, other_hp, _other_lp = self.make_pair('Other')
        self.ui.root_pairs.append(other_pair)
        other = self.cube(other_hp, 'Other_Main_high_001')
        items = self.final_items()
        main = items['Main']['hp_nodes'][0]
        not_main = items['NotMain']['hp_nodes'][0]
        for node in (main, not_main, other):
            cmds.setAttr(node + '.visibility', True)
            for shape in cmds.listRelatives(node, shapes=True, fullPath=True, noIntermediate=True):
                cmds.setAttr(shape + '.displaySmoothMesh', 0)
        self.ui.is_preview_active = True
        items['Main']['combo'].setCurrentIndex(2)
        self.ui.update_single_preview(items['Main']['full_prefix'], items['Main']['combo'])
        for node, expected in [(main, 2), (not_main, 0), (other, 0)]:
            shape = cmds.listRelatives(node, shapes=True, fullPath=True, noIntermediate=True)[0]
            self.assertEqual(cmds.getAttr(shape + '.displaySmoothMesh'), expected)
        self.ui.toggle_final_hp_vis(items['Main']['hp_nodes'], items['Main']['btn_vis'])
        self.assertFalse(cmds.getAttr(main + '.visibility'))
        self.assertTrue(cmds.getAttr(not_main + '.visibility'))
        self.assertTrue(cmds.getAttr(other + '.visibility'))

    def test_second_combine_preserves_exact_list_and_node_counts(self):
        names = ['panel_high_detail', 'rear_Asset_label', 'Main']
        for name in names:
            self.create_group(name)
        self.combine()
        first = self.final_items()
        counts = {name: len(item['hp_nodes']) for name, item in first.items()}
        self.combine()
        self.assert_rows(names)
        second = self.final_items()
        self.assertEqual({name: len(item['hp_nodes']) for name, item in second.items()}, counts)
        output = cmds.listRelatives('|LP_Combine_BG|Asset', children=True, type='transform', fullPath=True)
        self.assertEqual(len(output), len(names))

    def test_internal_hp_lp_tokens_and_collision_digits_use_one_name(self):
        names = ['part_HP_core', 'part_LP_mid']
        for name in names + ['Foo']:
            self.create_group(name)
        cmds.rename('|Asset_HP|Foo_HP', 'Foo_HP2')
        cmds.rename('|Asset_LP|Foo_LP', 'Foo_LP2')
        names.append('Foo2')
        self.assertEqual(self.normal_rows(), sorted(names))
        self.combine()
        self.assert_rows(names)
        for name in names:
            self.assertTrue(cmds.objExists('|LP_Combine_BG|Asset|Asset_' + name + '_low'))
            self.assertTrue(self.final_items()[name]['hp_nodes'])

    def test_rename_updates_sources_finals_smooth_state_and_next_combine(self):
        self.create_group('Main')
        self.combine()
        self.final_items()['Main']['combo'].setCurrentIndex(2)
        self.ui.snapshot_final_smooth_states()
        self.ui.rename_final_subgroup('Main', 'Renamed')
        self.assert_rows(['Renamed'])
        self.assertTrue(cmds.objExists('|Asset_HP|Renamed_HP'))
        self.assertTrue(cmds.objExists('|Asset_LP|Renamed_LP'))
        self.assertTrue(cmds.objExists('|LP_Combine_BG|Asset|Asset_Renamed_low'))
        item = self.final_items()['Renamed']
        self.assertEqual(item['combo'].currentIndex(), 2)
        self.assertTrue(all(node.split('|')[-1].startswith('Asset_Renamed_high') for node in item['hp_nodes']))
        self.assertNotIn('Main', self.pair['final_smooth_states'])
        self.assertNotIn('prefix:asset_main', self.pair['final_smooth_states'])
        self.combine()
        self.assert_rows(['Renamed'])
        self.assertEqual(self.final_items()['Renamed']['combo'].currentIndex(), 2)

    def test_material_export_copies_never_become_final_group_members(self):
        self.create_group('Main')
        self.combine()
        original = self.final_items()['Main']['hp_nodes']
        low = '|LP_Combine_BG|Asset|Asset_Main_low'
        shading_groups = []
        for index in (1, 2):
            shader = cmds.shadingNode('lambert', asShader=True, name='RegressionMat' + str(index))
            sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=shader + 'SG')
            cmds.connectAttr(shader + '.outColor', sg + '.surfaceShader', force=True)
            shading_groups.append(sg)
        cmds.sets(low + '.f[0:5]', edit=True, forceElement=shading_groups[0])
        cmds.sets(low + '.f[6:11]', edit=True, forceElement=shading_groups[1])
        processor = bg_final_export.FinalExportProcessor
        materials = processor._get_mesh_materials_and_faces(low)
        self.assertEqual(len(materials), 2)
        split_low, split_high = processor._process_multimaterial_mesh(low, original, materials)
        try:
            self.assertEqual(len(split_high), 2)
            items = self.final_items()
            self.assertEqual(sorted(items), ['Main'])
            self.assertEqual(items['Main']['hp_nodes'], original)
        finally:
            processor._cleanup_zero_transform_hp_export_temps(split_low + split_high)

    def test_export_snapshot_matches_rows_and_ignores_stale_widget_members(self):
        names = ['Main', 'panel_high_detail', 'rear_Asset_label']
        for name in names:
            self.create_group(name)
        self.combine()
        other_pair, other_hp, _other_lp = self.make_pair('Other')
        self.ui.root_pairs.append(other_pair)
        foreign = self.cube(other_hp, 'Other_Main_high_001')
        self.cube(self.hp, 'Asset_Ghost_high_001')
        self.cube('|LP_Combine_BG|Asset', 'Asset_Ghost_low')
        # Keep-structure is OFF: an unregistered arbitrary container is not a
        # subgroup in either normal UI or final UI and must not enter export.
        stray = cmds.group(empty=True, name='Unregistered', parent=self.hp)
        self.cube(stray, 'Asset_Unregistered_high_001')
        items = self.final_items()
        self.assertEqual(sorted(items), sorted(names))
        items['Main']['combo'].setCurrentIndex(2)
        stale_widgets = [dict(item, hp_nodes=[foreign]) for item in items.values()]
        stale_widgets.append(dict(items['Main'], full_prefix='Asset_Ghost', hp_nodes=[foreign]))
        snapshot = bg_final_export.FinalExportProcessor.build_chapter_snapshot(
            'Asset', self.hp, self.lp, final_mesh_widgets=stale_widgets)
        expected_prefixes = {'Asset_' + name for name in names}
        self.assertEqual({item['prefix'] for item in snapshot['prefix_items']}, expected_prefixes)
        self.assertEqual(set(snapshot['hp_by_prefix']), expected_prefixes)
        self.assertEqual(set(snapshot['lp_by_prefix']), expected_prefixes)
        expected_highs, expected_lows = [], []
        for name, item in items.items():
            prefix = 'Asset_' + name
            expected_low = '|LP_Combine_BG|Asset|Asset_' + name + '_low'
            self.assertEqual(snapshot['hp_by_prefix'][prefix], item['hp_nodes'])
            self.assertEqual(snapshot['lp_by_prefix'][prefix], [expected_low])
            expected_highs.extend(item['hp_nodes'])
            expected_lows.append(expected_low)
        self.assertEqual(snapshot['hp_all'], sorted(expected_highs))
        self.assertEqual(snapshot['lp_all'], sorted(expected_lows))
        self.assertNotIn(foreign, snapshot['hp_all'])
        main = next(item for item in snapshot['prefix_items'] if item['prefix'] == 'Asset_Main')
        self.assertEqual(main['smooth_level'], 2)

        # Keep Structure explicitly opts the untagged source container into
        # both views and the export index; snapshot defaults must not override it.
        self.ui.cb_keep_hp_structure.setChecked(True)
        kept_items = self.final_items()
        self.assertEqual(sorted(kept_items), sorted(names + ['Unregistered']))
        kept_snapshot = bg_final_export.FinalExportProcessor.build_chapter_snapshot(
            'Asset', self.hp, self.lp, final_mesh_widgets=stale_widgets,
            keep_structure=True)
        self.assertEqual({item['prefix'] for item in kept_snapshot['prefix_items']},
                         {item['full_prefix'] for item in kept_items.values()})
        self.assertEqual(kept_snapshot['hp_by_prefix']['Asset_Unregistered'],
                         kept_items['Unregistered']['hp_nodes'])
        self.assertEqual(kept_snapshot['lp_by_prefix']['Asset_Unregistered'], [])
        self.assertNotIn(foreign, kept_snapshot['hp_all'])

    def test_cage_rename_preserves_geometry_source_link_and_manual_edits(self):
        self.create_group('Main')
        self.combine()
        self.final_items()
        self.ui.rebuild_active_cage(preserve_existing=True)
        cages = bg_cage.CageManager.chapter_cages('Asset')
        self.assertEqual(len(cages), 1)
        cage = cages[0]
        identifier = cmds.ls(cage, uuid=True)[0]
        source_attr = bg_cage.CageManager.SOURCE_ATTR
        self.assertEqual(cmds.getAttr(cage + '.' + source_attr), '|LP_Combine_BG|Asset|Asset_Main_low')
        cmds.move(0.31, 0.07, -0.11, cage + '.vtx[0]', relative=True, worldSpace=True)
        edited = cmds.xform(cage + '.vtx[*]', query=True, worldSpace=True, translation=True)
        self.ui.rename_final_subgroup('Main', 'Renamed')
        renamed = cmds.ls(identifier, long=True)
        self.assertEqual(renamed, ['|BakeMaster_Cage_BG|Asset|Asset_Renamed_cage'])
        cage = renamed[0]
        self.assertEqual(cmds.getAttr(cage + '.' + source_attr), '|LP_Combine_BG|Asset|Asset_Renamed_low')
        self.assertEqual(cmds.xform(cage + '.vtx[*]', query=True, worldSpace=True, translation=True), edited)
        # A second edit after rename must survive the standard missing-cage action.
        cmds.move(-0.12, 0.04, 0.25, cage + '.vtx[1]', relative=True, worldSpace=True)
        edited_again = cmds.xform(cage + '.vtx[*]', query=True, worldSpace=True, translation=True)
        self.ui.rebuild_active_cage(preserve_existing=True)
        self.assertEqual(bg_cage.CageManager.chapter_cages('Asset'), [cage])
        self.assertEqual(cmds.ls(cage, uuid=True), [identifier])
        self.assertEqual(cmds.xform(cage + '.vtx[*]', query=True, worldSpace=True, translation=True), edited_again)
        self.assertEqual(cmds.getAttr(cage + '.' + source_attr), '|LP_Combine_BG|Asset|Asset_Renamed_low')

    def test_panel_export_separate_writes_and_reimports_real_hp_lp_fbx(self):
        names = ['Main', 'panel_high_detail']
        for name in names:
            self.create_group(name)
        self.combine()
        items = self.final_items()
        self.ui.chk_cage_export.setChecked(False)
        self.ui._save_active_cage_settings()
        self.assertFalse(self.pair['cage_settings']['export_enabled'])

        source_nodes = cmds.ls(type='transform', long=True) or []
        source_identity = {cmds.ls(node, uuid=True)[0]: node for node in source_nodes}
        source_mesh_points = {}
        for identifier, node in source_identity.items():
            if cmds.listRelatives(node, shapes=True, type='mesh', noIntermediate=True):
                source_mesh_points[identifier] = cmds.xform(
                    node + '.vtx[*]', query=True, worldSpace=True, translation=True)
        selected = [items['Main']['hp_nodes'][0], '|LP_Combine_BG|Asset|Asset_panel_high_detail_low']
        cmds.select(selected, replace=True)
        selection_before = cmds.ls(selection=True, long=True)

        with tempfile.TemporaryDirectory(prefix='bakemaster-ui-export-') as directory:
            # Only the folder picker is mocked: the UI handler, snapshot,
            # actual FBX exporter and FBX importer all execute normally.
            with mock.patch.object(cmds, 'fileDialog2', return_value=[directory]) as picker:
                self.ui.export_final_group_ui(mode='separate')
            picker.assert_called_once()
            self.assertEqual(sorted(os.listdir(directory)), ['Asset_HP.fbx', 'Asset_LP.fbx'])
            self.assertEqual(cmds.ls(selection=True, long=True), selection_before)
            self.assertEqual(set(cmds.ls(type='transform', long=True)), set(source_nodes))
            for identifier, node in source_identity.items():
                self.assertEqual(cmds.ls(identifier, long=True), [node])
                if identifier in source_mesh_points:
                    self.assertEqual(cmds.xform(node + '.vtx[*]', query=True,
                                               worldSpace=True, translation=True),
                                     source_mesh_points[identifier])

            for role, suffix in [('high', 'HP'), ('low', 'LP')]:
                path = os.path.join(directory, 'Asset_' + suffix + '.fbx')
                self.assertGreater(os.path.getsize(path), 1000)
                # Import each file into an empty disposable scene, so source
                # geometry cannot accidentally satisfy the round-trip checks.
                cmds.file(new=True, force=True)
                cmds.file(path.replace('\\', '/'), i=True, type='FBX',
                          ignoreVersion=True, namespace='Regression' + suffix,
                          mergeNamespacesOnClash=False)
                shapes = cmds.ls(type='mesh', long=True) or []
                meshes = sorted(set(cmds.listRelatives(shape, parent=True, fullPath=True)[0]
                                    for shape in shapes
                                    if not cmds.getAttr(shape + '.intermediateObject')))
                self.assertEqual(len(meshes), len(names), (suffix, meshes))
                imported_groups = set()
                for mesh in meshes:
                    short = mesh.rsplit('|', 1)[-1].rsplit(':', 1)[-1]
                    match = re.match(r'^Asset_(Main|panel_high_detail)_' + role + r'(?:_\d+)?$', short)
                    self.assertIsNotNone(match, (role, mesh))
                    imported_groups.add(match.group(1))
                    self.assertEqual(cmds.polyEvaluate(mesh, vertex=True), 8)
                    self.assertEqual(cmds.polyEvaluate(mesh, face=True), 6 if role == 'high' else 12)
                self.assertEqual(imported_groups, set(names))


if __name__ == '__main__':
    try:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(FinalGroupWorkflowRegression)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        print('BAKE_MASTER_FINAL_GROUPS_REGRESSION', cmds.about(version=True), VERSION,
              'PASS' if result.wasSuccessful() else 'FAIL')
    finally:
        shutdown()
    sys.exit(0 if result.wasSuccessful() else 1)
