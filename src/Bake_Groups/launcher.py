"""Fast local startup for the single-source Bake Master distribution."""
import importlib
import os
import re
import sys
import maya.cmds as cmds

try:
    from PySide6 import QtCore, QtWidgets
except ImportError:
    try:
        from PySide2 import QtCore, QtWidgets
    except ImportError:
        QtCore = QtWidgets = None

WORKSPACE_CONTROL_NAME = "BakeManagerUIWorkspaceControl"
RUNTIME_MODULES = (
    "bg_version", "bg_core", "bg_worker_hp", "bg_worker_lp",
    "bg_final_export", "bg_final_groups", "bg_scene_state", "bg_cage",
    "bg_ui_widgets", "bg_localization", "bg_mixins", "bg_main_window",
)

def _path_is_inside(path, directory):
    path = os.path.normcase(os.path.realpath(path))
    directory = os.path.normcase(os.path.realpath(directory)).rstrip("\\/")
    return path == directory or path.startswith(directory + os.sep)

def _process_qt_events(cycles=3):
    if not QtWidgets:
        return
    app = QtWidgets.QApplication.instance()
    if not app:
        return
    for _ in range(max(1, int(cycles or 1))):
        try:
            app.processEvents(QtCore.QEventLoop.AllEvents, 50)
        except Exception:
            try:
                app.processEvents()
            except Exception:
                break

def _restore_existing_ui(runtime_dir):
    """Bring an already-loaded UI forward without reloading or revalidating it."""
    if not QtWidgets:
        return False
    old_mod = sys.modules.get("bg_main_window")
    loaded_path = getattr(old_mod, "__file__", None) if old_mod else None
    old_ui = getattr(old_mod, "bake_manager_ui", None) if old_mod else None
    if not old_ui or not loaded_path or not _path_is_inside(loaded_path, runtime_dir):
        return False
    try:
        if old_ui.objectName() != "BakeManagerUI":
            return False
        if getattr(old_ui, "_shutdown_for_reload_done", False):
            return False
    except RuntimeError:
        return False

    try:
        if cmds.workspaceControl(WORKSPACE_CONTROL_NAME, exists=True):
            cmds.workspaceControl(WORKSPACE_CONTROL_NAME, edit=True, restore=True)
    except Exception:
        pass
    for method_name in ("show", "raise_", "activateWindow"):
        try:
            getattr(old_ui, method_name)()
        except (AttributeError, RuntimeError):
            pass
    _process_qt_events(1)
    return True

def _delete_workspace_control():
    try:
        if cmds.workspaceControl(WORKSPACE_CONTROL_NAME, exists=True):
            cmds.deleteUI(WORKSPACE_CONTROL_NAME, control=True)
    except Exception as exc:
        print("Bake Master workspace control delete failed: {}".format(exc))
    try:
        if cmds.workspaceControlState(WORKSPACE_CONTROL_NAME, exists=True):
            cmds.workspaceControlState(WORKSPACE_CONTROL_NAME, remove=True)
    except Exception:
        pass

def _delete_old_qt_widgets():
    if not QtWidgets:
        return
    app = QtWidgets.QApplication.instance()
    if not app:
        return
    for widget in list(app.allWidgets()):
        try:
            object_name = widget.objectName()
        except RuntimeError:
            continue
        if object_name not in ("BakeManagerUI", WORKSPACE_CONTROL_NAME):
            continue
        try:
            if hasattr(widget, "shutdown_for_reload") and widget.shutdown_for_reload() is False:
                raise RuntimeError("Bake Master background work is still stopping.")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("Bake Master UI shutdown failed: {}".format(exc))
        try:
            widget.close()
        except RuntimeError:
            pass
        try:
            widget.setParent(None)
        except RuntimeError:
            pass
        try:
            widget.deleteLater()
        except RuntimeError:
            pass

def _shutdown_existing_bake_groups_ui():
    old_mod = sys.modules.get("bg_main_window")
    old_ui = getattr(old_mod, "bake_manager_ui", None) if old_mod else None
    if old_ui:
        try:
            if hasattr(old_ui, "shutdown_for_reload"):
                if old_ui.shutdown_for_reload() is False:
                    raise RuntimeError("Bake Master background work is still stopping.")
        except Exception as exc:
            print("Bake Master UI shutdown failed: {}".format(exc))
            raise
        try:
            old_ui.close()
        except RuntimeError:
            pass
        try:
            old_ui.setParent(None)
        except RuntimeError:
            pass
        try:
            old_ui.deleteLater()
        except RuntimeError:
            pass
        try:
            setattr(old_mod, "bake_manager_ui", None)
        except Exception:
            pass
    _delete_old_qt_widgets()
    _process_qt_events(4)
    _delete_workspace_control()
    _process_qt_events(2)

def _prepare_runtime():
    runtime_dir = os.path.dirname(os.path.abspath(__file__))
    match = re.search(r"\d{4}", str(cmds.about(version=True)))
    if match is None:
        raise RuntimeError("Cannot identify this Maya version.")
    year = match.group(0)
    native_dirs = [os.path.join(runtime_dir, "bin", year)]
    # Source checkouts use the same binaries produced by tools/build-native.ps1.
    if os.path.basename(os.path.dirname(runtime_dir)) == "src":
        native_dirs.append(os.path.join(runtime_dir, "..", "..", "build", "native", year))
    native_dir = next((os.path.realpath(path) for path in native_dirs
                       if os.path.isfile(os.path.join(path, "bg_math_core.pyd"))), None)
    if native_dir is None:
        raise RuntimeError(
            "Bake Master native acceleration for Maya {} is missing. "
            "Install the complete Windows package or build the native module.".format(year)
        )
    loaded_core = sys.modules.get("bg_math_core")
    loaded_path = getattr(loaded_core, "__file__", None)
    expected = os.path.normcase(os.path.realpath(os.path.join(native_dir, "bg_math_core.pyd")))
    if loaded_core is not None and (not loaded_path or
            os.path.normcase(os.path.realpath(loaded_path)) != expected):
        raise RuntimeError("Another Bake Master native module is loaded. Restart Maya before opening this installation.")
    for path in (runtime_dir, native_dir):
        while path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
    importlib.invalidate_caches()
    # Fail clearly instead of silently using slower, less accurate fallbacks.
    importlib.import_module("bg_math_core")
    return runtime_dir


def main():
    runtime_dir = _prepare_runtime()
    if _restore_existing_ui(runtime_dir):
        return sys.modules["bg_main_window"].bake_manager_ui
    _shutdown_existing_bake_groups_ui()
    for name in RUNTIME_MODULES:
        sys.modules.pop(name, None)
    module = importlib.import_module("bg_main_window")
    module.main()
    return getattr(module, "bake_manager_ui", None)


launch = main

if __name__ == "__main__":
    main()
