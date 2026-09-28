"""Differential native geometry tests, with no activation or private build inputs.

Set BG_NATIVE_TEST_BIN to build/native/<Maya year> and run with that Maya
release's CPython ABI. Standard test discovery skips this suite without a binary.
"""
import math
import os
import random
import sys
import unittest

native_bin = os.environ.get("BG_NATIVE_TEST_BIN")
if native_bin:
    sys.path.insert(0, native_bin)
try:
    import bg_math_core
except ImportError:
    if native_bin:
        raise
    bg_math_core = None

def points(flat):
    return [flat[index:index + 3] for index in range(0, len(flat), 3)]


def brute_avg(source, target):
    target_points = points(target)
    distances = []
    for point in points(source):
        distances.append(
            math.sqrt(min(sum((point[axis] - other[axis]) ** 2 for axis in range(3)) for other in target_points))
        )
    return sum(distances) / len(distances)


def brute_min(source, target):
    return min(
        math.sqrt(sum((left[axis] - right[axis]) ** 2 for axis in range(3)))
        for left in points(source)
        for right in points(target)
    )


@unittest.skipIf(bg_math_core is None, "set BG_NATIVE_TEST_BIN to a matching compiled binary")
class NativeGeometryTests(unittest.TestCase):
    def test_public_api_has_no_activation_or_machine_interfaces(self):
        forbidden = ("activate_signed_license", "deactivate_license", "machine_hash",
                     "is_authorized", "require_authorized")
        self.assertFalse(set(forbidden).intersection(dir(bg_math_core)))
        index = bg_math_core.PointCloudIndex([0.0, 0.0, 0.0])
        self.assertEqual(index.average_distance([3.0, 4.0, 0.0]), 5.0)
        self.assertEqual(index.target_point_count, 1)

    def test_geometry_entry_points_and_invalid_index_inputs(self):
        left, right = [0., 0., 0.], [3., 4., 0.]
        self.assertEqual(bg_math_core.calculate_bidirectional_avg_distance(left, right), 5.)
        self.assertEqual(bg_math_core.resolve_hp_collision(left, [right, left]), 1)
        self.assertTrue(bg_math_core.are_symmetric(
            [-1., 0., 0., -2., 1., 0., -3., 0., 1.],
            [1., 0., 0., 2., 1., 0., 3., 0., 1.], .01))
        for invalid in ([], [0., 0.], [float('nan'), 0., 0.]):
            with self.assertRaises(ValueError):
                bg_math_core.PointCloudIndex(invalid)

    def test_geometry_against_reference_calculations(self):
        rng = random.Random(7331)
        for count_a, count_b in ((1, 1), (7, 11), (40, 55), (137, 211)):
            source = [rng.uniform(-20.0, 20.0) for _ in range(count_a * 3)]
            target = [rng.uniform(-20.0, 20.0) for _ in range(count_b * 3)]
            expected_avg = brute_avg(source, target)
            expected_min = brute_min(source, target)
            assert abs(bg_math_core.calculate_avg_distance(source, target) - expected_avg) < 1e-9
            target_index = bg_math_core.PointCloudIndex(target)
            assert target_index.target_point_count == count_b
            assert abs(target_index.average_distance(source) - expected_avg) < 1e-9
            assert abs(bg_math_core.calculate_min_distance(source, target) - expected_min) < 1e-9

        translated_a = [10000000.0, 10000000.0, 10000000.0]
        translated_b = [10000000.1, 10000000.0, 10000000.0]
        assert abs(bg_math_core.calculate_min_distance(translated_a, translated_b) - 0.1) < 1e-8
        assert not bg_math_core.check_mesh_collision(translated_a, translated_b, 0.005)
        assert bg_math_core.check_mesh_collision(translated_a, translated_b, 0.11)

        assert not bg_math_core.check_mesh_collision([0.0, 0.0, 0.0], [0.019, 0.019, 0.019], 0.01)
        assert bg_math_core.check_mesh_collision([0.0, 0.0, 0.0], [0.009, 0.0, 0.0], 0.01)
        assert not bg_math_core.check_mesh_collision([0.0, 0.0], [0.0, 0.0, 0.0], 0.01)
        assert not bg_math_core.check_mesh_collision([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], 0.0)

        coverage, average = bg_math_core.calculate_coverage_stats(
            [0.0, 0.0, 0.0, 2.0, 0.0, 0.0, 10.0, 0.0, 0.0],
            [0.1, 0.0, 0.0, 2.2, 0.0, 0.0],
            0.15,
        )
        assert abs(coverage - (1.0 / 3.0)) < 1e-12
        assert abs(average - ((0.1 + 0.2 + 7.8) / 3.0)) < 1e-12
        invalid_coverage, invalid_average = bg_math_core.calculate_coverage_stats(
            [0.0, 0.0], [0.0, 0.0, 0.0], 0.1
        )
        assert invalid_coverage == 0.0 and math.isinf(invalid_average)

        line = []
        for index in range(300):
            line.extend([float(index), 0.0, 0.0])
        line_metrics = bg_math_core.analyze_mesh_shape(line)
        assert line_metrics.elongation > 100.0

        cube = []
        for x in (-1.0, 1.0):
            for y in (-1.0, 1.0):
                for z in (-1.0, 1.0):
                    cube.extend([x, y, z])
        cube_metrics = bg_math_core.analyze_mesh_shape(cube)
        assert abs(cube_metrics.elongation - 1.0) < 1e-9

        # 1.5.0: normalized PCA metrics retain the plate-safe axis-ratio convention.
        assert callable(getattr(bg_math_core, "analyze_mesh_shape_v2", None))
        asymmetric = []
        for index in range(70):
            asymmetric.extend([rng.uniform(-5.0, 8.0), rng.uniform(-2.0, 2.0), rng.uniform(-0.6, 0.6)])
        reference_metrics = bg_math_core.analyze_mesh_shape_v2(asymmetric)
        for scale in (0.0001, 1.0, 1000.0):
            rotated = []
            angle_a, angle_b = 0.63, -0.47
            for x,y,z in points(asymmetric):
                rx = math.cos(angle_a)*x-math.sin(angle_a)*y
                ry = math.sin(angle_a)*x+math.cos(angle_a)*y
                rz = z
                rotated.extend([
                    scale*(math.cos(angle_b)*rx+math.sin(angle_b)*rz)+3.0,
                    scale*ry-4.0,
                    scale*(-math.sin(angle_b)*rx+math.cos(angle_b)*rz)+7.0,
                ])
            metrics = bg_math_core.analyze_mesh_shape_v2(rotated)
            assert abs(metrics.elongation-reference_metrics.elongation) < 1e-7
            assert abs(metrics.symmetry_score-reference_metrics.symmetry_score) < 1e-7
        plate = [-1,-1,0, -1,1,0, 1,-1,0, 1,1,0]
        assert abs(bg_math_core.analyze_mesh_shape_v2(plate).elongation-1.0) < 1e-9
        assert bg_math_core.analyze_mesh_shape_v2(line).elongation > 100.0

        # Interior-to-triangle distances must not be measured to sparse corner vertices.
        triangle = [0,0,0, 10,0,0, 0,10,0]
        sample = [2,2,.01]
        surface = bg_math_core.calculate_surface_match(sample, triangle, sample, triangle, .02)
        assert abs(surface["hp_to_lp_distance"]-.01) < 1e-12
        assert surface["hp_coverage"] == 1.0
        assert surface["lp_coverage"] == 1.0
        shift = 10000000.0
        translated_triangle = [value+shift for value in triangle]
        translated_sample = [value+shift for value in sample]
        translated_surface = bg_math_core.calculate_surface_match(translated_sample, translated_triangle, translated_sample, translated_triangle, .02)
        assert abs(translated_surface["hp_to_lp_distance"]-.01) < 1e-8
        # Degenerate faces reduce to segments; malformed/nonfinite proxies cannot match.
        degenerate = [0,0,0, 1,0,0, 2,0,0]
        segment_surface = bg_math_core.calculate_surface_match([.5,1,0],degenerate,[.5,1,0],degenerate,1.1)
        assert abs(segment_surface["hp_to_lp_distance"]-1.0) < 1e-12
        for bad_triangles in ([0,0,0], [float('nan')]*9):
            invalid = bg_math_core.calculate_surface_match(sample,bad_triangles,sample,bad_triangles,.02)
            assert math.isinf(invalid["hp_to_lp_distance"]) and invalid["hp_coverage"] == 0.0

        owner = bg_math_core.calculate_vertex_owner_scores(
            [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]],
            [[0.1, 0.0, 0.0], [10.1, 0.0, 0.0]],
            [(0, 0), (0, 1), (1, 0), (1, 1)],
        )
        assert len(owner) == 4
        assert owner[0][2] == 100.0 and owner[3][2] == 100.0

        assert bg_math_core.generate_fingerprint_data(cube, [0.0, 0.0, 0.0]).startswith("v8_")

if __name__ == "__main__":
    unittest.main(verbosity=2)
