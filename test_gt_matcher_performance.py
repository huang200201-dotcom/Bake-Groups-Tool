from __future__ import absolute_import, division, print_function

import ast
import math
import os
import unittest


RUNTIME = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "plugin",
    "Bake_Groups",
    "versions",
    "1.3.15",
)


def load_method(name, extra_namespace=None):
    path = os.path.join(RUNTIME, "bg_gt_matcher.py")
    with open(path, "r", encoding="utf-8") as stream:
        tree = ast.parse(stream.read(), filename=path)
    method = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            method = node
            break
    if method is None:
        raise AssertionError("Method not found: %s" % name)
    method.decorator_list = []
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"bg_core": None, "cmds": None, "math": math}
    namespace.update(extra_namespace or {})
    exec(compile(module, path, "exec"), namespace)
    return namespace[name]


BUILD_LP_DATA = load_method("_build_lp_data_list")
BBOX_OVERLAP = load_method("_bbox_overlap_ratio")


class FakeMatcher(object):
    def __init__(self, mode):
        self.mode = mode
        self.shell_calls = 0
        self.intersector_calls = 0

    def current_match_mode(self):
        return self.mode

    def get_lp_shell_bbox_data(self, node):
        self.shell_calls += 1
        return [{
            "node": node + "::shell_000",
            "real_node": node,
            "is_virtual_shell": True,
            "sample_points": [object()],
        }]

    def _mesh_intersector(self, node):
        self.intersector_calls += 1
        return "surface:" + node


class FakePoint(object):
    def __init__(self, x, y, z):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)


class FakeOpenMaya(object):
    polygons = [
        [FakePoint(0, 0, 0), FakePoint(1, 0, 0), FakePoint(0, 1, 0)],
        [FakePoint(1, 0, 0), FakePoint(1, 1, 0), FakePoint(0, 1, 0)],
    ]
    iterator_count = 0
    MPoint = FakePoint

    class MSpace(object):
        kWorld = object()

    class MItMeshPolygon(object):
        def __init__(self, unused_dag):
            FakeOpenMaya.iterator_count += 1
            self.index = 0

        def isDone(self):
            return self.index >= len(FakeOpenMaya.polygons)

        def getTriangles(self, unused_space):
            return FakeOpenMaya.polygons[self.index], []

        def next(self):
            self.index += 1


MESH_SURFACE_SAMPLES = load_method("_mesh_surface_samples", {"om": FakeOpenMaya})
SAMPLE_FACE_FANS = load_method("_sample_indexed_face_fans", {"om": FakeOpenMaya})


class FakeSurfaceSampler(object):
    def __init__(self):
        self._gt_surface_sample_cache = {}

    def _mesh_dag_path(self, node):
        return node

    def _triangle_area(self, a, b, c):
        return 0.5


class FakeFastCmds(object):
    @staticmethod
    def polyEvaluate(unused_node, **kwargs):
        return 2 if kwargs.get("shell") else 0


BUILD_LP_FAST = load_method("_build_lp_data_list", {"cmds": FakeFastCmds()})
SAMPLE_MAYA_TRIANGLES = load_method("_sample_indexed_mesh_triangles", {"om": FakeOpenMaya})


class SingleShellReuseTests(unittest.TestCase):
    def test_balanced_reuses_shell_snapshot_without_surface_intersector(self):
        matcher = FakeMatcher("BALANCED")
        result = BUILD_LP_DATA(matcher, ["asset_low"])

        self.assertEqual(1, matcher.shell_calls)
        self.assertEqual(0, matcher.intersector_calls)
        self.assertEqual("asset_low", result[0]["node"])
        self.assertFalse(result[0]["is_virtual_shell"])
        self.assertEqual([object().__class__], [value.__class__ for value in result[0]["sample_points"]])

    def test_accurate_reuses_snapshot_and_adds_exact_surface_intersector(self):
        matcher = FakeMatcher("ACCURATE")
        result = BUILD_LP_DATA(matcher, ["asset_low"])

        self.assertEqual(1, matcher.shell_calls)
        self.assertEqual(1, matcher.intersector_calls)
        self.assertEqual("surface:asset_low", result[0]["intersector"])

    def test_fast_mode_still_extracts_disconnected_lp_shells(self):
        class FastMatcher(FakeMatcher):
            def get_lp_shell_bbox_data(self, node):
                self.shell_calls += 1
                return [
                    {"node": node + "::shell_000"},
                    {"node": node + "::shell_001"},
                ]

        matcher = FastMatcher("FAST")
        result = BUILD_LP_FAST(matcher, ["combined_low"])
        self.assertEqual(1, matcher.shell_calls)
        self.assertEqual(2, len(result))


class DegenerateBboxTests(unittest.TestCase):
    def test_coincident_planes_have_full_overlap(self):
        plane_a = {"min": [0.0, 0.0, 0.0], "max": [2.0, 3.0, 0.0]}
        plane_b = {"min": [0.0, 0.0, 0.0], "max": [2.0, 3.0, 0.0]}
        self.assertAlmostEqual(1.0, BBOX_OVERLAP(object(), plane_a, plane_b))

    def test_parallel_separated_planes_do_not_overlap(self):
        plane_a = {"min": [0.0, 0.0, 0.0], "max": [2.0, 3.0, 0.0]}
        plane_b = {"min": [0.0, 0.0, 0.1], "max": [2.0, 3.0, 0.1]}
        self.assertEqual(0.0, BBOX_OVERLAP(object(), plane_a, plane_b))

    def test_crossed_thin_boxes_only_report_their_true_volume_overlap(self):
        horizontal = {"min": [0.0, 0.0, 0.0], "max": [100.0, 1.0, 1.0]}
        vertical = {"min": [0.0, 0.0, 0.0], "max": [1.0, 100.0, 1.0]}
        self.assertAlmostEqual(0.01, BBOX_OVERLAP(object(), horizontal, vertical))


class StreamingSurfaceSampleTests(unittest.TestCase):
    def test_sampling_uses_two_streaming_passes_and_a_bounded_result(self):
        FakeOpenMaya.iterator_count = 0
        sampler = FakeSurfaceSampler()
        samples = MESH_SURFACE_SAMPLES(sampler, "mesh", 5)

        self.assertEqual(2, FakeOpenMaya.iterator_count)
        self.assertEqual(5, len(samples))
        self.assertIs(samples, MESH_SURFACE_SAMPLES(sampler, "mesh", 5))
        self.assertEqual(2, FakeOpenMaya.iterator_count)

    def test_shell_face_sampling_does_not_build_a_triangle_list(self):
        sampler = FakeSurfaceSampler()
        points = [
            FakePoint(0, 0, 0),
            FakePoint(1, 0, 0),
            FakePoint(1, 1, 0),
            FakePoint(0, 1, 0),
        ]
        samples = SAMPLE_FACE_FANS(sampler, points, [[0, 1, 2, 3]], [0], 3)
        self.assertEqual(3, len(samples))

    def test_single_shell_reuse_samples_mayas_actual_triangulation(self):
        sampler = FakeSurfaceSampler()
        points = [
            FakePoint(0, 0, 0),
            FakePoint(1, 0, 0),
            FakePoint(1, 1, 0),
            FakePoint(0, 1, 0),
        ]
        samples = SAMPLE_MAYA_TRIANGLES(
            sampler,
            points,
            [2],
            [0, 1, 3, 1, 2, 3],
            [0],
            [0],
            3,
        )
        self.assertEqual(3, len(samples))


if __name__ == "__main__":
    unittest.main()
