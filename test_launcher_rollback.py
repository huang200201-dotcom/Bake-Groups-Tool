from __future__ import absolute_import, division, print_function

import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest

try:
    from unittest import mock
except ImportError:  # pragma: no cover
    import mock


PLUGIN_ROOT = os.path.join(os.path.dirname(__file__), "plugin", "Bake_Groups")


def runtime_under_test():
    with io.open(
        os.path.join(PLUGIN_ROOT, "active_version.json"),
        "r",
        encoding="utf-8-sig",
    ) as stream:
        active_version = str(json.load(stream).get("active_version") or "")
    for version in ("1.3.14", active_version):
        candidate = os.path.join(PLUGIN_ROOT, "versions", version)
        if version and os.path.isfile(os.path.join(candidate, "launcher.py")):
            return candidate
    raise RuntimeError("No Bake Master runtime is available for launcher tests")


def load_launcher_module():
    maya_module = types.ModuleType("maya")
    cmds_module = types.ModuleType("maya.cmds")
    cmds_module.warning = lambda unused_message: None
    cmds_module.about = lambda **unused_kwargs: "2024"
    cmds_module.workspaceControl = lambda *unused_args, **unused_kwargs: False
    cmds_module.workspaceControlState = lambda *unused_args, **unused_kwargs: False
    cmds_module.deleteUI = lambda *unused_args, **unused_kwargs: None
    maya_module.cmds = cmds_module
    previous_maya = sys.modules.get("maya")
    previous_cmds = sys.modules.get("maya.cmds")
    sys.modules["maya"] = maya_module
    sys.modules["maya.cmds"] = cmds_module
    try:
        path = os.path.join(runtime_under_test(), "launcher.py")
        spec = importlib.util.spec_from_file_location("bg_launcher_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous_maya is None:
            sys.modules.pop("maya", None)
        else:
            sys.modules["maya"] = previous_maya
        if previous_cmds is None:
            sys.modules.pop("maya.cmds", None)
        else:
            sys.modules["maya.cmds"] = previous_cmds


launcher = load_launcher_module()


class LauncherRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="bg-launcher-test-")
        self.runtime_dir = os.path.join(self.tempdir, "versions", "1.3.14")
        os.makedirs(self.runtime_dir)
        self.active_path = os.path.join(self.tempdir, "active_version.json")
        self.active_config = {
            "active_version": "1.3.14",
            "pending_version": "1.3.14",
            "previous_version": "1.3.13",
        }

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tempdir)

    def _base_patches(self):
        return (
            mock.patch.object(
                launcher,
                "_prepare_versioned_math_core_path",
                return_value=(
                    self.tempdir,
                    self.runtime_dir,
                    self.active_config,
                    self.active_path,
                ),
            ),
            mock.patch.object(
                launcher, "_pending_update_requires_restart", return_value=False
            ),
            mock.patch.object(launcher, "_shutdown_existing_bake_groups_ui"),
        )

    def test_normal_license_rejection_does_not_rollback_pending_runtime(self):
        rollback = mock.Mock(return_value=True)
        patches = self._base_patches()
        with patches[0], patches[1], patches[2]:
            with mock.patch.object(
                launcher,
                "_ensure_license",
                side_effect=launcher.LicenseRejectedError("revoked"),
            ):
                with mock.patch.object(launcher, "_rollback_pending_runtime", rollback):
                    with self.assertRaises(launcher.LicenseRejectedError):
                        launcher.main()
        rollback.assert_not_called()

    def test_native_or_runtime_failure_rolls_back_pending_runtime(self):
        rollback = mock.Mock(return_value=True)
        patches = self._base_patches()
        with patches[0], patches[1], patches[2]:
            with mock.patch.object(
                launcher, "_ensure_license", side_effect=RuntimeError("bad pyd")
            ):
                with mock.patch.object(launcher, "_rollback_pending_runtime", rollback):
                    with self.assertRaisesRegex(RuntimeError, "rolled back"):
                        launcher.main()
        rollback.assert_called_once()

    def test_successful_ui_start_marks_pending_runtime_healthy(self):
        mark_healthy = mock.Mock()
        main_window = types.ModuleType("bg_main_window")
        main_window.main = mock.Mock()
        real_import = __import__("builtins").__import__

        def fake_import(name, *args, **kwargs):
            if name == "bg_main_window":
                return main_window
            return real_import(name, *args, **kwargs)

        patches = self._base_patches()
        with patches[0], patches[1], patches[2]:
            with mock.patch.object(launcher, "_ensure_license"):
                with mock.patch.object(launcher, "_mark_runtime_healthy", mark_healthy):
                    with mock.patch("builtins.__import__", side_effect=fake_import):
                        launcher.main()
        main_window.main.assert_called_once_with()
        mark_healthy.assert_called_once_with(
            self.active_config, self.active_path, self.runtime_dir
        )

    def test_license_rejection_is_classified_separately_without_qt(self):
        class LicenseError(RuntimeError):
            pass

        class LicenseRuntimeError(LicenseError):
            pass

        fake_license = types.ModuleType("bg_license")
        fake_license.LicenseError = LicenseError
        fake_license.LicenseRuntimeError = LicenseRuntimeError
        fake_license.validate = mock.Mock(side_effect=LicenseError("expired"))
        previous = sys.modules.get("bg_license")
        sys.modules["bg_license"] = fake_license
        try:
            with mock.patch.object(launcher, "QtWidgets", None):
                with self.assertRaises(launcher.LicenseRejectedError):
                    launcher._ensure_license(self.runtime_dir)
        finally:
            if previous is None:
                sys.modules.pop("bg_license", None)
            else:
                sys.modules["bg_license"] = previous

    def test_native_module_loaded_outside_runtime_is_a_startup_failure(self):
        class LicenseError(RuntimeError):
            pass

        class LicenseRuntimeError(LicenseError):
            pass

        fake_license = types.ModuleType("bg_license")
        fake_license.LicenseError = LicenseError
        fake_license.LicenseRuntimeError = LicenseRuntimeError
        fake_license.validate = mock.Mock(return_value={"online": True})
        fake_license.install_native_guard = mock.Mock(return_value=True)
        fake_core = types.ModuleType("bg_math_core")
        fake_core.__file__ = os.path.join(self.tempdir, "foreign", "bg_math_core.pyd")
        previous_license = sys.modules.get("bg_license")
        previous_core = sys.modules.get("bg_math_core")
        sys.modules["bg_license"] = fake_license
        sys.modules["bg_math_core"] = fake_core
        try:
            with self.assertRaisesRegex(RuntimeError, "active runtime"):
                launcher._ensure_license(self.runtime_dir)
        finally:
            if previous_license is None:
                sys.modules.pop("bg_license", None)
            else:
                sys.modules["bg_license"] = previous_license
            if previous_core is None:
                sys.modules.pop("bg_math_core", None)
            else:
                sys.modules["bg_math_core"] = previous_core


if __name__ == "__main__":
    unittest.main()
