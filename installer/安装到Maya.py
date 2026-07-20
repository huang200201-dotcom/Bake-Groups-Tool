# -*- coding: utf-8 -*-
from __future__ import print_function, division, absolute_import

import hashlib
import os
import shutil
import sys
import traceback
import uuid

import maya.cmds as cmds
import maya.mel as mel


PACKAGE_DIR = os.path.normpath(os.path.dirname(os.path.abspath(__file__)))
SOURCE_DIR = os.path.join(PACKAGE_DIR, "Bake_Groups")
TARGET_DIR = os.path.normpath(os.path.join(cmds.internalVar(userScriptDir=True), "Bake_Groups"))
BUTTON_LABEL = "BAKE GROUPS"
BUTTON_ANNOTATION = u"打开 Bake Master 高低模烘焙分组工具"
RUNTIME_VERSION = "1.3.15"
SUPPORTED_MAYA = ("2022", "2023", "2024", "2025", "2026", "2027")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest().upper()


def _source_files(root_dir):
    result = {}
    for current, dir_names, file_names in os.walk(root_dir):
        dir_names[:] = [name for name in dir_names if name != "__pycache__"]
        for name in file_names:
            if name.endswith(".pyc") or name == "desktop.ini":
                continue
            path = os.path.join(current, name)
            relative = os.path.relpath(path, root_dir)
            result[relative] = path
    return result


def _all_target_files(root_dir):
    result = {}
    if not os.path.isdir(root_dir):
        return result
    for current, _dir_names, file_names in os.walk(root_dir):
        for name in file_names:
            path = os.path.join(current, name)
            result[os.path.relpath(path, root_dir)] = path
    return result


def _copy_file_atomic(source, target):
    if os.path.isfile(target) and _sha256(source) == _sha256(target):
        return
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    temporary = target + ".bg_install_" + uuid.uuid4().hex
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    except (IOError, OSError):
        if source.lower().endswith(".pyd") and os.path.isfile(target):
            raise RuntimeError(
                u"Maya 正在占用旧的核心文件，无法安全覆盖。\n\n"
                u"请关闭 Maya，然后双击安装包中的“安装_Bake_Groups.bat”。"
            )
        raise
    finally:
        try:
            if os.path.isfile(temporary):
                os.remove(temporary)
        except Exception:
            pass


def _sync_tree_exact(source_dir, target_dir):
    source_files = _source_files(source_dir)
    if not os.path.isdir(target_dir):
        os.makedirs(target_dir)
    for relative in sorted(source_files):
        _copy_file_atomic(source_files[relative], os.path.join(target_dir, relative))

    for relative, path in _all_target_files(target_dir).items():
        if relative not in source_files:
            os.remove(path)
    for current, _dir_names, _file_names in os.walk(target_dir, topdown=False):
        if current != target_dir and not os.listdir(current):
            os.rmdir(current)


def _path_is_inside(path, directory):
    path = os.path.normcase(os.path.realpath(path))
    directory = os.path.normcase(os.path.realpath(directory)).rstrip("\\/")
    return path == directory or path.startswith(directory + os.sep)


def _validate_source():
    required = (
        os.path.join(SOURCE_DIR, "launcher.py"),
        os.path.join(SOURCE_DIR, "active_version.json"),
        os.path.join(SOURCE_DIR, "versions", RUNTIME_VERSION, "bg_main_window.py"),
    )
    required += tuple(
        os.path.join(SOURCE_DIR, "versions", RUNTIME_VERSION, "bin", version, "bg_math_core.pyd")
        for version in SUPPORTED_MAYA
    )
    missing = [path for path in required if not os.path.isfile(path)]
    if missing:
        raise RuntimeError(u"安装包不完整，缺少文件：\n{}".format(missing[0]))

    manifest_path = os.path.join(PACKAGE_DIR, "SHA256.txt")
    if not os.path.isfile(manifest_path):
        raise RuntimeError(u"安装包不完整：找不到 SHA256.txt")
    expected = {}
    with open(manifest_path, "rb") as handle:
        content = handle.read().decode("utf-8-sig")
    for line in content.splitlines():
        if "  " not in line:
            continue
        digest, relative = line.split("  ", 1)
        expected[relative.replace("/", "\\")] = digest.upper()
    actual = {
        "Bake_Groups\\" + relative.replace("/", "\\"): path
        for relative, path in _source_files(SOURCE_DIR).items()
    }
    expected_plugin = {
        relative: digest for relative, digest in expected.items()
        if relative.startswith("Bake_Groups\\")
    }
    if set(actual) != set(expected_plugin):
        different = sorted(set(actual).symmetric_difference(set(expected_plugin)))
        raise RuntimeError(u"安装包文件列表不完整：\n{}".format(different[0]))
    for manifest_name, path in actual.items():
        if expected.get(manifest_name) != _sha256(path):
            raise RuntimeError(u"安装包校验失败，文件可能损坏：\n{}".format(manifest_name))


