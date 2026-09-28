"""Behavior/FBX round-trip tests. Run only in a fresh mayapy process.

These tests call file(new=True, force=True) in their own standalone Maya process;
never source this script into an artist's existing Maya session.
"""
import ast
import contextlib
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

try:
    import maya.standalone
    import maya.cmds as cmds
    import maya.api.OpenMaya as om
    import maya.mel as mel
    MAYA = True
except ImportError:
    MAYA = False

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "src" / "Bake_Groups"



class Progress:
    def __init__(self, *args): pass
    def __getattr__(self, name): return lambda *args: False


def load_runtime():
    """Load production geometry functions; UI behavior has separate tests."""
    groups_spec = importlib.util.spec_from_file_location('bg_final_groups', RUNTIME / 'bg_final_groups.py')
    groups = importlib.util.module_from_spec(groups_spec)
    groups_spec.loader.exec_module(groups)
    sys.modules['bg_final_groups'] = groups
    namespace = dict(cmds=cmds, om=om, mel=mel, os=os, tempfile=tempfile, shutil=shutil,
                     contextlib=contextlib, math=math, re=re,
                     bg_core=types.SimpleNamespace(undo_chunk=lambda name: contextlib.nullcontext(),
                         BakeConfig=types.SimpleNamespace(SUFFIX_HP='_HP', SUFFIX_LP='_LP',
                                                          ATTR_BAKE_GROUP='BakeManagerGroup')),
                     bg_l10n=types.SimpleNamespace(text=lambda text: text),

                     CAGE_ROOT='BakeMaster_Cage_BG', CAGE_SUFFIX='_cage', CAGE_COLOR=(.18,.72,.92),
                     QtWidgets=types.SimpleNamespace(QProgressDialog=Progress,
                         QApplication=types.SimpleNamespace(processEvents=lambda: None)),
                     QtCore=types.SimpleNamespace(Qt=types.SimpleNamespace(WindowModal=1)))
    for filename in ('bg_final_export.py', 'bg_cage.py'):
        tree = ast.parse((RUNTIME / filename).read_text(encoding='utf-8-sig'))
        nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, 'exec'), namespace)
    return namespace['FinalExportProcessor'], namespace['CageManager'], namespace['CageCancelled']


