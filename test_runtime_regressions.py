# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

import ast
import copy
import heapq
import importlib.util
import io
import json
import math
import os
import random
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager

try:
    from unittest import mock
except ImportError:  # pragma: no cover - Maya 2022 still provides Python 3.7.
    import mock


WORK_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.join(WORK_DIR, "plugin", "Bake_Groups")
if not os.path.isdir(PLUGIN_ROOT):
    PLUGIN_ROOT = os.path.join(WORK_DIR, "Bake-Groups-Tool", "plugin", "Bake_Groups")


def runtime_under_test():
    manifest_path = os.path.join(PLUGIN_ROOT, "active_version.json")
    with io.open(manifest_path, "r", encoding="utf-8-sig") as stream:
        manifest = json.load(stream)
    active_version = str(manifest.get("active_version") or "").strip()
    if not active_version:
        raise RuntimeError("active_version.json does not name an active runtime")

    versions_root = os.path.realpath(os.path.join(PLUGIN_ROOT, "versions"))
    candidate = os.path.realpath(os.path.join(versions_root, active_version))
    try:
        is_inside = os.path.commonpath((versions_root, candidate)) == versions_root
    except ValueError:
        is_inside = False
    if not is_inside or not os.path.isdir(candidate):
        raise RuntimeError("Active runtime is missing or outside the versions directory")
    return candidate


RUNTIME_DIR = runtime_under_test()


@contextmanager
def temporary_modules(replacements):
    missing = object()
    previous = {}
    for name, module in replacements.items():
        previous[name] = sys.modules.get(name, missing)
        sys.modules[name] = module
    try:
        yield
    finally:
        for name, old_module in previous.items():
            if old_module is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module


def load_module(module_name, filename, replacements):
    path = os.path.join(RUNTIME_DIR, filename)
    with temporary_modules(replacements):
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def extract_function(filename, function_name, namespace):
    path = os.path.join(RUNTIME_DIR, filename)
    with io.open(path, "r", encoding="utf-8-sig") as stream:
        tree = ast.parse(stream.read(), path)
    matches = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "Expected one {} in {}, found {}".format(
                function_name, filename, len(matches)
            )
        )
    node = copy.deepcopy(matches[0])
    module_tree = ast.Module(body=[node])
    ast.fix_missing_locations(module_tree)
    exec(compile(module_tree, path, "exec"), namespace)
    return namespace[function_name]


class _Point(object):
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)

    def __sub__(self, other):
        return _Vector(self.x - other.x, self.y - other.y, self.z - other.z)


class _Vector(object):
    def __init__(self, x=0.0, y=0.0, z=0.0):
        if isinstance(x, _Vector):
            self.x, self.y, self.z = x.x, x.y, x.z
        else:
            self.x = float(x)
            self.y = float(y)
            self.z = float(z)

    def length(self):
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)


class _LazyPointArray(object):
    def __init__(self, count):
        self.count = int(count)
        self.accessed_indices = []

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if index < 0 or index >= self.count:
            raise IndexError(index)
        self.accessed_indices.append(index)
        return _Point(index, index % 97, -(index % 31))


def make_geo_maya_modules(state):
    class DagPath(object):
        def extendToShape(self):
            return None

    class SelectionList(object):
        def add(self, unused_path):
            return None

        def getDagPath(self, unused_index):
            return DagPath()

    class MeshFunction(object):
        def __init__(self, unused_dag_path):
            self.numVertices = len(state["points"])

        def getPoints(self, unused_space):
            state["get_points_calls"] = state.get("get_points_calls", 0) + 1
            return state["points"]

        def getPoint(self, point_index, unused_space):
            return state["points"][point_index]

    maya_module = types.ModuleType("maya")
    api_module = types.ModuleType("maya.api")
    om_module = types.ModuleType("maya.api.OpenMaya")
    cmds_module = types.ModuleType("maya.cmds")
    om_module.MSelectionList = SelectionList
    om_module.MFnMesh = MeshFunction
    om_module.MSpace = type("MSpace", (), {"kWorld": 1})
    maya_module.cmds = cmds_module
    maya_module.api = api_module
    api_module.OpenMaya = om_module
    return {
        "maya": maya_module,
        "maya.cmds": cmds_module,
        "maya.api": api_module,
        "maya.api.OpenMaya": om_module,
    }


