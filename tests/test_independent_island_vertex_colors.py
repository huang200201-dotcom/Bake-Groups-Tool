from __future__ import absolute_import, division, print_function

import io
import json
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.join(ROOT, "src", "Bake_Groups")



def read(path):
    with io.open(path, 'r', encoding='utf-8-sig') as stream:
        return stream.read()


class IndependentIslandVertexColorTests(unittest.TestCase):
    def test_export_helper_is_copy_only_and_topology_based(self):
        source = read(os.path.join(RUNTIME, 'bg_final_export.py'))
        self.assertIn('def _assign_independent_island_vertex_colors', source)
        self.assertIn('MItMeshPolygon', source)
        self.assertIn('polyColorSet', source)
        self.assertIn('polyColorPerVertex', source)
        self.assertIn("if independent_island_vertex_colors and mode in ['both', 'hp']", source)
        self.assertIn('export copies only', read(os.path.join(RUNTIME, 'bg_mixins.py')))

    def test_final_check_ui_and_export_wiring(self):
        source = read(os.path.join(RUNTIME, 'bg_mixins.py'))
        self.assertIn('独立岛顶点色分组', read(os.path.join(RUNTIME, 'localization', 'zh-CN.json')))
        self.assertIn('build_final_export_options(pair)', source)
        self.assertIn('self._island_vertex_colors_enabled(pair)', source)
        self.assertIn('independent_island_vertex_colors', source)

    def test_session_default_is_disabled(self):
        source = read(os.path.join(RUNTIME, 'bg_core.py'))
        self.assertIn("setdefault('independent_island_vertex_colors', False)", source)

    def test_localizations_parse(self):
        for name in ('zh-CN.json', 'en.json', 'ja.json', 'ru.json'):
            with io.open(os.path.join(RUNTIME, 'localization', name), encoding='utf-8-sig') as stream:
                data = json.load(stream)
            self.assertIn('Independent Island Vertex Color Grouping', data['texts'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