class AtomicExportFilenameTests(unittest.TestCase):
    """Exercise Maya filename-return variations without requiring a host install."""
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='bakemaster-file-test-'))
        self.output = self.directory / '中文 空格.151'
        self.output.mkdir()
        self.target = self.output / '刀具 part.v2_HP.fbx'
        self.warnings = []
        namespace = dict(cmds=types.SimpleNamespace(warning=self.warnings.append),
                         os=os, tempfile=tempfile, shutil=shutil, contextlib=contextlib)
        tree = ast.parse((RUNTIME / 'bg_final_export.py').read_text(encoding='utf-8-sig'))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == 'FinalExportProcessor')
        exec(compile(ast.Module(body=[cls], type_ignores=[]), 'bg_final_export.py', 'exec'), namespace)
        self.exporter = namespace['FinalExportProcessor']
        self.exporter._fbx_session_depth = 1
        self.commands = namespace['cmds']

    def tearDown(self):
        shutil.rmtree(str(self.directory))

    def test_writer_receives_real_basename_inside_owned_subdirectory(self):
        def write(path, **kwargs):
            staged = Path(path)
            self.assertEqual(staged.name, self.target.name)
            self.assertNotEqual(staged.parent, self.target.parent)
            self.assertEqual(staged.parent.parent, self.target.parent)
            self.assertFalse(staged.exists())
            staged.write_bytes(b'complete FBX')
            (staged.parent / 'writer.log').write_text('owned auxiliary file')
            return str(staged)
        self.commands.file = write
        self.exporter.export_selected_fbx(str(self.target))
        self.assertEqual(self.target.read_bytes(), b'complete FBX')
        self.assertEqual([node.name for node in self.output.iterdir()], [self.target.name])

    def test_returned_rewritten_filename_is_published_with_requested_name(self):
        def write(path, **kwargs):
            expected = Path(path)
            actual = expected.with_name(expected.stem + '.' + expected.name)
            actual.write_bytes(b'renamed by Maya')
            return str(actual)
        self.commands.file = write
        self.exporter.export_selected_fbx(str(self.target))
        self.assertEqual(self.target.read_bytes(), b'renamed by Maya')
        self.assertEqual([node.name for node in self.output.iterdir()], [self.target.name])

    def test_outside_return_path_is_never_moved_or_deleted(self):
        artist = self.output / 'artist.fbx'
        artist.write_bytes(b'artist asset')
        self.target.write_bytes(b'previous export')
        self.commands.file = lambda path, **kwargs: str(artist)
        with self.assertRaises(RuntimeError):
            self.exporter.export_selected_fbx(str(self.target))
        self.assertEqual(artist.read_bytes(), b'artist asset')
        self.assertEqual(self.target.read_bytes(), b'previous export')
        self.assertEqual({node.name for node in self.output.iterdir()}, {artist.name, self.target.name})

    def test_rewritten_partial_file_is_cleaned_when_writer_raises(self):
        self.target.write_bytes(b'previous export')
        def partial(path, **kwargs):
            requested = Path(path)
            requested.with_name('rewritten.rewritten.fbx').write_bytes(b'partial')
            raise RuntimeError('writer failed')
        self.commands.file = partial
        with self.assertRaises(RuntimeError):
            self.exporter.export_selected_fbx(str(self.target))
        self.assertEqual(self.target.read_bytes(), b'previous export')
        self.assertEqual([node.name for node in self.output.iterdir()], [self.target.name])

    def test_empty_file_cannot_replace_previous_export(self):
        self.target.write_bytes(b'previous export')
        def empty(path, **kwargs):
            Path(path).touch()
            return path
        self.commands.file = empty
        with self.assertRaises(RuntimeError):
            self.exporter.export_selected_fbx(str(self.target))
        self.assertEqual(self.target.read_bytes(), b'previous export')
        self.assertEqual([node.name for node in self.output.iterdir()], [self.target.name])

    def test_missing_return_uses_expected_file_without_guessing_other_files(self):
        def write(path, **kwargs):
            Path(path).write_bytes(b'expected')
            return None
        self.commands.file = write
        self.exporter.export_selected_fbx(str(self.target))
        self.assertEqual(self.target.read_bytes(), b'expected')
        self.assertEqual([node.name for node in self.output.iterdir()], [self.target.name])


