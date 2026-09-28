# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

import ast
import contextlib
import io
import json
import math
import os
import re
import threading
import traceback
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(ROOT, "src", "Bake_Groups")



def load_top_level(filename, names, namespace):
    path = os.path.join(RUNTIME, filename)
    with io.open(path, "r", encoding="utf-8-sig") as stream:
        tree = ast.parse(stream.read(), path)
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
    ]
    missing = set(names) - set(node.name for node in selected)
    if missing:
        raise RuntimeError("Missing nodes in {}: {}".format(filename, sorted(missing)))
    try:
        module = ast.Module(body=selected, type_ignores=[])
    except TypeError:  # Python 3.7
        module = ast.Module(body=selected)
    ast.fix_missing_locations(module)
    exec(compile(module, path, "exec"), namespace)
    return namespace


CORE_NAMESPACE = load_top_level("bg_core.py", {"MathUtils"}, {"math": math})
MathUtils = CORE_NAMESPACE["MathUtils"]


class DegenerateBoundingBoxTests(unittest.TestCase):
    @staticmethod
    def box(minimum, maximum):
        return {"min": list(minimum), "max": list(maximum)}

    def test_coincident_planes_overlap(self):
        first = self.box((0.0, 0.0, 0.0), (2.0, 2.0, 0.0))
        second = self.box((0.5, 0.5, 0.0), (1.5, 1.5, 0.0))
        self.assertTrue(MathUtils.is_overlapping(first, second, padding=1.0))

    def test_exact_boundary_contact_overlaps(self):
        first = self.box((0.0, 0.0, 0.0), (1.0, 1.0, 0.0))
        second = self.box((1.0, 0.25, 0.0), (2.0, 0.75, 0.0))
        self.assertTrue(MathUtils.is_overlapping(first, second, padding=1.0))

    def test_real_axis_gap_is_rejected_but_padding_is_preserved(self):
        first = self.box((0.0, 0.0, 0.0), (1.0, 1.0, 0.0))
        second = self.box((1.01, 0.0, 0.0), (2.01, 1.0, 0.0))
        self.assertFalse(MathUtils.is_overlapping(first, second, padding=1.0))
        self.assertTrue(MathUtils.is_overlapping(first, second, padding=1.02))

    def test_padding_gives_planes_a_scale_relative_normal_tolerance(self):
        first = self.box((0.0, 0.0, 0.0), (10.0, 10.0, 0.0))
        second = self.box((0.0, 0.0, 0.001), (10.0, 10.0, 0.001))
        self.assertFalse(MathUtils.is_overlapping(first, second, padding=1.0))
        self.assertTrue(MathUtils.is_overlapping(first, second, padding=1.05))


class NativeCoverageTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def calculate(source, target, threshold):
            self.calls.append((len(source), len(target), threshold))
            return 0.75, 0.125

        math_core = types.SimpleNamespace(calculate_coverage_stats=calculate)
        namespace = {
            "math": math,
            "HAS_MATH_CORE": True,
            "bg_math_core": math_core,
        }
        load_top_level(
            "bg_worker_hp.py",
            {"sample_flat_vertices", "calculate_surface_near_coverage"},
            namespace,
        )
        self.calculate = namespace["calculate_surface_near_coverage"]

    def test_native_coverage_kernel_is_used_with_bounded_samples(self):
        source = [float(value) for value in range(3000)]
        target = [float(value) for value in range(6000)]
        result = self.calculate(source, target, 0.25, max_source=40, max_target=80)
        self.assertEqual(result, (0.75, 0.125))
        self.assertEqual(len(self.calls), 1)
        self.assertLessEqual(self.calls[0][0], 40 * 3)
        self.assertLessEqual(self.calls[0][1], 80 * 3)

    def test_cancel_before_native_call_returns_immediately(self):
        result = self.calculate(
            [0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0],
            0.25,
            cancelled_getter=lambda: True,
        )
        self.assertEqual(result, (0.0, float("inf")))
        self.assertEqual(self.calls, [])


