# -*- coding: utf-8 -*-
from __future__ import print_function, division, absolute_import

import ast
import heapq
import io
import json
import math
import os
import random
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(ROOT, "src", "Bake_Groups")
WORKER_PATH = os.path.join(RUNTIME, "bg_worker_hp.py")



def load_scaling_helpers():
    with open(WORKER_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source, WORKER_PATH)
    wanted = {
        "bounded_grid_cells",
        "bbox_sweep_candidate_pairs",
        "bboxes_within_tolerance",
        "RevisionedCandidateQueue"
    }
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in wanted
    ]
    namespace = {"math": math, "heapq": heapq}
    try:
        module = ast.Module(body=selected, type_ignores=[])
    except TypeError:  # Python 3.7
        module = ast.Module(body=selected)
    ast.fix_missing_locations(module)
    exec(compile(module, WORKER_PATH, "exec"), namespace)
    return namespace


HELPERS = load_scaling_helpers()
bounded_grid_cells = HELPERS["bounded_grid_cells"]
bbox_sweep_candidate_pairs = HELPERS["bbox_sweep_candidate_pairs"]
bboxes_within_tolerance = HELPERS["bboxes_within_tolerance"]
RevisionedCandidateQueue = HELPERS["RevisionedCandidateQueue"]


class BoundedGridTests(unittest.TestCase):
    def test_non_cube_limit_is_strict(self):
        cells = bounded_grid_cells([0, 0, 0, 99, 99, 99], 1.0, max_cells=100)
        self.assertLessEqual(len(cells), 100)
        self.assertEqual(len(cells), len(set(cells)))

    def test_long_thin_bbox_uses_budget_without_exceeding_it(self):
        cells = bounded_grid_cells([0, 0, 0, 9999, 1, 1], 1.0, max_cells=101)
        self.assertLessEqual(len(cells), 101)
        x_values = sorted(set(cell[0] for cell in cells))
        self.assertEqual(x_values[0], 0)
        self.assertEqual(x_values[-1], 9999)
        self.assertGreater(len(x_values), 90)

    def test_one_cell_limit(self):
        cells = bounded_grid_cells([-100, -100, -100, 100, 100, 100], 1.0, max_cells=1)
        self.assertEqual(len(cells), 1)


class CompoundBroadPhaseTests(unittest.TestCase):
    def test_separated_meshes_do_not_materialize_all_pairs(self):
        count = 2000
        names = ["hp_{:04d}".format(index) for index in range(count)]
        bboxes = {}
        paddings = {}
        for index, name in enumerate(names):
            x = index * 10.0
            bboxes[name] = [x, 0.0, 0.0, x + 1.0, 1.0, 1.0]
            paddings[name] = 0.1
        self.assertEqual(list(bbox_sweep_candidate_pairs(names, bboxes, paddings)), [])

    def test_sweep_axis_adapts_to_y_separation(self):
        names = ["hp_y_{:04d}".format(index) for index in range(1000)]
        bboxes = dict(
            (name, [0.0, index * 8.0, 0.0, 20.0, index * 8.0 + 1.0, 20.0])
            for index, name in enumerate(names)
        )
        paddings = dict((name, 0.1) for name in names)
        self.assertEqual(list(bbox_sweep_candidate_pairs(names, bboxes, paddings)), [])

    def test_sweep_never_rejects_pair_within_pair_tolerance(self):
        randomizer = random.Random(142857)
        names = ["hp_{:03d}".format(index) for index in range(240)]
        bboxes = {}
        paddings = {}
        for name in names:
            center = [randomizer.uniform(-50.0, 50.0) for _axis in range(3)]
            half_size = [randomizer.uniform(0.05, 2.5) for _axis in range(3)]
            bboxes[name] = [
                center[0] - half_size[0], center[1] - half_size[1], center[2] - half_size[2],
                center[0] + half_size[0], center[1] + half_size[1], center[2] + half_size[2]
            ]
            paddings[name] = randomizer.uniform(0.01, 1.25)

        swept = set(bbox_sweep_candidate_pairs(names, bboxes, paddings))
        exact_possible = set()
        for first_index, first_name in enumerate(names):
            for second_name in names[first_index + 1:]:
                pair_tolerance = min(paddings[first_name], paddings[second_name])
                if bboxes_within_tolerance(
                        bboxes[first_name], bboxes[second_name], pair_tolerance):
                    exact_possible.add(tuple(sorted((first_name, second_name))))
        self.assertTrue(exact_possible.issubset(swept))


def run_queue_simulation(group_count):
    names = ["group_{:03d}".format(index) for index in range(group_count)]
    active = list(names)
    order = dict((name, index) for index, name in enumerate(names))
    revisions = dict((name, 0) for name in names)
    sizes = dict((name, 1 + (index % 5)) for index, name in enumerate(names))
    centers = dict((name, float(index * 3)) for index, name in enumerate(names))

    def revision(name):
        return revisions.get(name)

    def source_key(name):
        return (sizes[name], 1, name)

    def candidate(source, target):
        if source == target:
            return None
        distance = abs(centers[source] - centers[target])
        score = (sizes[target] * 7.0) - distance
        return score, "simulation", 1.0, distance

    queue = RevisionedCandidateQueue(candidate, revision, source_key, order)
    if not queue.seed(active):
        raise AssertionError("unexpected cancellation")
    merges = []
    while len(active) > 1:
        best = queue.pop_best()
        if best is None:
            break
        _score, source, target, _reason, _ratio, _distance = best
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
        merges.append((source, target))
        if not queue.refresh(target, active):
            raise AssertionError("unexpected cancellation")
    return queue.evaluation_count, merges


def run_brute_simulation(group_count):
    names = ["group_{:03d}".format(index) for index in range(group_count)]
    active = list(names)
    sizes = dict((name, 1 + (index % 5)) for index, name in enumerate(names))
    centers = dict((name, float(index * 3)) for index, name in enumerate(names))
    merges = []
    while len(active) > 1:
        best = None
        sources = sorted(active, key=lambda name: (sizes[name], 1, name))
        for source in sources:
            for target in active:
                if source == target:
                    continue
                distance = abs(centers[source] - centers[target])
                score = (sizes[target] * 7.0) - distance
                record = (score, source, target)
                if best is None or score > best[0]:
                    best = record
        _score, source, target = best
        source_size = sizes[source]
        target_size = sizes[target]
        centers[target] = (
            centers[target] * target_size + centers[source] * source_size
        ) / float(target_size + source_size)
        sizes[target] += sizes[source]
        del sizes[source]
        del centers[source]
        active.remove(source)
        merges.append((source, target))
    return merges


class RevisionedQueueTests(unittest.TestCase):
    def test_queue_matches_full_recalculation_order(self):
        _evaluations, queue_merges = run_queue_simulation(18)
        self.assertEqual(queue_merges, run_brute_simulation(18))

    def test_candidate_evaluations_scale_quadratically(self):
        count_32, merges_32 = run_queue_simulation(32)
        count_64, merges_64 = run_queue_simulation(64)
        count_128, merges_128 = run_queue_simulation(128)
        self.assertEqual(len(merges_128), 127)
        self.assertLessEqual(count_32, 2 * 32 * 32)
        self.assertLessEqual(count_64, 2 * 64 * 64)
        self.assertLessEqual(count_128, 2 * 128 * 128)
        self.assertLess(count_64 / float(count_32), 4.3)
        self.assertLess(count_128 / float(count_64), 4.2)
        self.assertEqual(merges_32, run_queue_simulation(32)[1])
        self.assertEqual(merges_64, run_queue_simulation(64)[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