@unittest.skipUnless(MAYA, 'requires a fresh mayapy standalone process')
class ExportCageMayaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        maya.standalone.initialize(name='python')
        cls.exporter, cls.cage, cls.cancelled = load_runtime()
        cmds.loadPlugin('fbxmaya', quiet=True)

    def setUp(self):
        cmds.file(new=True, force=True)
        self.exporter._owned_export_uuids.clear()
        self.directory = tempfile.mkdtemp(prefix='bakemaster-test-')
        self.hp = cmds.group(empty=True, name='HP')
        self.lp = cmds.group(empty=True, name='LP')
        self.highs = []
        for index in (1, 2):
            node = cmds.polyCube(name='asset_part_high_{:03d}'.format(index))[0]
            cmds.setAttr(node + '.tx', index * 3)
            self.highs.append(cmds.ls(cmds.parent(node, self.hp)[0], long=True)[0])
        low = cmds.polyCube(name='asset_part_low')[0]
        self.low = cmds.ls(cmds.parent(low, self.lp)[0], long=True)[0]

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def export(self, **kwargs):
        return self.exporter.export_chapter('asset', self.hp, self.lp, [],
            mode='hp', export_dir=self.directory, **kwargs)

    def test_copy_failure_never_colors_source_or_replaces_existing_file(self):
        target = Path(self.directory) / 'asset_HP.fbx'
        target.write_bytes(b'existing asset')
        with mock.patch.object(cmds, 'duplicate', side_effect=RuntimeError('copy failed')):
            self.assertFalse(self.export(independent_island_vertex_colors=True))
        self.assertEqual(target.read_bytes(), b'existing asset')
        self.assertEqual(cmds.polyColorSet(self.highs[0], query=True, allColorSets=True) or [], [])
        self.assertEqual(cmds.ls('BG_HP_Export_Zero_Temp*'), [])

    def test_color_failure_is_not_success_and_target_survives(self):
        target = Path(self.directory) / 'asset_HP.fbx'
        target.write_bytes(b'existing asset')
        with mock.patch.object(cmds, 'polyColorPerVertex', side_effect=RuntimeError('color failed')):
            self.assertFalse(self.export(independent_island_vertex_colors=True))
        self.assertEqual(target.read_bytes(), b'existing asset')
        self.assertFalse(cmds.ls('BG_HP_Export_Zero_Temp*'))

    def test_cleanup_ignores_lookalike_artist_group(self):
        artist = cmds.group(empty=True, name='BG_HP_Export_Zero_TempArtist')
        self.exporter._cleanup_zero_transform_hp_export_temps()
        self.assertTrue(cmds.objExists(artist))

    def test_atomic_fbx_write_preserves_target_on_partial_writer_failure(self):
        target = Path(self.directory) / 'kept.fbx'
        target.write_bytes(b'old')
        def partial(path, **kwargs):
            Path(path).write_bytes(b'partial')
            raise RuntimeError('disk failure')
        self.exporter._fbx_session_depth = 1
        try:
            with mock.patch.object(cmds, 'file', side_effect=partial):
                with self.assertRaises(RuntimeError):
                    self.exporter.export_selected_fbx(str(target))
        finally:
            self.exporter._fbx_session_depth = 0
        self.assertEqual(target.read_bytes(), b'old')
        self.assertEqual(sorted(os.listdir(self.directory)), ['kept.fbx'])

    def test_export_filenames_and_source_names_across_all_modes(self):
        output = Path(self.directory) / '中文 空格.151'
        output.mkdir()
        cage = self.make_cage()[0]
        sources = self.highs + [self.low, cage]
        identities = {cmds.ls(node, uuid=True)[0]: node for node in sources}
        snapshot = self.exporter.build_chapter_snapshot('asset', self.hp, self.lp)
        for mode, name in [('hp', 'asset_HP'), ('lp', 'asset_LP'), ('both', 'asset')]:
            self.assertEqual(self.exporter.export_chapter(
                'asset', self.hp, self.lp, [], mode=mode, export_dir=str(output),
                prepared_snapshot=snapshot), name)
        self.assertTrue(self.exporter.export_meshes_fbx(
            [cage], str(output / 'asset_Cage.fbx'), triangulate_all=True))
        self.assertTrue(self.exporter.export_meshes_fbx(
            [self.low], str(output / '刀具 part.v2.fbx')))
        expected = {'asset_HP.fbx', 'asset_LP.fbx', 'asset.fbx',
                    'asset_Cage.fbx', '刀具 part.v2.fbx'}
        self.assertEqual({node.name for node in output.iterdir()}, expected)
        for identifier, name in identities.items():
            self.assertEqual(cmds.ls(identifier, long=True), [name])
        for name in sorted(expected):
            path = output / name
            self.assertGreater(path.stat().st_size, 0)
            cmds.file(new=True, force=True)
            cmds.file(str(path), i=True, type='FBX', options='fbx')
            shapes = cmds.ls(type='mesh', long=True, noIntermediate=True) or []
            self.assertTrue(shapes, name)
            for node in cmds.ls(dag=True, long=True):
                self.assertNotIn('BakeMasterExport-', node)
                self.assertNotIn('.bakemaster-export-', node)

    def test_combine_keeps_internal_role_tokens_and_numeric_pairing(self):
        cmds.delete(self.highs + [self.low])
        high_group = cmds.group(empty=True, name='wheel_HP_detail_HP2', parent=self.hp)
        low_group = cmds.group(empty=True, name='wheel_HP_detail_LP2', parent=self.lp)
        high = cmds.polyCube(name='sculptedSource')[0]
        cmds.parent(high, high_group)
        low = cmds.polyCube(name='retopoSource')[0]
        low = cmds.ls(cmds.parent(low, low_group)[0], long=True)[0]
        hidden = cmds.polyCube(name='historyOnlySource')[0]
        hidden = cmds.ls(cmds.parent(hidden, high_group)[0], long=True)[0]
        hidden_shape = cmds.listRelatives(hidden, shapes=True, fullPath=True)[0]
        cmds.setAttr(hidden_shape + '.intermediateObject', True)
        hidden_id = cmds.ls(hidden, uuid=True)[0]
        self.assertEqual(self.exporter.combine_all_subgroups('asset', self.hp, self.lp),
                         {'success': True, 'hp': 1, 'lp': 1})
        self.assertEqual(cmds.ls(hidden_id, long=True), [hidden])
        self.assertEqual(cmds.polyEvaluate(low, face=True), 6)
        snapshot = self.exporter.build_chapter_snapshot('asset', self.hp, self.lp)
        self.assertEqual(len(snapshot['prefix_items']), 1)
        self.assertEqual(snapshot['prefix_items'][0]['prefix'], 'asset_wheel_HP_detail2')
        self.assertEqual([node.rsplit('|', 1)[-1] for node in snapshot['hp_all']],
                         ['asset_wheel_HP_detail2_high_001'])
        self.assertEqual([node.rsplit('|', 1)[-1] for node in snapshot['lp_all']],
                         ['asset_wheel_HP_detail2_low'])

    def test_generated_final_chapter_is_hidden_and_other_chapter_stays_visible(self):
        source_nodes = [self.hp, self.lp] + self.highs + [self.low]
        source_nodes += cmds.listRelatives(self.hp, allDescendents=True, type='mesh', fullPath=True) or []
        source_nodes += cmds.listRelatives(self.lp, allDescendents=True, type='mesh', fullPath=True) or []
        cmds.setAttr(self.highs[0] + '.visibility', False)
        cmds.setAttr(self.lp + '.visibility', False)
        before = {cmds.ls(node, uuid=True)[0]: cmds.getAttr(node + '.visibility') for node in source_nodes}
        root = cmds.group(empty=True, name='LP_Combine_BG')
        other = cmds.group(empty=True, name='OtherChapter', parent=root)
        other_mesh = cmds.polyCube(name='OtherChapter_part_low')[0]
        other_mesh = cmds.parent(other_mesh, other)[0]
        other_id = cmds.ls(other_mesh, uuid=True)[0]
        for repeat in range(2):
            if repeat:
                cmds.setAttr('|LP_Combine_BG|asset.visibility', True)
            result = self.exporter.combine_all_subgroups('asset', self.hp, self.lp)
            self.assertTrue(result['success'])
            self.assertEqual(result['lp'], 1)
            self.assertFalse(cmds.getAttr('|LP_Combine_BG|asset.visibility'))
            self.assertTrue(cmds.getAttr('|LP_Combine_BG.visibility'))
            self.assertTrue(cmds.getAttr('|LP_Combine_BG|OtherChapter.visibility'))
            self.assertEqual(len(cmds.ls(other_id)), 1)
            for identifier, visible in before.items():
                node = cmds.ls(identifier, long=True)[0]
                self.assertEqual(cmds.getAttr(node + '.visibility'), visible, node)

    def test_first_generation_hidden_container_can_be_explicitly_shown(self):
        self.assertFalse(cmds.objExists('|LP_Combine_BG'))
        result = self.exporter.combine_all_subgroups('asset', self.hp, self.lp)
        self.assertTrue(result['success'])
        chapter = '|LP_Combine_BG|asset'
        self.assertFalse(cmds.getAttr(chapter + '.visibility'))
        finals = self.exporter.get_valid_mesh_transforms(chapter)
        self.assertEqual(len(finals), 1)
        self.assertTrue(cmds.getAttr(finals[0] + '.visibility'))
        cmds.setAttr(chapter + '.visibility', True)
        self.assertIn(finals[0], cmds.ls(finals[0], visible=True, long=True))

    def test_export_existing_final_never_resets_manual_display(self):
        self.assertTrue(self.exporter.combine_all_subgroups('asset', self.hp, self.lp)['success'])
        chapter = '|LP_Combine_BG|asset'
        final = self.exporter.get_valid_mesh_transforms(chapter)[0]
        nodes = [self.hp, self.lp, self.low, '|LP_Combine_BG', chapter, final]
        nodes += cmds.listRelatives(final, shapes=True, fullPath=True) or []
        for visible in (True, False):
            cmds.setAttr(chapter + '.visibility', visible)
            before = {node: cmds.getAttr(node + '.visibility') for node in nodes}
            self.assertEqual(self.exporter.export_chapter(
                'asset', self.hp, self.lp, [], mode='lp', export_dir=self.directory), 'asset_LP')
            self.assertEqual({node: cmds.getAttr(node + '.visibility') for node in nodes}, before)

    def test_internal_high_low_words_never_change_export_roles(self):
        cmds.delete(self.highs + [self.low])
        sources = []
        for group_name in ('brace_low_insert', 'panel_high_detail'):
            for role, root in [('high_001', self.hp), ('low', self.lp)]:
                node = cmds.polyCube(name='asset_' + group_name + '_' + role)[0]
                sources.append(cmds.ls(cmds.parent(node, root)[0], long=True)[0])
        identities = {cmds.ls(node, uuid=True)[0]: node for node in sources}
        self.assertEqual(self.exporter.export_chapter('asset', self.hp, self.lp, [],
            mode='both', export_dir=self.directory, independent_island_vertex_colors=True), 'asset')
        for identifier, name in identities.items():
            self.assertEqual(cmds.ls(identifier, long=True), [name])
            self.assertEqual(cmds.polyEvaluate(name, face=True), 6)
            self.assertEqual(cmds.polyColorSet(name, query=True, allColorSets=True) or [], [])
        cmds.file(new=True, force=True)
        cmds.file(str(Path(self.directory) / 'asset.fbx'), i=True, type='FBX', options='fbx')
        from bg_final_groups import final_role
        transforms = [cmds.listRelatives(shape, parent=True, fullPath=True)[0]
                      for shape in cmds.ls(type='mesh', long=True, noIntermediate=True)]
        self.assertEqual(len(transforms), 4)
        for node in transforms:
            role = final_role(node)
            self.assertIn(role, ('high', 'low'))
            self.assertEqual(cmds.polyEvaluate(node, face=True), 6 if role == 'high' else 12)
            color_sets = cmds.polyColorSet(node, query=True, allColorSets=True) or []
            self.assertEqual(bool(color_sets), role == 'high', (node, color_sets))

    def test_case_distinct_groups_keep_separate_combination_and_smoothing(self):
        cmds.delete(self.highs + [self.low])
        sources = []
        for name in ('Main', 'main'):
            for index in (1, 2):
                node = cmds.polyCube(name='asset_' + name + '_high_{:03d}'.format(index))[0]
                sources.append(cmds.ls(cmds.parent(node, self.hp)[0], long=True)[0])
            node = cmds.polyCube(name='asset_' + name + '_low')[0]
            sources.append(cmds.ls(cmds.parent(node, self.lp)[0], long=True)[0])
        widgets = [{'full_prefix': 'asset_Main', 'smooth_level': 1},
                   {'full_prefix': 'asset_main', 'smooth_level': 0}]
        snapshot = self.exporter.build_chapter_snapshot(
            'asset', self.hp, self.lp, final_mesh_widgets=widgets)
        self.assertEqual(set(snapshot['hp_by_prefix']), {'asset_Main', 'asset_main'})
        self.assertEqual({item['prefix']: item['smooth_level'] for item in snapshot['prefix_items']},
                         {'asset_Main': 1, 'asset_main': 0})
        self.assertEqual(self.exporter.export_chapter('asset', self.hp, self.lp, widgets,
            mode='both', export_dir=self.directory, prepared_snapshot=snapshot), 'asset')
        self.assertEqual(self.exporter.export_chapter('asset', self.hp, self.lp, widgets,
            mode='hp', export_dir=self.directory, prepared_snapshot=snapshot), 'asset_HP')
        self.assertEqual([cmds.polyEvaluate(node, face=True) for node in sources], [6] * 6)
        expected = {
            'asset.fbx': {'asset_Main_high_001': 24, 'asset_Main_high_002': 24,
                          'asset_main_high_001': 6, 'asset_main_high_002': 6,
                          'asset_Main_low': 12, 'asset_main_low': 12},
            'asset_HP.fbx': {'asset_Main_high': 48, 'asset_main_high': 12}}
        for filename, expected_counts in expected.items():
            cmds.file(new=True, force=True)
            cmds.file(str(Path(self.directory) / filename), i=True, type='FBX', options='fbx')
            transforms = [cmds.listRelatives(shape, parent=True, fullPath=True)[0]
                          for shape in cmds.ls(type='mesh', long=True, noIntermediate=True)]
            counts = {node.rsplit('|', 1)[-1]: cmds.polyEvaluate(node, face=True) for node in transforms}
            self.assertEqual(counts, expected_counts)

    def test_id_colors_round_trip_preserves_sources_and_distinct_islands(self):
        result = self.export(independent_island_vertex_colors=True)
        self.assertEqual(result, 'asset_HP')
        for source in self.highs:
            self.assertEqual(cmds.polyColorSet(source, query=True, allColorSets=True) or [], [])
            self.assertEqual(cmds.polyEvaluate(source, face=True), 6)
        cmds.file(new=True, force=True)
        cmds.file(str(Path(self.directory) / 'asset_HP.fbx'), i=True, type='FBX', options='fbx')
        shapes = cmds.ls(type='mesh', long=True, noIntermediate=True) or []
        self.assertEqual(len(shapes), 2)
        palette = set()
        for shape in shapes:
            selection = om.MSelectionList(); selection.add(shape)
            fn = om.MFnMesh(selection.getDagPath(0))
            sets = fn.getColorSetNames()
            self.assertTrue(sets)
            colors = fn.getVertexColors(colorSet=sets[0])
            self.assertEqual(len(colors), fn.numVertices)
            palette.add(tuple(round(float(colors[0][i]), 4) for i in range(3)))
        self.assertEqual(len(palette), 2)
        self.assertFalse(cmds.ls('BG_HP_Export_Zero_Temp*'))

    def test_hp_combine_preserves_zbrush_and_source_geometry(self):
        layer = cmds.createDisplayLayer(empty=True, name='zbrush_test')
        zbrush = cmds.polyCube(name='asset_part_high_003')[0]
        zbrush = cmds.ls(cmds.parent(zbrush, self.hp)[0], long=True)[0]
        cmds.editDisplayLayerMembers(layer, zbrush)
        result = self.export(smooth_states={'asset_part': 1})
        self.assertEqual(result, 'asset_HP')
        self.assertEqual([cmds.polyEvaluate(node, face=True) for node in self.highs + [zbrush]], [6,6,6])
        cmds.file(new=True, force=True)
        cmds.file(str(Path(self.directory) / 'asset_HP.fbx'), i=True, type='FBX', options='fbx')
        shapes = cmds.ls(type='mesh', long=True, noIntermediate=True) or []
        self.assertEqual(sorted(cmds.polyEvaluate(shape, face=True) for shape in shapes), [6,48])

    def test_snapshot_reuse_and_hp_only_avoids_lp_material_work(self):
        snapshot = self.exporter.build_chapter_snapshot('asset', self.hp, self.lp)
        with mock.patch.object(self.exporter, 'get_valid_mesh_transforms', side_effect=AssertionError('rescan')):
            with mock.patch.object(self.exporter, '_get_mesh_materials_and_faces', side_effect=AssertionError('unneeded materials')):
                self.assertEqual(self.export(prepared_snapshot=snapshot), 'asset_HP')
                self.assertEqual(self.exporter.export_chapter('asset', self.hp, self.lp, [],
                    mode='lp', export_dir=self.directory, prepared_snapshot=snapshot), 'asset_LP')

    def test_multimaterial_cache_round_trip_and_source_immutability(self):
        for name, faces in [('MaterialA', '0:2'), ('MaterialB', '3:5')]:
            material = cmds.shadingNode('lambert', asShader=True, name=name)
            group = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=name + 'SG')
            cmds.connectAttr(material + '.outColor', group + '.surfaceShader', force=True)
            cmds.sets(self.low + '.f[' + faces + ']', edit=True, forceElement=group)
        original_uvs = cmds.polyEditUV(self.low + '.map[*]', query=True)
        snapshot = self.exporter.build_chapter_snapshot('asset', self.hp, self.lp)
        lookup = self.exporter._get_mesh_materials_and_faces
        with mock.patch.object(self.exporter, '_get_mesh_materials_and_faces', wraps=lookup) as calls:
            for unused in range(2):
                self.assertEqual(self.exporter.export_chapter('asset', self.hp, self.lp, [],
                    mode='both', export_dir=self.directory, prepared_snapshot=snapshot,
                    independent_island_vertex_colors=True), 'asset')
            self.assertEqual(calls.call_count, 1)
        self.assertEqual(cmds.polyEvaluate(self.low, face=True), 6)
        self.assertEqual(cmds.polyEditUV(self.low + '.map[*]', query=True), original_uvs)
        self.assertFalse(self.exporter._owned_export_uuids)
        cmds.file(new=True, force=True)
        cmds.file(str(Path(self.directory) / 'asset.fbx'), i=True, type='FBX', options='fbx')
        self.assertEqual(len(cmds.ls(type='mesh', noIntermediate=True)), 6)
        self.assertFalse(cmds.ls('BG_*'))

    def make_cage(self, **kwargs):
        return self.cage.rebuild_chapter('asset', [self.low], {},
            settings={'fit_to_high': False, 'inflate_pct': 2}, **kwargs)

    def test_cage_cancel_and_failure_preserve_existing_uuid_and_sculpt(self):
        original = self.make_cage()[0]
        identifier = cmds.ls(original, uuid=True)[0]
        cmds.move(0.4, 0, 0, original + '.vtx[0]', relative=True)
        points = cmds.xform(original + '.vtx[*]', query=True, translation=True, worldSpace=True)
        with self.assertRaises(self.cancelled):
            self.make_cage(preserve_existing=False, cancelled_getter=lambda: True)
        with mock.patch.object(self.cage, '_inflate_and_fit', side_effect=RuntimeError('fit failed')):
            with self.assertRaises(RuntimeError):
                self.make_cage(preserve_existing=False)
        restored = cmds.ls(identifier, long=True)[0]
        self.assertEqual(cmds.xform(restored + '.vtx[*]', query=True, translation=True, worldSpace=True), points)
        self.assertEqual(cmds.ls('BakeMaster_Cage_Stage*'), [])
        self.assertEqual(self.make_cage(), [restored])

    def test_cage_successful_explicit_rebuild_and_incremental_undo(self):
        old = self.make_cage()[0]
        old_identifier = cmds.ls(old, uuid=True)[0]
        replacement = self.make_cage(preserve_existing=False)[0]
        self.assertNotEqual(cmds.ls(replacement, uuid=True)[0], old_identifier)
        points = cmds.xform(replacement + '.vtx[*]', query=True, translation=True, worldSpace=True)
        cmds.undoInfo(openChunk=True)
        try:
            self.cage.inflate_existing_cage(replacement, .1)
        finally:
            cmds.undoInfo(closeChunk=True)
        cmds.undo()
        self.assertEqual(cmds.xform(replacement + '.vtx[*]', query=True, translation=True, worldSpace=True), points)

    def test_cage_cancel_after_generation_retains_previous(self):
        original = self.make_cage()[0]
        identifier = cmds.ls(original, uuid=True)[0]
        cancelled = [False]
        def progress(*args): cancelled[0] = True
        with self.assertRaises(self.cancelled):
            self.make_cage(preserve_existing=False, progress_callback=progress,
                           cancelled_getter=lambda: cancelled[0])
        self.assertEqual(cmds.ls(identifier, long=True), [original])
        self.assertFalse(cmds.ls('BakeMaster_Cage_Stage*'))

    def test_cage_commit_failure_restores_original_chapter(self):
        original = self.make_cage()[0]
        identifier = cmds.ls(original, uuid=True)[0]
        parent = cmds.parent
        def failing_parent(node, destination=None, **kwargs):
            if 'BakeMaster_Cage_Stage' in node and destination == '|BakeMaster_Cage_BG':
                raise RuntimeError('commit parent failed')
            return parent(node, destination, **kwargs)
        with mock.patch.object(cmds, 'parent', side_effect=failing_parent):
            with self.assertRaises(RuntimeError):
                self.make_cage(preserve_existing=False)
        self.assertEqual(cmds.ls(identifier, long=True), [original])
        self.assertFalse(cmds.ls('BakeMaster_Cage_Stage*'))
        self.assertFalse(cmds.ls('BakeMaster_Cage_Backup*'))

    def test_cage_fbx_triangulates_copy_and_preserves_source(self):
        cage = self.make_cage()[0]
        path = Path(self.directory) / 'asset_Cage.fbx'
        self.exporter.export_meshes_fbx([cage], str(path), triangulate_all=True)
        self.assertEqual(cmds.polyEvaluate(cage, face=True), 6)
        self.assertEqual(cmds.polyEvaluate(cage, vertex=True), 8)
        cmds.file(new=True, force=True)
        cmds.file(str(path), i=True, type='FBX', options='fbx')
        shapes = cmds.ls(type='mesh', long=True, noIntermediate=True)
        self.assertEqual(len(shapes), 1)
        self.assertEqual(cmds.polyEvaluate(shapes[0], face=True), 12)
        self.assertEqual(cmds.polyEvaluate(shapes[0], vertex=True), 8)

    def test_cage_rebuild_undo_redo_preserves_generated_geometry(self):
        old = self.make_cage()[0]
        old_identifier = cmds.ls(old, uuid=True)[0]
        cmds.undoInfo(openChunk=True)
        try:
            new = self.make_cage(preserve_existing=False)[0]
        finally:
            cmds.undoInfo(closeChunk=True)
        new_identifier = cmds.ls(new, uuid=True)[0]
        expected = cmds.xform(new + '.vtx[*]', query=True, translation=True, worldSpace=True)
        cmds.undo()
        self.assertEqual(cmds.ls(old_identifier, long=True), [old])
        cmds.redo()
        restored = cmds.ls(new_identifier, long=True)[0]
        self.assertEqual(cmds.xform(restored + '.vtx[*]', query=True, translation=True, worldSpace=True), expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
