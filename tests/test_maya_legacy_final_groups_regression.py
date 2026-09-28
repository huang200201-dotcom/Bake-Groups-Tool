"""Disposable Maya scene/FBX regressions for legacy final-group discovery.

Run in fresh mayapy. The shared UI fixture initializes standalone Maya
and uses src/Bake_Groups without authorization fixtures.
No artist scene is opened or modified; FBX output lives in a temporary folder.
"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import test_maya_final_groups_regression as fixture
import bg_cage
import bg_final_groups

cmds = fixture.cmds
Processor = fixture.bg_final_export.FinalExportProcessor


class LegacyFinalGroupRegression(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.FinalGroupWorkflowRegression('runTest')
        self.fixture.setUp()
        self.directory = tempfile.mkdtemp(prefix='bakemaster-legacy-final-')
        if not cmds.pluginInfo('fbxmaya', query=True, loaded=True):
            cmds.loadPlugin('fbxmaya', quiet=True)

    def tearDown(self):
        self.fixture.tearDown()
        shutil.rmtree(self.directory, ignore_errors=True)

    def snapshot(self):
        test = self.fixture
        return Processor.build_chapter_snapshot(
            'Asset', test.hp, test.lp,
            final_mesh_widgets=list(test.final_items().values()),
            keep_structure=test.ui._final_keep_structure())

    def imported_mesh_names(self, path):
        cmds.file(new=True, force=True)
        cmds.file(path, i=True, type='FBX', ignoreVersion=True,
                  mergeNamespacesOnClash=False)
        return sorted(node.rsplit('|', 1)[-1] for node in cmds.ls(type='transform', long=True)
                      if cmds.listRelatives(node, shapes=True, type='mesh', noIntermediate=True))

    def test_direct_low_combine_snapshot_and_fbx_round_trip(self):
        test = self.fixture
        test.cube(test.hp, 'RawHigh')
        test.cube(test.lp, 'RawLow')
        test.ui.combine_all_subgroups_ui()
        expected = '|LP_Combine_BG|Asset|Asset_low'
        self.assertTrue(cmds.objExists(expected))
        # A role token inside an arbitrary name is not a final export suffix.
        test.cube(test.hp, 'Asset_Ghost_high_notes')
        test.cube('|LP_Combine_BG|Asset', 'Other_Main_low')
        items = test.final_items()
        self.assertEqual(sorted(items), ['Asset'])
        self.assertEqual(items['Asset']['full_prefix'], 'Asset')
        snapshot = self.snapshot()
        self.assertEqual(snapshot['lp_all'], [expected])
        self.assertEqual(snapshot['hp_all'], [])
        self.assertEqual(Processor.export_chapter(
            'Asset', test.hp, test.lp, list(items.values()), mode='lp',
            export_dir=self.directory, prepared_snapshot=snapshot), 'Asset_LP')
        self.assertEqual(os.listdir(self.directory), ['Asset_LP.fbx'])
        self.assertTrue(cmds.objExists(expected))
        self.assertEqual(self.imported_mesh_names(os.path.join(self.directory, 'Asset_LP.fbx')),
                         ['Asset_low'])

    def test_flat_legacy_pairs_ignore_role_fragments_and_foreign_highs(self):
        test = self.fixture
        expected_hp = test.cube(test.hp, 'Asset_panel_high_detail_high_001')
        expected_lp = test.cube(test.lp, 'Asset_panel_high_detail_low')
        test.cube(test.hp, 'Asset_Ghost_high_notes')
        test.cube(test.hp, 'Other_panel_high_detail_high_001')
        self.assertEqual(sorted(test.final_items()), ['panel_high_detail'])
        snapshot = self.snapshot()
        self.assertEqual(snapshot['hp_all'], [expected_hp])
        self.assertEqual(snapshot['lp_all'], [expected_lp])
        self.assertEqual(Processor.export_chapter(
            'Asset', test.hp, test.lp, list(test.final_items().values()), mode='hp',
            export_dir=self.directory, prepared_snapshot=snapshot,
            combine_regular_hp=False), 'Asset_HP')
        self.assertEqual(self.imported_mesh_names(os.path.join(self.directory, 'Asset_HP.fbx')),
                         ['Asset_panel_high_detail_high_001'])

    def test_flat_legacy_high_only_group_remains_exportable(self):
        test = self.fixture
        high = test.cube(test.hp, 'Asset_Main_high_001')
        test.cube(test.hp, 'Other_Main_high_001')
        self.assertEqual(sorted(test.final_items()), ['Main'])
        snapshot = self.snapshot()
        self.assertEqual(snapshot['hp_all'], [high])
        self.assertEqual(snapshot['lp_all'], [])
        self.assertEqual(Processor.export_chapter(
            'Asset', test.hp, test.lp, list(test.final_items().values()), mode='hp',
            export_dir=self.directory, prepared_snapshot=snapshot,
            combine_regular_hp=False), 'Asset_HP')
        self.assertEqual(self.imported_mesh_names(os.path.join(self.directory, 'Asset_HP.fbx')),
                         ['Asset_Main_high_001'])

    def test_keep_structure_list_preview_and_snapshot_use_same_members(self):
        test = self.fixture
        test.create_group('Main')
        custom_hp = cmds.group(empty=True, name='Custom', parent=test.hp)
        custom_lp = cmds.group(empty=True, name='Custom_LP', parent=test.lp)
        test.cube(custom_hp, 'CustomHigh')
        test.cube(custom_lp, 'CustomLow')
        result = Processor.combine_all_subgroups('Asset', test.hp, test.lp)
        self.assertTrue(result['success'])
        for keep, names in [(False, ['Main']), (True, ['Custom', 'Main'])]:
            test.ui.cb_keep_hp_structure.setChecked(keep)
            items = test.final_items()
            self.assertEqual(sorted(items), names)
            snapshot = self.snapshot()
            expected_hp = sorted(node for item in items.values() for node in item['hp_nodes'])
            self.assertEqual(snapshot['hp_all'], expected_hp)
            expected_groups = bg_final_groups.discover('Asset', test.hp, test.lp, keep)
            self.assertEqual(sorted(expected_groups), names)
            self.assertEqual(snapshot['lp_all'], sorted(
                node for group in expected_groups.values() for node in group['lp_nodes']))
            # Preview and double-click selection use the same active group.
            for name, item in items.items():
                test.ui.is_preview_active = True
                item['combo'].setCurrentIndex(2)
                test.ui.update_single_preview(item['full_prefix'], item['combo'])
                test.ui.select_final_hp_nodes(name)
                self.assertEqual(sorted(cmds.ls(selection=True, long=True)), sorted(item['hp_nodes']))
                for node in item['hp_nodes']:
                    shape = cmds.listRelatives(node, shapes=True, type='mesh',
                                               fullPath=True, noIntermediate=True)[0]
                    self.assertEqual(cmds.getAttr(shape + '.displaySmoothMesh'), 2)

    def test_case_distinct_groups_keep_separate_cage_high_members(self):
        test = self.fixture
        test.create_group('Main')
        test.create_group('main')
        test.combine()
        items = test.final_items()
        lows = ['|LP_Combine_BG|Asset|Asset_' + name + '_low' for name in ('Main', 'main')]
        expected_map = {'Asset_' + name: items[name]['hp_nodes'] for name in ('Main', 'main')}
        high_map = test.ui._cage_high_map(test.hp, lows)
        self.assertEqual(high_map, expected_map)
        expected_calls = {low: expected_map[low.rsplit('|', 1)[-1][:-4]] for low in lows}
        manager = bg_cage.CageManager
        # Record the actual fitting inputs while retaining the real inflation path.
        with fixture.mock.patch.object(manager, '_inflate_and_fit',
                                       wraps=manager._inflate_and_fit) as inflate:
            cages = manager.rebuild_chapter(
                'Asset', lows, high_map,
                settings={'fit_to_high': False, 'inflate_pct': 2})
        self.assertEqual(len(cages), 2)
        self.assertEqual(inflate.call_count, 2)
        self.assertEqual({call.args[1]: call.args[2] for call in inflate.call_args_list}, expected_calls)


if __name__ == '__main__':
    try:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(LegacyFinalGroupRegression)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        print('BAKE_MASTER_LEGACY_FINAL_GROUPS', cmds.about(version=True), fixture.VERSION,
              'PASS' if result.wasSuccessful() else 'FAIL')
    finally:
        fixture.shutdown()
    sys.exit(0 if result.wasSuccessful() else 1)
