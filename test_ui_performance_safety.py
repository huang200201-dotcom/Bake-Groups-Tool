from __future__ import absolute_import, division, print_function

import ast
import importlib.util
import json
import os
import sys
import time
import types
import unittest

try:
    from unittest import mock
except ImportError:  # pragma: no cover
    import mock


REPO_ROOT = os.path.dirname(__file__)
PLUGIN_ROOT = os.path.join(REPO_ROOT, "plugin", "Bake_Groups")
RUNTIME_ROOT = os.path.join(PLUGIN_ROOT, "versions", "1.3.17")
MAIN_WINDOW_PATH = os.path.join(RUNTIME_ROOT, "bg_main_window.py")
CORE_PATH = os.path.join(RUNTIME_ROOT, "bg_core.py")
MIXINS_PATH = os.path.join(RUNTIME_ROOT, "bg_mixins.py")
UI_WIDGETS_PATH = os.path.join(RUNTIME_ROOT, "bg_ui_widgets.py")


def read_text(path):
    with open(path, "r", encoding="utf-8-sig") as stream:
        return stream.read()


def class_method_source(path, class_name, method_name):
    source = read_text(path)
    tree = ast.parse(source, filename=path)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
                    lines = source.splitlines(True)
                    start = child.lineno - 1
                    definition = start
                    while definition < len(lines):
                        stripped = lines[definition].lstrip()
                        if (stripped.startswith("def {}(".format(method_name)) or
                                stripped.startswith("async def {}(".format(method_name))):
                            break
                        definition += 1
                    indent = child.col_offset
                    end = len(lines)
                    for index in range(definition + 1, len(lines)):
                        stripped = lines[index].strip()
                        if not stripped:
                            continue
                        line_indent = len(lines[index]) - len(lines[index].lstrip())
                        if line_indent <= indent:
                            end = index
                            break
                    return "".join(lines[start:end]), child
    raise AssertionError("{}.{} not found".format(class_name, method_name))


def build_method_harness(path, class_name, method_names, namespace):
    source = read_text(path)
    tree = ast.parse(source, filename=path)
    methods = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            methods = [
                child for child in node.body
                if isinstance(child, ast.FunctionDef) and child.name in method_names
            ]
            break
    if len(methods) != len(method_names):
        raise AssertionError("Could not extract all requested methods")
    harness = ast.ClassDef(
        name="Harness",
        bases=[ast.Name(id="object", ctx=ast.Load())],
        keywords=[],
        body=methods,
        decorator_list=[],
    )
    module = ast.Module(body=[harness], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, path, "exec"), namespace)
    return namespace["Harness"]


def load_launcher(path):
    maya_module = types.ModuleType("maya")
    cmds_module = types.ModuleType("maya.cmds")
    cmds_module.about = lambda **unused_kwargs: "2024"
    cmds_module.warning = lambda unused_message: None
    cmds_module.workspaceControl = lambda *unused_args, **unused_kwargs: False
    cmds_module.workspaceControlState = lambda *unused_args, **unused_kwargs: False
    cmds_module.deleteUI = lambda *unused_args, **unused_kwargs: None
    maya_module.cmds = cmds_module
    old_maya = sys.modules.get("maya")
    old_cmds = sys.modules.get("maya.cmds")
    sys.modules["maya"] = maya_module
    sys.modules["maya.cmds"] = cmds_module
    try:
        name = "launcher_1315_{}".format(abs(hash(path)))
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if old_maya is None:
            sys.modules.pop("maya", None)
        else:
            sys.modules["maya"] = old_maya
        if old_cmds is None:
            sys.modules.pop("maya.cmds", None)
        else:
            sys.modules["maya.cmds"] = old_cmds


