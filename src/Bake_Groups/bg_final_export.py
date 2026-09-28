# -*- coding: utf-8 -*-
from __future__ import print_function, division, absolute_import

import maya.cmds as cmds
import maya.mel as mel
import maya.api.OpenMaya as om
import bg_core
import bg_localization as bg_l10n
import re
import math
import contextlib
import os
import tempfile
import shutil

try:
    from PySide6 import QtWidgets, QtCore
except ImportError:
    from PySide2 import QtWidgets, QtCore

class FinalExportProcessor(object):
    _ALL_FACES = object()
    ISLAND_COLOR_SET = "BakeMaster_Island_ID"
    _fbx_session_depth = 0
    _fbx_previous_settings = None
    _viewport_session_depth = 0
    _viewport_was_suspended = False
    _undo_session_depth = 0
    _undo_was_enabled = False
    _owned_export_uuids = set()

    @classmethod
    def _own_export_node(cls, node):
        """Record identity, never a name pattern, before mutating a temporary node."""
        identifiers = cmds.ls(node, uuid=True) or []
        if len(identifiers) != 1:
            raise RuntimeError("Cannot establish export-copy ownership: {}".format(node))
        cls._owned_export_uuids.add(identifiers[0])
        return node

    @classmethod
    def _is_owned_export_node(cls, node):
        identifiers = cmds.ls(node, uuid=True) or []
        return len(identifiers) == 1 and identifiers[0] in cls._owned_export_uuids

    @classmethod
    def _duplicate_export_node(cls, node):
        duplicate = cmds.duplicate(node, returnRootsOnly=True)[0]
        cls._own_export_node(duplicate)
        return (cmds.ls(duplicate, long=True) or [duplicate])[0]

    @classmethod
    @contextlib.contextmanager
    def _fbx_settings_session(cls):
        """Apply FBX settings once for a complete export operation."""
        outermost = cls._fbx_session_depth == 0
        if outermost:
            if not cmds.pluginInfo('fbxmaya', query=True, loaded=True):
                cmds.loadPlugin('fbxmaya')
            cls._fbx_previous_settings = {
                "FBXExportInputConnections": cls._query_fbx_bool("FBXExportInputConnections"),
                "FBXExportGenerateLog": cls._query_fbx_bool("FBXExportGenerateLog"),
                "FBXExportSmoothMesh": cls._query_fbx_bool("FBXExportSmoothMesh"),
            }
            cls._set_fbx_bool("FBXExportGenerateLog", False)
            cls._set_fbx_bool("FBXExportInputConnections", False)
            cls._set_fbx_bool("FBXExportSmoothMesh", False)
        cls._fbx_session_depth += 1
        try:
            yield
        finally:
            cls._fbx_session_depth = max(0, cls._fbx_session_depth - 1)
            if outermost:
                for command, value in (cls._fbx_previous_settings or {}).items():
                    if value is not None:
                        cls._set_fbx_bool(command, value)
                cls._fbx_previous_settings = None

    @classmethod
    @contextlib.contextmanager
    def _viewport_session(cls):
        """Suspend redraw once for a single export or a whole batch."""
        outermost = cls._viewport_session_depth == 0
        if outermost:
            try:
                cls._viewport_was_suspended = bool(cmds.refresh(query=True, suspend=True))
            except Exception:
                cls._viewport_was_suspended = False
            if not cls._viewport_was_suspended:
                cmds.refresh(suspend=True)
        cls._viewport_session_depth += 1
        try:
            yield
        finally:
            cls._viewport_session_depth = max(0, cls._viewport_session_depth - 1)
            if outermost:
                if not cls._viewport_was_suspended:
                    try:
                        cmds.refresh(suspend=False)
                        cmds.refresh(force=True)
                    except Exception:
                        pass
                cls._viewport_was_suspended = False

    @classmethod
    @contextlib.contextmanager
    def _undo_session(cls):
        """Keep disposable export copies out of Maya's Undo history."""
        outermost = cls._undo_session_depth == 0
        if outermost:
            try:
                cls._undo_was_enabled = bool(cmds.undoInfo(query=True, state=True))
            except Exception:
                cls._undo_was_enabled = False
            if cls._undo_was_enabled:
                cmds.undoInfo(stateWithoutFlush=False)
        cls._undo_session_depth += 1
        try:
            yield
        finally:
            cls._undo_session_depth = max(0, cls._undo_session_depth - 1)
            if outermost:
                if cls._undo_was_enabled:
                    try:
                        cmds.undoInfo(stateWithoutFlush=True)
                    except Exception:
                        pass
                cls._undo_was_enabled = False

    @classmethod
    @contextlib.contextmanager
    def export_session(cls):
        """Reuse FBX, viewport, and undo setup across a complete export batch."""
        outermost = cls._fbx_session_depth == 0
        with cls._fbx_settings_session():
            with cls._viewport_session():
                with cls._undo_session():
                    if outermost:
                        cls._cleanup_zero_transform_hp_export_temps()
                    try:
                        yield
                    finally:
                        if outermost:
                            cls._cleanup_zero_transform_hp_export_temps()

    @staticmethod
    def _is_high_export_mesh(mesh_transform):
        from bg_final_groups import final_role
        # Normal export duplicates live below BG_HP_Export_Zero_Temp*.  Use
        # that provenance first; renamed high meshes do not necessarily keep
        # an ``_high`` suffix.
        if ("BG_HP_Export_Zero_Temp".lower() in str(mesh_transform).lower() and
                FinalExportProcessor._is_owned_export_node(mesh_transform)):
            return True
        return final_role(mesh_transform) == 'high'

    @staticmethod
    def _island_color(island_index):
        """Return a stable, non-repeating color for an island index.

        The previous 12-color table repeated quickly.  Golden-ratio hue
        stepping distributes colors around the wheel; the small saturation /
        value cycle also prevents a long export from degenerating into one
        repeated primary color while remaining deterministic for ID baking.
        """
        index = max(0, int(island_index))
        hue = (index * 0.618033988749895) % 1.0
        saturation = (0.78, 0.9, 0.68, 0.98)[index % 4]
        value = (1.0, 0.92, 0.84, 0.96)[(index // 4) % 4]
        h6 = hue * 6.0
        sector = int(h6) % 6
        fraction = h6 - int(h6)
        p = value * (1.0 - saturation)
        q = value * (1.0 - saturation * fraction)
        t = value * (1.0 - saturation * (1.0 - fraction))
        rgb = (
            (value, t, p), (q, value, p), (p, value, t),
            (p, q, value), (t, p, value), (value, p, q),
        )[sector]
        return tuple(round(float(channel), 6) for channel in rgb)

    @staticmethod
    def _assign_independent_island_vertex_colors(meshes):
        """Color every disconnected topology island on temporary HP copies.

        This deliberately operates only on export duplicates, so enabling the
        option never changes the user's source meshes or their material slots.
        """
        island_index = 0
        colored_meshes = 0
        for mesh_number, mesh in enumerate(sorted(set(meshes or []), key=str)):
            if not FinalExportProcessor._is_high_export_mesh(mesh) or not cmds.objExists(mesh):
                continue
            if not FinalExportProcessor._is_owned_export_node(mesh):
                raise RuntimeError("Refusing to color a source mesh: {}".format(mesh))
            shapes = cmds.listRelatives(mesh, shapes=True, fullPath=True, type="mesh") or []
            shape = next((s for s in shapes if not cmds.getAttr(s + ".intermediateObject")), None)
            if not shape:
                continue
            try:
                selection = om.MSelectionList()
                selection.add(shape)
                mesh_fn = om.MFnMesh(selection.getDagPath(0))
                vertex_count = int(mesh_fn.numVertices)
                if not vertex_count:
                    continue
                face_vertices = []
                vertex_to_faces = {}
                iterator = om.MItMeshPolygon(selection.getDagPath(0))
                while not iterator.isDone():
                    face_id = int(iterator.index())
                    verts = [int(v) for v in iterator.getVertices()]
                    face_vertices.append(verts)
                    for vertex_id in verts:
                        vertex_to_faces.setdefault(vertex_id, []).append(face_id)
                    iterator.next()
                visited = set()
                vertex_island = [-1] * vertex_count
                for start_face in range(len(face_vertices)):
                    if start_face in visited:
                        continue
                    queue = [start_face]
                    visited.add(start_face)
                    island_vertices = set()
                    while queue:
                        face_id = queue.pop()
                        verts = face_vertices[face_id]
                        island_vertices.update(verts)
                        for vertex_id in verts:
                            for next_face in vertex_to_faces.get(vertex_id, []):
                                if next_face not in visited:
                                    visited.add(next_face)
                                    queue.append(next_face)
                    for vertex_id in island_vertices:
                        if 0 <= vertex_id < vertex_count:
                            vertex_island[vertex_id] = island_index
                    island_index += 1

                # Isolated/unreferenced vertices are still assigned a color so
                # every disconnected vertex receives an ID as requested.
                for vertex_id, value in enumerate(vertex_island):
                    if value < 0:
                        vertex_island[vertex_id] = island_index
                        island_index += 1

                try:
                    mesh_fn.deleteColorSet(FinalExportProcessor.ISLAND_COLOR_SET)
                except Exception:
                    pass
                # Use the command layer as the authoritative writer.  This is
                # the most consistent path in Maya 2024.2/FBX 2020.3.4; the
                # API color-set methods can report success without attaching
                # per-vertex data on some API builds.
                try:
                    existing_sets = cmds.polyColorSet(shape, query=True, allColorSets=True) or []
                    if FinalExportProcessor.ISLAND_COLOR_SET in existing_sets:
                        cmds.polyColorSet(shape, delete=True, colorSet=FinalExportProcessor.ISLAND_COLOR_SET)
                    cmds.polyColorSet(shape, create=True, colorSet=FinalExportProcessor.ISLAND_COLOR_SET, representation="RGB")
                    # Some Maya FBX 2020 exporters only serialize the first or
                    # default color set. Keep a compatibility mirror there.
                    if "colorSet1" not in (cmds.polyColorSet(shape, query=True, allColorSets=True) or []):
                        cmds.polyColorSet(shape, create=True, colorSet="colorSet1", representation="RGB")
                    cmds.polyColorSet(shape, currentColorSet=True, colorSet=FinalExportProcessor.ISLAND_COLOR_SET)
                except Exception as color_set_exc:
                    raise RuntimeError("cannot create color set '{}': {}".format(FinalExportProcessor.ISLAND_COLOR_SET, color_set_exc))
                # Maya 2024.2 is most reliable when colors are written in
                # batches of vertices while the requested set is current.
                written = False
                # The color set can exist while viewport/export display is
                # disabled.  Explicitly enable display colors on the temporary
                # export shape; this is also honored by FBX in Maya versions
                # that use the shape flag as the export gate.
                if cmds.attributeQuery("displayColors", node=shape, exists=True):
                    cmds.setAttr(shape + ".displayColors", True)
                try:
                    island_vertices = {}
                    for vertex_id, group_id in enumerate(vertex_island):
                        island_vertices.setdefault(group_id, []).append(vertex_id)
                    cmds.polyColorSet(shape, currentColorSet=True, colorSet=FinalExportProcessor.ISLAND_COLOR_SET)
                    for group_id, vertex_list in sorted(island_vertices.items()):
                        # group_id is already the global topology-island ID;
                        # do not add mesh_number, which created an even-only
                        # sequence and caused premature palette repetition.
                        color_index = int(group_id)
                        rgb = FinalExportProcessor._island_color(color_index)
                        components = ["{}.vtx[{}]".format(shape, vertex_id) for vertex_id in vertex_list]
                        # Match the proven Create Asset > Color HP path:
                        # select the components, then color the current set.
                        cmds.select(components, replace=True)
                        for color_set in (FinalExportProcessor.ISLAND_COLOR_SET, "colorSet1"):
                            cmds.polyColorSet(shape, currentColorSet=True, colorSet=color_set)
                            cmds.polyColorPerVertex(rgb=rgb, colorDisplayOption=True, notUndoable=True)
                    cmds.polyColorSet(shape, currentColorSet=True, colorSet=FinalExportProcessor.ISLAND_COLOR_SET)
                    cmds.select(clear=True)
                    try:
                        cmds.polyOptions(shape, colorShadedDisplay=True, colorMaterialChannel="ambientDiffuse")
                    except Exception:
                        pass
                    # An empty color set is not proof that Maya accepted the data.
                    # Reacquire the function set after command-layer writes.
                    verify_fn = om.MFnMesh(selection.getDagPath(0))
                    palette = {group_id: FinalExportProcessor._island_color(group_id)
                               for group_id in island_vertices}
                    for color_set in (FinalExportProcessor.ISLAND_COLOR_SET, "colorSet1"):
                        actual = verify_fn.getVertexColors(colorSet=color_set)
                        if len(actual) != vertex_count:
                            raise RuntimeError("Incomplete vertex colors in {}".format(color_set))
                        for vertex_id, group_id in enumerate(vertex_island):
                            expected = palette[group_id]
                            color = actual[vertex_id]
                            if any(abs(float(color[channel]) - expected[channel]) > 0.0001
                                   for channel in range(3)):
                                raise RuntimeError("Vertex color verification failed in {} at vertex {}".format(
                                    color_set, vertex_id))
                    written = True
                except Exception as write_exc:
                    raise RuntimeError("Maya island vertex-color write failed for '{}': {}".format(mesh, write_exc))
                if not written:
                    raise RuntimeError("Island vertex color set was not written on '{}'".format(mesh))
                colored_meshes += 1
            except Exception as exc:
                raise RuntimeError("Could not assign island vertex colors to '{}': {}".format(mesh, exc))
        return colored_meshes, island_index

    @staticmethod
    def _query_fbx_bool(command):
        try:
            return bool(mel.eval("{} -q;".format(command)))
        except Exception:
            return None

    @staticmethod
    def _set_fbx_bool(command, value):
        try:
            mel.eval("{} -v {};".format(command, "true" if value else "false"))
        except Exception:
            pass

    @staticmethod
    def export_selected_fbx(export_path):
        """Stage under the requested basename, then publish one complete FBX.

        Some Maya/FBX builds rewrite dot-prefixed temporary filenames. Keep the
        real filename and isolate temporary output in a same-volume directory;
        an explicit returned filename is accepted only inside that directory.
        """
        destination = os.path.abspath(export_path)
        parent = os.path.dirname(destination)
        parent_real = os.path.normcase(os.path.realpath(parent))
        staging = tempfile.mkdtemp(prefix='BakeMasterExport-', dir=parent)
        staging_real = os.path.normcase(os.path.realpath(staging))
        requested = os.path.join(staging, os.path.basename(destination))
        try:
            if FinalExportProcessor._fbx_session_depth:
                returned = cmds.file(requested.replace('\\', '/'), force=True,
                                     type="FBX export", exportSelected=True)
            else:
                with FinalExportProcessor._fbx_settings_session():
                    returned = cmds.file(requested.replace('\\', '/'), force=True,
                                         type="FBX export", exportSelected=True)
            if returned is not None and not isinstance(returned, str):
                raise RuntimeError('Maya returned an unexpected FBX output path')
            actual = returned or requested
            if not os.path.isabs(actual):
                actual = os.path.join(staging, actual)
            actual = os.path.abspath(actual)
            actual_real = os.path.normcase(os.path.realpath(actual))
            try:
                contained = os.path.commonpath((staging_real, actual_real)) == staging_real
            except ValueError:
                contained = False
            if not contained or actual_real == staging_real or os.path.islink(actual):
                raise RuntimeError('Maya returned an FBX path outside its temporary export directory')
            if not os.path.isfile(actual) or os.path.getsize(actual) == 0:
                raise RuntimeError('Maya did not produce a nonempty FBX file')
            os.replace(actual, destination)
        finally:
            # Remove only this invocation's directory, including any renamed
            # partial FBX or auxiliary file. Never scan/delete the user's output.
            try:
                if os.path.islink(staging):
                    os.unlink(staging)
                elif os.path.exists(staging):
                    resolved = os.path.normcase(os.path.realpath(staging))
                    if (resolved != staging_real or resolved == parent_real or
                            os.path.dirname(resolved) != parent_real):
                        raise RuntimeError('Temporary export directory changed during export')
                    shutil.rmtree(staging)
            except (OSError, RuntimeError) as cleanup_error:
                cmds.warning('Could not clean temporary export directory {}: {}'.format(
                    staging, cleanup_error))

    @staticmethod
    def export_meshes_fbx(meshes, export_path, triangulate_low=False, triangulate_all=False):
        """Triangulate disposable LP/Cage copies and omit internal helper parents."""
        from bg_final_groups import final_role
        previous = cmds.ls(selection=True, long=True) or []
        owned_before = set(FinalExportProcessor._owned_export_uuids)
        try:
            with FinalExportProcessor.export_session():
                prepared = []
                for mesh in meshes:
                    if not cmds.objExists(mesh):
                        raise RuntimeError('Export mesh disappeared: {}'.format(mesh))
                    node = mesh
                    if triangulate_all or (triangulate_low and final_role(mesh) == 'low'
                                           and not FinalExportProcessor._is_high_export_mesh(mesh)):
                        if not FinalExportProcessor._is_owned_export_node(node):
                            node = FinalExportProcessor._duplicate_export_node(node)
                        cmds.polyTriangulate(node, constructionHistory=False)
                    if FinalExportProcessor._is_owned_export_node(node):
                        short = mesh.split('|')[-1]
                        node = cmds.parent(node, world=True, absolute=True)[0]
                        node = cmds.rename(node, short)
                        node = (cmds.ls(node, long=True) or [node])[0]
                    prepared.append(node)
                if not prepared:
                    raise RuntimeError('No meshes available for FBX export')
                cmds.select(prepared, replace=True)
                FinalExportProcessor.export_selected_fbx(export_path)
                return True
        finally:
            created = FinalExportProcessor._owned_export_uuids - owned_before
            nodes = [node for identifier in created for node in (cmds.ls(identifier, long=True) or [])]
            FinalExportProcessor._cleanup_zero_transform_hp_export_temps(nodes)
            restored = [node for node in previous if cmds.objExists(node)]
            cmds.select(restored, replace=True) if restored else cmds.select(clear=True)

    @staticmethod
    def _is_zbrush_mesh(mesh_transform):
        if not mesh_transform or not cmds.objExists(mesh_transform):
            return False

        shapes = cmds.listRelatives(mesh_transform, shapes=True, fullPath=True) or []

        layers = cmds.listConnections(mesh_transform, type="displayLayer") or []
        for shape in shapes:
            layers.extend(cmds.listConnections(shape, type="displayLayer") or [])

        return any(layer and "zbrush" in layer.lower() for layer in layers)

    @staticmethod
    def _smooth_level_from_item(item):
        if 'smooth_level' in item:
            try:
                return int(item.get('smooth_level') or 0)
            except Exception:
                return 0
        combo = item.get('combo')
        if combo:
            return combo.currentIndex()
        return 0

    @staticmethod
    def _smooth_level_from_states(smooth_states, base_name, prefix, default_level=0):
        if not smooth_states:
            return default_level

        exact_prefix = str(prefix).strip()
        exact_base = str(base_name).strip() + '_'
        exact_keys = [exact_prefix, 'prefix:' + exact_prefix]
        if exact_prefix.startswith(exact_base):
            exact_keys.insert(0, exact_prefix[len(exact_base):])
        for key in exact_keys:
            if key in smooth_states:
                try:
                    return max(0, min(3, int(smooth_states[key])))
                except Exception:
                    return default_level

        prefix_key = str(prefix).strip().lower()
        base_prefix = str(base_name).strip().lower() + "_"
        keys = [prefix_key, "prefix:{}".format(prefix_key)]
        if prefix_key.startswith(base_prefix):
            short_key = prefix_key[len(base_prefix):]
            keys.extend([short_key, "prefix:{}".format(short_key)])

        lower_map = {}
        for key, value in smooth_states.items():
            lower_map[str(key).strip().lower()] = value

        for key in keys:
            value = lower_map.get(str(key).strip().lower())
            if value is not None:
                try:
                    return max(0, min(3, int(value)))
                except Exception:
                    return default_level
        return default_level

    @staticmethod
    def _resolve_child_under_parent(node, parent):
        parent_long = cmds.ls(parent, long=True)
        parent_long = parent_long[0] if parent_long else parent
        node_short = node.split('|')[-1]

        matches = cmds.ls(node, long=True) or []
        for match in matches:
            if match.startswith(parent_long + "|"):
                return match

        children = cmds.listRelatives(parent_long, children=True, fullPath=True, type='transform') or []
        for child in children:
            if child.split('|')[-1] == node_short:
                return child

        return matches[0] if matches else node

    @staticmethod
    def _prepare_hp_copy(entry, temp_root, reusable):
        mesh, level = entry['mesh'], entry['level']
        if mesh in reusable:
            if not FinalExportProcessor._is_owned_export_node(mesh):
                raise RuntimeError("Unowned reusable export mesh: {}".format(mesh))
            duplicate = mesh
        else:
            duplicate = FinalExportProcessor._duplicate_export_node(mesh)
        if level and not entry['zbrush']:
            cmds.polySmooth(duplicate, divisions=level, keepBorder=False, constructionHistory=False)
        duplicate = cmds.parent(duplicate, temp_root, absolute=True)[0]
        duplicate = cmds.rename(duplicate, entry['short'])
        duplicate = FinalExportProcessor._resolve_child_under_parent(duplicate, temp_root)
        if entry['zbrush']:
            for shape in cmds.listRelatives(duplicate, shapes=True, fullPath=True) or []:
                if cmds.objExists(shape + '.displaySmoothMesh'):
                    cmds.setAttr(shape + '.displaySmoothMesh', 0)
        cmds.makeIdentity(duplicate, apply=True, t=True, r=True, s=True, n=False, pn=True)
        return duplicate

    @staticmethod
    def _combine_hp_copies(entries, temp_root):
        """One command per subgroup; input and output nodes remain operation-owned."""
        created = []
        try:
            copies = cmds.duplicate([entry['mesh'] for entry in entries], returnRootsOnly=True) or []
            for node in copies:
                FinalExportProcessor._own_export_node(node)
                created.append(node)
            if len(copies) != len(entries):
                raise RuntimeError("Unexpected HP duplicate count")
            combined = cmds.polyUnite(copies, constructionHistory=False, mergeUVSets=1)[0]
            FinalExportProcessor._own_export_node(combined)
            created.append(combined)
            level = entries[0]['level']
            if level:
                cmds.polySmooth(combined, divisions=level, keepBorder=False, constructionHistory=False)
            combined = cmds.parent(combined, temp_root, absolute=True)[0]
            # Names are case-preserving; find the marker without changing the prefix.
            marker = entries[0]['short'].lower().rfind('_high')
            name = entries[0]['short'][:marker] + '_high'
            combined = cmds.rename(combined, name)
            combined = FinalExportProcessor._resolve_child_under_parent(combined, temp_root)
            cmds.makeIdentity(combined, apply=True, t=True, r=True, s=True, n=False, pn=True)
            return combined
        except Exception:
            FinalExportProcessor._cleanup_zero_transform_hp_export_temps(created)
            raise

    @staticmethod
    def _make_zero_transform_hp_export_copies(meshes, smooth_levels=None,
                                            reusable_temp_nodes=None,
                                            combine_non_zbrush=False,
                                            hp_metadata=None):
        """Prepare owned copies; never substitute a source object after failure."""
        from bg_final_groups import final_role
        if not meshes:
            return [], None
        smooth_levels = smooth_levels or {}
        reusable = set(reusable_temp_nodes or [])
        metadata = hp_metadata or {}
        temp_root = cmds.group(em=True, name='BG_HP_Export_Zero_Temp#', world=True)
        FinalExportProcessor._own_export_node(temp_root)
        temp_root = (cmds.ls(temp_root, long=True) or [temp_root])[0]
        prepared, groups = [], {}
        try:
            for mesh in meshes:
                if not mesh or not cmds.objExists(mesh):
                    raise RuntimeError("Export mesh disappeared: {}".format(mesh))
                short = mesh.split('|')[-1]
                full = (cmds.ls(mesh, long=True) or [mesh])[0]
                role = final_role(short)
                if (role != 'high' and mesh not in metadata and full not in metadata
                        and not FinalExportProcessor._is_high_export_mesh(mesh)):
                    prepared.append(mesh)
                    continue
                info = metadata.get(mesh, metadata.get(full, {}))
                zbrush = info.get('zbrush')
                if zbrush is None:
                    zbrush = FinalExportProcessor._is_zbrush_mesh(mesh)
                marker = short.lower().rfind('_high') if role == 'high' else -1
                prefix = short[:marker] if marker >= 0 else full
                level = info.get('smooth_level')
                if level is None:
                    level = smooth_levels.get(full, smooth_levels.get(short,
                        smooth_levels.get(short.lower(), smooth_levels.get('prefix:' + prefix,
                            smooth_levels.get('prefix:' + prefix.lower(), 0)))))
                level = 0 if zbrush else max(0, min(3, int(level or 0)))
                entry = {'mesh': full, 'short': short, 'level': level, 'zbrush': zbrush}
                # Different requested smoothing levels must never be merged together.
                key = (prefix, level) if (combine_non_zbrush and not zbrush and
                    full not in reusable and role == 'high') else (full,)
                groups.setdefault(key, []).append(entry)
            for key in sorted(groups, key=str):
                entries = groups[key]
                if len(entries) > 1:
                    try:
                        prepared.append(FinalExportProcessor._combine_hp_copies(entries, temp_root))
                        continue
                    except Exception as exc:
                        cmds.warning("HP combine failed; preparing independent copies: {}".format(exc))
                for entry in entries:
                    prepared.append(FinalExportProcessor._prepare_hp_copy(entry, temp_root, reusable))
            return prepared, temp_root
        except Exception:
            # Includes copies that failed before parenting, tracked by UUID.
            FinalExportProcessor._cleanup_zero_transform_hp_export_temps()
            raise

    @staticmethod
    def _cleanup_zero_transform_hp_export_temps(extra_nodes=None):
        """Delete only identities created by this processor, never name matches."""
        identifiers = set(FinalExportProcessor._owned_export_uuids)
        if extra_nodes is not None:
            requested = set()
            for node in extra_nodes:
                if node and cmds.objExists(node):
                    requested.update(cmds.ls(node, uuid=True) or [])
            identifiers.intersection_update(requested)
        for identifier in identifiers:
            nodes = cmds.ls(identifier, long=True) or []
            if not nodes:
                FinalExportProcessor._owned_export_uuids.discard(identifier)
                continue
            try:
                cmds.delete(nodes)
            except Exception as exc:
                cmds.warning("Could not clean temporary export node {}: {}".format(nodes[0], exc))
            if not (cmds.ls(identifier, long=True) or []):
                FinalExportProcessor._owned_export_uuids.discard(identifier)

    @staticmethod
    def _cleanup_empty_final_lp_groups(global_lp_root="LP_Combine_BG"):
        """Remove empty chapter containers owned by the final LP output root."""
        if not global_lp_root or not cmds.objExists(global_lp_root):
            return 0

        removed = 0
        chapter_groups = cmds.listRelatives(
            global_lp_root, children=True, fullPath=True, type='transform'
        ) or []
        for chapter_group in chapter_groups:
            if not chapter_group or not cmds.objExists(chapter_group):
                continue
            children = cmds.listRelatives(
                chapter_group, children=True, fullPath=True
            ) or []
            if children:
                continue
            try:
                cmds.delete(chapter_group)
                removed += 1
            except Exception:
                pass

        if cmds.objExists(global_lp_root):
            remaining = cmds.listRelatives(
                global_lp_root, children=True, fullPath=True
            ) or []
            if not remaining:
                try:
                    cmds.delete(global_lp_root)
                    removed += 1
                except Exception:
                    pass
        return removed


    @staticmethod
    def combine_all_subgroups(base_name, hp_main, lp_main, parent_window=None):
        """Final combine and rename logic."""
        from bg_final_groups import subgroup_name

        if not hp_main or not lp_main:
            cmds.warning("Bake Master: HP or LP root objects not found.")
            return {'success': False, 'hp': 0, 'lp': 0}

        progress_dlg = QtWidgets.QProgressDialog(bg_l10n.text("Baking Subgroups..."), bg_l10n.text("Cancel"), 0, 100, parent_window)
        progress_dlg.setWindowModality(QtCore.Qt.WindowModal)
        progress_dlg.show()

        # Combine replaces the final meshes. Carry forward display choices
        # by their exact output names while preserving the existing parents.
        output_visibility = {}
        chapter_before = '|LP_Combine_BG|' + base_name
        if cmds.objExists(chapter_before):
            for node in cmds.listRelatives(chapter_before, children=True, fullPath=True, type='transform') or []:
                shapes = cmds.listRelatives(node, shapes=True, fullPath=True, type='mesh', noIntermediate=True) or []
                if shapes:
                    output_visibility[node.rsplit('|', 1)[-1]] = (
                        cmds.getAttr(node + '.visibility'),
                        [cmds.getAttr(shape + '.visibility') for shape in shapes])

        with bg_core.undo_chunk("CombineAndRenameWithBase"):
            try:
                hp_count = 0
                lp_count = 0
                FinalExportProcessor._cleanup_empty_final_lp_groups()

                # --- HP LOGIC (Renaming inside subgroups) ---
                hp_subgroups = [g for g in cmds.listRelatives(hp_main, children=True, fullPath=True, type='transform') or []
                                if not cmds.listRelatives(g, shapes=True)]

                for sg in hp_subgroups:
                    sg_name = subgroup_name(sg, 'HP').replace(".", "_")
                    transforms = FinalExportProcessor.get_valid_mesh_transforms(sg)

                    for i, tr in enumerate(transforms):
                        new_name = "{}_{}_high_{:03d}".format(base_name, sg_name, i + 1).replace(".", "_")
                        cmds.rename(tr, new_name)
                        hp_count += 1

                # --- LP LOGIC (Combining in the global LP_Combine_BG root) ---
                def _has_mesh_shape(node):
                    return bool(cmds.listRelatives(node, shapes=True, type='mesh') or [])

                def _is_lp_subgroup(node):
                    short_name = node.split('|')[-1]
                    suffix_re = re.escape(bg_core.BakeConfig.SUFFIX_LP) + r'\d*$'
                    is_lp_group = bool(re.search(suffix_re, short_name, re.IGNORECASE))
                    attr = "{}.{}".format(node, bg_core.BakeConfig.ATTR_BAKE_GROUP)
                    if cmds.objExists(attr):
                        try:
                            is_lp_group = is_lp_group or cmds.getAttr(attr) == "LP"
                        except Exception:
                            pass
                    return is_lp_group

                def _clean_lp_group_name(node):
                    return subgroup_name(node, 'LP').replace(".", "_")

                def _is_old_chapter_lp_output(node, chapter_base):
                    if not node or not cmds.objExists(node):
                        return False
                    if not cmds.listRelatives(node, shapes=True, type='mesh'):
                        return False
                    short_name = node.split('|')[-1].replace(".", "_")
                    base = re.escape(chapter_base.replace(".", "_"))
                    pattern = r'^{}(?:_.+)?_low\d*$'.format(base)
                    return bool(re.match(pattern, short_name, re.IGNORECASE))

                def _cleanup_old_chapter_lp_outputs(chapter_lp_root, chapter_base):
                    old_outputs = [
                        child for child in (cmds.listRelatives(chapter_lp_root, children=True, fullPath=True, type='transform') or [])
                        if _is_old_chapter_lp_output(child, chapter_base)
                    ]
                    if old_outputs:
                        cmds.delete(old_outputs)
                    return len(old_outputs)

                def _combine_lp_transforms(transforms, final_lp_name, chapter_lp_root):
                    if not transforms:
                        return False

                    old_finals = [child for child in (cmds.listRelatives(chapter_lp_root, children=True, fullPath=True, type='transform') or [])
                                  if child.split('|')[-1].startswith(final_lp_name) and cmds.listRelatives(child, shapes=True)]
                    if old_finals:
                        cmds.delete(old_finals)

                    dups = cmds.duplicate(transforms, returnRootsOnly=True)
                    if len(dups) > 1:
                        combined = cmds.polyUnite(dups, ch=False, mergeUVSets=True)[0]
                    else:
                        combined = dups[0]

                    cmds.delete(combined, constructionHistory=True)
                    cmds.polyTriangulate(combined, constructionHistory=False)
                    combined = cmds.rename(combined, final_lp_name)

                    current_parent = cmds.listRelatives(combined, parent=True, fullPath=True)
                    chapter_lp_full = cmds.ls(chapter_lp_root, l=True)[0]
                    if not current_parent or current_parent[0] != chapter_lp_full:
                        combined = cmds.parent(combined, chapter_lp_full, absolute=True)[0]
                    combined = FinalExportProcessor._resolve_child_under_parent(combined, chapter_lp_full)
                    previous_display = output_visibility.get(final_lp_name)
                    if previous_display is not None:
                        if cmds.getAttr(combined + '.visibility') != previous_display[0]:
                            cmds.setAttr(combined + '.visibility', previous_display[0])
                        shapes = cmds.listRelatives(combined, shapes=True, fullPath=True, type='mesh', noIntermediate=True) or []
                        for shape, value in zip(shapes, previous_display[1]):
                            if cmds.getAttr(shape + '.visibility') != value:
                                cmds.setAttr(shape + '.visibility', value)
                    return True

                lp_children = cmds.listRelatives(lp_main, children=True, fullPath=True, type='transform') or []
                lp_subgroups = [g for g in lp_children if not _has_mesh_shape(g) and _is_lp_subgroup(g)]
                direct_lp_meshes = [g for g in lp_children if _has_mesh_shape(g)]

                if not lp_subgroups:
                    lp_subgroups = [g for g in lp_children if not _has_mesh_shape(g)]

                # Создаем или находим глобальный рут
                has_valid_lp_source = bool(direct_lp_meshes)
                if not has_valid_lp_source:
                    for subgroup in lp_subgroups:
                        subgroup_meshes = cmds.listRelatives(
                            subgroup,
                            allDescendents=True,
                            type='mesh',
                            fullPath=True
                        ) or []
                        if subgroup_meshes:
                            has_valid_lp_source = True
                            break

                global_lp_root = "LP_Combine_BG"
                existing_chapter_root = "{}|{}".format(
                    global_lp_root, base_name
                )
                if not has_valid_lp_source:
                    if cmds.objExists(existing_chapter_root):
                        _cleanup_old_chapter_lp_outputs(
                            existing_chapter_root, base_name
                        )
                    FinalExportProcessor._cleanup_empty_final_lp_groups(
                        global_lp_root
                    )
                    return {'success': True, 'hp': hp_count, 'lp': 0}

                if not cmds.objExists(global_lp_root):
                    cmds.group(em=True, name=global_lp_root, world=True)

                # Создаем или находим папку главы
                chapter_lp_root = "{}|{}".format(global_lp_root, base_name)
                if not cmds.objExists(chapter_lp_root):
                    chapter_lp_root = cmds.group(
                        em=True, name=base_name, parent=global_lp_root
                    )
                chapter_lp_root = (
                    cmds.ls(chapter_lp_root, long=True) or [chapter_lp_root]
                )[0]

                _cleanup_old_chapter_lp_outputs(chapter_lp_root, base_name)

                for sg in lp_subgroups:
                    sg_name = _clean_lp_group_name(sg)
                    final_lp_name = "{}_{}_low".format(base_name, sg_name).replace(".", "_")

                    # Ищем старые финалки уже в новой папке главы
                    meshes = cmds.listRelatives(sg, allDescendents=True, type='mesh', fullPath=True) or []
                    if not meshes: continue
                    transforms = sorted(set(
                        cmds.listRelatives(m, parent=True, fullPath=True)[0]
                        for m in meshes
                    ), key=str)
                    if not transforms: continue

                    if _combine_lp_transforms(transforms, final_lp_name, chapter_lp_root):
                        lp_count += 1

                if lp_count == 0 and direct_lp_meshes:
                    final_lp_name = "{}_low".format(base_name).replace(".", "_")
                    if _combine_lp_transforms(direct_lp_meshes, final_lp_name, chapter_lp_root):
                        lp_count += 1

            except Exception as e:
                cmds.warning("Combine error: {}".format(e))
                FinalExportProcessor._cleanup_empty_final_lp_groups()
                return {'success': False, 'hp': 0, 'lp': 0}
            finally:
                FinalExportProcessor._cleanup_empty_final_lp_groups()
                progress_dlg.close()

        return {'success': True, 'hp': hp_count, 'lp': lp_count}

    @staticmethod
    def process_final_group(base_name, hp_main, final_mesh_widgets):
        """Smooth final meshes and merge with ZBrush."""
        with bg_core.undo_chunk("ProcessFinalGroup"):
            for item in final_mesh_widgets:
                full_prefix = item['full_prefix']
                level = item['combo'].currentIndex()

                if level > 0:
                    hp_meshes = cmds.listRelatives(hp_main, children=True, fullPath=True) or []
                    for hp in hp_meshes:
                        short_name = hp.split('|')[-1]
                        if short_name.startswith(full_prefix + "_high") and cmds.objExists(hp):
                            if FinalExportProcessor._is_zbrush_mesh(hp):
                                cmds.warning("Skipped smooth for '{}' (ZBrush geometry)".format(hp))
                                continue
                            try:
                                cmds.polySmooth(hp, divisions=level, constructionHistory=False)
                            except Exception as e:
                                cmds.warning("Failed to smooth {}: {}".format(hp, e))

            hp_target = "Bake_Groups|{}|HP".format(base_name)
            if cmds.objExists(hp_target):
                smooth_meshes = cmds.ls("{}|Bake_Smooth_*".format(hp_target), type='transform', fullPath=True) or []
                for sm in smooth_meshes:
                    if not cmds.objExists(sm): continue

                    base_node_name = sm.split('|')[-1].replace("Bake_Smooth_", "Bake_")
                    zb_mesh = "{}|{}".format(hp_target, base_node_name)

                    if cmds.objExists(zb_mesh):
                        zb_short = zb_mesh.split('|')[-1]
                        combined = cmds.polyUnite([sm, zb_mesh], ch=False)[0]
                        cmds.delete(combined, constructionHistory=True)
                        cmds.xform(combined, cp=True)
                        combined = cmds.rename(combined, zb_short)
                        cmds.parent(combined, hp_target, absolute=True)
                    else:
                        new_name = sm.split('|')[-1].replace("Bake_Smooth_", "Bake_")
                        cmds.rename(sm, new_name)

    @staticmethod
    def get_valid_mesh_transforms(root_node):
        """Strict collection of only valid geometry for export."""
        if not cmds.objExists(root_node): return []
        shapes = cmds.listRelatives(root_node, allDescendents=True, fullPath=True, type='mesh') or []
        valid_shapes = [s for s in shapes if not cmds.getAttr(s + ".intermediateObject")]
        transforms = set()
        for shape in valid_shapes:
            parents = cmds.listRelatives(shape, parent=True, fullPath=True) or []
            if parents:
                transforms.add(parents[0])
        return sorted(transforms, key=str)

    @staticmethod
    def _get_mesh_materials_and_faces(mesh_transform):
        """Return stable shading-group face assignments without expanding whole-object ranges."""
        shapes = cmds.listRelatives(mesh_transform, shapes=True, fullPath=True)
        if not shapes: return {}
        shape = shapes[0]

        # API 2.0 reports one shader index per polygon directly.  This avoids
        # querying every shading set member and never materializes
        # list(range(total_faces)) for the common one-material case.
        try:
            selection = om.MSelectionList()
            selection.add(shape)
            dag_path = selection.getDagPath(0)
            mesh_fn = om.MFnMesh(dag_path)
            shaders, face_shader_indices = mesh_fn.getConnectedShaders(
                dag_path.instanceNumber()
            )
            shader_names = [
                om.MFnDependencyNode(shader_object).name()
                for shader_object in shaders
            ]
            used_shader_indices = set()
            assigned_face_count = 0
            for shader_index in face_shader_indices:
                if 0 <= shader_index < len(shader_names):
                    used_shader_indices.add(int(shader_index))
                    assigned_face_count += 1
            if len(used_shader_indices) == 1 and assigned_face_count == int(mesh_fn.numPolygons):
                only_shader_index = next(iter(used_shader_indices))
                return {
                    shader_names[only_shader_index]: FinalExportProcessor._ALL_FACES
                }

            api_assignments = dict((name, []) for name in shader_names)
            for face_index, shader_index in enumerate(face_shader_indices):
                if 0 <= shader_index < len(shader_names):
                    api_assignments[shader_names[shader_index]].append(face_index)
            api_assignments = dict(
                (name, api_assignments[name])
                for name in sorted(api_assignments.keys(), key=str)
                if api_assignments[name]
            )
            if api_assignments:
                return api_assignments
        except Exception:
            pass

        sgs = sorted(set(cmds.listConnections(shape, type='shadingEngine') or []), key=str)
        mat_dict = {}

        for sg in sgs:
            members = cmds.sets(sg, q=True) or []
            mesh_components = []

            # 1. Filter only the set elements belonging to our mesh
            for m in members:
                m_long = cmds.ls(m, long=True)
                if not m_long: continue
                m_path = m_long[0]

                if m_path == mesh_transform or m_path == shape:
                    mesh_components.append(m_path)
                elif m_path.startswith(mesh_transform + ".") or m_path.startswith(shape + "."):
                    mesh_components.append(m_path)

            if not mesh_components:
                continue

            # If the material is assigned to the entire object
            if mesh_transform in mesh_components or shape in mesh_components:
                mat_dict[sg] = FinalExportProcessor._ALL_FACES
                continue

            # 2. Convert components strictly to faces (solves issues with f[*], f[0:5], etc.)
            try:
                faces = cmds.polyListComponentConversion(mesh_components, toFace=True)
                faces_flat = cmds.ls(faces, flatten=True, long=True) or []

                valid_indices = set()
                for item in faces_flat:
                    match = re.search(r'\.f\[(\d+)\]', item)
                    if match:
                        valid_indices.add(int(match.group(1)))

                if valid_indices:
                    mat_dict[sg] = sorted(valid_indices)
            except Exception as e:
                cmds.warning("Error parsing faces for material {}: {}".format(sg, e))

        return mat_dict

    @staticmethod
    def _process_multimaterial_mesh(lp_mesh, hp_meshes, mat_dict):
        """Duplicates multi-material mesh, cleans faces, shifts UV, and duplicates HP."""
        new_lp_meshes = []
        new_hp_meshes = []
        created_nodes = []

        try:
            idx = 1
            for sg in sorted(mat_dict.keys(), key=str):
                face_indices = mat_dict[sg]
                if face_indices is not FinalExportProcessor._ALL_FACES and not face_indices:
                    continue

                # 1. Duplicate LP
                dup_lp = FinalExportProcessor._duplicate_export_node(lp_mesh)
                created_nodes.append(dup_lp)
                short_name = lp_mesh.split('|')[-1]
                new_lp_name = cmds.rename(dup_lp, "{}_mat{}".format(short_name, idx))
                created_nodes[-1] = new_lp_name
                new_lp_full = cmds.ls(new_lp_name, long=True)[0]
                created_nodes[-1] = new_lp_full

                # 2. Clean extra faces
                total_faces = cmds.polyEvaluate(new_lp_full, face=True)
                faces_to_delete = []
                if face_indices is not FinalExportProcessor._ALL_FACES:
                    faces_to_keep = set(face_indices)
                    delete_range_start = None
                    for face_index in range(total_faces):
                        should_delete = face_index not in faces_to_keep
                        if should_delete and delete_range_start is None:
                            delete_range_start = face_index
                        if delete_range_start is not None and (
                                not should_delete or face_index == total_faces - 1):
                            delete_range_end = face_index if should_delete else face_index - 1
                            if delete_range_start == delete_range_end:
                                component = "{}.f[{}]".format(new_lp_full, delete_range_start)
                            else:
                                component = "{}.f[{}:{}]".format(
                                    new_lp_full, delete_range_start, delete_range_end
                                )
                            faces_to_delete.append(component)
                            delete_range_start = None

                if faces_to_delete:
                    cmds.delete(faces_to_delete)

                # 3. Shift UV into 0-1 tile (UDIM)
                uvs = cmds.ls("{}.map[*]".format(new_lp_full), flatten=True)
                if uvs:
                    try:
                        # polyEvaluate on the whole object is safer than on a giant list of strings
                        uv_bbox = cmds.polyEvaluate(new_lp_full, boundingBoxComponent2d=True)
                        if uv_bbox:
                            u_min, u_max = uv_bbox[0]
                            v_min, v_max = uv_bbox[1]
                            offset_u = -math.floor(u_min)
                            offset_v = -math.floor(v_min)
                            if offset_u != 0 or offset_v != 0:
                                cmds.polyEditUV(uvs, uValue=offset_u, vValue=offset_v)
                    except Exception as e:
                        cmds.warning("UV shift failed for {}: {}".format(new_lp_full, e))

                new_lp_meshes.append(new_lp_full)

                # 4. Duplicate HP meshes
                if hp_meshes:
                    for hp in sorted(hp_meshes, key=str):
                        if not cmds.objExists(hp):
                            continue
                        dup_hp = FinalExportProcessor._duplicate_export_node(hp)
                        created_nodes.append(dup_hp)
                        hp_short = hp.split('|')[-1]
                        new_hp_name = cmds.rename(dup_hp, "{}_mat{}".format(hp_short, idx))
                        created_nodes[-1] = new_hp_name
                        new_hp_full = cmds.ls(new_hp_name, long=True)[0]
                        created_nodes[-1] = new_hp_full
                        new_hp_meshes.append(new_hp_full)

                idx += 1

            return new_lp_meshes, new_hp_meshes
        except Exception:
            FinalExportProcessor._cleanup_zero_transform_hp_export_temps(
                reversed(created_nodes)
            )
            raise

    @staticmethod
    def build_chapter_snapshot(base_name, hp_main, lp_main, smooth_states=None,
                               final_mesh_widgets=None, keep_structure=False):
        """Operation-scoped scene index shared by HP/LP export and preflight."""
        import bg_final_groups
        groups = bg_final_groups.discover(base_name, hp_main, lp_main, keep_structure)
        hp_by_prefix, lp_by_prefix, items = {}, {}, []
        widget_levels = {item['full_prefix']: FinalExportProcessor._smooth_level_from_item(item)
                         for item in (final_mesh_widgets or [])}
        for name, group in sorted(groups.items()):
            prefix = group['prefix']
            hp_by_prefix[prefix] = list(group['hp_nodes'])
            lp_by_prefix[prefix] = list(group['lp_nodes'])
            items.append({'prefix': prefix, 'hp_nodes': list(group['hp_nodes']),
                          'smooth_level': widget_levels.get(prefix,
                              FinalExportProcessor._smooth_level_from_states(
                                  smooth_states or {}, base_name, prefix, 0))})
        hp_all = sorted(set(node for nodes in hp_by_prefix.values() for node in nodes))
        lp_all = sorted(set(node for nodes in lp_by_prefix.values() for node in nodes))
        levels = {node: item['smooth_level'] for item in items for node in item['hp_nodes']}
        return {'base': base_name, 'hp': hp_main, 'lp': lp_main,
                'hp_all': hp_all, 'lp_all': lp_all,
                'hp_by_prefix': hp_by_prefix, 'lp_by_prefix': lp_by_prefix,
                'prefix_items': items, 'material_faces': {},
                'hp_metadata': {node: {'zbrush': FinalExportProcessor._is_zbrush_mesh(node),
                                       'smooth_level': levels.get(node, 0)}
                                for node in hp_all}}

    @staticmethod
    def export_chapter(base_name, hp_main, lp_main, final_mesh_widgets,
                       parent_window=None, mode='both', export_dir=None,
                       smooth_states=None, independent_island_vertex_colors=False,
                       prepared_snapshot=None, combine_regular_hp=True, cancel_check=None):
        previous = cmds.ls(selection=True, long=True) or []
        owned_before = set(FinalExportProcessor._owned_export_uuids)
        try:
            with FinalExportProcessor.export_session():
                return FinalExportProcessor._export_chapter_impl(
                    base_name, hp_main, lp_main, final_mesh_widgets, parent_window,
                    mode, export_dir, smooth_states, independent_island_vertex_colors,
                    prepared_snapshot, combine_regular_hp, cancel_check)
        except Exception as exc:
            cmds.warning('Export failed: {}'.format(exc))
            return False
        finally:
            created = FinalExportProcessor._owned_export_uuids - owned_before
            nodes = [node for identifier in created for node in (cmds.ls(identifier, long=True) or [])]
            FinalExportProcessor._cleanup_zero_transform_hp_export_temps(nodes)
            restored = [node for node in previous if cmds.objExists(node)]
            cmds.select(restored, replace=True) if restored else cmds.select(clear=True)

    @staticmethod
    def _export_chapter_impl(base_name, hp_main, lp_main, final_mesh_widgets, parent_window=None, mode='both', export_dir=None, smooth_states=None, independent_island_vertex_colors=False, prepared_snapshot=None, combine_regular_hp=True, cancel_check=None):
        """Prepare meshes, apply smoothing, export, and rollback."""
        if not export_dir:
            export_dirs = cmds.fileDialog2(fileMode=3, caption=bg_l10n.text("Select Export Directory"))
            if not export_dirs: return False
            export_dir = export_dirs[0]

        if not cmds.pluginInfo('fbxmaya', query=True, loaded=True):
            cmds.loadPlugin('fbxmaya')

        snapshot = prepared_snapshot or FinalExportProcessor.build_chapter_snapshot(
            base_name, hp_main, lp_main, smooth_states, final_mesh_widgets)
        lp_all = list(snapshot['lp_all'])
        prefixes_to_process = list(snapshot['prefix_items'])
        hp_metadata = dict(snapshot.get('hp_metadata') or {})
        def cancelled():
            return bool(cancel_check and cancel_check())
        if cancelled():
            return False

        if mode == 'lp':
            if not lp_all:
                return False

            cmds.select(lp_all, replace=True)
            export_name = "{}_LP".format(base_name).replace(".", "_")
            export_path = "{}/{}.fbx".format(export_dir.rstrip('/\\'), export_name).replace('\\', '/')
            FinalExportProcessor.export_meshes_fbx(lp_all, export_path, triangulate_low=True)
            return export_name

        if not prefixes_to_process:
            prefixes_to_process.append({'prefix': '___dummy___', 'smooth_level': 0})

        progress_dlg = QtWidgets.QProgressDialog(bg_l10n.text("Exporting Chapter..."), bg_l10n.text("Cancel"), 0, len(prefixes_to_process), parent_window)
        progress_dlg.setWindowModality(QtCore.Qt.WindowModal)
        progress_dlg.show()

        smooth_levels = {}
        temp_nodes = []
        previous_selection = cmds.ls(selection=True, long=True) or []
        try:
            refresh_was_suspended = bool(cmds.refresh(query=True, suspend=True))
        except Exception:
            refresh_was_suspended = False
        cmds.refresh(suspend=True)

        try:
            hp_all = list(snapshot['hp_all'])

            exported_meshes = set()
            all_to_export = []

            for i, item in enumerate(prefixes_to_process):
                QtWidgets.QApplication.processEvents()
                if progress_dlg.wasCanceled() or cancelled():
                    return False

                full_prefix_lower = item['prefix']
                smooth_level = item['smooth_level']
                try:
                    smooth_level_value = max(0, int(smooth_level or 0))
                except Exception:
                    smooth_level_value = 0

                progress_dlg.setLabelText(bg_l10n.text("Preparing: {name}").format(name=full_prefix_lower))
                progress_dlg.setValue(i)
                QtWidgets.QApplication.processEvents()
                if progress_dlg.wasCanceled() or cancelled():
                    return False

                hp_meshes = list(snapshot['hp_by_prefix'].get(full_prefix_lower, []))
                lp_meshes = list(snapshot['lp_by_prefix'].get(full_prefix_lower, []))
                item_hp_meshes = []
                for hp_node in item.get('hp_nodes', []) or []:
                    for hp_match in cmds.ls(hp_node, long=True) or []:
                        if hp_match in hp_all:
                            item_hp_meshes.append(hp_match)
                if item_hp_meshes:
                    hp_meshes = sorted(set(hp_meshes + item_hp_meshes), key=str)

                if not hp_meshes:
                    cmds.warning("Warning: No HP meshes found for LP prefix '{}'. Check naming.".format(full_prefix_lower))

                smoothable_hp_shorts = set()
                if smooth_level_value > 0 and mode in ['both', 'hp']:
                    smooth_levels["prefix:{}".format(full_prefix_lower)] = max(
                        smooth_levels.get("prefix:{}".format(full_prefix_lower), 0),
                        smooth_level_value
                    )
                    for hp in hp_meshes:
                        short_name = hp.split('|')[-1].lower()
                        if hp_metadata.get(hp, {}).get('zbrush', False):
                            cmds.warning("Skipped smooth for '{}' (ZBrush geometry)".format(hp))
                        else:
                            hp_long = cmds.ls(hp, long=True)
                            hp_long = hp_long[0] if hp_long else hp
                            smoothable_hp_shorts.add(short_name)
                            smooth_levels[hp_long] = max(smooth_levels.get(hp_long, 0), smooth_level_value)
                            smooth_levels[short_name] = max(smooth_levels.get(short_name, 0), smooth_level_value)

                # --- MULTI-MATERIAL LOGIC ---
                was_split = False
                final_hp_for_export = set(hp_meshes)
                final_lp_for_export = set()

                for lp in sorted(lp_meshes if mode == 'both' else [], key=str):
                    material_cache = snapshot.setdefault('material_faces', {})
                    if lp not in material_cache:
                        material_cache[lp] = FinalExportProcessor._get_mesh_materials_and_faces(lp)
                    mat_dict = material_cache[lp]

                    if len(mat_dict) > 1:
                        was_split = True
                        new_lps, new_hps = FinalExportProcessor._process_multimaterial_mesh(lp, hp_meshes, mat_dict)
                        temp_nodes.extend(new_lps)
                        temp_nodes.extend(new_hps)
                        for new_hp in new_hps:
                            stem = new_hp.split('|')[-1].rsplit('_mat', 1)[0]
                            source_hp = next((hp for hp in hp_meshes if hp.split('|')[-1] == stem), None)
                            if source_hp:
                                hp_metadata[new_hp] = dict(hp_metadata.get(source_hp, {}))
                        if smooth_level_value > 0 and mode in ['both', 'hp']:
                            for hp in new_hps:
                                short_name = hp.split('|')[-1].lower()
                                if any(short_name.startswith(source + "_mat") for source in smoothable_hp_shorts):
                                    hp_long = cmds.ls(hp, long=True)
                                    hp_long = hp_long[0] if hp_long else hp
                                    smooth_levels[hp_long] = max(smooth_levels.get(hp_long, 0), smooth_level_value)
                                    smooth_levels[short_name] = max(smooth_levels.get(short_name, 0), smooth_level_value)
                        final_lp_for_export.update(new_lps)
                        final_hp_for_export.update(new_hps)
                        exported_meshes.add(lp)
                    else:
                        final_lp_for_export.add(lp)

                if was_split:
                    final_hp_for_export.difference_update(hp_meshes)
                    for hp in hp_meshes:
                        exported_meshes.add(hp)

                # --- ADD TO FINAL LIST (With mode filtering) ---
                if mode == 'hp' or mode == 'both':
                    for m in sorted(final_hp_for_export, key=str):
                        if m not in exported_meshes:
                            all_to_export.append(m)
                            exported_meshes.add(m)

                if mode == 'lp' or mode == 'both':
                    for m in sorted(final_lp_for_export, key=str):
                        if m not in exported_meshes:
                            all_to_export.append(m)
                            exported_meshes.add(m)

            QtWidgets.QApplication.processEvents()
            if progress_dlg.wasCanceled() or cancelled():
                return False

            # Fallback for lost meshes (accounting for export mode)
            for m in sorted(hp_all, key=str):
                if m not in exported_meshes and mode in ['both', 'hp']:
                    all_to_export.append(m)
                    exported_meshes.add(m)

            for m in sorted(lp_all, key=str):
                if m not in exported_meshes and mode in ['both', 'lp']:
                    all_to_export.append(m)
                    exported_meshes.add(m)

            # --- EXPORT ---
            if all_to_export:
                export_nodes = all_to_export
                if mode in ['both', 'hp']:
                    export_nodes, temp_root = FinalExportProcessor._make_zero_transform_hp_export_copies(
                        all_to_export, smooth_levels,
                        reusable_temp_nodes=[node for node in temp_nodes if node in hp_metadata],
                        combine_non_zbrush=(combine_regular_hp and mode == 'hp' and not independent_island_vertex_colors),
                        hp_metadata=hp_metadata)
                    if temp_root:
                        temp_nodes.append(temp_root)

                if independent_island_vertex_colors and mode in ['both', 'hp']:
                    colored_count, island_count = FinalExportProcessor._assign_independent_island_vertex_colors(export_nodes)
                    if colored_count and parent_window is not None:
                        try:
                            parent_window.log(
                                bg_l10n.text("Island vertex colors assigned: {meshes} mesh(es), {islands} island(s)")
                                .format(meshes=colored_count, islands=island_count),
                                "lightblue",
                            )
                        except Exception:
                            pass

                cmds.select(export_nodes, replace=True)

                # If exporting separately, add corresponding suffix to the file
                suffix = ""
                if mode == 'hp': suffix = "_HP"
                elif mode == 'lp': suffix = "_LP"

                export_name = "{}{}".format(base_name, suffix).replace(".", "_")
                export_path = "{}/{}.fbx".format(export_dir.rstrip('/\\'), export_name).replace('\\', '/')

                FinalExportProcessor.export_meshes_fbx(export_nodes, export_path, triangulate_low=True)
                return export_name

        except Exception as e:
            cmds.warning("Export failed: {}".format(e))
            return False

        finally:
            FinalExportProcessor._cleanup_zero_transform_hp_export_temps(reversed(temp_nodes))
            try:
                if previous_selection:
                    cmds.select(previous_selection, replace=True)
                else:
                    cmds.select(clear=True)
            except Exception:
                cmds.select(clear=True)
            cmds.refresh(suspend=refresh_was_suspended)
            progress_dlg.close()

        return False
