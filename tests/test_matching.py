"""Scene-independent matching regressions retained by the open-source edition."""
from __future__ import absolute_import, division, print_function

import ast
import heapq
import itertools
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



def read_tree(filename):
    with open(os.path.join(RUNTIME, filename), encoding="utf-8-sig") as handle:
        return ast.parse(handle.read(), filename)


def compile_nodes(nodes, namespace):
    ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "matching-regression", "exec"), namespace)
    return namespace


class Signal(object):
    def __init__(self, *args):
        self.values = []

    def emit(self, *args):
        self.values.append(args)


class Thread(object):
    def isInterruptionRequested(self):
        return False


CORE = compile_nodes([
    node for node in read_tree("bg_core.py").body
    if isinstance(node, ast.ClassDef) and node.name in ("MathUtils", "StatsUtils")
], {"math": math})


def workers(native=None):
    namespace = {
        "math": math, "re": re, "heapq": heapq,
        "threading": threading, "traceback": traceback,
        "QtCore": types.SimpleNamespace(QThread=Thread, Signal=Signal),
        "bg_core": types.SimpleNamespace(MathUtils=CORE["MathUtils"], StatsUtils=CORE["StatsUtils"]),

        "HAS_MATH_CORE": native is not None, "bg_math_core": native,
    }
    for filename in ("bg_worker_hp.py", "bg_worker_lp.py"):
        compile_nodes([
            node for node in read_tree(filename).body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        ], namespace)
    return namespace


def cloud_distance(source, target):
    source_points = list(zip(*[iter(source)] * 3))
    target_points = list(zip(*[iter(target)] * 3))
    return sum(min(math.sqrt(sum((a-b)**2 for a, b in zip(p,q))) for q in target_points) for p in source_points) / len(source_points)


def box(name, low, high, vtx=8):
    diag = math.sqrt(sum((high[i]-low[i])**2 for i in range(3)))
    volume = (high[0]-low[0])*(high[1]-low[1])*(high[2]-low[2])
    info = {
        "name": name, "uuid": name, "min": list(low), "max": list(high),
        "bbox": list(low)+list(high), "diag": diag, "radius": diag*.5,
        "center": [(low[i]+high[i])*.5 for i in range(3)],
        "bbox_vol": volume, "volume": volume, "vtx": vtx, "edges":12,
        "hash":"empty", "uv_signature":"empty", "variance":999.0,
    }
    vertices = [coordinate for p in itertools.product(*[(low[i],high[i]) for i in range(3)]) for coordinate in p]
    return info, vertices


class LPGeometryRankingTests(unittest.TestCase):
    def test_nearby_duplicate_cannot_preempt_exact_candidate(self):
        calls=[]
        def distance(source,target):
            calls.append(1)
            return cloud_distance(source,target)
        klass=workers(types.SimpleNamespace(calculate_avg_distance=distance))["LPMatchingWorker"]
        low, lv=box("lp",(-1,-1,-1),(1,1,1))
        wrong,wv=box("wrong",(-.95,-1,-1),(1.05,1,1))
        right,rv=box("right",(-1,-1,-1),(1,1,1))
        worker=klass({"A_wrong":["wrong"],"Z_right":["right"]},{"wrong":wrong,"right":right},{"lp":low},{"wrong":wv,"right":rv},{"lp":lv},{"lp":lv},1.5)
        self.assertEqual(worker.get_best_match(low,lv),"Z_right")
        self.assertEqual(len(calls),2)

    def test_equal_bbox_and_counts_do_not_prove_geometric_identity(self):
        klass=workers(types.SimpleNamespace(calculate_avg_distance=cloud_distance))["LPMatchingWorker"]
        low,lv=box("lp",(-1,-1,-1),(1,1,1))
        wrong=dict(low)
        # Same bounds and counts, different actual points: retain all six extrema.
        wv=[-1,0,0, 1,0,0, 0,-1,0, 0,1,0, 0,0,-1, 0,0,1, .3,.3,.3, -.3,-.3,-.3]
        worker=klass({"A_wrong":["wrong"],"Z_right":["right"]},{"wrong":wrong,"right":low},{"lp":low},{"wrong":wv,"right":lv},{"lp":lv},{"lp":lv},1.5)
        self.assertEqual(worker.get_best_match(low,lv),"Z_right")

    def test_exact_plane_remains_matchable(self):
        klass=workers(types.SimpleNamespace(calculate_avg_distance=cloud_distance))["LPMatchingWorker"]
        low,lv=box("lp",(-1,-1,0),(1,1,0))
        worker=klass({"Plane":["hp"]},{"hp":low},{"lp":low},{"hp":lv},{"lp":lv},{"lp":lv},1.5)
        self.assertEqual(worker.get_best_match(low,lv),"Plane")