class StaticSafetyTests(unittest.TestCase):
    def test_version_and_root_launchers_stay_in_sync(self):
        self.assertEqual(
            read_text(os.path.join(PLUGIN_ROOT, "launcher.py")),
            read_text(os.path.join(RUNTIME_ROOT, "launcher.py")),
        )

    def test_mixins_do_not_reload_dependencies_at_import_time(self):
        source = read_text(MIXINS_PATH)
        self.assertNotIn("modules_to_reload", source)
        self.assertNotIn("reload(sys.modules", source)

    def test_undo_uses_window_shortcut_without_scene_snapshot_or_global_filter(self):
        source = read_text(MAIN_WINDOW_PATH)
        self.assertIn("QtCore.Qt.WidgetWithChildrenShortcut", source)
        self.assertNotIn("installEventFilter", source)
        self.assertNotIn("def eventFilter", source)
        self.assertNotIn("capture_bg_undo_snapshot", source)
        self.assertNotIn("restore_bg_undo_snapshot", source)
        self.assertNotIn("_bg_undo_candidate_nodes", source)
        state_source, unused_node = class_method_source(
            MAIN_WINDOW_PATH, "BakeManagerUI", "capture_bg_undo_state"
        )
        self.assertNotIn("listRelatives", state_source)
        method_source, _ = class_method_source(
            MAIN_WINDOW_PATH, "BakeManagerUI", "run_undoable_bg_action"
        )
        self.assertIn("bg_core.undo_chunk", method_source)
        self.assertIn("_create_bg_undo_marker", method_source)
        self.assertNotIn("listRelatives", method_source)
        self.assertNotIn("getAttr", method_source)

    def test_maya_undo_redo_and_save_callbacks_are_registered(self):
        source = read_text(MAIN_WINDOW_PATH)
        self.assertIn('(\"Undo\", self._on_maya_undo)', source)
        self.assertIn('(\"Redo\", self._on_maya_redo)', source)
        self.assertIn("MSceneMessage.kBeforeSave", source)
        self.assertIn("MSceneMessage.kAfterSave", source)

    def test_scene_undo_bridge_is_persistent_and_loads_before_sidecar_data(self):
        core_source = read_text(CORE_PATH)
        self.assertIn('UNDO_BRIDGE_NODE = "BakeMasterSessionBridge"', core_source)
        self.assertIn('UNDO_BRIDGE_STATE_ATTR = "bgBakeMasterUndoState"', core_source)
        self.assertIn('LEGACY_UNDO_MARKER_ATTR = "bgBakeMasterUndoToken"', core_source)
        save_source, unused_node = class_method_source(
            CORE_PATH, "BakeSessionModel", "save"
        )
        self.assertIn("write_undo_state", save_source)
        load_source, unused_node = class_method_source(
            CORE_PATH, "BakeSessionModel", "load"
        )
        self.assertLess(
            load_source.index("read_undo_state"),
            load_source.index("get_json_path"),
        )
        self.assertIn("preserve_modified=True", load_source)

    def test_maya_file_info_json_extra_escape_layer_is_decoded(self):
        Harness = build_method_harness(
            CORE_PATH,
            "BakeSessionModel",
            {"_decode_file_info_json"},
            {"json": json},
        )
        expected = [{"id": "pair", "base": "Test"}]
        self.assertEqual(expected, Harness._decode_file_info_json(json.dumps(expected)))
        maya_escaped = r'[{\"id\":\"pair\",\"base\":\"Test\"}]'
        self.assertEqual(expected, Harness._decode_file_info_json(maya_escaped))

    def test_preview_cleanup_has_no_history_delete_and_log_is_bounded(self):
        source = read_text(MAIN_WINDOW_PATH)
        remove_source, _ = class_method_source(
            MAIN_WINDOW_PATH, "BakeManagerUI", "remove_subgroup_preview_color_set"
        )
        self.assertNotIn("cmds.delete", remove_source)
        self.assertNotIn("constructionHistory", remove_source)
        self.assertIn("setMaximumBlockCount(3000)", source)

    def test_scene_generation_and_before_scene_callbacks_are_present(self):
        main_source = read_text(MAIN_WINDOW_PATH)
        mixin_source = read_text(MIXINS_PATH)
        self.assertIn("MSceneMessage.kBeforeOpen", main_source)
        self.assertIn("MSceneMessage.kBeforeNew", main_source)
        self.assertIn('"NewSceneOpened"', main_source)
        self.assertIn("_scene_generation", main_source)
        self.assertIn("_bg_scene_generation", mixin_source)
        self.assertIn("_worker_matches_scene", mixin_source)

    def test_icons_have_a_module_cache(self):
        source = read_text(UI_WIDGETS_PATH)
        self.assertIn("_ICON_CACHE = {}", source)
        self.assertIn("_ICON_CACHE.get", source)


class LauncherFastPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.launcher = load_launcher(os.path.join(PLUGIN_ROOT, "launcher.py"))

    def test_existing_same_runtime_ui_is_restored(self):
        calls = []

        class FakeUI(object):
            _shutdown_for_reload_done = False

            def objectName(self):
                return "BakeManagerUI"

            def show(self):
                calls.append("show")

            def raise_(self):
                calls.append("raise")

            def activateWindow(self):
                calls.append("activate")

        module = types.ModuleType("bg_main_window")
        module.__file__ = os.path.join(RUNTIME_ROOT, "bg_main_window.py")
        module.bake_manager_ui = FakeUI()
        old_module = sys.modules.get("bg_main_window")
        sys.modules["bg_main_window"] = module
        try:
            with mock.patch.object(self.launcher, "QtWidgets", object()):
                with mock.patch.object(self.launcher, "_process_qt_events"):
                    self.assertTrue(self.launcher._restore_existing_ui(RUNTIME_ROOT))
        finally:
            if old_module is None:
                sys.modules.pop("bg_main_window", None)
            else:
                sys.modules["bg_main_window"] = old_module
        self.assertEqual(calls, ["show", "raise", "activate"])

    def test_main_fast_path_skips_shutdown_reload_and_license(self):
        active = {"active_version": "1.3.15"}
        active_path = os.path.join(PLUGIN_ROOT, "active_version.json")
        with mock.patch.object(
            self.launcher,
            "_prepare_versioned_math_core_path",
            return_value=(PLUGIN_ROOT, RUNTIME_ROOT, active, active_path),
        ):
            with mock.patch.object(self.launcher, "_pending_update_requires_restart", return_value=False):
                with mock.patch.object(self.launcher, "_restore_existing_ui", return_value=True):
                    with mock.patch.object(self.launcher, "_shutdown_existing_bake_groups_ui") as shutdown:
                        with mock.patch.object(self.launcher, "_ensure_license") as ensure_license:
                            with mock.patch.object(self.launcher, "_mark_runtime_healthy") as mark:
                                self.launcher.main()
        shutdown.assert_not_called()
        ensure_license.assert_not_called()
        mark.assert_called_once_with(active, active_path, RUNTIME_ROOT)


