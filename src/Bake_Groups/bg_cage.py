# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

"""Bake cage creation for Bake Master.

New cages are staged from finalized LP copies. Existing cages and manual edits
are preserved by default; explicit rebuilds replace a chapter only after the
replacement is ready. Source LP geometry is never edited.
"""

import math

import maya.cmds as cmds
import maya.api.OpenMaya as om



CAGE_ROOT = "BakeMaster_Cage_BG"
CAGE_SUFFIX = "_cage"
CAGE_COLOR = (0.18, 0.72, 0.92)


class CageCancelled(RuntimeError):
    """Raised when the user cancels cage creation."""


def default_cage_settings():
    return {
        "inflate_pct": 2.0,
        "gap_pct": 0.35,
        "fit_to_high": True,
        "visible": True,
        "wireframe": True,
        "export_enabled": True,
    }


def normalized_cage_settings(settings):
    result = default_cage_settings()
    if isinstance(settings, dict):
        result.update(settings)
    try:
        result["inflate_pct"] = max(-5.0, min(30.0, float(result["inflate_pct"])))
    except Exception:
        result["inflate_pct"] = 2.0
    try:
        result["gap_pct"] = max(0.0, min(10.0, float(result["gap_pct"])))
    except Exception:
        result["gap_pct"] = 0.35
    for key in ("fit_to_high", "visible", "wireframe", "export_enabled"):
        result[key] = bool(result.get(key))
    return result


def cage_name_from_low(low_name):
    short_name = str(low_name or "").split("|")[-1]
    lower_name = short_name.lower()
    marker = lower_name.rfind("_low")
    if marker >= 0:
        short_name = short_name[:marker] + CAGE_SUFFIX + short_name[marker + 4:]
    elif not lower_name.endswith(CAGE_SUFFIX):
        short_name += CAGE_SUFFIX
    return short_name