class _BoundSignal(object):
    def __init__(self):
        self.emissions = []

    def emit(self, *args):
        self.emissions.append(args)

    def connect(self, unused_callback):
        return None


class _Signal(object):
    def __init__(self, *unused_types):
        self.storage_name = None

    def __set_name__(self, unused_owner, name):
        self.storage_name = "_test_signal_{}".format(name)

    def __get__(self, instance, unused_owner):
        if instance is None:
            return self
        signal = instance.__dict__.get(self.storage_name)
        if signal is None:
            signal = _BoundSignal()
            instance.__dict__[self.storage_name] = signal
        return signal


class _QThread(object):
    def __init__(self, *unused_args, **unused_kwargs):
        self._test_interrupted = False
        self.wait_calls = 0

    def requestInterruption(self):
        self._test_interrupted = True

    def isInterruptionRequested(self):
        return self._test_interrupted

    def wait(self, *unused_args, **unused_kwargs):
        self.wait_calls += 1
        raise AssertionError("Worker.stop() must never wait for its own thread")


def load_runtime_modules():
    geo_state = {"points": _LazyPointArray(0)}
    maya_modules = make_geo_maya_modules(geo_state)
    core_module = load_module(
        "bg_core_runtime_regressions",
        "bg_core.py",
        maya_modules,
    )

    qt_core = types.ModuleType("PySide6.QtCore")
    qt_core.Signal = _Signal
    qt_core.QThread = _QThread
    pyside = types.ModuleType("PySide6")
    pyside.QtCore = qt_core

    license_module = types.ModuleType("bg_license")
    license_module.require_authorized_action = lambda: None
    math_core_module = types.ModuleType("bg_math_core")
    math_core_module.calculate_avg_distance = lambda unused_a, unused_b: 0.0
    worker_replacements = {
        "bg_core": core_module,
        "bg_license": license_module,
        "bg_math_core": math_core_module,
        "PySide6": pyside,
        "PySide6.QtCore": qt_core,
    }
    hp_module = load_module(
        "bg_worker_hp_runtime_regressions",
        "bg_worker_hp.py",
        worker_replacements,
    )
    lp_module = load_module(
        "bg_worker_lp_runtime_regressions",
        "bg_worker_lp.py",
        worker_replacements,
    )
    return geo_state, core_module, hp_module, lp_module


GEO_STATE, BG_CORE, BG_WORKER_HP, BG_WORKER_LP = load_runtime_modules()


class GeoMatcherSamplingTests(unittest.TestCase):
    def test_million_point_mesh_obeys_max_points_deterministically(self):
        GEO_STATE["get_points_calls"] = 0
        first_points = _LazyPointArray(1000003)
        GEO_STATE["points"] = first_points
        first = BG_CORE.GeoMatcher.get_world_vertices(
            "million_point_mesh", density_pct=100.0, max_points=257
        )

        second_points = _LazyPointArray(1000003)
        GEO_STATE["points"] = second_points
        second = BG_CORE.GeoMatcher.get_world_vertices(
            "million_point_mesh", density_pct=100.0, max_points=257
        )

        self.assertEqual(len(first), 257 * 3)
        self.assertEqual(first, second)
        self.assertEqual(first_points.accessed_indices, second_points.accessed_indices)
        self.assertEqual(len(first_points.accessed_indices), 257)
        self.assertEqual(len(set(first_points.accessed_indices)), 257)
        self.assertEqual(GEO_STATE["get_points_calls"], 0)


