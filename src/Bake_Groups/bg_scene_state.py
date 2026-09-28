"""Restore the artist's view after a temporary export operation."""
from __future__ import absolute_import, division, print_function

import contextlib
import functools
import json

import maya.cmds as cmds
import maya.mel as mel


def _references(items):
    result = []
    for item in items or []:
        node, dot, component = item.partition('.')
        identifiers = cmds.ls(node, uuid=True) or []
        paths = cmds.ls(node, long=True) or []
        if len(identifiers) == 1 and paths:
            result.append((identifiers[0], paths[0], dot + component))
    return result


def _resolve(reference):
    identifier, original, suffix = reference
    paths = cmds.ls(identifier, long=True, allPaths=True) or []
    if not paths:
        return None
    path = original if original in paths else paths[0]
    return path + suffix if cmds.objExists(path + suffix) else None


def _restore_selection(references):
    selection = [node for node in (_resolve(ref) for ref in references) if node]
    if (cmds.ls(selection=True, long=True) or []) != selection:
        cmds.select(selection, replace=True, noExpand=True) if selection else cmds.select(clear=True)


def _auto_add(panel, value=None):
    # Maya's Auto Load New Objects is a MEL preference, not an isolateSelect
    # flag. Leave it alone in batch hosts where these UI procedures are absent.
    if not mel.eval('exists "isolateSelectAutoAdd"'):
        return None
    quoted = json.dumps(panel)
    if value is None:
        return bool(mel.eval('isolateSelectAutoAdd({})'.format(quoted)))
    mel.eval('setIsolateSelectAutoAdd({}, {});'.format(quoted, int(bool(value))))
    mel.eval('updateIsolateSelectAutoAddScriptJob();')


def capture_isolation():
    states = []
    for panel in cmds.getPanel(type='modelPanel') or []:
        try:
            enabled = bool(cmds.isolateSelect(panel, query=True, state=True))
            object_set = cmds.isolateSelect(panel, query=True, viewObjects=True) if enabled else None
            if isinstance(object_set, (tuple, list)):
                object_set = object_set[0] if object_set else None
            members = cmds.sets(object_set, query=True) if object_set else []
            states.append(dict(panel=panel, enabled=enabled, members=_references(members),
                               auto_add=_auto_add(panel),
                               connection=cmds.modelEditor(panel, query=True, mainListConnection=True)))
        except RuntimeError:
            # A panel can close while an export dialog is active.
            continue
    return states


def restore_isolation(states):
    for state in states:
        panel = state['panel']
        if not cmds.modelPanel(panel, exists=True):
            continue
        try:
            if state['auto_add'] is not None:
                _auto_add(panel, False)
            connection = state['connection']
            if connection and cmds.selectionConnection(connection, exists=True):
                current = cmds.modelEditor(panel, query=True, mainListConnection=True)
                if current != connection:
                    cmds.modelEditor(panel, edit=True, forceMainConnection=connection)
            if cmds.isolateSelect(panel, query=True, state=True) != state['enabled']:
                cmds.isolateSelect(panel, state=state['enabled'])
            if state['enabled']:
                object_set = cmds.isolateSelect(panel, query=True, viewObjects=True)
                if isinstance(object_set, (tuple, list)):
                    object_set = object_set[0] if object_set else None
                if object_set and cmds.objExists(object_set):
                    members = [node for node in (_resolve(ref) for ref in state['members']) if node]
                    current = cmds.sets(object_set, query=True) or []
                    if set(cmds.ls(current, long=True) or []) != set(members):
                        cmds.sets(clear=object_set)
                        if members:
                            cmds.sets(members, addElement=object_set)
                    cmds.isolateSelect(panel, update=True)
        except RuntimeError as exc:
            cmds.warning('Could not restore export isolation for {}: {}'.format(panel, exc))
        finally:
            if state['auto_add'] is not None:
                try:
                    _auto_add(panel, state['auto_add'])
                except RuntimeError:
                    pass


@contextlib.contextmanager
def suspended_isolation():
    states = capture_isolation()
    selection = _references(cmds.ls(selection=True, long=True) or [])
    try:
        for state in states:
            if state['auto_add'] is not None:
                _auto_add(state['panel'], False)
            if state['enabled']:
                cmds.isolateSelect(state['panel'], state=False)
        yield
    finally:
        try:
            _restore_selection(selection)
        except RuntimeError as exc:
            cmds.warning('Could not restore export selection: {}'.format(exc))
        finally:
            restore_isolation(states)