class CompoundOwnershipTests(unittest.TestCase):
    def test_transitive_component_cannot_pull_member_outside_its_lp_candidates(self):
        klass=workers()["HPGroupingWorker"]
        hp, hv, lp, lv={}, {}, {}, {}
        for index,name in enumerate(("A","B","C")):
            hp[name],hv[name]=box(name,(index,0,0),(index+1,1,1))
        lp["Left"],lv["Left"]=box("Left",(0,0,0),(.9,1,1))
        lp["Right"],lv["Right"]=box("Right",(2.1,0,0),(3,1,1))
        worker=klass(hp,lp,hv,lv,{},15,12,strategy=0,compound_link_verts=1,detect_floaters=False,grouping_mode="one_lp_per_group")
        groups,unused_logs=worker._run_impl()
        owners={mesh["name"]:group for group,meshes in groups.items() for mesh in meshes}
        self.assertNotEqual(owners["A"],owners["C"])
        self.assertIn("Left",owners["A"])
        self.assertIn("Right",owners["C"])
        self.assertTrue(any("COMPOUND_COMPONENT" in line for line in worker.debug_lines))

    def test_surface_evidence_selects_lp_when_vertex_density_would_mislead(self):
        calls=[]
        def surface(hp_samples, hp_triangles, lp_samples, lp_triangles, tolerance):
            calls.append(lp_samples[0])
            correct=lp_samples[0]==11
            coverage=.95 if correct else .1
            distance=.001 if correct else 1.0
            return {"hp_to_lp_distance":distance,"lp_to_hp_distance":distance,
                    "average_distance":distance,"hp_coverage":coverage,"lp_coverage":coverage,"coverage":coverage}
        native=types.SimpleNamespace(calculate_surface_match=surface,
            calculate_avg_distance=lambda hp,lp:.01 if lp[0]==22 else .2,
            analyze_mesh_shape_v2=lambda unused:types.SimpleNamespace(elongation=1,symmetry_score=0),
            check_mesh_collision=lambda *unused:False)
        klass=workers(native)["HPGroupingWorker"]
        high,hv=box("HP",(0,0,0),(1,1,1))
        left,_=box("Left",(0,0,0),(1,1,1))
        right,_=box("Right",(0,0,0),(1,1,1))
        proxy=lambda marker:{"samples":[marker,0,0],"triangles":[0]*9}
        worker=klass({"HP":high},{"Left":left,"Right":right},{"HP":hv},{"Left":[11,0,0],"Right":[22,0,0]},
            {},15,12,detect_floaters=False,use_symmetry=False,grouping_mode="one_lp_per_group",
            hp_surface_cache={"HP":proxy(1)},lp_surface_cache={"Left":proxy(11),"Right":proxy(22)})
        groups,unused=worker._run_impl()
        self.assertEqual(list(groups),["Pair_Left"])
        self.assertEqual(sorted(calls),[11,22])
        self.assertTrue(any("directional surface evidence" in line for line in worker.debug_lines))


class OwnLPProtectionTests(unittest.TestCase):
    def own_claim(self):
        node=next(node for node in ast.walk(read_tree("bg_worker_hp.py")) if isinstance(node,ast.FunctionDef) and node.name=="_has_own_lp_claim")
        return compile_nodes([node],{})[node.name]

    def test_confident_claim_survives_multiple_overlapping_lp_boxes(self):
        claim={"owner_lp":"bolt_low","score":80,"source":"lp","candidate_count":3}
        self.assertTrue(self.own_claim()("bolt_high",claim))

    def test_ambiguous_own_lp_is_not_automatically_a_floater(self):
        claim={"owner_lp":"left_low","score":40,"source":"lp","candidate_count":2,"ambiguous":True}
        self.assertTrue(self.own_claim()("bolt_high",claim))

    def test_weak_claim_is_still_eligible_for_repair(self):
        self.assertFalse(self.own_claim()("decal",{"owner_lp":"body","score":20,"source":"lp","candidate_count":1}))


class AssemblyClassificationTests(unittest.TestCase):
    def test_complete_assembly_classification_is_order_independent(self):
        tree=read_tree("bg_worker_hp.py")
        loop=next(node for node in ast.walk(tree) if isinstance(node,ast.For) and isinstance(node.target,ast.Tuple) and [getattr(elt,"id","") for elt in node.target.elts]==["item_idx","item"])
        start=next(index for index,node in enumerate(loop.body) if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=="max_single_diag" for target in node.targets))
        prefix=ast.parse('''def classify(item):
    true_vol, cluster_diag = .03, .5
    is_zb = is_hard_custom = False
    huge_items, large_items, medium_items, small_items, bolt_items = [], [], [], [], []
    huge_zb, large_zb, medium_zb, small_zb, bolt_zb = [], [], [], [], []
    bolt_mixed_reclass_count = 0
''').body[0]
        prefix.body.extend(loop.body[start:])
        prefix.body.extend(ast.parse("return 'Bolts' if bolt_items else 'Medium' if medium_items else 'Other'").body)
        ns={"self":types.SimpleNamespace(use_symmetry=True,bolt_symmetry=.8,bolt_elongation=2.5,wire_elongation=6),
            "get_shape_metrics":lambda mesh:(10 if mesh["name"]=="wire" else 1,0),
            "_mesh_is_bolt_like":lambda mesh:mesh["name"].startswith("bolt"),
            "small_threshold":.1,"medium_threshold":1,"large_threshold":2,
            "_short_name":str,"_debug":lambda unused:None}
        classify=compile_nodes([prefix],ns)["classify"]
        items=[{"name":name,"diag":.3,"bbox_vol":.01} for name in ("wire","bolt1","bolt2")]
        results=[classify(list(order)) for order in itertools.permutations(items)]
        self.assertEqual(results,["Bolts"]*6)


if __name__=="__main__":
    unittest.main(verbosity=2)