class AugmentedSamplingTests(unittest.TestCase):
    def test_transform_extends_to_shape_and_total_budget_is_strict(self):
        state = {"dag_paths": [], "mesh_functions": []}

        class DagPath(object):
            def __init__(self):
                self.is_shape = False
                self.extend_calls = 0

            def hasFn(self, function_type):
                return function_type == 10 and not self.is_shape

            def extendToShape(self):
                self.extend_calls += 1
                self.is_shape = True

        class SelectionList(object):
            def add(self, unused_path):
                return None

            def getDagPath(self, unused_index):
                dag_path = DagPath()
                state["dag_paths"].append(dag_path)
                return dag_path

        class MeshFunction(object):
            def __init__(self, dag_path):
                if not dag_path.is_shape:
                    raise AssertionError("Transform DAG path was not extended to shape")
                self.numVertices = 1000003
                self.numEdges = 1000002
                self.point_requests = []
                state["mesh_functions"].append(self)

            def getPoint(self, vertex_id, unused_space):
                self.point_requests.append(vertex_id)
                return _Point(vertex_id * 10.0, 0.0, 0.0)

            def getEdgeVertices(self, edge_id):
                return edge_id, edge_id + 1

        om_module = types.SimpleNamespace(
            MSelectionList=SelectionList,
            MFnMesh=MeshFunction,
            MFn=types.SimpleNamespace(kTransform=10),
            MSpace=types.SimpleNamespace(kWorld=20),
            MVector=lambda value: _Vector(value),
            MPoint=_Point,
        )
        namespace = {
            "om": om_module,
            "get_flat_verts_safe": lambda unused_path, unused_density: [],
        }
        build_cache = extract_function(
            "bg_mixins.py", "build_augmented_vertex_cache", namespace
        )

        first = build_cache(
            "large_transform", threshold=1.0, spacing=1.0,
            max_virtual_verts=8, max_total_samples=100
        )
        second = build_cache(
            "large_transform", threshold=1.0, spacing=1.0,
            max_virtual_verts=8, max_total_samples=100
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 100 * 3)
        self.assertTrue(state["dag_paths"])
        self.assertTrue(all(dag.extend_calls == 1 for dag in state["dag_paths"]))
        self.assertTrue(all(len(mesh.point_requests) < 200 for mesh in state["mesh_functions"]))


class BoundedGridTests(unittest.TestCase):
    def test_huge_bbox_with_100_cell_budget_includes_boundary_corners(self):
        cells = BG_WORKER_HP.bounded_grid_cells(
            [-1.0e12, -1.0e12, -1.0e12, 1.0e12, 1.0e12, 1.0e12],
            0.001,
            max_cells=100,
        )
        low = int(math.floor(-1.0e12 / 0.001))
        high = int(math.floor(1.0e12 / 0.001))
        self.assertLessEqual(len(cells), 100)
        self.assertEqual(len(cells), len(set(cells)))
        self.assertIn((low, low, low), cells)
        self.assertIn((high, high, high), cells)

    def test_long_thin_bbox_with_4096_budget_includes_both_ends(self):
        cells = BG_WORKER_HP.bounded_grid_cells(
            [0.0, 2.0, 3.0, 1.0e12, 2.0, 3.0],
            0.5,
            max_cells=4096,
        )
        self.assertLessEqual(len(cells), 4096)
        self.assertEqual(len(cells), len(set(cells)))
        self.assertIn((0, 4, 6), cells)
        self.assertIn((2000000000000, 4, 6), cells)


class BroadPhaseTests(unittest.TestCase):
    def test_sweep_keeps_every_bbox_pair_within_pair_tolerance(self):
        randomizer = random.Random(271828)
        names = ["hp_{:03d}".format(index) for index in range(180)]
        bboxes = {}
        paddings = {}
        for name in names:
            center = [randomizer.uniform(-40.0, 40.0) for unused_axis in range(3)]
            half = [randomizer.uniform(0.02, 2.0) for unused_axis in range(3)]
            bboxes[name] = [
                center[0] - half[0], center[1] - half[1], center[2] - half[2],
                center[0] + half[0], center[1] + half[1], center[2] + half[2],
            ]
            paddings[name] = randomizer.uniform(0.001, 1.0)

        swept = set(BG_WORKER_HP.bbox_sweep_candidate_pairs(names, bboxes, paddings))
        required = set()
        for first_index, first_name in enumerate(names):
            for second_name in names[first_index + 1:]:
                tolerance = min(paddings[first_name], paddings[second_name])
                if BG_WORKER_HP.bboxes_within_tolerance(
                        bboxes[first_name], bboxes[second_name], tolerance):
                    required.add(tuple(sorted((first_name, second_name))))
        self.assertTrue(required.issubset(swept))

    def test_sweep_keeps_exact_tolerance_boundary(self):
        names = ["left", "right"]
        bboxes = {
            "left": [0.0, 0.0, 0.0, 1.0, 1.0, 1.0],
            "right": [1.25, 0.0, 0.0, 2.0, 1.0, 1.0],
        }
        paddings = {"left": 0.25, "right": 0.25}
        pairs = set(BG_WORKER_HP.bbox_sweep_candidate_pairs(names, bboxes, paddings))
        self.assertIn(("left", "right"), pairs)