def _check_loaded_binary():
    loaded = sys.modules.get("bg_math_core")
    loaded_path = getattr(loaded, "__file__", None) if loaded else None
    if not loaded_path or not _path_is_inside(loaded_path, TARGET_DIR):
        return
    relative = os.path.relpath(os.path.normpath(loaded_path), TARGET_DIR)
    source = os.path.join(SOURCE_DIR, relative)
    target = os.path.join(TARGET_DIR, relative)
    if os.path.isfile(source) and os.path.isfile(target) and _sha256(source) != _sha256(target):
        raise RuntimeError(
            u"当前 Maya 已加载另一版本的 Bake Master 核心文件。\n\n"
            u"为防止版本混装，请关闭 Maya，再双击“安装_Bake_Groups.bat”。"
        )


def _copy_plugin():
    _validate_source()
    _check_loaded_binary()

    backup_dir = TARGET_DIR + ".backup_" + uuid.uuid4().hex
    had_existing = os.path.isdir(TARGET_DIR)
    keep_backup = False
    if had_existing:
        try:
            shutil.copytree(TARGET_DIR, backup_dir)
        except Exception:
            try:
                if os.path.isdir(backup_dir):
                    shutil.rmtree(backup_dir)
            except Exception:
                pass
            raise
    try:
        _sync_tree_exact(SOURCE_DIR, TARGET_DIR)
    except Exception:
        try:
            if had_existing and os.path.isdir(backup_dir):
                _sync_tree_exact(backup_dir, TARGET_DIR)
            elif os.path.isdir(TARGET_DIR):
                shutil.rmtree(TARGET_DIR)
        except Exception:
            traceback.print_exc()
            keep_backup = True
            raise RuntimeError(
                u"安装失败且自动回滚未完成。旧文件备份位于：\n{}".format(backup_dir)
            )
        raise
    finally:
        try:
            if not keep_backup and os.path.isdir(backup_dir):
                shutil.rmtree(backup_dir)
        except Exception:
            pass


def _shelf_command():
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


def _create_shelf_button():
    shelf_top = mel.eval('$tmpVar=$gShelfTopLevel')
    current_shelf = cmds.tabLayout(shelf_top, query=True, selectTab=True)
    for button in cmds.shelfLayout(current_shelf, query=True, childArray=True) or []:
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

    icon_path = os.path.join(TARGET_DIR, "Bake_Group.png")
    cmds.shelfButton(
        parent=current_shelf,
        label=BUTTON_LABEL,
        image=icon_path if os.path.isfile(icon_path) else "commandButton.png",
        annotation=BUTTON_ANNOTATION,
        command=_shelf_command(),
        sourceType="python",
    )
    try:
        mel.eval('saveAllShelves $gShelfTopLevel')
    except Exception:
        pass
    return current_shelf


def _launch():
    launcher_path = os.path.join(TARGET_DIR, "launcher.py")
    namespace = {"__file__": launcher_path, "__name__": "__main__"}
    with open(launcher_path, "rb") as handle:
        source = handle.read()
    if not isinstance(source, str):
        source = source.decode("utf-8", "replace")
    exec(compile(source, launcher_path, "exec"), namespace, namespace)


def install():
    _copy_plugin()
    shelf = _create_shelf_button()
    try:
        _launch()
    except Exception:
        traceback.print_exc()
        cmds.confirmDialog(
            title=u"Bake Master 已安装",
            message=u"插件文件和工具架按钮已安装，但自动打开失败。\n请重启 Maya 后点击 BAKE GROUPS 按钮。",
            button=[u"完成"],
            defaultButton=u"完成",
        )
        return
    cmds.confirmDialog(
        title=u"Bake Master 安装完成",
        message=u"已安装到：\n{}\n\n已在当前工具架“{}”创建 BAKE GROUPS 按钮。".format(TARGET_DIR, shelf),
        button=[u"完成"],
        defaultButton=u"完成",
    )


def onMayaDroppedPythonFile(*args):
    try:
        install()
    except Exception as exc:
        traceback.print_exc()
        cmds.confirmDialog(
            title=u"Bake Master 安装失败",
            message=u"安装失败：\n\n{}\n\n若仍无法解决，请打开脚本编辑器查看详细错误。".format(exc),
            button=[u"关闭"],
        )


if __name__ == "__main__":
    onMayaDroppedPythonFile()