class CageManager(object):
    SOURCE_ATTR = "bakeMasterCageSource"
    CHAPTER_ATTR = "bakeMasterCageChapter"

    @staticmethod
    def _long(node):
        matches = cmds.ls(node, long=True) or []
        return matches[0] if matches else node

    @staticmethod
    def _valid_mesh(node):
        if not node or not cmds.objExists(node):
            return False
        shapes = cmds.listRelatives(node, shapes=True, fullPath=True, type="mesh") or []
        return any(not cmds.getAttr(shape + ".intermediateObject") for shape in shapes)

    @staticmethod
    def _bbox_diag(node):
        bbox = cmds.exactWorldBoundingBox(node)
        return math.sqrt(
            (bbox[3] - bbox[0]) ** 2 +
            (bbox[4] - bbox[1]) ** 2 +
            (bbox[5] - bbox[2]) ** 2
        )

    @staticmethod
    def _mesh_function(node):
        selection = om.MSelectionList()
        selection.add(node)
        dag = selection.getDagPath(0)
        dag.extendToShape()
        return om.MFnMesh(dag)

    @staticmethod
    def _ensure_string_attr(node, name, value):
        if not cmds.attributeQuery(name, node=node, exists=True):
            cmds.addAttr(node, longName=name, dataType="string")
        cmds.setAttr("{}.{}".format(node, name), str(value), type="string")

    @staticmethod
    def chapter_path(base_name):
        return "|{}|{}".format(CAGE_ROOT, base_name)

    @staticmethod
    def _ensure_chapter(base_name):
        root = "|{}".format(CAGE_ROOT)
        if not cmds.objExists(root):
            root = CageManager._long(cmds.group(empty=True, world=True, name=CAGE_ROOT))
        chapter = "{}|{}".format(root, base_name)
        if not cmds.objExists(chapter):
            chapter = CageManager._long(cmds.group(empty=True, parent=root, name=base_name))
        return chapter

    @staticmethod
    def _cleanup_empty_hierarchy(base_name=None):
        root = "|{}".format(CAGE_ROOT)
        if not cmds.objExists(root):
            return
        chapters = cmds.listRelatives(root, children=True, fullPath=True, type="transform") or []
        for chapter in chapters:
            if base_name and chapter.split("|")[-1] != base_name:
                continue
            descendants = cmds.listRelatives(
                chapter, allDescendents=True, fullPath=True, type="transform"
            ) or []
            for node in sorted(descendants, key=lambda value: value.count("|"), reverse=True):
                if not cmds.objExists(node):
                    continue
                children = cmds.listRelatives(node, children=True, fullPath=True) or []
                shapes = cmds.listRelatives(node, shapes=True, fullPath=True) or []
                if not children and not shapes:
                    cmds.delete(node)
            if cmds.objExists(chapter):
                children = cmds.listRelatives(chapter, children=True, fullPath=True) or []
                shapes = cmds.listRelatives(chapter, shapes=True, fullPath=True) or []
                if not children and not shapes:
                    cmds.delete(chapter)
        if cmds.objExists(root):
            if not (cmds.listRelatives(root, children=True, fullPath=True) or []):
                cmds.delete(root)

    @staticmethod
    def chapter_cages(base_name):
        chapter = CageManager.chapter_path(base_name)
        if not cmds.objExists(chapter):
            return []
        result = []
        for node in cmds.listRelatives(
                chapter, allDescendents=True, fullPath=True, type="transform") or []:
            if CageManager._valid_mesh(node) and node.split("|")[-1].lower().find(CAGE_SUFFIX) >= 0:
                result.append(node)
        return sorted(set(result), key=str)

    @staticmethod
    def _set_display(cages, visible=True, wireframe=True):
        root = "|{}".format(CAGE_ROOT)
        if cmds.objExists(root):
            cmds.setAttr(root + ".visibility", bool(visible))
        for cage in cages or []:
            if not cmds.objExists(cage):
                continue
            cmds.setAttr(cage + ".visibility", bool(visible))
            for shape in cmds.listRelatives(cage, shapes=True, fullPath=True, type="mesh") or []:
                try:
                    cmds.setAttr(shape + ".overrideEnabled", 1)
                    cmds.setAttr(shape + ".overrideRGBColors", 1)
                    cmds.setAttr(shape + ".overrideColorRGB", *CAGE_COLOR)
                    cmds.setAttr(shape + ".overrideShading", 0 if wireframe else 1)
                except Exception:
                    pass

    @staticmethod
    def set_chapter_display(base_name, visible=True, wireframe=True):

        cages = CageManager.chapter_cages(base_name)
        CageManager._set_display(cages, visible=visible, wireframe=wireframe)
        return len(cages)

    @staticmethod
    def _inflate_and_fit(cage, source_low, high_nodes, settings,
                         cancelled_getter=None):
        cancelled_getter = cancelled_getter or (lambda: False)
        cage_fn = CageManager._mesh_function(cage)
        low_fn = CageManager._mesh_function(source_low)
        cage_points = cage_fn.getPoints(om.MSpace.kWorld)
        low_points = low_fn.getPoints(om.MSpace.kWorld)
        low_normals = low_fn.getVertexNormals(True, om.MSpace.kWorld)
        if len(cage_points) != len(low_points) or len(low_points) != len(low_normals):
            raise RuntimeError("Cage topology no longer matches its source low mesh")

        diagonal = max(CageManager._bbox_diag(source_low), 0.0001)
        inflate = diagonal * settings["inflate_pct"] / 100.0
        gap = diagonal * settings["gap_pct"] / 100.0
        high_functions = []
        if settings["fit_to_high"]:
            for high in high_nodes or []:
                if CageManager._valid_mesh(high):
                    try:
                        high_functions.append(CageManager._mesh_function(high))
                    except Exception:
                        pass

        output = om.MPointArray()
        for index in range(len(low_points)):
            if index % 128 == 0 and cancelled_getter():
                raise CageCancelled("Cage creation canceled")
            source = low_points[index]
            normal = low_normals[index]
            point = om.MPoint(
                source.x + normal.x * inflate,
                source.y + normal.y * inflate,
                source.z + normal.z * inflate,
            )
            if high_functions:
                best = None
                for high_fn in high_functions:
                    try:
                        closest, face_id = high_fn.getClosestPoint(point, om.MSpace.kWorld)
                        distance = point.distanceTo(closest)
                        if best is None or distance < best[0]:
                            high_normal = high_fn.getPolygonNormal(face_id, om.MSpace.kWorld)
                            best = (distance, closest, high_normal)
                    except Exception:
                        continue
                if best is not None:
                    _distance, closest, high_normal = best
                    signed = (
                        high_normal.x * (point.x - closest.x) +
                        high_normal.y * (point.y - closest.y) +
                        high_normal.z * (point.z - closest.z)
                    )
                    if signed < gap:
                        correction = gap - signed
                        point = om.MPoint(
                            point.x + high_normal.x * correction,
                            point.y + high_normal.y * correction,
                            point.z + high_normal.z * correction,
                        )
            output.append(point)
        cage_fn.setPoints(output, om.MSpace.kWorld)

    @staticmethod
    def _uuid(node):
        identifiers = cmds.ls(node, uuid=True) or []
        if len(identifiers) != 1:
            raise RuntimeError('Cannot track cage node: {}'.format(node))
        return identifiers[0]

    @staticmethod
    def _from_uuid(identifier):
        nodes = cmds.ls(identifier, long=True) or []
        return nodes[0] if nodes else None

    @staticmethod
    def rebuild_chapter(base_name, low_meshes, high_by_prefix, settings=None,
                        progress_callback=None, cancelled_getter=None,
                        preserve_existing=True, only_low_meshes=None):
        """Build off-scene, then commit; cancel/failure preserves the previous cage.

        The default creates missing cages without replacing manually sculpted
        ones. Pass preserve_existing=False for an explicit complete rebuild.
        only_low_meshes scopes creation and never removes unrelated cages.
        """

        settings = normalized_cage_settings(settings)
        cancelled_getter = cancelled_getter or (lambda: False)
        low_meshes = [CageManager._long(node) for node in low_meshes or []
                      if CageManager._valid_mesh(node)]
        if not low_meshes:
            return []
        if cancelled_getter():
            raise CageCancelled('Cage creation canceled')
        existing = CageManager.chapter_cages(base_name)
        existing_names = {node.split('|')[-1] for node in existing}
        if only_low_meshes is not None:
            requested = {CageManager._long(node) for node in only_low_meshes}
            low_meshes = [node for node in low_meshes if node in requested]
            # A partial request must not replace the chapter's other cages.
            preserve_existing = True
        if preserve_existing:
            low_meshes = [node for node in low_meshes
                          if cage_name_from_low(node) not in existing_names]
        if not low_meshes:
            return existing

        stage = cmds.group(empty=True, world=True, name='BakeMaster_Cage_Stage#')
        stage_id = CageManager._uuid(stage)
        created_ids = []
        old_id = None
        committed = False
        try:
            duplicates = cmds.duplicate(low_meshes, returnRootsOnly=True) or []
            created_ids = [CageManager._uuid(node) for node in duplicates]
            if len(duplicates) != len(low_meshes):
                raise RuntimeError('Maya returned an unexpected cage duplicate count')
            for index, (source_low, identifier) in enumerate(zip(low_meshes, created_ids)):
                if cancelled_getter():
                    raise CageCancelled('Cage creation canceled')
                duplicate = CageManager._from_uuid(identifier)
                cmds.delete(duplicate, constructionHistory=True)
                duplicate = cmds.parent(duplicate, stage, absolute=True)[0]
                duplicate = cmds.rename(duplicate, cage_name_from_low(source_low))
                duplicate = CageManager._from_uuid(identifier)
                short = source_low.split('|')[-1]
                prefix = short[:short.lower().rfind('_low')]
                high_nodes = (high_by_prefix.get(prefix, high_by_prefix.get(prefix.lower(), []))
                              if isinstance(high_by_prefix, dict) else [])
                CageManager._inflate_and_fit(duplicate, source_low, high_nodes, settings,
                                            cancelled_getter=cancelled_getter)
                CageManager._ensure_string_attr(duplicate, CageManager.SOURCE_ATTR, source_low)
                CageManager._ensure_string_attr(duplicate, CageManager.CHAPTER_ATTR, base_name)
                if progress_callback:
                    progress_callback(index + 1, len(low_meshes), duplicate)
            if cancelled_getter():
                raise CageCancelled('Cage creation canceled')

            # Display changes apply only to the new copies until commit.
            new_nodes = [CageManager._from_uuid(value) for value in created_ids]
            for node in new_nodes:
                cmds.setAttr(node + '.visibility', settings['visible'])
                for shape in cmds.listRelatives(node, shapes=True, fullPath=True, type='mesh') or []:
                    cmds.setAttr(shape + '.overrideEnabled', 1)
                    cmds.setAttr(shape + '.overrideRGBColors', 1)
                    cmds.setAttr(shape + '.overrideColorRGB', *CAGE_COLOR)
                    cmds.setAttr(shape + '.overrideShading', 0 if settings['wireframe'] else 1)

            chapter = CageManager.chapter_path(base_name)
            if preserve_existing and cmds.objExists(chapter):
                # Existing identities and manual edits survive. On a failed move,
                # only newly created UUIDs are removed by the exception handler.
                for identifier in created_ids:
                    cmds.parent(CageManager._from_uuid(identifier), chapter, absolute=True)
                committed = True
            else:
                root = '|' + CAGE_ROOT
                if not cmds.objExists(root):
                    cmds.group(empty=True, world=True, name=CAGE_ROOT)
                if cmds.objExists(chapter):
                    old_id = CageManager._uuid(chapter)
                    cmds.rename(chapter, 'BakeMaster_Cage_Backup#')
                stage = CageManager._from_uuid(stage_id)
                stage = cmds.parent(stage, root, absolute=True)[0]
                cmds.rename(stage, base_name)
                if old_id:
                    old_node = CageManager._from_uuid(old_id)
                    if old_node:
                        cmds.delete(old_node)
                committed = True
            result = CageManager.chapter_cages(base_name)
            return result
        except Exception:
            # Stage/copy UUIDs remain valid through every parent and rename.
            for identifier in reversed(created_ids):
                node = CageManager._from_uuid(identifier)
                if node:
                    cmds.delete(node)
            stage_node = CageManager._from_uuid(stage_id)
            if stage_node:
                cmds.delete(stage_node)
            if old_id:
                old_node = CageManager._from_uuid(old_id)
                if old_node:
                    cmds.rename(old_node, base_name)
            CageManager._cleanup_empty_hierarchy(base_name)
            raise
        finally:
            # During replacement the stage itself becomes the committed chapter.
            if committed and preserve_existing:
                stage_node = CageManager._from_uuid(stage_id)
                if stage_node and stage_node != CageManager.chapter_path(base_name):
                    cmds.delete(stage_node)

    @staticmethod
    def inflate_existing_cage(cage, delta, source_low=None):
        """Apply a relative world-unit offset while preserving manual sculpting."""

        if source_low is None and cmds.attributeQuery(CageManager.SOURCE_ATTR, node=cage, exists=True):
            source_low = cmds.getAttr(cage + '.' + CageManager.SOURCE_ATTR)
        cage_fn = CageManager._mesh_function(cage)
        points = cage_fn.getPoints(om.MSpace.kWorld)
        normal_fn = CageManager._mesh_function(source_low) if CageManager._valid_mesh(source_low) else cage_fn
        normals = normal_fn.getVertexNormals(True, om.MSpace.kWorld)
        if len(points) != len(normals):
            raise RuntimeError('Cage topology no longer matches its source low mesh')
        output = om.MPointArray()
        for point, normal in zip(points, normals):
            output.append(om.MPoint(point.x + normal.x * delta,
                                   point.y + normal.y * delta,
                                   point.z + normal.z * delta))
        # Command-layer point writes participate in Maya's Undo history. MFnMesh
        # setPoints would silently bypass an otherwise valid user undo chunk.
        for index, point in enumerate(output):
            cmds.xform('{}.vtx[{}]'.format(cage, index), worldSpace=True,
                       translation=(point.x, point.y, point.z))
        return len(output)

    @staticmethod
    def find_hp_intersection_islands(cage, high_nodes):
        """Return connected face regions whose vertices lie inside an HP shell.

        This signed-nearest-surface test is a manual touch-up aid, not proof that
        open/non-manifold HP meshes or all face-only penetrations are clear.
        """
        cage_fn = CageManager._mesh_function(cage)
        points = cage_fn.getPoints(om.MSpace.kWorld)
        probes = [CageManager._mesh_function(node) for node in high_nodes or []
                  if CageManager._valid_mesh(node)]
        inside = set()
        for index, point in enumerate(points):
            for probe in probes:
                closest, face_id = probe.getClosestPoint(point, om.MSpace.kWorld)
                normal = probe.getPolygonNormal(face_id, om.MSpace.kWorld)
                if ((point.x - closest.x) * normal.x +
                        (point.y - closest.y) * normal.y +
                        (point.z - closest.z) * normal.z) < -1e-7:
                    inside.add(index)
                    break
        faces = {}
        adjacent = {}
        for face_id in range(cage_fn.numPolygons):
            vertices = set(cage_fn.getPolygonVertices(face_id))
            if vertices.intersection(inside):
                faces[face_id] = vertices
                for vertex in vertices:
                    adjacent.setdefault(vertex, set()).add(face_id)
        islands = []
        remaining = set(faces)
        while remaining:
            queue = [min(remaining)]
            region, vertices = set(), set()
            while queue:
                face = queue.pop()
                if face in region:
                    continue
                region.add(face)
                remaining.discard(face)
                vertices.update(faces[face])
                for vertex in faces[face]:
                    queue.extend(adjacent[vertex] - region)
            islands.append({'faces': sorted(region), 'vertices': sorted(vertices)})
        return islands

    @staticmethod
    def move_islands(cage, islands, delta):
        """Undoable world-space normal move of the selected intersection regions."""

        mesh_fn = CageManager._mesh_function(cage)
        points = mesh_fn.getPoints(om.MSpace.kWorld)
        normals = mesh_fn.getVertexNormals(True, om.MSpace.kWorld)
        vertices = sorted({vertex for island in islands or []
                           for vertex in island.get('vertices', [])})
        for index in vertices:
            point, normal = points[index], normals[index]
            cmds.xform('{}.vtx[{}]'.format(cage, index), worldSpace=True,
                       translation=(point.x + normal.x * delta,
                                    point.y + normal.y * delta,
                                    point.z + normal.z * delta))
        return len(vertices)

    @staticmethod
    def delete_chapter(base_name):

        chapter = CageManager.chapter_path(base_name)
        if cmds.objExists(chapter):
            cmds.delete(chapter)
        CageManager._cleanup_empty_hierarchy(base_name)