class UndoSafetyBehaviorTests(unittest.TestCase):
    def test_mismatched_maya_undo_top_is_never_undone_or_popped(self):
        undo_calls = []

        class FakeApplication(object):
            @staticmethod
            def focusWidget():
                return object()

        fake_cmds = types.SimpleNamespace(
            undo=lambda: undo_calls.append("undo"),
            inViewMessage=lambda **unused_kwargs: None,
        )
        fake_l10n = types.SimpleNamespace(text=lambda value: value)
        Harness = build_method_harness(
            MAIN_WINDOW_PATH,
            "BakeManagerUI",
            {"undo_last_bg_action"},
            {
                "QtWidgets": types.SimpleNamespace(QApplication=FakeApplication),
                "cmds": fake_cmds,
                "bg_l10n": fake_l10n,
            },
        )
        instance = Harness()
        instance.bg_undo_stack = [{"action": "Plugin action", "undo_name": "BakeMaster_1"}]
        instance._bg_undo_restoring = False
        instance.is_bg_undo_focus_inside_window = lambda unused_focus: True
        instance.is_bg_undo_editable_focus = lambda unused_focus: False
        instance._bg_undo_entry_is_current = lambda unused_entry: False
        instance.update_bg_undo_button = lambda: None
        instance.log = lambda *unused_args: None
        instance.reload_data_from_scene = lambda: None

        self.assertTrue(instance.undo_last_bg_action())
        self.assertEqual(undo_calls, ["undo"])
        self.assertEqual(len(instance.bg_undo_stack), 1)

    def test_metadata_only_action_restores_state_without_maya_undo(self):
        undo_calls = []
        restored = []

        class FakeApplication(object):
            @staticmethod
            def focusWidget():
                return object()

        fake_cmds = types.SimpleNamespace(
            undo=lambda: undo_calls.append("undo"),
            inViewMessage=lambda **unused_kwargs: None,
        )
        Harness = build_method_harness(
            MAIN_WINDOW_PATH,
            "BakeManagerUI",
            {"undo_last_bg_action"},
            {
                "QtWidgets": types.SimpleNamespace(QApplication=FakeApplication),
                "cmds": fake_cmds,
                "bg_l10n": types.SimpleNamespace(text=lambda value: value),
            },
        )
        instance = Harness()
        state = {"root_pairs": [{"id": "before"}]}
        instance.bg_undo_stack = [{
            "action": "Toggle Lock",
            "undo_name": None,
            "metadata_only": True,
            "maya_top": "UserMove",
            "state": state,
        }]
        instance._bg_undo_restoring = False
        instance.is_bg_undo_focus_inside_window = lambda unused_focus: True
        instance.is_bg_undo_editable_focus = lambda unused_focus: False
        instance._bg_undo_entry_is_current = lambda unused_entry: True
        instance.update_bg_undo_button = lambda: None
        instance.log = lambda *unused_args: None
        instance.restore_bg_undo_state = lambda value: restored.append(value)

        self.assertTrue(instance.undo_last_bg_action())
        self.assertEqual([], undo_calls)
        self.assertEqual([state], restored)
        self.assertEqual([], instance.bg_undo_stack)

    def test_external_maya_undo_and_redo_restore_metadata_states(self):
        class FakeCmds(object):
            def __init__(self):
                self.mode = "undo"

            def undoInfo(self, **kwargs):
                if kwargs.get("redoName"):
                    return "BakeMaster_00000001_Rename"
                if kwargs.get("undoName"):
                    return "BakeMaster_00000001_Rename"
                return ""

        fake_cmds = FakeCmds()
        Harness = build_method_harness(
            MAIN_WINDOW_PATH,
            "BakeManagerUI",
            {"_stack_entry_matches_undo_name", "_on_maya_undo", "_on_maya_redo"},
            {"cmds": fake_cmds},
        )
        before = {"root_pairs": [{"base": "Old"}]}
        after = {"root_pairs": [{"base": "New"}]}
        entry = {
            "undo_name": "BakeMaster_00000001_Rename",
            "state_before": before,
            "state_after": after,
        }
        instance = Harness()
        instance._bg_undo_restoring = False
        instance._is_closing = False
        instance.bg_undo_stack = [entry]
        instance.bg_redo_stack = []
        restored = []
        instance.restore_bg_undo_state = lambda state: restored.append(state)
        instance._restore_bg_undo_bridge_state = lambda: False
        instance.log = lambda *unused_args: None
        instance.update_bg_undo_button = lambda: None

        instance._on_maya_undo()
        self.assertEqual([before], restored)
        self.assertEqual([], instance.bg_undo_stack)
        self.assertEqual([entry], instance.bg_redo_stack)

        instance._on_maya_redo()
        self.assertEqual([before, after], restored)
        self.assertEqual([entry], instance.bg_undo_stack)
        self.assertEqual([], instance.bg_redo_stack)

    def test_reopened_window_restores_bridge_without_in_memory_undo_stack(self):
        class FakeCmds(object):
            @staticmethod
            def undoInfo(**kwargs):
                if kwargs.get("redoName"):
                    return "BakeMaster_closed_window_action"
                if kwargs.get("undoName"):
                    return "BakeMaster_closed_window_action"
                return ""

        class FakeSessionModel(object):
            state = None
            payload = None

            @classmethod
            def read_undo_state(cls):
                return cls.state, cls.payload

        fake_core = types.SimpleNamespace(BakeSessionModel=FakeSessionModel)
        Harness = build_method_harness(
            MAIN_WINDOW_PATH,
            "BakeManagerUI",
            {
                "_stack_entry_matches_undo_name",
                "_restore_bg_undo_bridge_state",
                "_on_maya_undo",
                "_on_maya_redo",
            },
            {"cmds": FakeCmds(), "bg_core": fake_core},
        )
        before = {"root_pairs": [{"base": "Before"}]}
        after = {"root_pairs": [{"base": "After"}]}
        instance = Harness()
        instance._bg_undo_restoring = False
        instance._is_closing = False
        instance._bg_undo_bridge_payload = "after-payload"
        instance.bg_undo_stack = []
        instance.bg_redo_stack = []
        restored = []
        instance.restore_bg_undo_state = lambda state: restored.append(state)
        instance.update_bg_undo_button = lambda: None
        instance.log = lambda *unused_args: None

        FakeSessionModel.state = before
        FakeSessionModel.payload = "before-payload"
        instance._on_maya_undo()
        self.assertEqual([before], restored)
        self.assertEqual("before-payload", instance._bg_undo_bridge_payload)

        FakeSessionModel.state = after
        FakeSessionModel.payload = "after-payload"
        instance._on_maya_redo()
        self.assertEqual([before, after], restored)
        self.assertEqual("after-payload", instance._bg_undo_bridge_payload)