class DirectSurfaceProtectionTests(unittest.TestCase):
    def setUp(self):
        namespace = {
            "math": math,
            "calculate_surface_near_coverage": lambda *unused_args, **unused_kwargs: (0.0, float("inf")),
        }
        load_top_level(
            "bg_worker_hp.py",
            {"find_confident_direct_matches"},
            namespace,
        )
        self.find_matches = namespace["find_confident_direct_matches"]

    @staticmethod
    def _info(center):
        x, y, z = center
        return {
            "diag": 2.0,
            "center": [x, y, z],
            "bbox": [x - 0.5, y - 0.5, z - 0.5, x + 0.5, y + 0.5, z + 0.5],
        }

    @staticmethod
    def _coverage(mapping):
        def calculate(source, target, unused_threshold, **unused_kwargs):
            return mapping.get((int(source[0]), int(target[0])), (0.0, float("inf")))
        return calculate

    def test_only_clear_conflict_winner_is_protected(self):
        hp_data = {"hp_a": self._info((0, 0, 0)), "hp_b": self._info((0, 0, 0))}
        lp_data = {"lp": self._info((0, 0, 0))}
        mapping = {
            (1, 11): (0.97, 0.005), (11, 1): (0.94, 0.006),
            (2, 11): (0.20, 0.050), (11, 2): (0.20, 0.050),
        }
        result = self.find_matches(
            {"hp_a": ["lp"], "hp_b": ["lp"]},
            hp_data, lp_data,
            {"hp_a": [1.0, 0, 0], "hp_b": [2.0, 0, 0]},
            {"lp": [11.0, 0, 0]},
            coverage_getter=self._coverage(mapping),
        )
        self.assertEqual(set(result), {"hp_a"})
        self.assertEqual(result["hp_a"]["lp"], "lp")

    def test_non_conflicting_pair_is_left_to_normal_resolver(self):
        mapping = {
            (1, 11): (0.99, 0.001), (11, 1): (0.99, 0.001),
        }
        result = self.find_matches(
            {"hp": ["lp"]},
            {"hp": self._info((0, 0, 0))},
            {"lp": self._info((0, 0, 0))},
            {"hp": [1.0, 0, 0]},
            {"lp": [11.0, 0, 0]},
            coverage_getter=self._coverage(mapping),
        )
        self.assertEqual(result, {})

    def test_ambiguous_hp_is_not_protected(self):
        mapping = {
            (1, 11): (0.96, 0.005), (11, 1): (0.94, 0.006),
            (1, 12): (0.95, 0.005), (12, 1): (0.93, 0.006),
        }
        result = self.find_matches(
            {"hp": ["lp_a", "lp_b"]},
            {"hp": self._info((0, 0, 0))},
            {"lp_a": self._info((0, 0, 0)), "lp_b": self._info((0, 0, 0))},
            {"hp": [1.0, 0, 0]},
            {"lp_a": [11.0, 0, 0], "lp_b": [12.0, 0, 0]},
            coverage_getter=self._coverage(mapping),
        )
        self.assertEqual(result, {})

    def test_two_hps_competing_for_one_lp_are_not_protected(self):
        mapping = {
            (1, 11): (0.96, 0.005), (11, 1): (0.94, 0.006),
            (2, 11): (0.96, 0.005), (11, 2): (0.94, 0.006),
        }
        result = self.find_matches(
            {"hp_a": ["lp"], "hp_b": ["lp"]},
            {"hp_a": self._info((0, 0, 0)), "hp_b": self._info((0, 0, 0))},
            {"lp": self._info((0, 0, 0))},
            {"hp_a": [1.0, 0, 0], "hp_b": [2.0, 0, 0]},
            {"lp": [11.0, 0, 0]},
            coverage_getter=self._coverage(mapping),
        )
        self.assertEqual(result, {})


class _DummySignal(object):
    def emit(self, *unused_args):
        return None


class _DummyThread(object):
    def __init__(self, *unused_args, **unused_kwargs):
        self._interrupted = False

    def isInterruptionRequested(self):
        return self._interrupted

    def requestInterruption(self):
        self._interrupted = True


