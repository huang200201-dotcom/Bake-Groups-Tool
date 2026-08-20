import sys
import os
import re
import json
import io
import maya.cmds as cmds

try:
    from PySide6 import QtWidgets, QtCore
except ImportError:
    try:
        from PySide2 import QtWidgets, QtCore
    except ImportError:
        QtWidgets = None
        QtCore = None


WORKSPACE_CONTROL_NAME = "BakeManagerUIWorkspaceControl"


class LicenseRejectedError(RuntimeError):
    """A normal activation refusal that must not roll back a healthy runtime."""

    pass


def _read_active_config(script_dir):
    active_path = os.path.join(script_dir, "active_version.json")
    if os.path.exists(active_path):
        try:
            with io.open(active_path, "r", encoding="utf-8-sig") as handle:
                return json.load(handle), active_path
        except Exception as exc:
            print("Bake Master active version load failed: {}".format(exc))
    return {}, active_path


def _write_active_config(active_path, data):
    temporary = active_path + ".tmp.{}".format(os.getpid())
    try:
        with io.open(temporary, "w", encoding="utf-8", newline="") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=4, sort_keys=True)
            handle.write(u"\n")
        os.replace(temporary, active_path)
    finally:
        try:
            if os.path.isfile(temporary):
                os.remove(temporary)
        except Exception:
            pass


def _active_runtime_dir(script_dir, data):
    active_version = data.get("active_version") or data.get("version")
    if active_version:
        candidate = os.path.normpath(os.path.join(script_dir, "versions", str(active_version)))
        if os.path.exists(os.path.join(candidate, "bg_main_window.py")):
            return candidate
    if os.path.isdir(os.path.join(script_dir, "versions")):
        raise RuntimeError(
            "Bake Master active version is missing or invalid. Reinstall the complete Bake_Groups folder."
        )
    return script_dir


def _path_is_inside(path, directory):
    path = os.path.normcase(os.path.realpath(path))
    directory = os.path.normcase(os.path.realpath(directory)).rstrip("\\/")
    return path == directory or path.startswith(directory + os.sep)


def _prepare_versioned_math_core_path():
    script_dir = os.path.normpath(os.path.dirname(os.path.abspath(__file__)))
    active_config, active_path = _read_active_config(script_dir)
    runtime_dir = _active_runtime_dir(script_dir, active_config)
    maya_version_raw = str(cmds.about(version=True))
    version_match = re.search(r"\d{4}", maya_version_raw)
    maya_version = version_match.group(0) if version_match else maya_version_raw
    bin_dir = os.path.join(runtime_dir, "bin", maya_version)
    versions_root = os.path.join(script_dir, "versions")
    for path in list(sys.path):
        if not path:
            continue
        if _path_is_inside(path, versions_root) or os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(script_dir)):
            while path in sys.path:
                sys.path.remove(path)
    if os.path.exists(os.path.join(bin_dir, "bg_math_core.pyd")):
        sys.path.insert(0, bin_dir)
    sys.path.insert(0, runtime_dir)
    return script_dir, runtime_dir, active_config, active_path


def _pending_update_requires_restart(active_config, runtime_dir):
    pending = active_config.get("pending_version")
    runtime_version = os.path.basename(runtime_dir)
    if not pending or str(pending) != runtime_version:
        return False
    loaded_core = sys.modules.get("bg_math_core")
    loaded_path = getattr(loaded_core, "__file__", None) if loaded_core else None
    return bool(loaded_path and not _path_is_inside(loaded_path, runtime_dir))


def _mark_runtime_healthy(active_config, active_path, runtime_dir):
    runtime_version = os.path.basename(runtime_dir)
    if str(active_config.get("pending_version") or "") != runtime_version:
        return
    active_config["pending_version"] = None
    active_config["failed_version"] = None
    active_config.pop("rollback_reason", None)
    _write_active_config(active_path, active_config)


def _rollback_pending_runtime(active_config, active_path, script_dir, runtime_dir, error):
    runtime_version = os.path.basename(runtime_dir)
    if str(active_config.get("pending_version") or "") != runtime_version:
        return False
    previous = str(active_config.get("previous_version") or "")
    previous_dir = os.path.join(script_dir, "versions", previous)
    if not previous or previous == runtime_version:
        return False
    if not os.path.isfile(os.path.join(previous_dir, "bg_main_window.py")):
        return False
    reason = "{}: {}".format(type(error).__name__, error).replace("\r", " ").replace("\n", " ")[:500]
    active_config["active_version"] = previous
    active_config["package_version"] = previous
    active_config["pending_version"] = None
    active_config["failed_version"] = runtime_version
    active_config["rollback_reason"] = reason
    _write_active_config(active_path, active_config)
    return True


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