class PreparationUndoSafetyTests(unittest.TestCase):
    def test_query_only_preparation_chunk_is_not_recorded(self):
        class FakeCmds(object):
            @staticmethod
            def undoInfo(**unused_kwargs):
                return "UserMove"

        Harness = build_method_harness(
            MIXINS_PATH,
            "HPAnalysisMixin",
            {"_reset_prep_undo_tracking", "_mark_prep_undo_step"},
            {"cmds": FakeCmds()},
        )
        instance = Harness()
        instance._reset_prep_undo_tracking("hp_analysis")
        self.assertFalse(
            instance._mark_prep_undo_step("hp_analysis", "PrepareHPAnalysis_unique")
        )
        self.assertEqual(0, instance._prep_undo_depth_hp_analysis)
        self.assertEqual([], instance._prep_undo_names_hp_analysis)

    def test_user_maya_operation_is_not_undone_when_analysis_is_cancelled(self):
        class FakeCmds(object):
            def __init__(self):
                self.undo_calls = 0

            def undoInfo(self, **unused_kwargs):
                return "UserMove"

            def undo(self):
                self.undo_calls += 1

        fake_cmds = FakeCmds()
        Harness = build_method_harness(
            MIXINS_PATH,
            "HPAnalysisMixin",
            {"_revert_prep_undo"},
            {"cmds": fake_cmds},
        )
        instance = Harness()
        instance._prep_undo_depth_hp_analysis = 1
        instance._prep_undo_names_hp_analysis = ["PrepareHPAnalysis"]
        messages = []
        instance.log = lambda message, color=None: messages.append((message, color))
        instance.refresh_left_panel = lambda: None

        instance._revert_prep_undo("hp_analysis", "reverted")

        self.assertEqual(0, fake_cmds.undo_calls)
        self.assertTrue(any("undo queue changed" in message for message, unused in messages))
        self.assertEqual(0, instance._prep_undo_depth_hp_analysis)

    def test_analysis_dialogs_block_other_maya_edits(self):
        source = read_text(MIXINS_PATH)
        self.assertIn("self.progress_dlg.setWindowModality(QtCore.Qt.ApplicationModal)", source)
        self.assertIn("self.progress_dlg_lp.setWindowModality(QtCore.Qt.ApplicationModal)", source)

    def test_scene_change_clears_view_and_color_state(self):
        source, unused_node = class_method_source(
            MAIN_WINDOW_PATH, "BakeManagerUI", "_clear_scene_bound_state"
        )
        for token in (
            "self.is_final_view = False",
            "self.is_preview_active = False",
            "self.saved_subgroup_vis = {}",
            "self.subgroup_color_override_cache.clear()",
            "self._owned_subgroup_preview_color_sets.clear()",
            "self._sync_final_view_ui()",
        ):
            self.assertIn(token, source)

    def test_undo_restore_resynchronizes_final_view_controls(self):
        source, unused_node = class_method_source(
            MAIN_WINDOW_PATH, "BakeManagerUI", "restore_bg_undo_state"
        )
        self.assertIn("self._sync_final_view_ui()", source)

    def test_failed_scene_open_keeps_current_ui_state(self):
        scheduled = []

        class FakeTimer(object):
            @staticmethod
            def singleShot(unused_delay, callback):
                scheduled.append(callback)

        Harness = build_method_harness(
            MAIN_WINDOW_PATH,
            "BakeManagerUI",
            {"_on_scene_will_change", "_recover_failed_scene_change"},
            {"QtCore": types.SimpleNamespace(QTimer=FakeTimer)},
        )
        instance = Harness()
        instance._is_closing = False
        instance._scene_change_pending = False
        instance._scene_generation = 0
        instance.cb_color_subgroups = None
        instance.restore_subgroup_colors = lambda: None
        instance._cancel_scene_analysis = lambda revert_prep: None
        instance.update_bg_undo_button = lambda: None
        instance._clear_scene_bound_state = mock.Mock()

        instance._on_scene_will_change()
        self.assertTrue(instance._scene_change_pending)
        instance._clear_scene_bound_state.assert_not_called()
        self.assertEqual(1, len(scheduled))

        scheduled[0]()
        self.assertFalse(instance._scene_change_pending)
        instance._clear_scene_bound_state.assert_not_called()

    def test_scene_change_reverts_preparation_before_nonundoable_preview_cleanup(self):
        events = []

        class FakeTimer(object):
            @staticmethod
            def singleShot(unused_delay, unused_callback):
                pass

        class UndoSuspension(object):
            def __enter__(self):
                events.append("undo-off")

            def __exit__(self, unused_type, unused_value, unused_traceback):
                events.append("undo-on")

        Harness = build_method_harness(
            MAIN_WINDOW_PATH,
            "BakeManagerUI",
            {"_on_scene_will_change"},
            {"QtCore": types.SimpleNamespace(QTimer=FakeTimer)},
        )
        instance = Harness()
        instance._is_closing = False
        instance._scene_change_pending = False
        instance._scene_generation = 0
        instance._cancel_scene_analysis = lambda revert_prep: events.append(
            "prep-rollback" if revert_prep else "cancel"
        )
        instance._suspend_maya_undo_recording = lambda: UndoSuspension()
        instance.restore_subgroup_colors = lambda: events.append("preview-cleanup")

        instance._on_scene_will_change()

        self.assertEqual(
            ["prep-rollback", "undo-off", "preview-cleanup", "undo-on"],
            events,
        )


