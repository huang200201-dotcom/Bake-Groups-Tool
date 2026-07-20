from __future__ import absolute_import, division, print_function

import json
import os
import shutil
import sys
import tempfile

import maya.standalone


def _pair(pair_id, base):
    return {
        "id": pair_id,
        "base": base,
        "locked": [],
        "final_smooth_states": {},
        "grouping_mode": "conflict_safe",
    }


def _sidecar_pairs(path):
    with open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def main(runtime_root):
    maya.standalone.initialize(name="python")
    import maya.cmds as cmds

    runtime_root = os.path.realpath(os.path.abspath(runtime_root))
    sys.path.insert(0, runtime_root)
    import bg_core

    temporary = tempfile.mkdtemp(prefix="bake-master-undo-bridge-")
    scene_path = os.path.join(temporary, "bridge_test.ma")
    sidecar_path = os.path.splitext(scene_path)[0] + "_BakeGroups.json"
    before = [_pair("pair-before", "Before")]
    after = [_pair("pair-after", "After")]
    direct = [_pair("pair-direct", "DirectSave")]
    try:
        cmds.file(new=True, force=True)
        cmds.file(rename=scene_path)
        old_bridge = cmds.createNode("network", name="BakeMasterUndoBridge")
        cmds.addAttr(
            old_bridge, longName="bgBakeMasterUndoToken",
            dataType="string", hidden=True
        )
        cmds.setAttr(
            old_bridge + ".bgBakeMasterUndoToken", "legacy", type="string"
        )
        cmds.file(save=True, force=True, type="mayaAscii")
        bg_core.BakeSessionModel.sync_legacy_copies(
            before, preserve_modified=True
        )
        cmds.file(modified=False)

        modified_before_initialize = bool(cmds.file(query=True, modified=True))
        bridge, payload = bg_core.BakeSessionModel.initialize_undo_bridge(before)
        assert bridge == old_bridge
        assert payload
        assert bool(cmds.file(query=True, modified=True)) == modified_before_initialize

        marked_nodes = []
        for node in cmds.ls(type="network", long=True) or []:
            if (cmds.attributeQuery(
                    bg_core.BakeSessionModel.UNDO_BRIDGE_MARKER_ATTR,
                    node=node, exists=True) or
                    cmds.attributeQuery(
                        bg_core.BakeSessionModel.LEGACY_UNDO_MARKER_ATTR,
                        node=node, exists=True)):
                marked_nodes.append(node)
        assert marked_nodes == [old_bridge]

        with bg_core.undo_chunk("BakeMasterBridgeIntegration"):
            cmds.createNode("transform", name="BridgeAfterDag")
            bg_core.BakeSessionModel.save(after)

        state, unused_payload = bg_core.BakeSessionModel.read_undo_state()
        assert state["root_pairs"] == after
        assert cmds.objExists("BridgeAfterDag")

        # No UI callback runs here: this is the closed-window case.
        cmds.undo()
        assert not cmds.objExists("BridgeAfterDag")
        state, unused_payload = bg_core.BakeSessionModel.read_undo_state()
        assert state["root_pairs"] == before
        modified_before_load = bool(cmds.file(query=True, modified=True))
        assert bg_core.BakeSessionModel.load() == before
        assert bool(cmds.file(query=True, modified=True)) == modified_before_load
        assert bg_core.BakeSessionModel._legacy_scene_data() == before
        assert _sidecar_pairs(sidecar_path) == before

        cmds.redo()
        assert cmds.objExists("BridgeAfterDag")
        state, unused_payload = bg_core.BakeSessionModel.read_undo_state()
        assert state["root_pairs"] == after
        modified_before_load = bool(cmds.file(query=True, modified=True))
        assert bg_core.BakeSessionModel.load() == after
        assert bool(cmds.file(query=True, modified=True)) == modified_before_load
        assert bg_core.BakeSessionModel._legacy_scene_data() == after
        assert _sidecar_pairs(sidecar_path) == after

        bg_core.BakeSessionModel.save(direct)
        state, unused_payload = bg_core.BakeSessionModel.read_undo_state()
        assert state["root_pairs"] == direct
        assert bg_core.BakeSessionModel.find_undo_bridge() == old_bridge
        cmds.undo()
        state, unused_payload = bg_core.BakeSessionModel.read_undo_state()
        assert state["root_pairs"] == after
        print("MAYA_UNDO_BRIDGE_OK")
        return 0
    finally:
        try:
            cmds.file(new=True, force=True)
        except Exception:
            pass
        maya.standalone.uninitialize()
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: test-maya-undo-bridge.py <runtime-root>")
    raise SystemExit(main(sys.argv[1]))