def _scene_nodes(ui):
    """Visit only registered asset trees and shared export roots, not the scene."""
    roots, high_roots = set(), set()
    for pair in getattr(ui, 'root_pairs', []) or []:
        hp, lp, unused = ui.core.resolve_main_nodes(pair)
        roots.update(node for node in (hp, lp) if node and cmds.objExists(node))
        if hp and cmds.objExists(hp):
            high_roots.add(hp)
    roots.update(node for node in ('LP_Combine_BG', 'BakeMaster_Cage_BG') if cmds.objExists(node))
    nodes, high_nodes = set(), set()
    for root in roots:
        tree = (cmds.ls(root, long=True) or []) + (cmds.listRelatives(root, allDescendents=True, fullPath=True) or [])
        nodes.update(tree)
        if root in high_roots:
            high_nodes.update(tree)
    nodes.update(item.partition('.')[0] for item in (cmds.ls(selection=True, long=True) or []))
    for node in list(nodes):
        parent = cmds.listRelatives(node, parent=True, fullPath=True) or []
        while parent:
            if parent[0] in nodes:
                break
            nodes.add(parent[0])
            parent = cmds.listRelatives(parent[0], parent=True, fullPath=True) or []
    return nodes, high_nodes


def _capture_view(ui):
    nodes, high_nodes = _scene_nodes(ui)
    attributes = []
    for node in nodes:
        refs = _references([node])
        if not refs:
            continue
        names = ['visibility']
        if node in high_nodes:
            names.extend(('displaySmoothMesh', 'smoothLevel'))
        values = {name: cmds.getAttr(node + '.' + name) for name in names
                  if cmds.objExists(node + '.' + name)}
        attributes.append((refs[0], values))
    buttons = {}
    for name in ('btn_preview', 'btn_toggle_lp'):
        button = getattr(ui, name, None)
        if button is not None:
            buttons[name] = (button.text(), button.isChecked(), button.styleSheet())
    return dict(attributes=attributes,
                active_root_id=getattr(ui, 'active_root_id', None),
                selection=_references(cmds.ls(selection=True, long=True) or []),
                isolation=capture_isolation(), buttons=buttons,
                flags={name: getattr(ui, name) for name in ('is_preview_active', 'is_final_low_visible')
                       if hasattr(ui, name)})


def _restore_view(ui, state):
    for reference, values in state['attributes']:
        node = _resolve(reference)
        if not node:
            continue
        for name, value in values.items():
            attr = node + '.' + name
            try:
                if (cmds.objExists(attr) and cmds.getAttr(attr) != value
                        and not cmds.getAttr(attr, lock=True)
                        and not cmds.connectionInfo(attr, isDestination=True)
                        and cmds.getAttr(attr, settable=True)):
                    cmds.setAttr(attr, value)
            except RuntimeError as exc:
                cmds.warning('Could not restore export view attribute {}: {}'.format(attr, exc))
    try:
        for name, value in state['flags'].items():
            setattr(ui, name, value)
        if hasattr(ui, '_sync_final_view_ui'):
            ui._sync_final_view_ui()
        synced_low = False
        if (hasattr(ui, '_sync_final_low_visibility_ui') and
                getattr(ui, 'active_root_id', None) == state['active_root_id']):
            pair = next((pair for pair in getattr(ui, 'root_pairs', [])
                         if pair.get('id') == state['active_root_id']), None)
            if pair:
                ui._sync_final_low_visibility_ui(pair.get('base', ''))
                synced_low = True
        for name, (text, checked, style) in state['buttons'].items():
            if name == 'btn_toggle_lp' and synced_low:
                continue
            button = getattr(ui, name, None)
            if button is not None:
                previous = button.blockSignals(True)
                try:
                    button.setText(text)
                    button.setChecked(checked)
                    button.setStyleSheet(style)
                finally:
                    button.blockSignals(previous)
    finally:
        # A closed/deleted Qt control must not prevent scene restoration.
        try:
            _restore_selection(state['selection'])
        finally:
            restore_isolation(state['isolation'])


def preserve_export_view(method):
    """Restore the outermost UI export's view on success, cancel, or failure."""
    @functools.wraps(method)
    def wrapped(ui, *args, **kwargs):
        depth = getattr(ui, '_export_view_depth', 0)
        state = _capture_view(ui) if depth == 0 else None
        ui._export_view_depth = depth + 1
        try:
            return method(ui, *args, **kwargs)
        finally:
            ui._export_view_depth = depth
            if state is not None:
                undo_enabled = cmds.undoInfo(query=True, state=True)
                try:
                    if undo_enabled:
                        cmds.undoInfo(stateWithoutFlush=False)
                    try:
                        _restore_view(ui, state)
                    except Exception as exc:
                        cmds.warning('Could not fully restore export view: {}'.format(exc))
                finally:
                    if undo_enabled:
                        cmds.undoInfo(stateWithoutFlush=True)
    return wrapped
