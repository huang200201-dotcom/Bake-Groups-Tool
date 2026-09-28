from __future__ import absolute_import, division, print_function

import ast
import io
import json
import os
import unittest
import types


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(ROOT, "src", "Bake_Groups")
CAGE_PATH = os.path.join(RUNTIME, "bg_cage.py")
MIXINS_PATH = os.path.join(RUNTIME, "bg_mixins.py")
CORE_PATH = os.path.join(RUNTIME, "bg_core.py")
LAUNCHER_PATH = os.path.join(RUNTIME, "launcher.py")



def load_helpers():
    with io.open(CAGE_PATH, "r", encoding="utf-8-sig") as stream:
        tree = ast.parse(stream.read(), CAGE_PATH)
    wanted = {
        "default_cage_settings",
        "normalized_cage_settings",
        "cage_name_from_low",
    }
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"CAGE_SUFFIX": "_cage"}
    exec(compile(module, CAGE_PATH, "exec"), namespace)
    return namespace


HELPERS = load_helpers()


class CageSettingsTests(unittest.TestCase):
    def test_defaults_are_safe_and_exportable(self):
        settings = HELPERS["default_cage_settings"]()
        self.assertEqual(settings["inflate_pct"], 2.0)
        self.assertEqual(settings["gap_pct"], 0.35)
        self.assertTrue(settings["fit_to_high"])
        self.assertTrue(settings["export_enabled"])

    def test_untrusted_session_values_are_clamped(self):
        settings = HELPERS["normalized_cage_settings"]({
            "inflate_pct": 999,
            "gap_pct": -4,
            "visible": 0,
        })
        self.assertEqual(settings["inflate_pct"], 30.0)
        self.assertEqual(settings["gap_pct"], 0.0)
        self.assertFalse(settings["visible"])

    def test_cage_name_replaces_only_final_low_marker(self):
        naming = HELPERS["cage_name_from_low"]
        self.assertEqual(naming("|root|asset_panel_low"), "asset_panel_cage")
        self.assertEqual(naming("asset_low_detail_low_002"), "asset_low_detail_cage_002")
        self.assertEqual(naming("asset_panel"), "asset_panel_cage")


class CageSourceSafetyTests(unittest.TestCase):
    def test_empty_input_and_early_cancel_never_delete_previous_cage(self):
        with io.open(CAGE_PATH, "r", encoding="utf-8-sig") as stream:
            tree = ast.parse(stream.read(), CAGE_PATH)
        selected = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))]
        namespace = {"CAGE_SUFFIX": "_cage"}
        exec(compile(ast.Module(body=selected, type_ignores=[]), CAGE_PATH, "exec"), namespace)
        manager = namespace["CageManager"]
        deleted = []
        manager.delete_chapter = staticmethod(lambda chapter: deleted.append(chapter))
        manager._valid_mesh = staticmethod(lambda node: True)
        manager._long = staticmethod(lambda node: node)
        self.assertEqual(manager.rebuild_chapter("asset", [], {}), [])
        with self.assertRaises(namespace["CageCancelled"]):
            manager.rebuild_chapter("asset", ["|LP|asset_low"], {}, cancelled_getter=lambda: True)
        self.assertEqual(deleted, [])

    def test_runtime_wiring_and_export_are_independent_from_smoothing(self):
        with io.open(MIXINS_PATH, "r", encoding="utf-8-sig") as stream:
            mixins = stream.read()
        with io.open(CORE_PATH, "r", encoding="utf-8-sig") as stream:
            core = stream.read()
        with io.open(LAUNCHER_PATH, "r", encoding="utf-8-sig") as stream:
            launcher = stream.read()
        self.assertIn('"bg_cage"', launcher)
        self.assertIn("'cage_settings'", core)
        self.assertIn("except bg_cage.CageCancelled:", mixins)
        self.assertNotIn(
            "smooth_states is not None and pair.get('cage_settings'",
            mixins,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