class StableLPMatchingTests(unittest.TestCase):
    def test_equal_candidates_choose_stable_group_name(self):
        qt_core = types.SimpleNamespace(
            QThread=_DummyThread,
            Signal=lambda *unused_args: _DummySignal(),
        )
        namespace = {
            "QtCore": qt_core,
            "threading": threading,
            "traceback": traceback,
            "bg_core": types.SimpleNamespace(MathUtils=MathUtils),

            "bg_math_core": types.SimpleNamespace(
                calculate_avg_distance=lambda unused_source, unused_target: 0.25
            ),
            "HAS_MATH_CORE": True,
        }
        load_top_level("bg_worker_lp.py", {"LPMatchingWorker"}, namespace)
        worker_class = namespace["LPMatchingWorker"]

        low = {
            "min": [0.0, 0.0, 0.0], "max": [1.0, 1.0, 0.0],
            "diag": 1.5, "vtx": 10, "edges": 20,
        }
        high = {
            "min": [0.0, 0.0, 0.0], "max": [1.0, 1.0, 0.0],
            "diag": 1.5, "vtx": 11, "edges": 21,
        }
        worker = worker_class(
            {"z_group": ["hp_z"], "a_group": ["hp_a"]},
            {"hp_z": dict(high), "hp_a": dict(high)},
            {"lp": low},
            {"hp_z": [0.0, 0.0, 0.0], "hp_a": [0.0, 0.0, 0.0]},
            {"lp": [0.0, 0.0, 0.0]},
            {"lp": [0.0, 0.0, 0.0]},
            1.0,
        )
        self.assertEqual(
            worker.get_best_match(low, [0.0, 0.0, 0.0]),
            "a_group",
        )

    def test_hp_native_index_is_built_once_and_reused_for_multiple_lps(self):
        qt_core = types.SimpleNamespace(
            QThread=_DummyThread,
            Signal=lambda *unused_args: _DummySignal(),
        )
        index_builds = []
        indexed_queries = []
        legacy_queries = []

        class FakePointCloudIndex(object):
            def __init__(self, target_vertices):
                index_builds.append(tuple(target_vertices))

            def average_distance(self, source_vertices):
                indexed_queries.append(tuple(source_vertices))
                return 0.1

        math_core = types.SimpleNamespace(
            PointCloudIndex=FakePointCloudIndex,
            calculate_avg_distance=lambda source, target: legacy_queries.append(
                (source, target)
            ),
        )
        namespace = {
            "QtCore": qt_core,
            "threading": threading,
            "traceback": traceback,
            "bg_core": types.SimpleNamespace(MathUtils=MathUtils),

            "bg_math_core": math_core,
            "HAS_MATH_CORE": True,
        }
        load_top_level("bg_worker_lp.py", {"LPMatchingWorker"}, namespace)
        worker_class = namespace["LPMatchingWorker"]

        low = {
            "min": [0.0, 0.0, 0.0], "max": [1.0, 1.0, 1.0],
            "diag": 2.0, "vtx": 10, "edges": 20,
        }
        high = {
            "min": [0.0, 0.0, 0.0], "max": [1.0, 1.0, 1.0],
            "diag": 2.0, "vtx": 11, "edges": 21,
        }
        worker = worker_class(
            {"group": ["hp"]},
            {"hp": high},
            {"lp_a": low, "lp_b": low},
            {"hp": [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]},
            {"lp_a": [0.0, 0.0, 0.0], "lp_b": [0.1, 0.0, 0.0]},
            {"lp_a": [0.0, 0.0, 0.0], "lp_b": [0.1, 0.0, 0.0]},
            1.0,
        )

        self.assertEqual(worker.get_best_match(low, [0.0, 0.0, 0.0]), "group")
        self.assertEqual(worker.get_best_match(low, [0.1, 0.0, 0.0]), "group")
        self.assertEqual(len(index_builds), 1)
        self.assertEqual(len(indexed_queries), 2)
        self.assertEqual(legacy_queries, [])


