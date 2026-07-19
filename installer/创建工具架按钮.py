# -*- coding: utf-8 -*-
from __future__ import print_function

import os
import traceback

import maya.cmds as cmds
import maya.mel as mel


BUTTON_LABEL = "BAKE GROUPS"
BUTTON_ANNOTATION = u"打开 Bake Master 高低模烘焙分组工具"
TARGET_DIR = os.path.normpath(os.path.join(cmds.internalVar(userScriptDir=True), "Bake_Groups"))


def shelf_command():
    return """import os
import traceback
import maya.cmds as cmds

try:
    s_dir = os.path.normpath(os.path.join(cmds.internalVar(userScriptDir=True), "Bake_Groups"))
    launcher_path = os.path.join(s_dir, "launcher.py")
    namespace = {"__file__": launcher_path, "__name__": "__main__"}
    with open(launcher_path, "rb") as handle:
        source = handle.read()
    if not isinstance(source, str):
        source = source.decode("utf-8", "replace")
    exec(compile(source, launcher_path, "exec"), namespace, namespace)
except Exception:
    traceback.print_exc()
    cmds.warning("Bake Master failed to launch. See Script Editor for traceback.")
"""


def create_button():
    launcher = os.path.join(TARGET_DIR, "launcher.py")
    if not os.path.isfile(launcher):
        raise RuntimeError(u"尚未安装 Bake Master：{}".format(TARGET_DIR))

    shelf_top = mel.eval('$tmpVar=$gShelfTopLevel')
    shelf = cmds.tabLayout(shelf_top, query=True, selectTab=True)
    for button in cmds.shelfLayout(shelf, query=True, childArray=True) or []:
        try:
            if cmds.objectTypeUI(button) != "shelfButton":
                continue
            label = cmds.shelfButton(button, query=True, label=True)
            annotation = cmds.shelfButton(button, query=True, annotation=True)
            command = cmds.shelfButton(button, query=True, command=True) or ""
            is_ours = annotation == BUTTON_ANNOTATION or ("Bake_Groups" in command and "launcher.py" in command)
            if label == BUTTON_LABEL and is_ours:
                cmds.deleteUI(button)
        except Exception:
            pass

    icon = os.path.join(TARGET_DIR, "Bake_Group.png")
    cmds.shelfButton(
        parent=shelf,
        label=BUTTON_LABEL,
        image=icon if os.path.isfile(icon) else "commandButton.png",
        annotation=BUTTON_ANNOTATION,
        command=shelf_command(),
        sourceType="python",
    )
    try:
        mel.eval('saveAllShelves $gShelfTopLevel')
    except Exception:
        pass
    cmds.confirmDialog(title=u"完成", message=u"已在当前工具架创建 BAKE GROUPS 按钮。", button=[u"完成"])


def onMayaDroppedPythonFile(*args):
    try:
        create_button()
    except Exception as exc:
        traceback.print_exc()
        cmds.confirmDialog(
            title=u"创建按钮失败",
            message=u"创建按钮失败：\n\n{}\n\n请查看脚本编辑器了解详细信息。".format(exc),
            button=[u"关闭"],
        )


if __name__ == "__main__":
    onMayaDroppedPythonFile()
