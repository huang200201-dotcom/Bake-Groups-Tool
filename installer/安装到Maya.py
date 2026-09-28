# -*- coding: utf-8 -*-
"""Drag this file into Maya after extracting the complete release ZIP.

The filesystem installer is independent of Maya so it can be tested without
touching a user's scripts directory. It never opens scenes or license records.
"""
from __future__ import absolute_import, division, print_function

import contextlib
import hashlib
import io
import os
import re
import shutil
import stat
import sys
import traceback
import uuid


SUPPORTED_MAYA = ('2022', '2023', '2024', '2025', '2026', '2027')
BUTTON_LABEL = 'BAKE MASTER'
BUTTON_ANNOTATION = u'打开 Bake Master 开源版'
REQUIRED_FILES = ('__init__.py', 'launcher.py', 'bg_version.py', 'bg_main_window.py')
LEGACY_FILES = ('bg_license.py', 'bg_credentials.py', 'bg_update.py',
                'active_version.json', 'license_config.json', 'update_config.json')


class InstallError(RuntimeError):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _reject_links(path):
    """Reject symlinks/junctions in an existing path and its ancestors."""
    cursor = os.path.abspath(path)
    while True:
        if os.path.lexists(cursor):
            info = os.lstat(cursor)
            if (stat.S_ISLNK(info.st_mode) or
                    getattr(info, 'st_file_attributes', 0) & 0x400):
                raise InstallError('Symbolic links and junctions are not supported: ' + cursor)
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        cursor = parent


def _ignored(name):
    return name == '__pycache__' or name.lower().endswith('.pyc') or name.lower() == 'desktop.ini'


def _files(root, ignore_generated=True):
    root = os.path.abspath(root)
    _reject_links(root)
    if not os.path.isdir(root):
        raise InstallError('Directory is missing: ' + root)
    result, folded = {}, set()
    for current, dirs, files in os.walk(root, followlinks=False):
        # Validate even ignored paths before pruning, so cleanup can never
        # follow a hidden junction left in an old installation.
        for name in dirs + files:
            _reject_links(os.path.join(current, name))
        dirs[:] = sorted(name for name in dirs if not (ignore_generated and _ignored(name)))
        for name in sorted(files):
            if ignore_generated and _ignored(name):
                continue
            path = os.path.join(current, name)
            if not stat.S_ISREG(os.stat(path).st_mode):
                raise InstallError('Unsupported filesystem entry: ' + path)
            relative = os.path.relpath(path, root).replace(os.sep, '/')
            if relative.casefold() in folded:
                raise InstallError('Case-insensitive path collision: ' + relative)
            folded.add(relative.casefold())
            result[relative] = path
    return result


def _safe_relative(value):
    if not value or '\\' in value or ':' in value or value.startswith('/'):
        raise InstallError('Unsafe package path: ' + value)
    parts = value.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL'}
    reserved.update('COM' + str(n) for n in range(1, 10))
    reserved.update('LPT' + str(n) for n in range(1, 10))
    for part in parts:
        if (part in ('', '.', '..') or part.rstrip(' .') != part or
                any(ord(char) < 32 or char in '<>"|?*' for char in part) or
                part.split('.', 1)[0].upper() in reserved):
            raise InstallError('Unsafe package path: ' + value)
    return parts