class WorkerShutdownSafetyTests(unittest.TestCase):
    class FakeSignal(object):
        def __init__(self):
            self.disconnect_calls = 0

        def disconnect(self):
            self.disconnect_calls += 1

    class FakeApplication(object):
        def __init__(self):
            self.process_calls = []

        def processEvents(self, *args):
            self.process_calls.append(args)

    class CooperativeWorker(object):
        def __init__(self, waits_before_exit=2):
            self.running = True
            self.waits_before_exit = waits_before_exit
            self.wait_calls = []
            self.stop_calls = 0
            self.interruption_calls = 0
            self.deleted = False
            for name in (
                    "progress_value", "progress_text", "result_ready", "failed",
                    "cancelled", "progress_changed", "installed", "error", "finished"):
                setattr(self, name, WorkerShutdownSafetyTests.FakeSignal())

        def isRunning(self):
            return self.running

        def stop(self):
            self.stop_calls += 1

        def requestInterruption(self):
            self.interruption_calls += 1

        def wait(self, milliseconds):
            self.wait_calls.append(milliseconds)
            if len(self.wait_calls) >= self.waits_before_exit:
                self.running = False
            return not self.running

        def deleteLater(self):
            self.deleted = True

    def _harness(self, application):
        Harness = build_method_harness(
            MAIN_WINDOW_PATH,
            "BakeManagerUI",
            {"_stop_worker_for_close"},
            {
                "time": time,
                "QtWidgets": types.SimpleNamespace(
                    QApplication=types.SimpleNamespace(instance=lambda: application)
                ),
                "QtCore": types.SimpleNamespace(
                    QEventLoop=types.SimpleNamespace(ExcludeUserInputEvents=1)
                ),
            },
        )
        instance = Harness()
        instance.log_messages = []
        instance.log = lambda message, color=None: instance.log_messages.append((message, color))
        return instance

    def test_cooperative_worker_is_cancelled_and_waited_without_long_ui_block(self):
        application = self.FakeApplication()
        instance = self._harness(application)
        worker = self.CooperativeWorker(waits_before_exit=2)
        instance.hp_worker = worker

        self.assertTrue(instance._stop_worker_for_close("hp_worker", timeout_ms=1000))

        self.assertEqual(1, worker.stop_calls)
        self.assertEqual(1, worker.interruption_calls)
        self.assertEqual([50, 50], worker.wait_calls)
        self.assertTrue(application.process_calls)
        self.assertTrue(worker.deleted)
        self.assertIsNone(instance.hp_worker)

    def test_timeout_retains_worker_and_blocks_concurrent_analysis(self):
        application = self.FakeApplication()
        instance = self._harness(application)
        worker = self.CooperativeWorker(waits_before_exit=1000000)
        instance.hp_worker = worker

        self.assertFalse(instance._stop_worker_for_close("hp_worker", timeout_ms=0))
        self.assertIs(instance.hp_worker, worker)
        self.assertFalse(worker.deleted)
        self.assertEqual(0, worker.finished.disconnect_calls)
        self.assertTrue(any("block another task" in message for message, unused in instance.log_messages))

        AnalysisHarness = build_method_harness(
            MIXINS_PATH,
            "HPAnalysisMixin",
            {"_analysis_worker_running"},
            {},
        )
        analysis = AnalysisHarness()
        analysis.hp_worker = worker
        analysis.lp_worker = None
        self.assertTrue(analysis._analysis_worker_running())

    def test_shutdown_refuses_to_close_while_analysis_worker_remains(self):
        Harness = build_method_harness(
            MAIN_WINDOW_PATH,
            "BakeManagerUI",
            {"shutdown_for_reload"},
            {},
        )
        instance = Harness()
        instance._shutdown_for_reload_done = False
        instance._is_closing = False
        instance._scene_generation = 0
        instance.update_timer = None
        instance._cancel_scene_analysis = lambda revert_prep: False
        instance.log = lambda *unused_args: None

        self.assertFalse(instance.shutdown_for_reload())
        self.assertFalse(instance._is_closing)


