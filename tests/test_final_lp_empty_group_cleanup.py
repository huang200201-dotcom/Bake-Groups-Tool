from __future__ import absolute_import, division, print_function

import ast
import contextlib
import io
import json
import os
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(ROOT, "src", "Bake_Groups")
EXPORT_PATH = os.path.join(RUNTIME, "bg_final_export.py")



def load_export_processor(cmds):
    with io.open(EXPORT_PATH, "r", encoding="utf-8-sig") as stream:
        tree = ast.parse(stream.read(), EXPORT_PATH)
    class_node = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "FinalExportProcessor"
    )
    try:
        module = ast.Module(body=[class_node], type_ignores=[])
    except TypeError:  # Python 3.7
        module = ast.Module(body=[class_node])
    ast.fix_missing_locations(module)
    namespace = {
        "cmds": cmds,
        "mel": types.SimpleNamespace(),
        "om": types.SimpleNamespace(),
        "bg_core": types.SimpleNamespace(),
        "bg_l10n": types.SimpleNamespace(text=lambda value: value),
        "QtWidgets": types.SimpleNamespace(),
        "QtCore": types.SimpleNamespace(),
        "re": __import__("re"),
        "math": __import__("math"),
        "os": os,
        "contextlib": contextlib,
    }
    exec(compile(module, EXPORT_PATH, "exec"), namespace)
    return namespace["FinalExportProcessor"]


class FakeCommands(object):
    def __init__(self, children):
        self.children = dict((key, list(value)) for key, value in children.items())
        self.deleted = []

    def objExists(self, node):
        return node in self.children

    def listRelatives(self, node, **unused_kwargs):
        return list(self.children.get(node, []))

    def delete(self, node, **unused_kwargs):
        nodes = node if isinstance(node, (list, tuple)) else [node]
        for target in nodes:
            self.deleted.append(target)
            self.children.pop(target, None)
            for parent in list(self.children):
                self.children[parent] = [
                    child for child in self.children[parent] if child != target
                ]


class FinalLPEmptyGroupCleanupTests(unittest.TestCase):
    def test_empty_chapters_are_removed_but_valid_outputs_are_preserved(self):
        commands = FakeCommands({
            "LP_Combine_BG": ["|LP_Combine_BG|Empty", "|LP_Combine_BG|Valid"],
            "|LP_Combine_BG|Empty": [],
            "|LP_Combine_BG|Valid": ["|LP_Combine_BG|Valid|asset_low"],
            "|LP_Combine_BG|Valid|asset_low": ["meshShape"],
        })
        processor = load_export_processor(commands)

        removed = processor._cleanup_empty_final_lp_groups()

        self.assertEqual(removed, 1)
        self.assertEqual(commands.deleted, ["|LP_Combine_BG|Empty"])
        self.assertTrue(commands.objExists("LP_Combine_BG"))
        self.assertTrue(commands.objExists("|LP_Combine_BG|Valid"))

    def test_global_root_is_removed_when_only_empty_chapters_remain(self):
        commands = FakeCommands({
            "LP_Combine_BG": ["|LP_Combine_BG|EmptyA", "|LP_Combine_BG|EmptyB"],
            "|LP_Combine_BG|EmptyA": [],
            "|LP_Combine_BG|EmptyB": [],
        })
        processor = load_export_processor(commands)

        removed = processor._cleanup_empty_final_lp_groups()

        self.assertEqual(removed, 3)
        self.assertFalse(commands.objExists("LP_Combine_BG"))

    def test_source_checks_for_valid_lp_before_creating_output_root(self):
        with io.open(EXPORT_PATH, "r", encoding="utf-8-sig") as stream:
            source = stream.read()
        check_position = source.index("if not has_valid_lp_source:")
        create_position = source.index(
            "cmds.group(em=True, name=global_lp_root, world=True)",
            check_position,
        )
        self.assertLess(check_position, create_position)
        self.assertIn(
            "FinalExportProcessor._cleanup_empty_final_lp_groups()",
            source,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