def validate_package(package_dir):
    """Validate all shipped files, then return the flat plugin payload."""
    package_dir = os.path.abspath(package_dir)
    actual = _files(package_dir)
    manifest_path = actual.pop('SHA256.txt', None)
    if manifest_path is None:
        raise InstallError('Extract the complete release ZIP: SHA256.txt is missing')
    if os.path.getsize(manifest_path) > 2 * 1024 * 1024:
        raise InstallError('Package checksum manifest is too large')
    expected, folded = {}, set()
    with io.open(manifest_path, 'r', encoding='utf-8-sig') as stream:
        for line in stream:
            line = line.rstrip('\r\n')
            if not line:
                continue
            match = re.match(r'^([0-9a-fA-F]{64})  (.+)$', line)
            if not match:
                raise InstallError('Invalid SHA256.txt entry')
            digest, relative = match.groups()
            _safe_relative(relative)
            if relative.casefold() in folded:
                raise InstallError('Duplicate package path: ' + relative)
            folded.add(relative.casefold())
            expected[relative] = digest.lower()
    if set(actual) != set(expected):
        raise InstallError('Package file list is incomplete or contains extra files')
    for relative, path in actual.items():
        if _sha256(path) != expected[relative]:
            raise InstallError('Package checksum failed: ' + relative)
    payload = {name[len('Bake_Groups/'):]: path for name, path in actual.items()
               if name.startswith('Bake_Groups/')}
    required = list(REQUIRED_FILES) + ['bin/' + year + '/bg_math_core.pyd' for year in SUPPORTED_MAYA]
    missing = sorted(set(required) - set(payload))
    if missing:
        raise InstallError('Flat Bake_Groups payload is incomplete: ' + ', '.join(missing))
    if any(name.split('/')[0].casefold() == 'versions' or name.casefold() in LEGACY_FILES for name in payload):
        raise InstallError('This installer requires the open-source flat package')
    for name in required:
        if name != '__init__.py' and os.path.getsize(payload[name]) == 0:
            raise InstallError('Empty required file: ' + name)
    return payload


def _check_loaded_native(loaded_modules):
    for name, module in list(loaded_modules.items()):
        if name.rsplit('.', 1)[-1] == 'bg_math_core' and module is not None:
            raise InstallError(u'Maya 已加载 Bake Master 原生模块。请重启 Maya，先安装，再打开插件。')


def _managed_remove(path, scripts_dir, prefix):
    path, scripts_dir = os.path.abspath(path), os.path.abspath(scripts_dir)
    if os.path.dirname(path) != scripts_dir or not os.path.basename(path).startswith(prefix):
        raise InstallError('Refusing to clean a path outside this installation transaction')
    if os.path.lexists(path):
        _files(path, ignore_generated=False)
        shutil.rmtree(path)


@contextlib.contextmanager
def _install_lock(scripts_dir):
    path = os.path.join(scripts_dir, 'Bake_Groups.install.lock')
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        raise InstallError('Another installation may be running; cannot create installation lock: ' + str(exc))
    try:
        os.close(descriptor)
        yield
    finally:
        os.remove(path)


def install_package(package_dir, scripts_dir, loaded_modules=None):
    """Replace only scripts_dir/Bake_Groups, independent of old version numbers.

    Staging and backup share the target's parent for atomic directory renames.
    Never examines a user license location or invokes Maya scene operations.
    """
    package_dir, scripts_dir = os.path.abspath(package_dir), os.path.abspath(scripts_dir)
    payload = validate_package(package_dir)
    _check_loaded_native(sys.modules if loaded_modules is None else loaded_modules)
    _reject_links(scripts_dir)
    target = os.path.join(scripts_dir, 'Bake_Groups')
    _reject_links(target)
    try:
        overlap = os.path.commonpath((target, package_dir))
    except ValueError:
        overlap = None
    if overlap in (target, package_dir):
        raise InstallError('Extract the package outside the installed Bake_Groups directory')
    if os.path.lexists(target):
        old_files = _files(target, ignore_generated=False)
        if any(name.lower().endswith(('.ma', '.mb')) for name in old_files):
            raise InstallError('Maya scenes were found inside Bake_Groups. Move them to a project folder before installing.')
    if not os.path.isdir(scripts_dir):
        os.makedirs(scripts_dir)
    transaction = uuid.uuid4().hex
    stage = os.path.join(scripts_dir, 'Bake_Groups.install-' + transaction)
    backup = os.path.join(scripts_dir, 'Bake_Groups.backup-' + transaction)
    backup_saved = False
    retain_backup = False
    warning = None
    with _install_lock(scripts_dir):
        try:
            os.mkdir(stage)
            for relative, source in sorted(payload.items()):
                destination = os.path.join(stage, *_safe_relative(relative))
                parent = os.path.dirname(destination)
                if not os.path.isdir(parent):
                    os.makedirs(parent)
                shutil.copy2(source, destination)
                if _sha256(destination) != _sha256(source):
                    raise InstallError('Staged file verification failed: ' + relative)
            if os.path.isdir(target):
                os.replace(target, backup)
                backup_saved = True
            try:
                os.replace(stage, target)
            except Exception:
                if backup_saved:
                    try:
                        os.replace(backup, target)
                        backup_saved = False
                    except Exception as rollback_error:
                        retain_backup = True
                        raise InstallError('Installation failed; restore the previous files from {}. Rollback error: {}'.format(backup, rollback_error))
                raise
        finally:
            if os.path.isdir(stage):
                _managed_remove(stage, scripts_dir, 'Bake_Groups.install-')
            if backup_saved and not retain_backup:
                try:
                    _managed_remove(backup, scripts_dir, 'Bake_Groups.backup-')
                except (OSError, InstallError):
                    warning = 'Installed successfully; old files could not be removed: ' + backup
    return {'target': target, 'warning': warning}