def _ensure_license(runtime_dir):
    """Validate the signed machine-bound license before loading the UI."""
    try:
        import bg_license
    except Exception as exc:
        raise RuntimeError("Bake Master licensing module could not load: {}".format(exc))

    runtime_error_type = getattr(bg_license, "LicenseRuntimeError", ())
    try:
        result = bg_license.validate()
    except runtime_error_type as runtime_error:
        raise RuntimeError(str(runtime_error))
    except bg_license.LicenseError as license_error:
        if not QtWidgets:
            raise LicenseRejectedError(str(license_error))
        machine = bg_license.machine_code()
        clipboard = QtWidgets.QApplication.clipboard()
        if clipboard:
            clipboard.setText(machine)
        prompt = (
            "本机机器码已复制到剪贴板：\n\n{}\n\n"
            "请把机器码发给插件管理员，获得许可证 JSON 后粘贴到下面。"
        ).format(machine)
        license_text, accepted = QtWidgets.QInputDialog.getMultiLineText(
            None,
            "Bake Master 激活",
            prompt + "\n\n许可证内容：",
            "",
        )
        if not accepted or not license_text.strip():
            raise LicenseRejectedError("未激活 Bake Master。")
        try:
            license_data = json.loads(license_text)
            bg_license.save_license(license_data)
            result = bg_license.validate()
        except runtime_error_type as runtime_error:
            raise RuntimeError(str(runtime_error))
        except Exception as exc:
            raise LicenseRejectedError("许可证无效：{}".format(exc))

    try:
        bg_license.install_native_guard()
    except bg_license.LicenseError as native_error:
        raise RuntimeError(str(native_error))

    loaded_core = sys.modules.get("bg_math_core")
    loaded_path = getattr(loaded_core, "__file__", None) if loaded_core else None
    expected_bin_root = os.path.join(runtime_dir, "bin")
    if not loaded_path or not _path_is_inside(loaded_path, expected_bin_root):
        raise RuntimeError(
            "Bake Master native module was not loaded from the active runtime."
        )
    return result


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


def main():
    script_dir, runtime_dir, active_config, active_path = _prepare_versioned_math_core_path()
    print("Bake Master runtime: {}".format(runtime_dir))

    if _pending_update_requires_restart(active_config, runtime_dir):
        raise RuntimeError(
            "Bake Master update is ready. Restart Maya before launching the updated version."
        )

    if _restore_existing_ui(runtime_dir):
        _mark_runtime_healthy(active_config, active_path, runtime_dir)
        return

    _shutdown_existing_bake_groups_ui()

    modules_to_reload = [
        "bg_license",
        "bg_version",
        "bg_core",
        "bg_worker_hp",
        "bg_worker_lp",
        "bg_gt_matcher",
        "bg_final_export",
        "bg_ui_widgets",
        "bg_localization",
        "bg_update",
        "bg_mixins",
        "bg_main_window",
    ]

    try:
        for mod_name in modules_to_reload:
            if mod_name in sys.modules:
                del sys.modules[mod_name]
                print("Unloaded {}".format(mod_name))
            else:
                print("{} not loaded yet".format(mod_name))

        # Authorization imports and verifies the version-specific native module,
        # so both are covered by the pending-version startup transaction.
        _ensure_license(runtime_dir)
        import bg_main_window
        bg_main_window.main()
    except LicenseRejectedError:
        # Missing, revoked, expired, or user-cancelled activation does not prove
        # that the newly installed runtime is bad. Keep it pending for a later try.
        raise
    except Exception as exc:
        if _rollback_pending_runtime(active_config, active_path, script_dir, runtime_dir, exc):
            cmds.warning(
                "Bake Master could not start version {} and restored the previous version. Restart Maya.".format(
                    os.path.basename(runtime_dir)
                )
            )
            raise RuntimeError(
                "Bake Master update startup failed and was rolled back. Restart Maya to use the previous version."
            )
        raise
    else:
        _mark_runtime_healthy(active_config, active_path, runtime_dir)


if __name__ == "__main__":
    main()
