from __future__ import print_function

import hashlib
import hmac
import math
import os
import pathlib
import random
import sys
import time


sys.path.insert(0, os.environ["BG_NATIVE_TEST_BIN"])
import bg_math_core

assert callable(getattr(bg_math_core, "calculate_coverage_stats", None))
assert callable(getattr(bg_math_core, "PointCloudIndex", None))


def activate():
    machine = bg_math_core.machine_hash()
    license_id = "BMA-NATIVE-MATH-TEST"
    expires_at = int(time.time()) + 3600
    message = "\n".join(
        ("BakeMaster:NativeLicense:v1", "bake-master", license_id, machine, str(expires_at))
    ).encode("utf-8")
    key = bytes.fromhex(
        pathlib.Path(r"D:\Bake_Groups_License_Keys\native_guard.key").read_text(encoding="ascii").strip()
    )
    proof = hmac.new(key, message, hashlib.sha256).hexdigest()
    assert bg_math_core.activate_signed_license(
        "bake-master", license_id, machine, expires_at, proof
    )


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


activate()
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

owner = bg_math_core.calculate_vertex_owner_scores(
    [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]],
    [[0.1, 0.0, 0.0], [10.1, 0.0, 0.0]],
    [(0, 0), (0, 1), (1, 0), (1, 1)],
)
assert len(owner) == 4
assert owner[0][2] == 100.0 and owner[3][2] == 100.0

assert bg_math_core.generate_fingerprint_data(cube, [0.0, 0.0, 0.0]).startswith("v8_")
print("Native math differential test passed: {}".format(sys.version.split()[0]))