def queue_evaluation_count(group_count):
    active = ["group_{:03d}".format(index) for index in range(group_count)]
    order = dict((name, index) for index, name in enumerate(active))
    revisions = dict((name, 0) for name in active)
    sizes = dict((name, 1 + (index % 5)) for index, name in enumerate(active))
    centers = dict((name, float(index * 3)) for index, name in enumerate(active))

    def candidate(source, target):
        if source == target:
            return None
        distance = abs(centers[source] - centers[target])
        return (sizes[target] * 7.0) - distance, "test", 1.0, distance

    queue = BG_WORKER_HP.RevisionedCandidateQueue(
        candidate_getter=candidate,
        revision_getter=lambda name: revisions.get(name),
        source_key_getter=lambda name: (sizes[name], name),
        order_by_name=order,
    )
    if not queue.seed(active):
        raise AssertionError("Queue seed unexpectedly cancelled")

    while len(active) > 1:
        best = queue.pop_best()
        if best is None:
            raise AssertionError("Queue ran out of candidates")
        unused_score, source, target = best[:3]
        source_size = sizes[source]
        target_size = sizes[target]
        centers[target] = (
            centers[target] * target_size + centers[source] * source_size
        ) / float(target_size + source_size)
        sizes[target] += sizes[source]
        del sizes[source]
        del centers[source]
        del revisions[source]
        active.remove(source)
        revisions[target] += 1
        if not queue.refresh(target, active):
            raise AssertionError("Queue refresh unexpectedly cancelled")
    return queue.evaluation_count


class RevisionedQueueTests(unittest.TestCase):
    def test_candidate_evaluation_growth_is_clearly_below_cubic(self):
        evaluations_32 = queue_evaluation_count(32)
        evaluations_64 = queue_evaluation_count(64)
        self.assertLess(evaluations_32, 2 * 32 * 32)
        self.assertLess(evaluations_64, 2 * 64 * 64)
        self.assertLess(evaluations_64 / float(evaluations_32), 4.5)


class _FatalWorkerError(BaseException):
    pass


def raise_worker_error(error):
    raise error