class PreviewColorOwnershipTests(unittest.TestCase):
    def test_preexisting_same_name_is_preserved(self):
        class FakeCmds(object):
            def __init__(self):
                self.color_sets = {"BG_Subgroup_Preview_test"}

            def ls(self, target, **kwargs):
                if kwargs.get("uuid"):
                    return ["target-uuid"]
                return [target]

            def objExists(self, unused_target):
                return True

            def polyColorSet(self, target, **kwargs):
                if kwargs.get("query") and kwargs.get("allColorSets"):
                    return sorted(self.color_sets)
                name = kwargs.get("colorSet")
                if kwargs.get("create"):
                    self.color_sets.add(name)
                elif kwargs.get("delete"):
                    self.color_sets.remove(name)
                return None

        fake_cmds = FakeCmds()
        Harness = build_method_harness(
            MAIN_WINDOW_PATH,
            "BakeManagerUI",
            {
                "_subgroup_preview_target_key",
                "_resolve_owned_subgroup_preview_target",
                "ensure_subgroup_color_set",
                "remove_subgroup_preview_color_set",
            },
            {"cmds": fake_cmds},
        )
        instance = Harness()
        instance._subgroup_preview_color_set_base = "BG_Subgroup_Preview_test"
        instance._owned_subgroup_preview_color_sets = {}

        created = instance.ensure_subgroup_color_set("mesh")
        self.assertEqual(created, "BG_Subgroup_Preview_test_2")
        self.assertTrue(instance.remove_subgroup_preview_color_set("mesh"))
        self.assertEqual(fake_cmds.color_sets, {"BG_Subgroup_Preview_test"})

    def test_save_temporarily_removes_and_then_restores_preview(self):
        scheduled = []
        modified_values = []

        class FakeTimer(object):
            @staticmethod
            def singleShot(unused_delay, callback):
                scheduled.append(callback)

        class FakeCmds(object):
            @staticmethod
            def file(**kwargs):
                if kwargs.get("query") and kwargs.get("modified"):
                    return True
                if "modified" in kwargs:
                    modified_values.append(kwargs["modified"])

        class FakeCheckBox(object):
            @staticmethod
            def isChecked():
                return True

        class NullContext(object):
            def __enter__(self):
                return self

            def __exit__(self, unused_type, unused_value, unused_traceback):
                return False

        Harness = build_method_harness(
            MAIN_WINDOW_PATH,
            "BakeManagerUI",
            {
                "_on_scene_before_save",
                "_on_scene_after_save",
                "_recover_preview_after_failed_save",
                "_restore_preview_after_save",
            },
            {
                "QtCore": types.SimpleNamespace(QTimer=FakeTimer),
                "cmds": FakeCmds(),
            },
        )
        instance = Harness()
        instance._is_closing = False
        instance._preview_save_restore_pending = False
        instance._preview_save_was_enabled = False
        instance._preview_save_modified_before = False
        instance.subgroup_color_override_cache = {"meshShape": {}}
        instance._owned_subgroup_preview_color_sets = {"mesh": {}}
        instance.cb_color_subgroups = FakeCheckBox()
        removed = []
        restored = []
        instance.restore_subgroup_colors = lambda: removed.append(True)
        instance.refresh_subgroup_color_preview = lambda reset_indices=False: restored.append(reset_indices)
        instance._suspend_maya_undo_recording = lambda: NullContext()
        instance.log = lambda *unused_args: None

        instance._on_scene_before_save()
        self.assertEqual([True], removed)
        self.assertTrue(instance._preview_save_restore_pending)
        self.assertEqual(1, len(scheduled))

        instance._on_scene_after_save()
        self.assertEqual([False], restored)
        self.assertEqual([False], modified_values)
        self.assertFalse(instance._preview_save_restore_pending)

        scheduled[0]()
        self.assertEqual([False], restored)