class _FakeCommands(object):
    def __init__(self):
        self.select_calls = []
        self.refresh_calls = []
        self.poly_evaluate_calls = 0

    def pluginInfo(self, *unused_args, **unused_kwargs):
        return True

    def objExists(self, unused_node):
        return False

    def listRelatives(self, node, **kwargs):
        if kwargs.get("children"):
            return ["|LP|asset_low"]
        if kwargs.get("shapes"):
            return [node + "|shape"]
        return []

    def getAttr(self, unused_attribute):
        return False

    def ls(self, *args, **kwargs):
        if kwargs.get("selection"):
            return ["|original_selection"]
        return list(args)

    def refresh(self, **kwargs):
        if kwargs.get("query"):
            return False
        self.refresh_calls.append(kwargs.get("suspend"))
        return None

    def select(self, *args, **kwargs):
        self.select_calls.append((args, kwargs))

    def warning(self, unused_message):
        return None

    def polyEvaluate(self, *unused_args, **unused_kwargs):
        self.poly_evaluate_calls += 1
        raise AssertionError("Single-material API path must not query or expand face ranges")

    def listConnections(self, *unused_args, **unused_kwargs):
        raise AssertionError("API material lookup unexpectedly fell back to cmds")


class _ProgressDialog(object):
    instances = []

    def __init__(self, *unused_args, **unused_kwargs):
        self.closed = False
        self.__class__.instances.append(self)

    def setWindowModality(self, unused_value):
        return None

    def show(self):
        return None

    def wasCanceled(self):
        return True

    def setLabelText(self, unused_text):
        return None

    def setValue(self, unused_value):
        return None

    def close(self):
        self.closed = True


def load_export_processor(cmds, om_module):
    qt_widgets = types.SimpleNamespace(
        QProgressDialog=_ProgressDialog,
        QApplication=types.SimpleNamespace(processEvents=lambda: None),
    )
    namespace = {
        "cmds": cmds,
        "mel": types.SimpleNamespace(eval=lambda unused_command: None),
        "om": om_module,
        "bg_core": types.SimpleNamespace(),
        "bg_l10n": types.SimpleNamespace(text=lambda value: value),
        "QtWidgets": qt_widgets,
        "QtCore": types.SimpleNamespace(Qt=types.SimpleNamespace(WindowModal=1)),
        "contextlib": contextlib,
        "re": re,
        "math": math,
    }
    load_top_level("bg_final_export.py", {"FinalExportProcessor"}, namespace)
    return namespace["FinalExportProcessor"]