class WorkerTerminalTests(unittest.TestCase):
    def make_hp_worker(self):
        return BG_WORKER_HP.HPGroupingWorker(
            {}, {}, {}, {}, {}, threshold_pct=15.0, group_limit=8
        )

    def make_lp_worker(self):
        return BG_WORKER_LP.LPMatchingWorker(
            {}, {}, {}, {}, {}, {}, lp_threshold_coef=0.1
        )

    def assert_terminal(self, worker, expected):
        counts = {
            "result": len(worker.result_ready.emissions),
            "failed": len(worker.failed.emissions),
            "cancelled": len(worker.cancelled.emissions),
        }
        self.assertEqual(sum(counts.values()), 1, counts)
        self.assertEqual(counts[expected], 1, counts)

    def test_hp_success_emits_only_result(self):
        worker = self.make_hp_worker()
        worker._run_impl = lambda: ({"group": []}, ["ok"])
        worker.run()
        self.assert_terminal(worker, "result")

    def test_hp_exception_emits_only_failure(self):
        worker = self.make_hp_worker()
        worker._run_impl = lambda: raise_worker_error(RuntimeError("hp exception"))
        worker.run()
        self.assert_terminal(worker, "failed")
        self.assertIn("hp exception", worker.failed.emissions[0][0])

    def test_hp_base_exception_emits_only_failure(self):
        worker = self.make_hp_worker()
        worker._run_impl = lambda: raise_worker_error(_FatalWorkerError("hp fatal"))
        worker.run()
        self.assert_terminal(worker, "failed")
        self.assertIn("hp fatal", worker.failed.emissions[0][0])

    def test_hp_cancel_emits_only_cancelled_and_stop_never_waits(self):
        worker = self.make_hp_worker()
        worker._run_impl = lambda: ({"group": []}, ["unused"])
        worker.stop()
        self.assertEqual(worker.wait_calls, 0)
        self.assertTrue(worker.isInterruptionRequested())
        worker.run()
        self.assert_terminal(worker, "cancelled")

    def test_lp_success_emits_only_result(self):
        worker = self.make_lp_worker()
        worker._run_impl = lambda: {"group": set()}
        worker.run()
        self.assert_terminal(worker, "result")

    def test_lp_exception_emits_only_failure(self):
        worker = self.make_lp_worker()
        worker._run_impl = lambda: raise_worker_error(RuntimeError("lp exception"))
        worker.run()
        self.assert_terminal(worker, "failed")
        self.assertIn("lp exception", worker.failed.emissions[0][0])

    def test_lp_base_exception_emits_only_failure(self):
        worker = self.make_lp_worker()
        worker._run_impl = lambda: raise_worker_error(_FatalWorkerError("lp fatal"))
        worker.run()
        self.assert_terminal(worker, "failed")
        self.assertIn("lp fatal", worker.failed.emissions[0][0])

    def test_lp_cancel_emits_only_cancelled_and_stop_never_waits(self):
        worker = self.make_lp_worker()
        worker._run_impl = lambda: {"group": set()}
        worker.stop()
        self.assertEqual(worker.wait_calls, 0)
        self.assertTrue(worker.isInterruptionRequested())
        worker.run()
        self.assert_terminal(worker, "cancelled")


class BootstrapLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="bg-bootstrap-regression-")
        self.runtime_dir = os.path.join(self.temp_dir, "versions", "9.9.9")
        os.makedirs(self.runtime_dir)
        self.source_path = os.path.join(self.runtime_dir, "launcher.py")
        self.target_path = os.path.join(self.temp_dir, "launcher.py")
        with open(self.source_path, "wb") as stream:
            stream.write(b"new launcher\n")
        with open(self.target_path, "wb") as stream:
            stream.write(b"old launcher\n")
        self.bootstrap = extract_function(
            "bg_main_window.py",
            "_bootstrap_root_launcher",
            {"os": os, "uuid": __import__("uuid")},
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_target_outside_owned_root_is_rejected(self):
        outside_target = os.path.join(os.path.dirname(self.temp_dir), "outside.py")
        self.assertFalse(self.bootstrap(self.runtime_dir, outside_target))
        self.assertFalse(os.path.exists(outside_target))
        with open(self.target_path, "rb") as stream:
            self.assertEqual(stream.read(), b"old launcher\n")

    def test_sync_keeps_old_target_until_atomic_replace(self):
        real_replace = os.replace
        observations = []

        def observed_replace(source, target):
            with open(source, "rb") as stream:
                temporary_bytes = stream.read()
            with open(target, "rb") as stream:
                target_bytes = stream.read()
            observations.append((temporary_bytes, target_bytes))
            return real_replace(source, target)

        with mock.patch.object(os, "replace", side_effect=observed_replace):
            self.assertTrue(self.bootstrap(self.runtime_dir, self.target_path))

        self.assertEqual(observations, [(b"new launcher\n", b"old launcher\n")])
        with open(self.target_path, "rb") as stream:
            self.assertEqual(stream.read(), b"new launcher\n")
        leftovers = [name for name in os.listdir(self.temp_dir) if ".tmp.bootstrap." in name]
        self.assertEqual(leftovers, [])
        self.assertFalse(self.bootstrap(self.runtime_dir, self.target_path))


if __name__ == "__main__":
    print("Bake Master runtime under test: {}".format(RUNTIME_DIR))
    unittest.main(verbosity=2)