class UpdateInstallCompletionTests(unittest.TestCase):
    def test_late_interruption_does_not_override_committed_install(self):
        class FakeSignal(object):
            def __init__(self):
                self.values = []

            def emit(self, *args):
                self.values.append(args)

        class UpdateCancelled(Exception):
            pass

        result = {"version": "1.3.15"}

        class FakeClient(object):
            def __init__(self, **unused_kwargs):
                pass

            def download_and_install(self, unused_manifest, unused_maya_version):
                return result

        fake_qt = types.SimpleNamespace(
            QThread=object,
            Signal=lambda *unused_args: FakeSignal(),
        )
        fake_update = types.SimpleNamespace(
            UpdateClient=FakeClient,
            UpdateCancelled=UpdateCancelled,
        )
        source = read_text(MAIN_WINDOW_PATH)
        tree = ast.parse(source, filename=MAIN_WINDOW_PATH)
        worker_node = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "UpdateInstallWorker"
        )
        module = ast.Module(body=[worker_node], type_ignores=[])
        ast.fix_missing_locations(module)
        namespace = {"QtCore": fake_qt, "bg_update": fake_update}
        exec(compile(module, MAIN_WINDOW_PATH, "exec"), namespace)
        worker = object.__new__(namespace["UpdateInstallWorker"])
        worker.manifest = {}
        worker.maya_version = "2024"
        worker.token = "temporary"
        worker.progress_changed = FakeSignal()
        worker.installed = FakeSignal()
        worker.cancelled = FakeSignal()
        worker.error = FakeSignal()
        worker.isInterruptionRequested = lambda: True

        worker.run()

        self.assertEqual(worker.installed.values, [(result,)])
        self.assertEqual(worker.cancelled.values, [])
        self.assertIsNone(worker.token)


if __name__ == "__main__":
    unittest.main()