class ExportRegressionTests(unittest.TestCase):
    def test_smooth_state_supports_legacy_prefix_and_case_variants(self):
        processor = load_export_processor(_FakeCommands(), types.SimpleNamespace())
        states = {
            "  PREFIX:chapter_panel  ": 2,
            "unused": 3,
        }
        self.assertEqual(
            processor._smooth_level_from_states(
                states, "Chapter", "CHAPTER_PANEL", default_level=0
            ),
            2,
        )

    def test_unknown_or_invalid_smooth_state_falls_back_to_zero(self):
        processor = load_export_processor(_FakeCommands(), types.SimpleNamespace())
        self.assertEqual(
            processor._smooth_level_from_states(
                {"other": 2}, "Chapter", "chapter_panel", default_level=0
            ),
            0,
        )
        self.assertEqual(
            processor._smooth_level_from_states(
                {"panel": "invalid"}, "Chapter", "chapter_panel", default_level=0
            ),
            0,
        )

    def test_cancel_aborts_before_fallback_and_export_and_restores_selection(self):
        commands = _FakeCommands()
        commands.objExists = lambda node: node == "|original_selection"
        processor = load_export_processor(commands, types.SimpleNamespace())
        cleanup_calls = []
        export_calls = []
        processor._cleanup_zero_transform_hp_export_temps = staticmethod(
            lambda *args: cleanup_calls.append(args)
        )
        processor.get_valid_mesh_transforms = staticmethod(
            lambda unused_root: ["|HP|asset_high"]
        )
        processor.export_selected_fbx = staticmethod(
            lambda path: export_calls.append(path)
        )

        result = processor.export_chapter(
            "chapter", "|HP", "|LP", None,
            mode="both", export_dir="C:/exports",
            prepared_snapshot={'hp_all': ['|HP|asset_high'], 'lp_all': ['|LP|asset_low'],
                               'prefix_items': [{'prefix': 'asset', 'smooth_level': 0}],
                               'hp_by_prefix': {'asset': ['|HP|asset_high']},
                               'lp_by_prefix': {'asset': ['|LP|asset_low']}},
        )
        self.assertFalse(result)
        self.assertEqual(export_calls, [])
        self.assertTrue(cleanup_calls)
        refresh_states = [value for value in commands.refresh_calls if value is not None]
        self.assertTrue(refresh_states[0])
        self.assertFalse(refresh_states[-1])
        self.assertEqual(commands.select_calls[-1][0], (["|original_selection"],))
        self.assertTrue(_ProgressDialog.instances[-1].closed)

    def test_single_material_uses_constant_size_sentinel(self):
        commands = _FakeCommands()

        class DagPath(object):
            def instanceNumber(self):
                return 0

        class SelectionList(object):
            def add(self, unused_shape):
                return None

            def getDagPath(self, unused_index):
                return DagPath()

        class MeshFunction(object):
            numPolygons = 10000

            def __init__(self, unused_dag_path):
                return None

            def getConnectedShaders(self, unused_instance):
                return [object()], (0 for unused_index in range(self.numPolygons))

        om_module = types.SimpleNamespace(
            MSelectionList=SelectionList,
            MFnMesh=MeshFunction,
            MFnDependencyNode=lambda unused_object: types.SimpleNamespace(
                name=lambda: "onlySG"
            ),
        )
        processor = load_export_processor(commands, om_module)
        result = processor._get_mesh_materials_and_faces("|LP|asset_low")
        self.assertEqual(set(result.keys()), {"onlySG"})
        self.assertIs(result["onlySG"], processor._ALL_FACES)
        self.assertEqual(commands.poly_evaluate_calls, 0)

    def test_multimaterial_failure_cleans_every_created_duplicate(self):
        class FailingCommands(object):
            def __init__(self):
                self.duplicate_count = 0

            def duplicate(self, node, **unused_kwargs):
                self.duplicate_count += 1
                if self.duplicate_count == 3:
                    raise RuntimeError("simulated duplicate failure")
                return ["{}_copy{}".format(node, self.duplicate_count)]

            def rename(self, unused_node, new_name):
                return "|" + new_name

            def ls(self, *args, **kwargs):
                if kwargs.get("flatten"):
                    return []
                return list(args)

            def polyEvaluate(self, *unused_args, **unused_kwargs):
                return 2

            def objExists(self, unused_node):
                return True

            def delete(self, *unused_args, **unused_kwargs):
                return None

        commands = FailingCommands()
        processor = load_export_processor(commands, types.SimpleNamespace())
        cleaned = []
        processor._cleanup_zero_transform_hp_export_temps = staticmethod(
            lambda nodes=None: cleaned.extend(list(nodes or []))
        )

        with self.assertRaisesRegex(RuntimeError, "simulated duplicate failure"):
            processor._process_multimaterial_mesh(
                "|LP|asset_low",
                ["|HP|asset_high"],
                {"material_a": processor._ALL_FACES,
                 "material_b": processor._ALL_FACES},
            )

        self.assertEqual(
            cleaned,
            ["|asset_high_mat1", "|asset_low_mat1"],
        )

    def test_source_has_one_global_compound_sweep_and_no_full_face_range_list(self):
        hp_path = os.path.join(RUNTIME, "bg_worker_hp.py")
        export_path = os.path.join(RUNTIME, "bg_final_export.py")
        with io.open(hp_path, "r", encoding="utf-8-sig") as stream:
            hp_tree = ast.parse(stream.read(), hp_path)
        run_impl = next(
            node for node in ast.walk(hp_tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_run_impl"
        )
        sweep_calls = [
            node for node in ast.walk(run_impl)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "bbox_sweep_candidate_pairs"
        ]
        self.assertEqual(len(sweep_calls), 1)
        self.assertTrue(any(
            isinstance(node, ast.Attribute) and node.attr == "isdisjoint"
            for node in ast.walk(run_impl)
        ))

        with io.open(export_path, "r", encoding="utf-8-sig") as stream:
            export_tree = ast.parse(stream.read(), export_path)
        full_range_lists = []
        for node in ast.walk(export_tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "list" or len(node.args) != 1:
                continue
            argument = node.args[0]
            if (isinstance(argument, ast.Call) and isinstance(argument.func, ast.Name)
                    and argument.func.id == "range"):
                full_range_lists.append(node)
        self.assertEqual(full_range_lists, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