def _shelf_command():
    return '''import os
import maya.cmds as cmds
path = os.path.join(cmds.internalVar(userScriptDir=True), "Bake_Groups", "launcher.py")
namespace = {"__file__": path, "__name__": "__main__"}
with open(path, "rb") as stream:
    exec(compile(stream.read(), path, "exec"), namespace, namespace)
'''


def create_shelf_button(target):
    import maya.cmds as cmds
    import maya.mel as mel
    shelf_top = mel.eval('$tmpVar=$gShelfTopLevel')
    shelf = cmds.tabLayout(shelf_top, query=True, selectTab=True)
    for button in cmds.shelfLayout(shelf, query=True, childArray=True) or []:
        if cmds.objectTypeUI(button) != 'shelfButton':
            continue
        command = cmds.shelfButton(button, query=True, command=True) or ''
        label = cmds.shelfButton(button, query=True, label=True)
        if label in (BUTTON_LABEL, 'BAKE GROUPS') and 'Bake_Groups' in command and 'launcher' in command:
            cmds.deleteUI(button)
    icon = os.path.join(target, 'Bake_Group.png')
    cmds.shelfButton(parent=shelf, label=BUTTON_LABEL,
                     annotation=BUTTON_ANNOTATION,
                     image=icon if os.path.isfile(icon) else 'commandButton.png',
                     command=_shelf_command(), sourceType='python')
    try:
        mel.eval('saveAllShelves $gShelfTopLevel')
    except RuntimeError:
        pass
    return shelf


def install():
    import maya.cmds as cmds
    package_dir = os.path.dirname(os.path.abspath(__file__))
    result = install_package(package_dir, cmds.internalVar(userScriptDir=True))
    try:
        create_shelf_button(result['target'])
    except Exception:
        traceback.print_exc()
        cmds.warning(u'文件已安装，但工具架按钮创建失败。重新拖入安装器可重试。')
    if result['warning']:
        cmds.warning(result['warning'])
    cmds.confirmDialog(title=u'Bake Master 安装完成',
                       message=u'已安装开源版到：\n{}\n\n点击 BAKE MASTER 工具架按钮打开。'.format(result['target']),
                       button=[u'完成'])
    return result


def onMayaDroppedPythonFile(*args):
    try:
        return install()
    except Exception as exc:
        import maya.cmds as cmds
        traceback.print_exc()
        cmds.confirmDialog(title=u'Bake Master 安装失败', message=str(exc), button=[u'关闭'])
        return None


if __name__ == '__main__':
    onMayaDroppedPythonFile()
