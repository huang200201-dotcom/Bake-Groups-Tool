"""Chapter-scoped group identity shared by the list, preview and exporter."""
from __future__ import absolute_import, division, print_function

import re
import sys
import maya.cmds as cmds


def subgroup_name(node, kind):
    short = node.rsplit('|', 1)[-1]
    match = re.search(r'_?' + re.escape(kind) + r'(\d*)$', short, re.IGNORECASE)
    return short[:match.start()] + match.group(1) if match else short


def final_role(node):
    """Read the terminal export role; role words inside group names are literal."""
    match = re.search(r'_(high|low)(?:\d+|_\d+)?(?:_mat\d+)?$',
                      str(node or '').rsplit('|', 1)[-1], re.IGNORECASE)
    return match.group(1).lower() if match else None


def source_groups(hp_main, lp_main, keep_structure=False):
    """Use direct subgroup containers, never infer groups from mesh names."""
    groups = {}
    for root, kind in ((hp_main, 'HP'), (lp_main, 'LP')):
        if not root or not cmds.objExists(root):
            continue
        for child in cmds.listRelatives(root, children=True, type='transform', fullPath=True) or []:
            if cmds.listRelatives(child, shapes=True):
                continue
            attr = child + '.BakeManagerGroup'
            tagged = cmds.objExists(attr) and cmds.getAttr(attr) == kind
            has_suffix = re.search(r'_' + kind + r'\d*$', child.rsplit('|', 1)[-1], re.IGNORECASE)
            if not keep_structure and not tagged and not has_suffix:
                continue
            name = subgroup_name(child, kind)
            if kind == 'HP' or name in groups or keep_structure:
                groups.setdefault(name, {'hp': None, 'lp': None})[kind.lower()] = child
    return groups


def mesh_transforms(root, descendants=True):
    if not root or not cmds.objExists(root):
        return []
    nodes = cmds.listRelatives(root, allDescendents=True, fullPath=True, type='transform') if descendants else cmds.listRelatives(root, children=True, fullPath=True, type='transform')
    exporter = getattr(sys.modules.get('bg_final_export'), 'FinalExportProcessor', None)
    owned = getattr(exporter, '_owned_export_uuids', set())
    return sorted(node for node in (nodes or []) if cmds.listRelatives(
        node, shapes=True, fullPath=True, type='mesh', noIntermediate=True)
        and not owned.intersection(cmds.ls(node, uuid=True) or []))


def is_final_name(node, prefix, kind):
    # Only the terminal role and numeric piece/material suffixes are syntax.
    return bool(re.match(r'^' + re.escape(prefix) + '_' + kind +
                         r'(?:\d+|_\d+)?(?:_mat\d+)?$', node.rsplit('|', 1)[-1]))


def discover(base, hp_main, lp_main, keep_structure=False):
    groups = source_groups(hp_main, lp_main, keep_structure)
    chapter = '|LP_Combine_BG|' + base
    low_root = chapter if cmds.objExists(chapter) else lp_main
    lows = mesh_transforms(low_root, descendants=False)
    # Older saved scenes may contain only finalized meshes and no source groups.
    # In that format require an exact chapter prefix and LP role suffix.
    if not groups and not source_groups(hp_main, lp_main, True):
        pattern = r'^' + re.escape(base + '_') + r'(.+)_low$'
        for low in lows:
            if low.rsplit('|', 1)[-1] == base + '_low':
                groups.setdefault(base, {'hp': None, 'lp': None, 'prefix': base})
                continue
            match = re.match(pattern, low.rsplit('|', 1)[-1])
            if match:
                groups.setdefault(match.group(1), {'hp': None, 'lp': None})
        high_pattern = r'^' + re.escape(base + '_') + r'(.+)_high(?:\d+|_\d+)?(?:_mat\d+)?$'
        for high in mesh_transforms(hp_main):
            short = high.rsplit('|', 1)[-1]
            if is_final_name(high, base, 'high'):
                groups.setdefault(base, {'hp': None, 'lp': None, 'prefix': base})
                continue
            match = re.match(high_pattern, short)
            if match:
                groups.setdefault(match.group(1), {'hp': None, 'lp': None})
    flat_highs = mesh_transforms(hp_main) if groups and not any(g['hp'] for g in groups.values()) else []
    for name, group in groups.items():
        prefix = group.setdefault('prefix', base + '_' + name)
        highs = mesh_transforms(group['hp']) if group['hp'] else flat_highs
        group['hp_nodes'] = [node for node in highs if is_final_name(node, prefix, 'high')]
        group['lp_nodes'] = [node for node in lows if is_final_name(node, prefix, 'low')]
    return groups
