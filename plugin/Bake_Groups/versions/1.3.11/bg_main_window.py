# -*- coding: utf-8 -*-
from __future__ import print_function, division, absolute_import

import sys
import os
import json
import uuid
import contextlib
import re
import zipfile
import copy
from datetime import datetime

import maya.cmds as cmds
import bg_core
import bg_gt_matcher
import bg_final_export
import bg_localization as bg_l10n
import bg_version
import bg_update
import bg_credentials

from bg_worker_hp import HPGroupingWorker
from bg_worker_lp import LPMatchingWorker

try:
    from PySide6 import QtWidgets, QtCore, QtGui
    from maya.app.general.mayaMixin import MayaQWidgetDockableMixin
    QAction = QtGui.QAction
    QShortcut = QtGui.QShortcut
except ImportError:
    from PySide2 import QtWidgets, QtCore, QtGui
    from maya.app.general.mayaMixin import MayaQWidgetDockableMixin
    QAction = QtWidgets.QAction
    QShortcut = QtWidgets.QShortcut

from bg_ui_widgets import (CollapsibleSection, FinalMeshLineEdit, SubgroupButton,
                           ResolveNameDialog, configure_square_icon_button,
                           configure_symbol_button, get_icon)
from bg_mixins import (HPAnalysisMixin, LPMatchingMixin, FinalViewMixin,
                       ExportMixin, GroupManagementMixin, SceneInteractionMixin, TOCMixin)

try:
    import maya.api.OpenMaya as om
except Exception:
    om = None


class TOCNameDelegate(QtWidgets.QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = super(TOCNameDelegate, self).createEditor(parent, option, index)
        if isinstance(editor, QtWidgets.QLineEdit):
            editor.setMinimumWidth(0)
            editor.setStyleSheet("QLineEdit { padding: 2px 4px; }")
        return editor

    def updateEditorGeometry(self, editor, option, index):
        tree = self.parent()
        if tree and index.column() == 0:
            rect = QtCore.QRect(option.rect)
            right = tree.viewport().width() - tree.columnWidth(1) - 4
            rect.setRight(max(rect.left() + 160, right))
            rect.adjust(0, 1, 0, -1)
            editor.setGeometry(rect)
            return
        super(TOCNameDelegate, self).updateEditorGeometry(editor, option, index)


class _CooperativeInitTerminator(object):
    # Some PySide6 builds (confirmed on Maya 2025; not on Maya 2024/PySide2
    # or Maya 2027's PySide6 build) have QMainWindow.__init__ cooperatively
    # forward its leftover kwargs (including "parent") via super().__init__()
    # instead of just consuming them. HPAnalysisMixin/LPMatchingMixin/etc.
    # below have no __init__ of their own, so Python's MRO lookup skips
    # straight past them to object.__init__(), which only accepts self and
    # raises "TypeError: object.__init__() takes exactly one argument".
    # This class sits right after QtWidgets.QMainWindow in the MRO and
    # simply absorbs whatever gets forwarded, without forwarding further -
    # cheap insurance that does nothing on Maya versions where the crash
    # never happens in the first place.
    def __init__(self, *args, **kwargs):
        pass


class UpdateCheckWorker(QtCore.QThread):
    result_ready = QtCore.Signal(dict)
    error = QtCore.Signal(str)
    cancelled = QtCore.Signal()

    def __init__(self, current_version, maya_version, token, parent=None):
        super(UpdateCheckWorker, self).__init__(parent)
        self.current_version = current_version
        self.maya_version = maya_version
        self.token = token

    def run(self):
        try:
            client = bg_update.UpdateClient(
                token=self.token,
                cancelled=self.isInterruptionRequested,
            )
            result = client.check_for_update(self.current_version, self.maya_version)
            if self.isInterruptionRequested():
                self.cancelled.emit()
            else:
                self.result_ready.emit(result)
        except bg_update.UpdateCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.token = None

    def stop(self):
        self.requestInterruption()


class UpdateInstallWorker(QtCore.QThread):
    progress_changed = QtCore.Signal(int, str)
    installed = QtCore.Signal(dict)
    error = QtCore.Signal(str)
    cancelled = QtCore.Signal()

    def __init__(self, manifest, maya_version, token, parent=None):
        super(UpdateInstallWorker, self).__init__(parent)
        self.manifest = dict(manifest)
        self.maya_version = maya_version
        self.token = token

    def run(self):
        try:
            client = bg_update.UpdateClient(
                token=self.token,
                cancelled=self.isInterruptionRequested,
                progress=self.progress_changed.emit,
            )
            result = client.download_and_install(self.manifest, self.maya_version)
            if self.isInterruptionRequested():
                self.cancelled.emit()
            else:
                self.installed.emit(result)
        except bg_update.UpdateCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.token = None

    def stop(self):
        self.requestInterruption()


class BakeManagerUI(MayaQWidgetDockableMixin, QtWidgets.QMainWindow, _CooperativeInitTerminator,
                    HPAnalysisMixin, LPMatchingMixin, FinalViewMixin,
                    ExportMixin, GroupManagementMixin, SceneInteractionMixin, TOCMixin):
    def __init__(self, parent=None):
        super(BakeManagerUI, self).__init__(parent=parent)
        self.setWindowTitle("Bake Master")
        self.setObjectName("BakeManagerUI")
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        self.setMinimumSize(380, 400)
        self.resize(800, 800)
        self.setAcceptDrops(False)

        self.core = bg_core.MayaCore()
        self.root_pairs = bg_core.BakeSessionModel.load()
        self.active_root_id = None
        self.active_subgroup_name = None
        self.is_isolated = False
        self.script_jobs = []
        self.skip_delete_confirm = False
        self.picked_hp = None
        self.picked_lp = None
        self.hp_data_cache = {}
        self.lp_data_cache = {}
        self.is_preview_active = False
        self.is_final_view = False
        self.is_final_low_visible = False
        self.final_smooth_states = {}
        self.zbrush_triangle_threshold = 50
        self.last_debug_lines = []
        self.user_action_lines = []
        self.subgroup_color_override_cache = {}
        self.subgroup_color_index_map = {}
        self.active_material_visibility_filter = None
        self._is_closing = False
        self._dock_relayout_pending = False
        self._resize_relayout_pending = False
        self._shutdown_for_reload_done = False
        self.bg_undo_stack = []
        self._bg_undo_restoring = False
        self._bg_undo_running = False
        self._bg_undo_event_filter_app = None
        self._combined_check_skipped_chapters = set()
        self.update_check_worker = None
        self.update_install_worker = None
        self.update_progress_dialog = None
        self._pending_update = None
        self._update_check_manual = False
        self._update_config = {}
        self.update_timer = None

        self.init_ui()
        self.install_bg_undo_event_filter()
        self.apply_stylesheet()
        self.relax_dock_width_constraints()
        self.refresh_right_panel()
        self.refresh_left_panel()
        self.setup_script_jobs()
        self.setup_update_system()

    # ------------------------------------------------------------------------
    # UI Initialization
    # ------------------------------------------------------------------------
    def init_ui(self):
        main_widget = QtWidgets.QWidget()
        main_widget.setObjectName("AppSurface")
        self.setCentralWidget(main_widget)
        main_layout = QtWidgets.QVBoxLayout(main_widget)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        # Application header. Session/language controls stay global while the
        # work itself is separated into focused pages below.
        app_header = QtWidgets.QFrame()
        app_header.setObjectName("AppHeader")
        header_layout = QtWidgets.QHBoxLayout(app_header)
        header_layout.setContentsMargins(12, 8, 10, 8)
        header_layout.setSpacing(10)

        logo_label = QtWidgets.QLabel()
        logo_label.setObjectName("AppLogo")
        logo_label.setFixedSize(32, 32)
        logo_pixmap = get_icon("Bake_Group.png").pixmap(30, 30)
        if not logo_pixmap.isNull():
            logo_label.setPixmap(logo_pixmap)
        logo_label.setAlignment(QtCore.Qt.AlignCenter)
        header_layout.addWidget(logo_label)

        title_layout = QtWidgets.QVBoxLayout()
        title_layout.setSpacing(0)
        self.lbl_app_title = QtWidgets.QLabel("Bake Master")
        self.lbl_app_title.setObjectName("AppTitle")
        title_layout.addWidget(self.lbl_app_title)
        self.lbl_app_subtitle = QtWidgets.QLabel("Bake Master")
        self.lbl_app_subtitle.setObjectName("AppSubtitle")
        title_layout.addWidget(self.lbl_app_subtitle)
        header_layout.addLayout(title_layout)

        self.lbl_current_task = QtWidgets.QLabel("No Active Task")
        self.lbl_current_task.setObjectName("CurrentTask")
        self.lbl_current_task.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.lbl_current_task.setMinimumWidth(0)
        current_policy = self.lbl_current_task.sizePolicy()
        current_policy.setHorizontalPolicy(QtWidgets.QSizePolicy.Ignored)
        self.lbl_current_task.setSizePolicy(current_policy)
        header_layout.addWidget(self.lbl_current_task, 1)

        self.btn_update = QtWidgets.QToolButton()
        self.btn_update.setObjectName("HeaderIconButton")
        self.btn_update.setFixedSize(32, 32)
        self.btn_update.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_BrowserReload))
        self.btn_update.setIconSize(QtCore.QSize(17, 17))
        self.btn_update.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.btn_update.setProperty("bg_preserve_text", True)
        self.btn_update.setProperty("updateAvailable", False)
        self.update_menu = QtWidgets.QMenu(self.btn_update)
        self.action_check_update = self.update_menu.addAction("Check for Updates")
        self.action_check_update.triggered.connect(lambda checked=False: self.start_update_check(manual=True))
        self.action_auto_update = self.update_menu.addAction("Check Automatically")
        self.action_auto_update.setCheckable(True)
        self.action_auto_update.triggered.connect(self.set_auto_update_enabled)
        self.update_menu.addSeparator()
        self.action_open_update_repository = self.update_menu.addAction("Open Update Repository")
        self.action_open_update_repository.triggered.connect(self.open_update_repository)
        self.update_menu.aboutToShow.connect(self.refresh_update_menu)
        self.btn_update.setMenu(self.update_menu)
        header_layout.addWidget(self.btn_update)

        self.btn_language = QtWidgets.QPushButton("Language")
        self.btn_language.setObjectName("HeaderButton")
        self.btn_language.setFixedHeight(32)
        self.btn_language.setMinimumWidth(76)
        self.btn_language.clicked.connect(self.show_language_menu)
        header_layout.addWidget(self.btn_language)
        main_layout.addWidget(app_header)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.splitter.setObjectName("WorkspaceSplitter")
        self.splitter.setChildrenCollapsible(False)
        main_layout.addWidget(self.splitter, 1)

        self.workspace_tabs = QtWidgets.QTabWidget()
        self.workspace_tabs.setObjectName("WorkspaceTabs")
        self.workspace_tabs.setDocumentMode(True)
        self.workspace_tabs.tabBar().setUsesScrollButtons(False)
        self.workspace_tabs.tabBar().setElideMode(QtCore.Qt.ElideRight)
        self.splitter.addWidget(self.workspace_tabs)

        # ====================================================================
        # Automatic grouping workspace
        # ====================================================================
        self.left_panel = QtWidgets.QWidget()
        self.left_panel.setObjectName("AutomaticWorkspace")
        left_page_layout = QtWidgets.QVBoxLayout(self.left_panel)
        left_page_layout.setContentsMargins(0, 0, 0, 0)
        left_page_layout.setSpacing(0)
        self.auto_workspace_scroll = QtWidgets.QScrollArea()
        self.auto_workspace_scroll.setObjectName("AutomaticWorkspaceScroll")
        self.auto_workspace_scroll.setWidgetResizable(True)
        self.auto_workspace_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.auto_workspace_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.auto_workspace_content = QtWidgets.QWidget()
        self.auto_workspace_content.setObjectName("AutomaticWorkspaceContent")
        left_layout = QtWidgets.QVBoxLayout(self.auto_workspace_content)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(8)
        left_layout.setSizeConstraint(QtWidgets.QLayout.SetMinimumSize)
        self.auto_workspace_scroll.setWidget(self.auto_workspace_content)
        left_page_layout.addWidget(self.auto_workspace_scroll)

        # ---- Step 1: create an asset task ----
        self.source_section = CollapsibleSection("1. Create Asset Task")
        self.source_section.setObjectName("WorkflowSection")
        self.source_section.toggle_button.setChecked(True)
        self.source_section.on_pressed()
        source_layout = self.source_section.content_layout
        source_layout.setContentsMargins(10, 8, 10, 10)
        source_layout.setSpacing(8)

        source_grid = QtWidgets.QGridLayout()
        source_grid.setHorizontalSpacing(8)
        source_grid.setVerticalSpacing(7)
        lbl_hp_root = QtWidgets.QLabel("HP Root:")
        lbl_hp_root.setObjectName("FieldLabel")
        source_grid.addWidget(lbl_hp_root, 0, 0)
        self.le_picked_hp = QtWidgets.QLineEdit()
        self.le_picked_hp.setReadOnly(True)
        self.le_picked_hp.setPlaceholderText("Pick HP...")
        self.le_picked_hp.setMinimumWidth(80)
        btn_pick_hp = QtWidgets.QPushButton("Pick HP")
        btn_pick_hp.setObjectName("PickButton")
        btn_pick_hp.setMinimumWidth(96)
        btn_pick_hp.clicked.connect(lambda: self.pick_node("HP"))
        source_grid.addWidget(self.le_picked_hp, 0, 1)
        source_grid.addWidget(btn_pick_hp, 0, 2)

        lbl_lp_root = QtWidgets.QLabel("LP Root:")
        lbl_lp_root.setObjectName("FieldLabel")
        source_grid.addWidget(lbl_lp_root, 1, 0)
        self.le_picked_lp = QtWidgets.QLineEdit()
        self.le_picked_lp.setReadOnly(True)
        self.le_picked_lp.setPlaceholderText("Pick LP...")
        self.le_picked_lp.setMinimumWidth(80)
        btn_pick_lp = QtWidgets.QPushButton("Pick LP")
        btn_pick_lp.setObjectName("PickButton")
        btn_pick_lp.setMinimumWidth(96)
        btn_pick_lp.clicked.connect(lambda: self.pick_node("LP"))
        source_grid.addWidget(self.le_picked_lp, 1, 1)
        source_grid.addWidget(btn_pick_lp, 1, 2)
        source_grid.setColumnStretch(1, 1)
        source_layout.addLayout(source_grid)

        create_layout = QtWidgets.QGridLayout()
        create_layout.setHorizontalSpacing(8)
        btn_create_main = QtWidgets.QPushButton(" Create Pair from Picked")
        btn_create_main.setProperty("role", "primary")
        btn_create_main.setMinimumHeight(38)
        btn_create_main.clicked.connect(lambda checked=False: self.run_undoable_bg_action("Create Pair", self.create_root_pair_from_picked))
        create_layout.addWidget(btn_create_main, 0, 0)

        self.btn_create_by_material = QtWidgets.QPushButton("Create by Mat")
        self.btn_create_by_material.setProperty("role", "secondary")
        self.btn_create_by_material.setMinimumHeight(38)
        self.btn_create_by_material.clicked.connect(lambda checked=False: self.run_undoable_bg_action("Create by Mat", self.create_root_pairs_by_material_from_picked))
        create_layout.addWidget(self.btn_create_by_material, 0, 1)
        create_layout.setColumnStretch(0, 1)
        create_layout.setColumnStretch(1, 1)
        source_layout.addLayout(create_layout)

        tool_header = QtWidgets.QLabel("Preparation Tools")
        tool_header.setObjectName("MinorHeading")
        source_layout.addWidget(tool_header)

        tool_layout = QtWidgets.QHBoxLayout()
        tool_layout.setSpacing(6)
        self.cb_color_subgroups = QtWidgets.QCheckBox("Color HP")
        self.cb_color_subgroups.setChecked(False)
        self.cb_color_subgroups.toggled.connect(self.on_color_by_subgroups_toggled)
        self.cb_keep_hp_structure = QtWidgets.QCheckBox("Keep HP")
        self.cb_keep_hp_structure.setChecked(False)
        self.cb_keep_hp_structure.toggled.connect(self.on_keep_hp_structure_toggled)

        self.btn_combine_mesh = QtWidgets.QPushButton("Combine")
        self.btn_combine_mesh.clicked.connect(lambda checked=False: self.run_undoable_bg_action("Combine", self.tool_combine))
        self.btn_separate_mesh = QtWidgets.QPushButton("Separate")
        self.btn_separate_mesh.clicked.connect(lambda checked=False: self.run_undoable_bg_action("Separate", self.tool_separate))
        self.btn_find_zbrush = QtWidgets.QPushButton("Find ZBrush")
        self.btn_find_zbrush.clicked.connect(self.find_zbrush_meshes)
        self.btn_find_zbrush.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.btn_find_zbrush.customContextMenuRequested.connect(self.show_find_zbrush_context_menu)

        self.btn_check_before_analyze = QtWidgets.QPushButton("Check Before Analyze")
        self.btn_check_before_analyze.clicked.connect(lambda checked=False: self.run_undoable_bg_action("Check", self.run_pre_analysis_checks))

        prep_buttons = (
            self.btn_combine_mesh,
            self.btn_separate_mesh,
            self.btn_find_zbrush,
            self.btn_check_before_analyze,
        )
        for button in prep_buttons:
            button.setProperty("role", "secondary")
            button.setMinimumHeight(34)
            button.setMinimumWidth(104)
            button.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

        tool_layout.addWidget(self.btn_combine_mesh, 1)
        tool_layout.addWidget(self.btn_separate_mesh, 1)
        tool_layout.addWidget(self.btn_find_zbrush, 1)
        tool_layout.addWidget(self.btn_check_before_analyze, 1)

        tool_checks = QtWidgets.QVBoxLayout()
        tool_checks.setSpacing(0)
        tool_checks.addWidget(self.cb_color_subgroups)
        tool_checks.addWidget(self.cb_keep_hp_structure)
        tool_layout.addLayout(tool_checks)
        tool_layout.addStretch(1)
        source_layout.addLayout(tool_layout)
        left_layout.addWidget(self.source_section)

        # ---- Step 2: automatic grouping ----
        analysis_panel = QtWidgets.QFrame()
        analysis_panel.setObjectName("SectionPanel")
        analysis_layout = QtWidgets.QVBoxLayout(analysis_panel)
        analysis_layout.setContentsMargins(10, 9, 10, 10)
        analysis_layout.setSpacing(8)
        analysis_title = QtWidgets.QLabel("2. Automatic Grouping")
        analysis_title.setObjectName("SectionTitle")
        analysis_layout.addWidget(analysis_title)

        grouping_mode_layout = QtWidgets.QHBoxLayout()
        grouping_mode_label = QtWidgets.QLabel("Grouping Mode:")
        grouping_mode_label.setObjectName("FieldLabel")
        grouping_mode_label.setMinimumWidth(92)
        self.combo_grouping_mode = QtWidgets.QComboBox()
        self.combo_grouping_mode.setObjectName("Grouping Mode:")
        self.combo_grouping_mode.addItem("Smart Conflict-Safe", "conflict_safe")
        self.combo_grouping_mode.addItem("One LP per Group", "one_lp_per_group")
        self.combo_grouping_mode.addItem("Legacy Compact", "legacy_compact")
        self.combo_grouping_mode.currentIndexChanged.connect(self.on_grouping_mode_changed)
        grouping_mode_layout.addWidget(grouping_mode_label)
        grouping_mode_layout.addWidget(self.combo_grouping_mode, 1)
        analysis_layout.addLayout(grouping_mode_layout)

        s_layout = QtWidgets.QGridLayout()
        s_layout.setHorizontalSpacing(8)
        self.btn_run_hp = QtWidgets.QPushButton(" Analyze HP")
        self.btn_run_hp.setProperty("role", "primary")
        self.btn_run_hp.setMinimumHeight(40)
        self.btn_run_hp.clicked.connect(lambda: self.run_hp_analysis(None))

        self.btn_run_lp = QtWidgets.QPushButton(" Assign LP Meshes")
        self.btn_run_lp.setProperty("role", "success")
        self.btn_run_lp.setMinimumHeight(40)
        self.btn_run_lp.clicked.connect(self.run_lp_matching)
        s_layout.addWidget(self.btn_run_hp, 0, 0)
        s_layout.addWidget(self.btn_run_lp, 0, 1)
        s_layout.setColumnStretch(0, 1)
        s_layout.setColumnStretch(1, 1)
        analysis_layout.addLayout(s_layout)

        self.rebuild_algorithm_settings_ui(analysis_layout)
        left_layout.addWidget(analysis_panel)

        # ---- Step 3: inspect and finish ----
        groups_panel = QtWidgets.QFrame()
        groups_panel.setObjectName("SectionPanel")
        groups_layout = QtWidgets.QVBoxLayout(groups_panel)
        groups_layout.setContentsMargins(10, 9, 10, 10)
        groups_layout.setSpacing(8)

        groups_header = QtWidgets.QHBoxLayout()
        groups_title = QtWidgets.QLabel("3. Review Bake Master")
        groups_title.setObjectName("SectionTitle")
        groups_header.addWidget(groups_title)
        groups_header.addStretch(1)
        self.lbl_group_count = QtWidgets.QLabel("0 Groups")
        self.lbl_group_count.setObjectName("CountLabel")
        groups_header.addWidget(self.lbl_group_count)
        groups_layout.addLayout(groups_header)

        manual_group_layout = QtWidgets.QHBoxLayout()
        self.input_suffix = QtWidgets.QLineEdit()
        self.input_suffix.setPlaceholderText("Manual Group Name")
        self.input_suffix.setMinimumWidth(90)
        self.btn_create_subgroup = QtWidgets.QPushButton("Create Group")
        self.btn_create_subgroup.setProperty("role", "quiet")
        self.btn_create_subgroup.setMinimumWidth(104)
        self.btn_create_subgroup.clicked.connect(lambda checked=False: self.run_undoable_bg_action("Create Group", self.create_subgroup_pair))
        manual_group_layout.addWidget(self.input_suffix, 1)
        manual_group_layout.addWidget(self.btn_create_subgroup)
        groups_layout.addLayout(manual_group_layout)

        v_layout = QtWidgets.QHBoxLayout()
        v_layout.setSpacing(6)
        self.btn_toggle_hp = QtWidgets.QPushButton("HP Visible")
        self.btn_toggle_hp.setProperty("role", "segment")
        self.btn_toggle_hp.setCheckable(True)
        self.btn_toggle_hp.toggled.connect(lambda state: self.run_undoable_bg_action("HP Visibility", self.toggle_root_vis, "HP", state))
        self.btn_toggle_lp = QtWidgets.QPushButton("LP Visible")
        self.btn_toggle_lp.setProperty("role", "segment")
        self.btn_toggle_lp.setCheckable(True)
        self.btn_toggle_lp.toggled.connect(lambda state: self.run_undoable_bg_action("LP Visibility", self.toggle_root_vis, "LP", state))
        self.btn_toggle_groups = QtWidgets.QPushButton("Groups Vis")
        self.btn_toggle_groups.setProperty("role", "segment")
        self.btn_toggle_groups.setCheckable(True)
        self.btn_toggle_groups.toggled.connect(lambda state: self.run_undoable_bg_action("Groups Visibility", self.set_all_subgroups_vis, state))
        self.btn_toggle_groups.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.btn_toggle_groups.customContextMenuRequested.connect(self.show_groups_visibility_context_menu)
        v_layout.addWidget(self.btn_toggle_hp)
        v_layout.addWidget(self.btn_toggle_lp)
        v_layout.addWidget(self.btn_toggle_groups)
        groups_layout.addLayout(v_layout)

        self.subgroups_scroll = QtWidgets.QScrollArea()
        self.subgroups_scroll.setObjectName("GroupList")
        self.subgroups_scroll.setWidgetResizable(True)
        self.subgroups_scroll.setMinimumWidth(0)
        self.subgroups_scroll.setMinimumHeight(150)
        try:
            self.subgroups_scroll.setSizeAdjustPolicy(QtWidgets.QAbstractScrollArea.AdjustIgnored)
        except Exception:
            pass
        self.subgroups_widget = QtWidgets.QWidget()
        self.subgroups_widget.setObjectName("GroupListViewport")
        self.subgroups_widget.setMinimumWidth(0)
        self.subgroups_layout = QtWidgets.QVBoxLayout(self.subgroups_widget)
        self.subgroups_layout.setContentsMargins(4, 4, 4, 4)
        self.subgroups_layout.setSpacing(5)
        self.subgroups_layout.setAlignment(QtCore.Qt.AlignTop)
        self.subgroups_scroll.setWidget(self.subgroups_widget)
        groups_layout.addWidget(self.subgroups_scroll, 1)
        self.subgroups_scroll.setVisible(True)
        self.subgroups_widget.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.subgroups_widget.customContextMenuRequested.connect(self.show_subgroups_context_menu)

        action_layout = QtWidgets.QGridLayout()
        action_layout.setHorizontalSpacing(8)
        action_layout.setVerticalSpacing(6)
        self.btn_fs = QtWidgets.QPushButton("Find Sim")
        self.btn_fs.setMinimumHeight(36)
        self.btn_fs.setProperty("role", "secondary")
        self.btn_fs.clicked.connect(self.find_similar_meshes_ui)
        self.btn_fs.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.btn_fs.customContextMenuRequested.connect(self.toggle_find_sim_mode)
        self.find_sim_mode = "SIM"

        self.btn_add = QtWidgets.QPushButton("Add to Act")
        self.btn_add.setMinimumHeight(36)
        self.btn_add.setProperty("role", "quiet")
        self.btn_add.clicked.connect(lambda checked=False: self.run_undoable_bg_action("Add to Selected Group", self.add_to_selected_subgroup_ui))

        self.btn_combine_bake = QtWidgets.QPushButton("Combine Fin")
        self.btn_combine_bake.setMinimumHeight(36)
        self.btn_combine_bake.setProperty("role", "secondary")
        self.btn_combine_bake.clicked.connect(self.combine_all_subgroups_ui)

        self.btn_preview = QtWidgets.QPushButton("Smooth View")
        self.btn_preview.setMinimumHeight(36)
        self.btn_preview.setProperty("role", "secondary")
        self.btn_preview.clicked.connect(self.toggle_preview_smoothing)
        self.btn_preview.setVisible(False)

        self.btn_toggle_view = QtWidgets.QPushButton("Final Group")
        self.btn_toggle_view.setMinimumHeight(36)
        self.btn_toggle_view.setProperty("role", "primary")
        self.btn_toggle_view.clicked.connect(lambda checked=False: self.run_undoable_bg_action("Final Group", self.toggle_final_view))

        self.btn_process_final = QtWidgets.QPushButton("Export")
        self.btn_process_final.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.btn_process_final.customContextMenuRequested.connect(self.show_export_context_menu)
        self.btn_process_final.setMinimumHeight(36)
        self.btn_process_final.setProperty("role", "success")
        self.btn_process_final.clicked.connect(self.export_final_group_ui)
        self.btn_process_final.setVisible(False)

        action_layout.addWidget(self.btn_fs, 0, 0)
        action_layout.addWidget(self.btn_preview, 0, 0)
        action_layout.addWidget(self.btn_add, 0, 1)
        action_layout.addWidget(self.btn_process_final, 0, 1)
        action_layout.addWidget(self.btn_combine_bake, 1, 0)
        action_layout.addWidget(self.btn_toggle_view, 1, 1)
        action_layout.setColumnStretch(0, 1)
        action_layout.setColumnStretch(1, 1)
        groups_layout.addLayout(action_layout)
        left_layout.addWidget(groups_panel, 1)

        self.workspace_tabs.addTab(self.left_panel, "Automatic Grouping Workspace")

        # ====================================================================
        # Manual matching corrections workspace
        # ====================================================================
        self.gt_widget = bg_gt_matcher.GTWidget(self)
        self.gt_widget.setObjectName("ManualMatchingWorkspace")
        self.workspace_tabs.addTab(self.gt_widget, "Manual Match Fixes")

        # ====================================================================
        # Asset task workspace
        # ====================================================================
        self.right_panel = QtWidgets.QWidget()
        self.right_panel.setObjectName("AssetTasksWorkspace")
        right_layout = QtWidgets.QVBoxLayout(self.right_panel)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(8)

        task_panel = QtWidgets.QFrame()
        task_panel.setObjectName("SectionPanel")
        task_layout = QtWidgets.QVBoxLayout(task_panel)
        task_layout.setContentsMargins(10, 9, 10, 10)
        task_layout.setSpacing(8)
        lbl_toc = QtWidgets.QLabel("TABLE OF CONTENTS")
        lbl_toc.setObjectName("SectionTitle")
        task_layout.addWidget(lbl_toc)

        self.toc_tree = QtWidgets.QTreeWidget()
        self.toc_tree.setObjectName("AssetTaskTree")
        self.toc_tree.setHeaderHidden(True)
        self.toc_tree.setColumnCount(2)
        header = self.toc_tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Fixed)
        header.resizeSection(1, 28)
        self.toc_tree.setItemDelegate(TOCNameDelegate(self.toc_tree))
        self.toc_tree.setIndentation(14)
        self.toc_tree.setDragEnabled(False)
        self.toc_tree.setAcceptDrops(False)
        self.toc_tree.setDragDropMode(QtWidgets.QAbstractItemView.NoDragDrop)
        self.toc_tree.setFocusPolicy(QtCore.Qt.NoFocus)
        self.toc_tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.toc_tree.itemClicked.connect(self.on_toc_clicked)
        self.toc_tree.itemDoubleClicked.connect(self.on_toc_double_clicked)
        self.toc_tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.toc_tree.customContextMenuRequested.connect(self.show_toc_context_menu)
        self.toc_tree.itemChanged.connect(self.on_toc_item_changed)
        task_layout.addWidget(self.toc_tree, 1)

        self.shortcut_group = QShortcut(QtGui.QKeySequence("Ctrl+G"), self.toc_tree)
        self.shortcut_group.setContext(QtCore.Qt.WidgetShortcut)
        self.shortcut_group.activated.connect(lambda: self.run_undoable_bg_action("Group into Book", self.group_selected_into_book))
        self.shortcut_select_by_mesh = QShortcut(QtGui.QKeySequence("Ctrl+Shift+Z"), self)
        self.shortcut_select_by_mesh.setContext(QtCore.Qt.ApplicationShortcut)
        self.shortcut_select_by_mesh.activated.connect(self.select_subgroup_by_selected_mesh)

        session_buttons_layout = QtWidgets.QHBoxLayout()
        session_buttons_layout.setSpacing(8)
        self.btn_save_session = QtWidgets.QPushButton("Save Session")
        self.btn_save_session.setMinimumHeight(36)
        self.btn_save_session.setProperty("role", "primary")
        self.btn_save_session.clicked.connect(self.manual_save_session)
        session_buttons_layout.addWidget(self.btn_save_session)
        self.btn_load_session = QtWidgets.QPushButton("Load Session")
        self.btn_load_session.setMinimumHeight(36)
        self.btn_load_session.setProperty("role", "quiet")
        self.btn_load_session.clicked.connect(self.load_custom_session)
        session_buttons_layout.addWidget(self.btn_load_session)
        task_layout.addLayout(session_buttons_layout)
        right_layout.addWidget(task_panel, 1)
        self.workspace_tabs.addTab(self.right_panel, "Asset Tasks")

        # The log is available from every workspace but no longer permanently
        # consumes a large strip of the main workflow.
        self.log_section = CollapsibleSection("Run Log")
        self.log_section.setObjectName("LogSection")
        self.log_output = QtWidgets.QTextEdit()
        self.log_output.setObjectName("DebugLog")
        self.log_output.setReadOnly(True)
        self.log_output.setMinimumHeight(112)
        self.log_output.setMaximumHeight(150)
        self.log_output.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.log_output.customContextMenuRequested.connect(self.show_log_context_menu)
        self.log_section.addWidget(self.log_output)
        self.log_section.toggle_button.toggled.connect(self.resize_log_drawer)
        self.log_section.toggle_button.setChecked(False)
        self.log_section.on_pressed()
        self.splitter.addWidget(self.log_section)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([720, 32])

        bg_l10n.localize_widget_tree(self)
        self.refresh_workspace_texts()
        self.chk_ignore_floaters.setChecked(True)
        self.chk_material_slots.setChecked(False)
        self.update_responsive_layout(self.width())

    def refresh_workspace_texts(self):
        if hasattr(self, 'workspace_tabs'):
            self.workspace_tabs.setTabText(0, bg_l10n.text("Automatic Grouping Workspace"))
            self.workspace_tabs.setTabText(1, bg_l10n.text("Manual Match Fixes"))
            self.workspace_tabs.setTabText(2, bg_l10n.text("Asset Tasks"))
            self.workspace_tabs.setTabToolTip(0, bg_l10n.tooltip("Automatic Grouping Workspace"))
            self.workspace_tabs.setTabToolTip(1, bg_l10n.tooltip("Manual Match Fixes"))
            self.workspace_tabs.setTabToolTip(2, bg_l10n.tooltip("Asset Tasks"))
        self.update_workspace_context()

    def update_workspace_context(self):
        label = getattr(self, 'lbl_current_task', None)
        if not label:
            return
        pair = next((p for p in self.root_pairs if p.get('id') == self.active_root_id), None)
        if pair:
            value = bg_l10n.text("Current Task: {name}").format(name=pair.get('base', 'Unknown'))
            if getattr(self, '_source_collapsed_for', None) != pair.get('id'):
                self._source_collapsed_for = pair.get('id')
                if hasattr(self, 'source_section'):
                    self.source_section.toggle_button.setChecked(False)
        else:
            value = bg_l10n.text("No Active Task")
            if getattr(self, '_source_collapsed_for', None) is not None and hasattr(self, 'source_section'):
                self.source_section.toggle_button.setChecked(True)
            self._source_collapsed_for = None
        label.setText(value)
        label.setToolTip(value)

    def update_responsive_layout(self, width=None):
        width = self.width() if width is None else width
        compact_header = width < 520
        if hasattr(self, 'lbl_app_subtitle'):
            self.lbl_app_subtitle.setVisible(not compact_header)
        if hasattr(self, 'lbl_current_task'):
            self.lbl_current_task.setVisible(not compact_header)

    def resize_log_drawer(self, expanded):
        def apply_sizes():
            splitter = getattr(self, 'splitter', None)
            section = getattr(self, 'log_section', None)
            if not splitter or not section:
                return
            total = max(splitter.height(), 240)
            if expanded:
                desired = max(155, section.sizeHint().height())
                desired = min(desired, max(155, total // 2))
            else:
                desired = 34
            splitter.setSizes([max(100, total - desired), desired])
            splitter.updateGeometry()

        QtCore.QTimer.singleShot(0, apply_sizes)

    def relax_dock_width_constraints(self):
        policy_ignored = QtWidgets.QSizePolicy.Ignored
        policy_expanding = QtWidgets.QSizePolicy.Expanding
        targets = [
            (getattr(self, 'left_panel', None), 0, policy_expanding),
            (getattr(self, 'right_panel', None), 0, policy_expanding),
            (getattr(self, 'workspace_tabs', None), 0, policy_expanding),
            (getattr(self, 'splitter', None), 0, policy_expanding),
            (getattr(self, 'subgroups_scroll', None), 0, policy_expanding),
            (getattr(self, 'subgroups_widget', None), 0, policy_expanding),
            (getattr(self, 'toc_tree', None), 0, policy_expanding),
            (getattr(self, 'gt_widget', None), 0, policy_expanding),
            (getattr(self, 'log_output', None), 0, policy_expanding),
        ]
        for widget, min_width, horizontal_policy in targets:
            if not widget:
                continue
            try:
                widget.setMinimumWidth(min_width)
                sp = widget.sizePolicy()
                sp.setHorizontalPolicy(horizontal_policy)
                widget.setSizePolicy(sp)
                widget.updateGeometry()
            except RuntimeError:
                continue
            except Exception:
                continue
        flexible_types = (QtWidgets.QLineEdit, QtWidgets.QTextEdit,
                          QtWidgets.QAbstractItemView, QtWidgets.QScrollArea)
        for widget in self.findChildren(QtWidgets.QWidget):
            if not isinstance(widget, flexible_types):
                continue
            try:
                widget.setMinimumWidth(0)
                sp = widget.sizePolicy()
                sp.setHorizontalPolicy(policy_ignored)
                widget.setSizePolicy(sp)
            except RuntimeError:
                continue
            except Exception:
                continue
        for combo in self.findChildren(QtWidgets.QComboBox):
            try:
                if combo.maximumWidth() >= 16777215:
                    combo.setMinimumWidth(96)
                    sp = combo.sizePolicy()
                    sp.setHorizontalPolicy(policy_expanding)
                    combo.setSizePolicy(sp)
            except Exception:
                pass
        if hasattr(self, 'splitter'):
            try:
                self.splitter.setChildrenCollapsible(False)
                self.splitter.updateGeometry()
            except Exception:
                pass
        central = self.centralWidget()
        for widget in (central, getattr(self, 'left_panel', None), getattr(self, 'right_panel', None)):
            if not widget:
                continue
            try:
                layout = widget.layout()
                if layout:
                    layout.invalidate()
                    layout.activate()
                widget.updateGeometry()
            except Exception:
                pass

    def schedule_dock_relayout(self):
        if getattr(self, '_dock_relayout_pending', False):
            return
        self._dock_relayout_pending = True
        QtCore.QTimer.singleShot(0, self.run_dock_relayout)

    def run_dock_relayout(self):
        self._dock_relayout_pending = False
        if getattr(self, '_is_closing', False):
            return
        try:
            self.objectName()
        except RuntimeError:
            return
        self.relax_dock_width_constraints()
        try:
            if self.layout():
                self.layout().activate()
            self.updateGeometry()
        except Exception:
            pass

    def resizeEvent(self, event):
        super(BakeManagerUI, self).resizeEvent(event)
        self.update_responsive_layout(event.size().width())
        if getattr(self, '_is_closing', False):
            return
        if getattr(self, '_resize_relayout_pending', False):
            return
        self._resize_relayout_pending = True
        QtCore.QTimer.singleShot(60, self.run_resize_relayout)

    def run_resize_relayout(self):
        self._resize_relayout_pending = False
        log_bar = None
        keep_log_at_bottom = False
        try:
            log_bar = self.log_output.verticalScrollBar()
            keep_log_at_bottom = log_bar.value() >= (log_bar.maximum() - 2)
        except Exception:
            pass
        self.run_dock_relayout()
        if keep_log_at_bottom and log_bar is not None:
            QtCore.QTimer.singleShot(0, lambda bar=log_bar: bar.setValue(bar.maximum()))
        try:
            self.repaint()
        except Exception:
            pass

    def rebuild_algorithm_settings_ui(self, layout):
        self.algo_group = CollapsibleSection("Advanced Grouping Settings")
        self.algo_group.setObjectName("AdvancedSection")
        algo_label = QtWidgets.QLabel("HP Clustering Strategy:")
        algo_label.setObjectName("FieldLabel")
        self.combo_hp_strategy = QtWidgets.QComboBox()
        self.combo_hp_strategy.addItems([
            "Spatial Volume Match",
            "PCA Shape Alignment",
            "Topology Fingerprint"
        ])
        self.combo_hp_strategy.setCurrentIndex(1)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(7)
        grid.addWidget(algo_label, 0, 0)
        grid.addWidget(self.combo_hp_strategy, 0, 1)

        lbl_collision = QtWidgets.QLabel("HP Collision (%):")
        lbl_collision.setObjectName("FieldLabel")
        grid.addWidget(lbl_collision, 1, 0)
        self.spin_threshold = QtWidgets.QSpinBox()
        self.spin_threshold.setRange(0, 100)
        self.spin_threshold.setValue(15)
        self.spin_threshold.setFixedWidth(88)
        self.spin_threshold.setSuffix(" %")
        self.spin_collision_pct = self.spin_threshold
        grid.addWidget(self.spin_threshold, 1, 1)

        self.hp_group_limit = 12
        self.chk_ignore_floaters = QtWidgets.QCheckBox("Ignore Floaters")
        self.chk_ignore_floaters.setChecked(True)
        self.chk_material_slots = QtWidgets.QCheckBox("N_Mat")
        self.chk_material_slots.setChecked(False)
        option_layout = QtWidgets.QHBoxLayout()
        option_layout.setSpacing(14)
        option_layout.addWidget(self.chk_ignore_floaters)
        option_layout.addWidget(self.chk_material_slots)
        option_layout.addStretch(1)
        grid.addLayout(option_layout, 2, 0, 1, 2)

        self.lbl_hp_link_vtx = QtWidgets.QLabel("HP Link Vtx:")
        self.lbl_hp_link_vtx.setObjectName("FieldLabel")
        grid.addWidget(self.lbl_hp_link_vtx, 3, 0)
        self.spin_compound_link_verts = QtWidgets.QSpinBox()
        self.spin_compound_link_verts.setRange(1, 500)
        self.spin_compound_link_verts.setValue(8)
        self.spin_compound_link_verts.setObjectName("HP Link Vtx:")
        self.spin_compound_link_verts.setFixedWidth(88)
        grid.addWidget(self.spin_compound_link_verts, 3, 1)

        self.lbl_hp_link_dist = QtWidgets.QLabel("HP Link Dist (%):")
        self.lbl_hp_link_dist.setObjectName("FieldLabel")
        grid.addWidget(self.lbl_hp_link_dist, 4, 0)
        self.spin_compound_link_dist = QtWidgets.QDoubleSpinBox()
        self.spin_compound_link_dist.setRange(0.01, 25.0)
        self.spin_compound_link_dist.setDecimals(2)
        self.spin_compound_link_dist.setValue(0.1)
        self.spin_compound_link_dist.setSingleStep(0.05)
        self.spin_compound_link_dist.setObjectName("HP Link Dist (%):")
        self.spin_compound_link_dist.setFixedWidth(88)
        self.spin_compound_link_dist.setSuffix(" %")
        grid.addWidget(self.spin_compound_link_dist, 4, 1)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)

        self.algo_group.addLayout(grid)

        self.chk_use_symmetry = QtWidgets.QCheckBox("Calculate Symmetry Score (.pyd)")
        self.chk_use_symmetry.setChecked(True)

        # Advanced HP heuristics are intentionally kept out of the artist-facing UI,
        # but the worker still reads these widgets for its internal defaults.
        self.spin_bolt_elong = QtWidgets.QDoubleSpinBox()
        self.spin_bolt_elong.setRange(1.0, 10.0)
        self.spin_bolt_elong.setValue(1.5)
        self.spin_bolt_elong.setSingleStep(0.1)

        self.spin_bolt_sym = QtWidgets.QDoubleSpinBox()
        self.spin_bolt_sym.setRange(0.0, 5.0)
        self.spin_bolt_sym.setValue(0.8)
        self.spin_bolt_sym.setSingleStep(0.1)

        self.spin_wire_elong = QtWidgets.QDoubleSpinBox()
        self.spin_wire_elong.setRange(1.0, 20.0)
        self.spin_wire_elong.setValue(4.0)
        self.spin_wire_elong.setSingleStep(0.1)
        self.algo_group.toggle_button.setChecked(False)
        self.algo_group.on_pressed()
        layout.addWidget(self.algo_group)

    def apply_stylesheet(self):
        style = """
            QMainWindow, QWidget {
                background-color: #202225;
                color: #eef0f2;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 13px;
            }
            QWidget#AppSurface { background-color: #202225; }
            QLabel { color: #dfe2e5; background-color: transparent; }
            QFrame#AppHeader {
                background-color: #2b2e32;
                border: 1px solid #41464d;
                border-radius: 6px;
            }
            QLabel#AppLogo { background-color: transparent; }
            QLabel#AppTitle { color: #f7f8f9; font-size: 15px; font-weight: 600; }
            QLabel#AppSubtitle { color: #939aa2; font-size: 11px; }
            QLabel#CurrentTask {
                color: #72bbb7;
                font-size: 12px;
                font-weight: 600;
                padding: 0 5px;
            }
            QLabel#ContextValue { color: #72bbb7; font-weight: 600; }
            QLabel#SectionTitle { color: #f2f3f4; font-size: 14px; font-weight: 600; }
            QLabel#MinorHeading { color: #aeb4ba; font-size: 12px; font-weight: 600; }
            QLabel#FieldLabel { color: #b8bec5; }
            QLabel#CountLabel {
                color: #9fa6ad;
                background-color: #25282b;
                border: 1px solid #3c4146;
                border-radius: 4px;
                padding: 2px 7px;
            }
            QLabel#EmptyState {
                color: #8e959c;
                padding: 24px 12px;
            }
            QFrame#SectionPanel {
                background-color: #292c30;
                border: 1px solid #3d4248;
                border-radius: 6px;
            }
            QTabWidget#WorkspaceTabs::pane {
                background-color: #24272a;
                border: 1px solid #3d4248;
                border-radius: 5px;
                top: -1px;
            }
            QTabBar::tab {
                background-color: #282b2f;
                color: #aeb4ba;
                border: 1px solid #3b4046;
                border-bottom: none;
                min-height: 32px;
                min-width: 82px;
                padding: 0 12px;
                margin-right: 3px;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
            }
            QTabBar::tab:hover { background-color: #32363a; color: #f1f3f4; }
            QTabBar::tab:selected {
                background-color: #34383d;
                color: #ffffff;
                border-top: 2px solid #58aaa6;
            }
            QPushButton {
                background-color: #34383d;
                border: 1px solid #4a5057;
                border-radius: 4px;
                color: #eef0f2;
                padding: 3px 9px;
            }
            QPushButton:hover { background-color: #41464c; border-color: #686f77; }
            QPushButton:pressed { background-color: #26292c; }
            QPushButton:disabled { color: #727980; background-color: #292c2f; border-color: #383d42; }
            QPushButton#HeaderButton { background-color: #303438; }
            QToolButton#HeaderIconButton {
                background-color: #303438;
                border: 1px solid #4a5057;
                border-radius: 4px;
                padding: 0;
            }
            QToolButton#HeaderIconButton:hover { background-color: #41464c; border-color: #686f77; }
            QToolButton#HeaderIconButton:pressed { background-color: #26292c; }
            QToolButton#HeaderIconButton:disabled { background-color: #292c2f; border-color: #383d42; }
            QToolButton#HeaderIconButton[updateAvailable="true"] {
                background-color: #3f8582;
                border-color: #6ab5b1;
            }
            QToolButton#HeaderIconButton::menu-indicator { image: none; }
            QPushButton#PickButton { background-color: #303438; }
            QPushButton[role="primary"] {
                background-color: #3f8582;
                border-color: #4fa09c;
                color: #ffffff;
                font-weight: 600;
            }
            QPushButton[role="primary"]:hover { background-color: #4b9995; border-color: #6ab5b1; }
            QPushButton[role="secondary"] {
                background-color: #455f86;
                border-color: #55739f;
                color: #ffffff;
                font-weight: 600;
            }
            QPushButton[role="secondary"]:hover { background-color: #526f99; }
            QPushButton[role="success"] {
                background-color: #477f57;
                border-color: #579b6a;
                color: #ffffff;
                font-weight: 600;
            }
            QPushButton[role="success"]:hover { background-color: #539464; }
            QPushButton[role="danger"] {
                background-color: #884848;
                border-color: #a55a5a;
                color: #ffffff;
            }
            QPushButton[role="quiet"] { background-color: #303438; }
            QPushButton[role="segment"] {
                background-color: #303438;
                color: #bdc3c9;
                padding: 3px 6px;
            }
            QPushButton[role="segment"]:checked {
                background-color: #3c6e5b;
                border-color: #559576;
                color: #ffffff;
            }
            QPushButton[compact="true"] { padding: 0; }
            QToolButton {
                color: #eef0f2;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 13px;
            }
            QToolButton#SectionToggle {
                background-color: #292c30;
                border: 1px solid #3d4248;
                border-radius: 5px;
                color: #f0f2f3;
                min-height: 32px;
                padding: 0 9px;
                font-weight: 600;
                text-align: left;
            }
            QToolButton#SectionToggle:hover { background-color: #34383d; border-color: #50565d; }
            QWidget#SectionContent {
                background-color: #292c30;
                border: 1px solid #3d4248;
                border-top: none;
                border-bottom-left-radius: 5px;
                border-bottom-right-radius: 5px;
            }
            QLineEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                background-color: #1d1f22;
                border: 1px solid #464b51;
                border-radius: 4px;
                color: #f0f1f2;
                min-height: 27px;
                padding: 2px 7px;
                selection-background-color: #3f8582;
                selection-color: #ffffff;
            }
            QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
                border-color: #63afab;
            }
            QComboBox { padding-right: 20px; }
            QComboBox::drop-down { border: none; width: 20px; }
            QComboBox QAbstractItemView {
                background-color: #292c30;
                color: #eef0f2;
                border: 1px solid #50565d;
                selection-background-color: #3f8582;
            }
            QTextEdit#DebugLog {
                background-color: #17191b;
                color: #d8e2e1;
                font-family: Consolas, "Microsoft YaHei UI", monospace;
                font-size: 12px;
                border-color: #363b40;
                padding: 7px;
            }
            QTreeWidget, QListWidget {
                background-color: #1d1f22;
                border: 1px solid #3b4046;
                border-radius: 4px;
                outline: 0;
            }
            QTreeWidget::item, QListWidget::item { min-height: 28px; padding: 3px 6px; }
            QTreeWidget::item:hover, QListWidget::item:hover { background-color: #34383d; }
            QTreeWidget::item:selected, QListWidget::item:selected { background-color: #3f686b; color: #ffffff; }
            QCheckBox, QRadioButton { spacing: 7px; color: #d9dcdf; }
            QCheckBox::indicator, QRadioButton::indicator { width: 15px; height: 15px; }
            QScrollArea { border: none; background-color: transparent; }
            QScrollArea#GroupList { background-color: #232629; border: 1px solid #373c41; border-radius: 4px; }
            QWidget#GroupListViewport { background-color: #232629; }
            QSplitter::handle { background-color: #32363a; }
            QSplitter::handle:vertical { height: 4px; }
            QSplitter::handle:horizontal { width: 4px; }
            QScrollBar:vertical { background: #1d1f22; width: 11px; margin: 0; }
            QScrollBar::handle:vertical { background: #4b5157; min-height: 28px; border-radius: 4px; }
            QScrollBar::handle:vertical:hover { background: #626970; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QMenu { background-color: #292c30; color: #eef0f2; border: 1px solid #50565d; padding: 4px; }
            QMenu::item { padding: 6px 24px 6px 10px; }
            QMenu::item:selected { background-color: #3f8582; color: #ffffff; }
            QToolTip {
                background-color: #f1f3f4;
                color: #17191b;
                border: 1px solid #858b91;
                padding: 6px;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 12px;
            }
        """
        self.setStyleSheet(style)

    # ------------------------------------------------------------------------
    # Core methods (logging, script jobs, session, isolation, etc.)
    # ------------------------------------------------------------------------
    def log(self, msg, color="white"):
        self.log_output.append("<font color='{}'>{}</font>".format(color, msg))
        self.log_output.verticalScrollBar().setValue(self.log_output.verticalScrollBar().maximum())

    def record_user_action(self, action, detail=""):
        try:
            pair = next((p for p in self.root_pairs if p.get('id') == self.active_root_id), None)
            chapter = pair.get('base', 'None') if pair else 'None'
        except Exception:
            chapter = 'None'

        line = "[{}] {} | chapter={}".format(datetime.now().strftime("%H:%M:%S"), action, chapter)
        if detail:
            line += " | {}".format(detail)

        self.user_action_lines.append(line)
        if len(self.user_action_lines) > 500:
            self.user_action_lines = self.user_action_lines[-500:]

    def _bg_undo_candidate_nodes(self):
        nodes = set()
        for pair in self.root_pairs or []:
            try:
                hp_node, lp_node, _ = self.core.resolve_main_nodes(pair)
            except Exception:
                hp_node, lp_node = None, None
            for root in (hp_node, lp_node):
                if root and cmds.objExists(root):
                    long_root = (cmds.ls(root, long=True) or [root])[0]
                    nodes.add(long_root)
                    for child in (cmds.listRelatives(long_root, allDescendents=True, type='transform', fullPath=True) or []):
                        if cmds.objExists(child):
                            nodes.add(child)
        for root in ("LP_Combine_BG", "Bake_Groups"):
            if cmds.objExists(root):
                long_root = (cmds.ls(root, long=True) or [root])[0]
                nodes.add(long_root)
                for child in (cmds.listRelatives(long_root, allDescendents=True, type='transform', fullPath=True) or []):
                    if cmds.objExists(child):
                        nodes.add(child)
        return sorted(nodes)

    def capture_bg_undo_snapshot(self):
        visibility = {}
        for node in self._bg_undo_candidate_nodes():
            if not cmds.objExists(node):
                continue
            try:
                visibility[node] = bool(cmds.getAttr(node + ".visibility"))
            except Exception:
                pass
        return {
            "root_pairs": copy.deepcopy(self.root_pairs),
            "active_root_id": self.active_root_id,
            "active_subgroup_name": self.active_subgroup_name,
            "is_isolated": bool(getattr(self, 'is_isolated', False)),
            "is_final_view": bool(getattr(self, 'is_final_view', False)),
            "is_preview_active": bool(getattr(self, 'is_preview_active', False)),
            "is_final_low_visible": bool(getattr(self, 'is_final_low_visible', False)),
            "active_material_visibility_filter": getattr(self, 'active_material_visibility_filter', None),
            "final_smooth_states": copy.deepcopy(getattr(self, 'final_smooth_states', {}) or {}),
            "saved_subgroup_vis": copy.deepcopy(getattr(self, 'saved_subgroup_vis', {}) or {}),
            "visibility": visibility,
            "selection": cmds.ls(selection=True, long=True) or []
        }

    def restore_bg_undo_snapshot(self, snapshot):
        self.root_pairs = copy.deepcopy(snapshot.get("root_pairs", []))
        if hasattr(self.core, 'root_pairs'):
            self.core.root_pairs = self.root_pairs
        if hasattr(self.core, '_node_cache'):
            self.core._node_cache.clear()
        self.active_root_id = snapshot.get("active_root_id")
        self.active_subgroup_name = snapshot.get("active_subgroup_name")
        self.is_isolated = bool(snapshot.get("is_isolated", False))
        self.is_final_view = bool(snapshot.get("is_final_view", False))
        self.is_preview_active = bool(snapshot.get("is_preview_active", False))
        self.is_final_low_visible = bool(snapshot.get("is_final_low_visible", False))
        self.active_material_visibility_filter = snapshot.get("active_material_visibility_filter")
        self.final_smooth_states = copy.deepcopy(snapshot.get("final_smooth_states", {}) or {})
        self.saved_subgroup_vis = copy.deepcopy(snapshot.get("saved_subgroup_vis", {}) or {})
        active_pair = next((p for p in self.root_pairs if p.get('id') == self.active_root_id), None)
        self.set_grouping_mode(active_pair.get('grouping_mode', 'legacy_compact') if active_pair else 'conflict_safe')
        for node, state in (snapshot.get("visibility", {}) or {}).items():
            if cmds.objExists(node):
                try:
                    cmds.setAttr(node + ".visibility", bool(state))
                except Exception:
                    pass
        bg_core.BakeSessionModel.save(self.root_pairs)
        self.refresh_right_panel()
        self.refresh_left_panel()
        pair = next((p for p in self.root_pairs if p.get('id') == self.active_root_id), None)
        if pair:
            hp_node, lp_node, _ = self.core.resolve_main_nodes(pair)
            if hp_node and lp_node:
                self.sync_toggle_buttons(hp_node, lp_node)
        selection = [node for node in (snapshot.get("selection", []) or []) if cmds.objExists(node)]
        if selection:
            cmds.select(selection, replace=True)
        else:
            cmds.select(clear=True)

    def install_bg_undo_event_filter(self):
        app = QtWidgets.QApplication.instance()
        if not app:
            return
        try:
            app.installEventFilter(self)
            self._bg_undo_event_filter_app = app
        except RuntimeError:
            self._bg_undo_event_filter_app = None

    def remove_bg_undo_event_filter(self):
        app = getattr(self, '_bg_undo_event_filter_app', None)
        if not app:
            return
        try:
            app.removeEventFilter(self)
        except RuntimeError:
            pass
        self._bg_undo_event_filter_app = None

    def eventFilter(self, watched, event):
        try:
            event_type = event.type()
        except Exception:
            return super(BakeManagerUI, self).eventFilter(watched, event)
        shortcut_types = (QtCore.QEvent.KeyPress, QtCore.QEvent.ShortcutOverride)
        if event_type in shortcut_types and self.should_handle_bg_undo_shortcut(event):
            if event_type == QtCore.QEvent.ShortcutOverride:
                event.accept()
                return True
            self.undo_last_bg_action()
            event.accept()
            return True
        return super(BakeManagerUI, self).eventFilter(watched, event)

    def should_handle_bg_undo_shortcut(self, event):
        if not getattr(self, 'bg_undo_stack', []):
            return False
        if getattr(self, '_bg_undo_restoring', False) or getattr(self, '_bg_undo_running', False):
            return False
        try:
            if event.key() != QtCore.Qt.Key_Z:
                return False
            modifiers = event.modifiers()
            if not (modifiers & QtCore.Qt.ControlModifier):
                return False
            if modifiers & (QtCore.Qt.ShiftModifier | QtCore.Qt.AltModifier | QtCore.Qt.MetaModifier):
                return False
        except Exception:
            return False
        focus = QtWidgets.QApplication.focusWidget()
        if not self.is_bg_undo_focus_inside_window(focus):
            return False
        return not self.is_bg_undo_editable_focus(focus)

    def is_bg_undo_focus_inside_window(self, focus):
        if not focus:
            return False
        try:
            return focus is self or self.isAncestorOf(focus)
        except RuntimeError:
            return False

    def is_bg_undo_editable_focus(self, focus):
        widget = focus
        while widget and widget is not self:
            if isinstance(widget, QtWidgets.QLineEdit) and not widget.isReadOnly():
                return True
            if isinstance(widget, (QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit)) and not widget.isReadOnly():
                return True
            if isinstance(widget, QtWidgets.QAbstractSpinBox):
                return True
            if isinstance(widget, QtWidgets.QComboBox) and widget.isEditable():
                return True
            try:
                widget = widget.parentWidget()
            except RuntimeError:
                return False
        return False

    def update_bg_undo_button(self):
        pass

    def run_undoable_bg_action(self, action_name, callback, *args, **kwargs):
        if getattr(self, '_bg_undo_restoring', False) or getattr(self, '_bg_undo_running', False):
            return callback(*args, **kwargs)
        snapshot = self.capture_bg_undo_snapshot()
        self._bg_undo_running = True
        try:
            chunk_name = "BG_{}".format(re.sub(r'[^A-Za-z0-9_]+', '_', str(action_name))[:48])
            with bg_core.undo_chunk(chunk_name):
                result = callback(*args, **kwargs)
        finally:
            self._bg_undo_running = False
        self.bg_undo_stack.append({"action": str(action_name), "snapshot": snapshot})
        if len(self.bg_undo_stack) > 20:
            self.bg_undo_stack = self.bg_undo_stack[-20:]
        self.update_bg_undo_button()
        return result

    def undo_last_bg_action(self):
        if not getattr(self, 'bg_undo_stack', []):
            self.log(bg_l10n.text("No Bake Master action to undo."), "orange")
            return
        entry = self.bg_undo_stack.pop()
        self._bg_undo_restoring = True
        try:
            try:
                cmds.undo()
            except Exception as exc:
                self.log(bg_l10n.text("Maya undo failed: {error}").format(error=exc), "orange")
            self.restore_bg_undo_snapshot(entry.get("snapshot", {}))
        finally:
            self._bg_undo_restoring = False
            self.update_bg_undo_button()
        message = bg_l10n.text("Undone Bake Master action: {action}").format(action=entry.get("action", ""))
        self.log(message, "lightgreen")
        cmds.inViewMessage(amg=message, pos='midCenter', fade=True)

    def _format_debug_names(self, names, limit=30):
        names = [str(n) for n in (names or [])]
        if len(names) <= limit:
            return ", ".join(names)
        return "{}, ... +{} more".format(", ".join(names[:limit]), len(names) - limit)

    def build_current_scene_snapshot(self):
        lines = []
        pair = next((p for p in self.root_pairs if p.get('id') == self.active_root_id), None)
        if not pair:
            return ["No active chapter selected."]

        hp_main, lp_main, _ = self.core.resolve_main_nodes(pair)
        lines.append("Active chapter: {}".format(pair.get('base', 'Unknown')))

        def collect_group_snapshot(root_node, label):
            output = []
            if not root_node or not cmds.objExists(root_node):
                return ["{} root: missing".format(label)]

            children = cmds.listRelatives(root_node, children=True, fullPath=True, type='transform') or []
            direct_meshes = []
            groups = []
            for child in children:
                if cmds.listRelatives(child, shapes=True, type='mesh'):
                    direct_meshes.append(child.split('|')[-1])
                else:
                    groups.append(child)

            output.append("{} root: {}".format(label, root_node))
            if direct_meshes:
                output.append("{} direct meshes: {}".format(label, len(direct_meshes)))
                output.append("  meshes: {}".format(self._format_debug_names(direct_meshes)))

            if not groups:
                return output

            output.append("{} groups: {}".format(label, len(groups)))

            for group in groups:
                group_short = group.split('|')[-1]
                mesh_transforms = []
                children = cmds.listRelatives(group, children=True, fullPath=True, type='transform') or []
                for child in children:
                    if cmds.listRelatives(child, shapes=True, type='mesh'):
                        mesh_transforms.append(child.split('|')[-1])
                output.append("  {} | count={}".format(group_short, len(mesh_transforms)))
                if mesh_transforms:
                    output.append("    meshes: {}".format(self._format_debug_names(mesh_transforms)))
            return output

        lines.extend(collect_group_snapshot(hp_main, "HP"))
        lines.extend(collect_group_snapshot(lp_main, "LP"))
        return lines

    def show_log_context_menu(self, pos):
        menu = self.log_output.createStandardContextMenu()
        menu.addSeparator()
        action_support_package = menu.addAction(bg_l10n.text("Save Support Package"))
        action_support_package.triggered.connect(self.save_support_package)
        menu.addSeparator()
        action_save_debug = menu.addAction(bg_l10n.text("Save Debug Log"))
        action_save_debug.setEnabled(bool(getattr(self, 'last_debug_lines', []) or getattr(self, 'user_action_lines', [])))
        action_save_debug.triggered.connect(self.save_debug_log)
        global_pos = self.log_output.mapToGlobal(pos)
        if hasattr(menu, 'exec_'):
            menu.exec_(global_pos)
        else:
            menu.exec(global_pos)

    def save_debug_log(self):
        debug_lines = list(getattr(self, 'last_debug_lines', []) or [])
        action_lines = list(getattr(self, 'user_action_lines', []) or [])
        if not debug_lines and not action_lines:
            self.log(bg_l10n.text("No debug log to save yet. Run Analyze HP first."), "orange")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = os.path.join(os.path.expanduser("~"), "Desktop", "BakeGroups_Debug_{}.txt".format(timestamp))
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            bg_l10n.text("Save Debug Log"),
            default_path,
            bg_l10n.text("Text Files (*.txt);;All Files (*)")
        )
        if not file_path:
            return
        if not os.path.splitext(file_path)[1]:
            file_path += ".txt"

        scene_name = cmds.file(q=True, sn=True) or "Untitled"
        visible_log = self.log_output.toPlainText()
        report = []
        report.append("Bake Master Debug Log")
        report.append("Saved: {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        report.append("Scene: {}".format(scene_name))
        report.append("")
        report.append("=== Visible Log ===")
        report.append(visible_log if visible_log else "(empty)")
        report.append("")
        report.append("=== User Actions ===")
        report.extend(action_lines if action_lines else ["(no recorded user actions)"])
        report.append("")
        report.append("=== Current Scene Snapshot ===")
        report.extend(self.build_current_scene_snapshot())
        report.append("")
        report.append("=== Analyze HP Debug ===")
        report.extend(debug_lines if debug_lines else ["(Analyze HP debug is not available for this session.)"])

        try:
            with open(file_path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(report))
            self.log(bg_l10n.text("Debug log saved: {path}").format(path=file_path), "lightgreen")
        except Exception as exc:
            self.log(bg_l10n.text("Failed to save debug log: {error}").format(error=exc), "red")

    def build_support_environment_snapshot(self):
        pair = next((p for p in self.root_pairs if p.get('id') == self.active_root_id), None)
        try:
            maya_version = cmds.about(version=True)
        except Exception:
            maya_version = "Unknown"
        try:
            maya_api = cmds.about(apiVersion=True)
        except Exception:
            maya_api = "Unknown"
        data = {
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "plugin_version": getattr(bg_version, "__version__", "Unknown"),
            "plugin_name": getattr(bg_version, "PLUGIN_NAME", "Bake Master"),
            "language": bg_l10n.current_language(),
            "maya_version": maya_version,
            "maya_api": maya_api,
            "scene": cmds.file(q=True, sn=True) or "Untitled",
            "active_chapter": pair.get("base") if pair else None,
            "is_final_view": bool(getattr(self, "is_final_view", False)),
            "is_preview_active": bool(getattr(self, "is_preview_active", False)),
            "active_material_visibility_filter": getattr(self, "active_material_visibility_filter", None),
            "settings": {
                "hp_strategy": self.combo_hp_strategy.currentText() if hasattr(self, "combo_hp_strategy") else None,
                "hp_collision_pct": self.spin_threshold.value() if hasattr(self, "spin_threshold") else None,
                "ignore_floaters": self.chk_ignore_floaters.isChecked() if hasattr(self, "chk_ignore_floaters") else None,
                "material_slots": self.chk_material_slots.isChecked() if hasattr(self, "chk_material_slots") else None,
                "hp_link_vtx": self.spin_compound_link_verts.value() if hasattr(self, "spin_compound_link_verts") else None,
                "hp_link_dist_pct": self.spin_compound_link_dist.value() if hasattr(self, "spin_compound_link_dist") else None,
                "grouping_mode": self.current_grouping_mode(),
                "color_groups": self.cb_color_subgroups.isChecked() if hasattr(self, "cb_color_subgroups") else None,
                "keep_hp": self.cb_keep_hp_structure.isChecked() if hasattr(self, "cb_keep_hp_structure") else None,
            },
        }
        return data

    def build_display_layer_snapshot(self):
        lines = []
        layers = cmds.ls(type="displayLayer") or []
        for layer in sorted(layers):
            if layer == "defaultLayer":
                continue
            try:
                visible = cmds.getAttr("{}.visibility".format(layer))
            except Exception:
                visible = "Unknown"
            members = cmds.editDisplayLayerMembers(layer, q=True, fullNames=True) or []
            lines.append("{} | visible={} | members={}".format(layer, visible, len(members)))
            if members:
                lines.append("  {}".format(self._format_debug_names([m.split('|')[-1] for m in members], limit=40)))
        return lines or ["(no custom display layers)"]

    def save_support_package(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = os.path.join(os.path.expanduser("~"), "Desktop", "BakeGroups_Support_{}.zip".format(timestamp))
        file_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            bg_l10n.text("Save Support Package"),
            default_path,
            bg_l10n.text("Zip Files (*.zip);;All Files (*)")
        )
        if not file_path:
            return
        if not os.path.splitext(file_path)[1]:
            file_path += ".zip"

        debug_lines = list(getattr(self, 'last_debug_lines', []) or [])
        action_lines = list(getattr(self, 'user_action_lines', []) or [])
        visible_log = self.log_output.toPlainText()
        scene_snapshot = self.build_current_scene_snapshot()
        display_layers = self.build_display_layer_snapshot()
        environment = self.build_support_environment_snapshot()

        report = []
        report.append("Bake Master Support Package")
        report.append("Saved: {}".format(environment.get("saved_at")))
        report.append("Scene: {}".format(environment.get("scene")))
        report.append("Plugin: {} {}".format(environment.get("plugin_name"), environment.get("plugin_version")))
        report.append("Maya: {} | API: {}".format(environment.get("maya_version"), environment.get("maya_api")))
        report.append("Language: {}".format(environment.get("language")))
        report.append("")
        report.append("=== Visible Log ===")
        report.append(visible_log if visible_log else "(empty)")
        report.append("")
        report.append("=== User Actions ===")
        report.extend(action_lines if action_lines else ["(no recorded user actions)"])
        report.append("")
        report.append("=== Current Scene Snapshot ===")
        report.extend(scene_snapshot)
        report.append("")
        report.append("=== Display Layers ===")
        report.extend(display_layers)
        report.append("")
        report.append("=== Analyze HP Debug ===")
        report.extend(debug_lines if debug_lines else ["(Analyze HP debug is not available for this session.)"])

        try:
            with zipfile.ZipFile(file_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("support_report.txt", "\n".join(report))
                archive.writestr("visible_log.txt", visible_log or "")
                archive.writestr("user_actions.txt", "\n".join(action_lines))
                archive.writestr("scene_snapshot.txt", "\n".join(scene_snapshot))
                archive.writestr("display_layers.txt", "\n".join(display_layers))
                archive.writestr("analyze_hp_debug.txt", "\n".join(debug_lines))
                archive.writestr("environment.json", json.dumps(environment, indent=2, ensure_ascii=False, default=str))
                archive.writestr("session_pairs.json", json.dumps(self.root_pairs, indent=2, ensure_ascii=False, default=str))
        except Exception as exc:
            self.log(bg_l10n.text("Failed to save support package: {error}").format(error=exc), "red")
            return

        self.log(bg_l10n.text("Support package saved: {path}").format(path=file_path), "lightgreen")

    def setup_script_jobs(self):
        self.script_jobs.append(cmds.scriptJob(event=["SceneOpened", self.reload_data_from_scene]))

    def current_grouping_mode(self):
        combo = getattr(self, 'combo_grouping_mode', None)
        if combo is None:
            return "conflict_safe"
        try:
            value = combo.currentData()
        except Exception:
            value = combo.itemData(combo.currentIndex())
        value = str(value or "")
        valid_modes = ("conflict_safe", "one_lp_per_group", "legacy_compact")
        return value if value in valid_modes else "conflict_safe"

    def set_grouping_mode(self, mode):
        combo = getattr(self, 'combo_grouping_mode', None)
        if combo is None:
            return
        valid_modes = ("conflict_safe", "one_lp_per_group", "legacy_compact")
        mode = mode if mode in valid_modes else "conflict_safe"
        target_index = 0
        for index in range(combo.count()):
            if str(combo.itemData(index)) == mode:
                target_index = index
                break
        previous = combo.blockSignals(True)
        combo.setCurrentIndex(target_index)
        combo.blockSignals(previous)

    def on_grouping_mode_changed(self, _index):
        if getattr(self, '_syncing_grouping_mode', False):
            return
        pair = next((p for p in self.root_pairs if p.get('id') == self.active_root_id), None)
        if not pair:
            return
        pair['grouping_mode'] = self.current_grouping_mode()
        bg_core.BakeSessionModel.save(self.root_pairs)
        self.record_user_action("Grouping mode changed", pair['grouping_mode'])

    def on_keep_hp_structure_toggled(self, checked):
        combo = getattr(self, 'combo_grouping_mode', None)
        if combo is not None:
            combo.setEnabled(not checked)
        self.refresh_left_panel()

    def on_color_by_subgroups_toggled(self, checked):
        with self._suspended_viewport_refresh():
            if checked:
                self.subgroup_color_index_map = {}
                self.update_subgroup_colors()
            else:
                self.restore_subgroup_colors(clean_history=True)
                pair = next((p for p in self.root_pairs if p.get('id') == self.active_root_id), None)
                if pair:
                    hp_main, lp_main, _ = self.core.resolve_main_nodes(pair)
                    removed = self.cleanup_subgroup_preview_color_sets([hp_main, lp_main], clean_history=True)
                    if removed:
                        self.log("Color HP: removed {} old preview color set(s).".format(removed), "lightblue")
                self.subgroup_color_index_map = {}
        self.refresh_left_panel()

    def subgroup_color_for_name(self, name):
        palette = [
            (0.953, 0.071, 0.027),
            (0.988, 0.424, 0.012),
            (0.953, 0.851, 0.443),
            (0.196, 0.831, 0.145),
            (0.145, 0.831, 0.824),
            (0.090, 0.247, 0.827),
            (0.647, 0.090, 0.827),
            (0.827, 0.090, 0.820),
            (1.000, 1.000, 1.000),
            (0.000, 0.506, 0.000),
            (0.000, 0.506, 0.482),
            (0.000, 0.000, 0.498),
            (0.114, 0.000, 0.498),
            (0.498, 0.000, 0.478),
            (0.518, 0.518, 0.518),
            (0.518, 0.176, 0.110),
            (0.278, 0.204, 0.129),
            (0.518, 0.459, 0.404),
            (1.000, 0.945, 0.145),
        ]
        index_map = getattr(self, 'subgroup_color_index_map', {}) or {}
        if name in index_map:
            index = index_map[name]
        else:
            value = 0
            for char_index, char in enumerate(str(name or "")):
                value += (char_index + 1) * ord(char)
            index = value
        base = palette[index % len(palette)]
        pass_index = index // len(palette)
        shade = 1.0 if pass_index == 0 else max(0.38, 0.62 ** pass_index)
        return tuple(max(0.0, min(1.0, channel * shade)) for channel in base)

    def ensure_subgroup_color_indices(self, names, reset=False):
        if reset:
            self.subgroup_color_index_map = {}
        index_map = getattr(self, 'subgroup_color_index_map', None)
        if index_map is None:
            index_map = {}
            self.subgroup_color_index_map = index_map
        next_index = max(index_map.values()) + 1 if index_map else 0
        for name in names or []:
            if name not in index_map:
                index_map[name] = next_index
                next_index += 1
        return index_map

    def color_to_qss_rgb(self, color, scale=255):
        return "rgb({}, {}, {})".format(
            max(0, min(255, int(color[0] * scale))),
            max(0, min(255, int(color[1] * scale))),
            max(0, min(255, int(color[2] * scale)))
        )

    def color_to_qss_rgba(self, color, alpha):
        return "rgba({}, {}, {}, {})".format(
            max(0, min(255, int(color[0] * 255))),
            max(0, min(255, int(color[1] * 255))),
            max(0, min(255, int(color[2] * 255))),
            max(0, min(255, int(alpha)))
        )

    def viewport_subgroup_color(self, color):
        neutral = 0.22
        mix = 0.18
        brightness = 0.68
        return tuple(
            max(0.0, min(0.85, ((channel * (1.0 - mix)) + (neutral * mix)) * brightness))
            for channel in color
        )

    def subgroup_row_style(self, subgroup_name, active=False):
        if hasattr(self, 'cb_color_subgroups') and self.cb_color_subgroups.isChecked():
            color = self.subgroup_color_for_name(subgroup_name)
            border = "#dfe9e7" if active else self.color_to_qss_rgb(color)
            bg = self.color_to_qss_rgba(color, 24 if not active else 54)
            width = 2 if active else 1
            return "QFrame { background-color: %s; border: %dpx solid %s; border-radius: 5px; }" % (bg, width, border)
        if active:
            return "QFrame { background-color: #30494a; border: 2px solid #72bbb7; border-radius: 5px; }"
        return "QFrame { background-color: #292c30; border: 1px solid #3c4147; border-radius: 5px; }"

    def subgroup_name_style(self, subgroup_name, active=False):
        if hasattr(self, 'cb_color_subgroups') and self.cb_color_subgroups.isChecked():
            color = self.subgroup_color_for_name(subgroup_name)
            bg = self.color_to_qss_rgba(color, 68 if active else 28)
            border = self.color_to_qss_rgba(color, 180 if active else 95)
            weight = "bold" if active else "normal"
            return "background-color: %s; border: 1px solid %s; border-radius: 3px; font-weight: %s; text-align: left; padding-left: 5px;" % (bg, border, weight)
        if active:
            return "background-color: #3b6062; border: 1px solid #548d8c; font-weight: bold; text-align: left; padding-left: 8px;"
        return "background-color: transparent; border: none; text-align: left; padding-left: 8px;"

    def subgroup_add_button_style(self, active=False):
        if active:
            return "QPushButton { background-color: #58966c; color: white; border: 1px solid #77b48a; border-radius: 4px; font-weight: bold; padding: 0px; } QPushButton:hover { background-color: #65a879; }"
        return "QPushButton { background-color: #34383d; color: white; border: 1px solid #4a5057; border-radius: 4px; font-weight: bold; } QPushButton:hover { background-color: #41464c; }"

    def set_compact_visibility_state(self, button, state):
        if not button:
            return
        button.setProperty("bg_compact_visibility", True)
        button.setProperty("compact", True)
        button.setProperty("bg_i18n_key", "Vis" if state else "Hid")
        button.setText("")
        button.setFixedSize(32, 32)
        button.setIcon(get_icon("open_eye.png" if state else "close_eye.png"))
        button.setIconSize(QtCore.QSize(18, 18))
        button.setToolTip(bg_l10n.tooltip("Vis" if state else "Hid"))
        button.setStyleSheet(
            "QPushButton { background-color: %s; border: 1px solid %s; padding: 0; }"
            "QPushButton:hover { background-color: %s; }" % (
                "#3b6752" if state else "#643f45",
                "#4f866a" if state else "#86535b",
                "#487b61" if state else "#754951",
            )
        )

    def iter_colorable_nodes(self, transform):
        if not transform or not cmds.objExists(transform):
            return []
        if cmds.nodeType(transform) == "mesh":
            if not cmds.getAttr(transform + ".intermediateObject"):
                return [transform]
            return []
        transforms = [transform]
        transforms.extend(cmds.listRelatives(transform, allDescendents=True, type='transform', fullPath=True) or [])
        nodes = []
        seen = set()
        for item in transforms:
            if not item or not cmds.objExists(item):
                continue
            shapes = cmds.listRelatives(item, shapes=True, type='mesh', noIntermediate=True, fullPath=True) or []
            for shape in shapes:
                if shape in seen:
                    continue
                if cmds.objExists(shape) and cmds.attributeQuery("overrideEnabled", node=shape, exists=True):
                    seen.add(shape)
                    nodes.append(shape)
        return nodes

    def store_override_state(self, node):
        if node in self.subgroup_color_override_cache:
            return
        attrs = {}
        try:
            node_uuid = cmds.ls(node, uuid=True) or []
            if node_uuid:
                attrs["__uuid"] = node_uuid[0]
        except Exception:
            pass
        for attr in ("overrideEnabled", "overrideRGBColors", "overrideColor"):
            plug = "{}.{}".format(node, attr)
            if cmds.objExists(plug):
                try:
                    attrs[attr] = cmds.getAttr(plug)
                except Exception:
                    pass
        plug = "{}.overrideColorRGB".format(node)
        if cmds.objExists(plug):
            try:
                value = cmds.getAttr(plug)
                attrs["overrideColorRGB"] = value[0] if isinstance(value, list) else value
            except Exception:
                pass
        for attr in ("displayColors", "displayColorChannel"):
            plug = "{}.{}".format(node, attr)
            if cmds.objExists(plug):
                try:
                    attrs[attr] = cmds.getAttr(plug)
                except Exception:
                    pass
        target = self.color_target_for_shape(node)
        if target:
            try:
                color_sets = cmds.polyColorSet(target, query=True, allColorSets=True) or []
                attrs["__had_color_set"] = "BG_Subgroup_Color" in color_sets
                current = cmds.polyColorSet(target, query=True, currentColorSet=True) or []
                if current:
                    attrs["__current_color_set"] = current[0]
            except Exception:
                pass
        self.subgroup_color_override_cache[node] = attrs

    def remove_subgroup_preview_color_set(self, target, clean_history=False):
        if not target or not cmds.objExists(target):
            return False
        try:
            color_sets = cmds.polyColorSet(target, query=True, allColorSets=True) or []
            if "BG_Subgroup_Color" in color_sets:
                cmds.polyColorSet(target, delete=True, colorSet="BG_Subgroup_Color")
                if clean_history:
                    try:
                        cmds.delete(target, constructionHistory=True)
                    except Exception:
                        pass
                return True
        except Exception:
            pass
        return False

    def cleanup_subgroup_preview_color_sets(self, roots=None, clean_history=False):
        roots = roots or []
        targets = set()
        for root in roots:
            if not root or not cmds.objExists(root):
                continue
            mesh_shapes = cmds.listRelatives(root, allDescendents=True, fullPath=True, type='mesh', noIntermediate=True) or []
            for shape in mesh_shapes:
                parent = cmds.listRelatives(shape, parent=True, fullPath=True) or []
                if parent:
                    targets.add(parent[0])
        removed = 0
        for target in targets:
            try:
                color_sets = cmds.polyColorSet(target, query=True, allColorSets=True) or []
                if "BG_Subgroup_Color" in color_sets:
                    if self.remove_subgroup_preview_color_set(target, clean_history=clean_history):
                        removed += 1
            except Exception:
                pass
        return removed

    def color_target_for_shape(self, node):
        if not node or not cmds.objExists(node):
            return None
        if cmds.nodeType(node) == "mesh":
            parent = cmds.listRelatives(node, parent=True, fullPath=True) or []
            return parent[0] if parent else None
        shapes = cmds.listRelatives(node, shapes=True, type='mesh', noIntermediate=True, fullPath=True) or []
        return node if shapes else None

    def mesh_shape_for_color_node(self, node):
        if not node or not cmds.objExists(node):
            return None
        if cmds.nodeType(node) == "mesh":
            return node
        shapes = cmds.listRelatives(node, shapes=True, type='mesh', noIntermediate=True, fullPath=True) or []
        return shapes[0] if shapes else None

    def ensure_subgroup_color_set(self, target):
        color_set = "BG_Subgroup_Color"
        color_sets = cmds.polyColorSet(target, query=True, allColorSets=True) or []
        if color_set not in color_sets:
            cmds.polyColorSet(target, create=True, colorSet=color_set)
        cmds.polyColorSet(target, currentColorSet=True, colorSet=color_set)
        return color_set

    def apply_override_color(self, node, color):
        if not node or not cmds.objExists(node):
            return
        self.store_override_state(node)
        target = self.color_target_for_shape(node)
        shape = self.mesh_shape_for_color_node(node)
        if not target or not shape:
            return
        try:
            self.ensure_subgroup_color_set(target)
            cmds.polyColorPerVertex(target, rgb=self.viewport_subgroup_color(color), colorDisplayOption=True, notUndoable=True)
            if shape and cmds.objExists("{}.displayColors".format(shape)):
                cmds.setAttr("{}.displayColors".format(shape), True)
            if shape and cmds.objExists("{}.displayColorChannel".format(shape)):
                try:
                    cmds.setAttr("{}.displayColorChannel".format(shape), "color", type="string")
                except Exception:
                    pass
            try:
                cmds.polyOptions(target, colorShadedDisplay=True, colorMaterialChannel="ambientDiffuse")
            except Exception:
                pass
            return True
        except Exception:
            return False

    def delete_preview_materials(self):
        initial_sg = "initialShadingGroup"
        for sg in cmds.ls("BG_ColorPreview_SG_*") or []:
            if initial_sg and cmds.objExists(initial_sg):
                try:
                    members = cmds.sets(sg, query=True) or []
                except Exception:
                    members = []
                for member in members:
                    try:
                        cmds.sets(member, edit=True, forceElement=initial_sg)
                    except Exception:
                        pass
            try:
                cmds.delete(sg)
            except Exception:
                pass
        for node in cmds.ls("BG_ColorPreview_MAT_*") or []:
            try:
                cmds.delete(node)
            except Exception:
                pass

    @contextlib.contextmanager
    def _suspended_viewport_refresh(self):
        # Reentrant: update_subgroup_colors calls restore_subgroup_colors
        # internally, so only the outermost call actually toggles Maya's
        # refresh state. Batch mesh-color edits (polyColorPerVertex,
        # polyColorSet, construction-history deletes) can otherwise race
        # with an active Smooth Mesh Preview redraw and crash Maya.
        depth = getattr(self, '_viewport_refresh_suspend_depth', 0)
        if depth == 0:
            cmds.refresh(suspend=True)
        self._viewport_refresh_suspend_depth = depth + 1
        try:
            yield
        finally:
            depth = getattr(self, '_viewport_refresh_suspend_depth', 1) - 1
            self._viewport_refresh_suspend_depth = depth
            if depth <= 0:
                cmds.refresh(suspend=False)

                def safe_refresh():
                    try:
                        cmds.refresh()
                    except Exception:
                        pass

                # Даём Maya 100 мс на укладку памяти перед отрисовкой.
                QtCore.QTimer.singleShot(100, safe_refresh)

    def restore_subgroup_colors(self, clean_history=False):
        cache = getattr(self, 'subgroup_color_override_cache', {})
        with self._suspended_viewport_refresh():
            for node, attrs in list(cache.items()):
                if not cmds.objExists(node):
                    node_uuid = attrs.get("__uuid")
                    matches = cmds.ls(node_uuid, long=True) if node_uuid else []
                    if matches:
                        node = matches[0]
                    else:
                        continue
                try:
                    if "overrideRGBColors" in attrs:
                        cmds.setAttr("{}.overrideRGBColors".format(node), attrs["overrideRGBColors"])
                    if "overrideColorRGB" in attrs:
                        rgb = attrs["overrideColorRGB"]
                        cmds.setAttr("{}.overrideColorRGB".format(node), rgb[0], rgb[1], rgb[2])
                    if "overrideColor" in attrs:
                        cmds.setAttr("{}.overrideColor".format(node), attrs["overrideColor"])
                    if "overrideEnabled" in attrs:
                        cmds.setAttr("{}.overrideEnabled".format(node), attrs["overrideEnabled"])
                    target = self.color_target_for_shape(node)
                    has_external_color_set = False
                    if target:
                        try:
                            color_sets = cmds.polyColorSet(target, query=True, allColorSets=True) or []
                            if "BG_Subgroup_Color" in color_sets:
                                self.remove_subgroup_preview_color_set(target, clean_history=clean_history)
                                color_sets = [name for name in color_sets if name != "BG_Subgroup_Color"]
                            has_external_color_set = bool(color_sets)
                        except Exception:
                            pass
                        if attrs.get("__current_color_set"):
                            try:
                                cmds.polyColorSet(target, currentColorSet=True, colorSet=attrs["__current_color_set"])
                            except Exception:
                                pass
                    if "displayColorChannel" in attrs and cmds.objExists("{}.displayColorChannel".format(node)):
                        cmds.setAttr("{}.displayColorChannel".format(node), attrs["displayColorChannel"], type="string")
                    if "displayColors" in attrs and cmds.objExists("{}.displayColors".format(node)):
                        cmds.setAttr("{}.displayColors".format(node), False if has_external_color_set else attrs["displayColors"])
                except Exception:
                    pass
            self.subgroup_color_override_cache = {}
            self.delete_preview_materials()

    @contextlib.contextmanager
    def suspend_subgroup_color_preview(self):
        was_enabled = hasattr(self, 'cb_color_subgroups') and self.cb_color_subgroups.isChecked()
        if was_enabled:
            self.restore_subgroup_colors()
        try:
            yield
        finally:
            if was_enabled and hasattr(self, 'cb_color_subgroups') and self.cb_color_subgroups.isChecked():
                self.update_subgroup_colors()

    def refresh_subgroup_color_preview(self, reset_indices=False):
        if not hasattr(self, 'cb_color_subgroups') or not self.cb_color_subgroups.isChecked():
            return
        if not self.active_chapter_has_subgroups():
            return
        if reset_indices:
            self.subgroup_color_index_map = {}
        self.update_subgroup_colors()

    def active_chapter_has_subgroups(self, pair=None):
        pair = pair or next((p for p in self.root_pairs if p.get('id') == self.active_root_id), None)
        if not pair:
            return False
        hp_main, _, _ = self.core.resolve_main_nodes(pair)
        if not hp_main or not cmds.objExists(hp_main):
            return False
        for child in cmds.listRelatives(hp_main, children=True, type='transform', fullPath=True) or []:
            if not child or not cmds.objExists(child):
                continue
            if cmds.listRelatives(child, shapes=True, type='mesh', noIntermediate=True):
                continue
            return True
        return False

    def update_subgroup_colors(self):
        if not hasattr(self, 'cb_color_subgroups') or not self.cb_color_subgroups.isChecked():
            return
        if not self.active_root_id:
            return
        if getattr(self, 'is_final_view', False):
            colored_count = 0
            with self._suspended_viewport_refresh():
                self.ensure_subgroup_color_indices([w.get('subgroup_name') for w in getattr(self, 'final_mesh_widgets', []) if w.get('subgroup_name')])
                for widget_data in getattr(self, 'final_mesh_widgets', []):
                    name = widget_data.get('subgroup_name')
                    color = self.subgroup_color_for_name(name)
                    for node in widget_data.get('hp_nodes', []):
                        for color_node in self.iter_colorable_nodes(node):
                            if self.apply_override_color(color_node, color):
                                colored_count += 1
            self.log("Color HP: colored {} mesh shapes.".format(colored_count), "lightblue")
            return
        pair = next((p for p in self.root_pairs if p['id'] == self.active_root_id), None)
        if not pair:
            return
        if not self.active_chapter_has_subgroups(pair):
            return
        colored_count = 0
        with self._suspended_viewport_refresh():
            self.restore_subgroup_colors(clean_history=True)
            hp_main, lp_main, _ = self.core.resolve_main_nodes(pair)
            self.cleanup_subgroup_preview_color_sets([hp_main])
            group_names = []
            for root in (hp_main,):
                if not root or not cmds.objExists(root):
                    continue
                for child in cmds.listRelatives(root, children=True, type='transform', fullPath=True) or []:
                    if not cmds.objExists(child) or cmds.listRelatives(child, shapes=True, type='mesh', noIntermediate=True):
                        continue
                    short_name = child.split('|')[-1]
                    ui_name = short_name
                    for suffix in (bg_core.BakeConfig.SUFFIX_HP, bg_core.BakeConfig.SUFFIX_LP):
                        if ui_name.endswith(suffix):
                            ui_name = ui_name[:-len(suffix)]
                            break
                    if ui_name not in group_names:
                        group_names.append(ui_name)
            self.ensure_subgroup_color_indices(sorted(group_names))
            for root, suffix in ((hp_main, bg_core.BakeConfig.SUFFIX_HP),):
                if not root or not cmds.objExists(root):
                    continue
                children = cmds.listRelatives(root, children=True, type='transform', fullPath=True) or []
                for child in children:
                    if not cmds.objExists(child) or cmds.listRelatives(child, shapes=True, type='mesh', noIntermediate=True):
                        continue
                    short_name = child.split('|')[-1]
                    if not self.cb_keep_hp_structure.isChecked():
                        attr = "{}.{}".format(child, bg_core.BakeConfig.ATTR_BAKE_GROUP)
                        if cmds.objExists(attr):
                            group_type = cmds.getAttr(attr)
                            if suffix == bg_core.BakeConfig.SUFFIX_HP and group_type != "HP":
                                continue
                        elif not short_name.endswith(suffix):
                            continue
                    ui_name = short_name
                    if short_name.endswith(suffix):
                        ui_name = short_name[:-len(suffix)]
                    color = self.subgroup_color_for_name(ui_name)
                    for color_node in self.iter_colorable_nodes(child):
                        if self.apply_override_color(color_node, color):
                            colored_count += 1
        self.log("Color HP: colored {} mesh shapes.".format(colored_count), "lightblue")

    def recolor_moved_subgroup_nodes(self, nodes, subgroup_name):
        if not hasattr(self, 'cb_color_subgroups') or not self.cb_color_subgroups.isChecked():
            return
        pair = next((p for p in self.root_pairs if p['id'] == self.active_root_id), None)
        if not pair:
            return
        hp_main, _, _ = self.core.resolve_main_nodes(pair)
        if not hp_main or not cmds.objExists(hp_main):
            return
        self.ensure_subgroup_color_indices([subgroup_name])
        color = self.subgroup_color_for_name(subgroup_name)
        colored_count = 0
        for node in nodes or []:
            if not node or not cmds.objExists(node):
                continue
            if not self.core.is_descendant_of(node, hp_main):
                continue
            for color_node in self.iter_colorable_nodes(node):
                if self.apply_override_color(color_node, color):
                    colored_count += 1
        if colored_count:
            self.log("Color HP: recolored {} moved mesh shapes.".format(colored_count), "lightblue")

    def _stop_worker_for_close(self, attr_name, timeout_ms=5000):
        worker = getattr(self, attr_name, None)
        if not worker:
            return True
        try:
            running = worker.isRunning()
        except RuntimeError:
            setattr(self, attr_name, None)
            return True
        if running:
            try:
                worker.stop()
            except AttributeError:
                pass
            worker.wait(timeout_ms)
        try:
            if worker.isRunning():
                return False
        except RuntimeError:
            pass
        return True

    @contextlib.contextmanager
    def suspend_isolation(self):
        panels = cmds.getPanel(type='modelPanel') or []
        isolation_states = {}
        for panel in panels:
            state = cmds.isolateSelect(panel, query=True, state=True)
            isolation_states[panel] = state
            if state:
                cmds.isolateSelect(panel, state=False)
        try:
            yield
        finally:
            for panel, state in isolation_states.items():
                if state:
                    cmds.isolateSelect(panel, state=True)

    def shutdown_for_reload(self):
        if getattr(self, '_shutdown_for_reload_done', False):
            return True
        self._is_closing = True
        if self.update_timer:
            self.update_timer.stop()
        if not self._stop_worker_for_close('update_check_worker', 20000):
            self._is_closing = False
            return False
        if not self._stop_worker_for_close('update_install_worker', 20000):
            self._is_closing = False
            return False
        if not self._stop_worker_for_close('hp_worker'):
            self._is_closing = False
            return False
        if not self._stop_worker_for_close('lp_worker'):
            self._is_closing = False
            return False
        try:
            self.restore_subgroup_colors()
        except Exception:
            pass
        for job_id in self.script_jobs:
            try:
                if cmds.scriptJob(exists=job_id):
                    cmds.scriptJob(kill=job_id, force=True)
            except:
                pass
        self.script_jobs = []
        self.remove_bg_undo_event_filter()
        self.close_update_progress()
        self._shutdown_for_reload_done = True
        return True

    def closeEvent(self, event):
        if not self.shutdown_for_reload():
            event.ignore()
            return
        super(BakeManagerUI, self).closeEvent(event)

    def reload_data_from_scene(self):
        try:
            self.objectName()
        except RuntimeError:
            return
        self.core._node_cache.clear()
        self.root_pairs = bg_core.BakeSessionModel.load()
        self.active_root_id = None
        self.active_subgroup_name = None
        self.is_isolated = False
        self.set_grouping_mode('conflict_safe')
        self.refresh_right_panel()
        self.refresh_left_panel()

    def refresh_localized_ui(self):
        bg_l10n.localize_widget_tree(self)
        self.refresh_workspace_texts()
        self.refresh_update_menu()
        self.refresh_right_panel()
        self.refresh_left_panel()

    def set_localized_button_state(self, button, key):
        if not button:
            return
        button.setProperty("bg_i18n_key", key)
        button.setText(bg_l10n.text(key))
        tip = bg_l10n.tooltip(key)
        button.setToolTip(tip)
        button.setStatusTip(tip)
        button.setProperty("bg_status_tip", tip)

    def show_language_menu(self):
        menu = QtWidgets.QMenu(self)
        current = bg_l10n.current_language()
        for lang in bg_l10n.available_languages():
            action = menu.addAction(lang.get("label", lang.get("code", "")))
            action.setCheckable(True)
            action.setChecked(lang.get("code") == current)
            action.triggered.connect(lambda checked=False, code=lang.get("code"): self.set_language_ui(code))
        menu.exec_(self.btn_language.mapToGlobal(QtCore.QPoint(0, self.btn_language.height())))

    def set_language_ui(self, code):
        if not code:
            return
        bg_l10n.set_language(code)
        self.refresh_localized_ui()
        if self.active_root_id:
            pair = next((p for p in self.root_pairs if p['id'] == self.active_root_id), None)
            if pair:
                hp_main, lp_main, _ = self.core.resolve_main_nodes(pair)
                if hp_main and lp_main:
                    self.sync_toggle_buttons(hp_main, lp_main)
        cmds.inViewMessage(amg=bg_l10n.text("Language switched to {name}").format(name=code), pos='midCenter', fade=True)

    def setup_update_system(self):
        try:
            self._update_config = bg_update.UpdateClient(token="").config
        except Exception as exc:
            self._update_config = {}
            print("Bake Master update configuration load failed: {}".format(exc))
        self.refresh_update_menu()
        self.update_timer = QtCore.QTimer(self)
        self.update_timer.setSingleShot(True)
        self.update_timer.timeout.connect(lambda: self.start_update_check(manual=False))
        if self.auto_update_enabled() and self.update_check_is_due():
            self.update_timer.start(1500)

    def auto_update_enabled(self):
        option_name = "BakeGroupsAutoCheckUpdates"
        try:
            if cmds.optionVar(exists=option_name):
                return bool(cmds.optionVar(query=option_name))
        except Exception:
            pass
        return bool(self._update_config.get("auto_check", True))

    def set_auto_update_enabled(self, enabled):
        try:
            cmds.optionVar(intValue=("BakeGroupsAutoCheckUpdates", int(bool(enabled))))
        except Exception:
            pass
        if enabled and self.update_check_is_due():
            if self.update_timer and not self.update_timer.isActive():
                self.update_timer.start(250)
        elif self.update_timer:
            self.update_timer.stop()
        self.refresh_update_menu()

    def update_check_is_due(self):
        last_check = 0
        try:
            if cmds.optionVar(exists="BakeGroupsLastUpdateCheck"):
                last_check = int(cmds.optionVar(query="BakeGroupsLastUpdateCheck") or 0)
        except Exception:
            pass
        interval = max(1, int(self._update_config.get("check_interval_hours", 24))) * 3600
        import time
        return int(time.time()) - last_check >= interval

    def record_update_check_time(self):
        import time
        try:
            cmds.optionVar(intValue=("BakeGroupsLastUpdateCheck", int(time.time())))
        except Exception:
            pass

    def refresh_update_menu(self):
        if not hasattr(self, 'action_check_update'):
            return
        self.action_check_update.setText(bg_l10n.text("Check for Updates"))
        self.action_auto_update.setText(bg_l10n.text("Check Automatically"))
        self.action_auto_update.setChecked(self.auto_update_enabled())
        self.action_open_update_repository.setText(bg_l10n.text("Open Update Repository"))
        busy = bool(
            (self.update_check_worker and self.update_check_worker.isRunning()) or
            (self.update_install_worker and self.update_install_worker.isRunning())
        )
        self.action_check_update.setEnabled(not busy)
        self.btn_update.setToolTip(bg_l10n.tooltip("Check for Updates", bg_l10n.text("Check for Updates")))

    def _set_update_button_state(self, state):
        if not hasattr(self, 'btn_update'):
            return
        available = state == "available"
        self.btn_update.setProperty("updateAvailable", available)
        icon_type = QtWidgets.QStyle.SP_ArrowDown if available else QtWidgets.QStyle.SP_BrowserReload
        self.btn_update.setIcon(self.style().standardIcon(icon_type))
        self.btn_update.style().unpolish(self.btn_update)
        self.btn_update.style().polish(self.btn_update)
        self.btn_update.update()

    def _maya_version_for_update(self):
        raw = str(cmds.about(version=True))
        match = re.search(r"\d{4}", raw)
        return match.group(0) if match else raw

    def open_update_repository(self):
        url = self._update_config.get("repository_url")
        if url:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(str(url)))

    def start_update_check(self, manual=False):
        if self._is_closing:
            return
        if self.update_check_worker and self.update_check_worker.isRunning():
            return
        if self.update_install_worker and self.update_install_worker.isRunning():
            return
        self._update_check_manual = bool(manual)
        worker = UpdateCheckWorker(
            getattr(bg_version, "__version__", "0.0.0"),
            self._maya_version_for_update(),
            None,
            self,
        )
        self.update_check_worker = worker
        worker.result_ready.connect(self.on_update_check_result)
        worker.error.connect(self.on_update_check_error)
        worker.cancelled.connect(self.on_update_check_cancelled)
        worker.finished.connect(lambda w=worker: self.release_update_worker("update_check_worker", w))
        self.btn_update.setEnabled(False)
        self._set_update_button_state("checking")
        worker.start()

    def release_update_worker(self, attr_name, worker):
        if getattr(self, attr_name, None) is worker:
            setattr(self, attr_name, None)
        try:
            worker.deleteLater()
        except RuntimeError:
            pass
        busy = bool(
            (self.update_check_worker and self.update_check_worker.isRunning()) or
            (self.update_install_worker and self.update_install_worker.isRunning())
        )
        if not busy and not self._is_closing:
            self.btn_update.setEnabled(True)
            self.refresh_update_menu()

    def on_update_check_result(self, manifest):
        self.record_update_check_time()
        if manifest.get("available"):
            self._pending_update = dict(manifest)
            self._set_update_button_state("available")
            self.show_update_available_dialog(manifest)
        else:
            self._pending_update = None
            self._set_update_button_state("idle")
            if self._update_check_manual:
                QtWidgets.QMessageBox.information(
                    self,
                    bg_l10n.text("Check for Updates"),
                    bg_l10n.text("You're up to date.") + "\n" +
                    bg_l10n.text("Current version: {version}").format(
                        version=getattr(bg_version, "__version__", "Unknown")
                    ),
                )

    def on_update_check_error(self, error):
        self._set_update_button_state("idle")
        if self._update_check_manual:
            QtWidgets.QMessageBox.warning(
                self,
                bg_l10n.text("Update Error"),
                bg_l10n.text("Could not check for updates: {error}").format(error=error),
            )
        else:
            print("Bake Master automatic update check failed: {}".format(error))

    def on_update_check_cancelled(self):
        self._set_update_button_state("idle")

    def localized_update_notes(self, manifest):
        notes = manifest.get("notes") or {}
        if isinstance(notes, dict):
            value = notes.get(bg_l10n.current_language()) or notes.get("en") or []
        else:
            value = notes
        if isinstance(value, (list, tuple)):
            return "\n".join(u"• {}".format(item) for item in value)
        return str(value or "")

    def show_update_available_dialog(self, manifest):
        dialog = QtWidgets.QMessageBox(self)
        dialog.setIcon(QtWidgets.QMessageBox.Information)
        dialog.setWindowTitle(bg_l10n.text("Update Available"))
        dialog.setText(
            bg_l10n.text("Update available: {version}").format(version=manifest.get("version", ""))
        )
        details = self.localized_update_notes(manifest)
        if details:
            dialog.setInformativeText(bg_l10n.text("What's New") + "\n\n" + details)
        install_button = dialog.addButton(
            bg_l10n.text("Download and Install"),
            QtWidgets.QMessageBox.AcceptRole,
        )
        dialog.addButton(bg_l10n.text("Later"), QtWidgets.QMessageBox.RejectRole)
        if hasattr(dialog, "exec_"):
            dialog.exec_()
        else:
            dialog.exec()
        if dialog.clickedButton() is install_button:
            self.start_update_install(manifest)

    def start_update_install(self, manifest):
        if self._is_closing or (self.update_install_worker and self.update_install_worker.isRunning()):
            return
        progress = QtWidgets.QProgressDialog(
            bg_l10n.text("Downloading update... {percent}%").format(percent=0),
            bg_l10n.text("Cancel"),
            0,
            100,
            self,
        )
        progress.setWindowTitle(bg_l10n.text("Installing Update"))
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumDuration(0)
        self.update_progress_dialog = progress
        worker = UpdateInstallWorker(
            manifest,
            self._maya_version_for_update(),
            None,
            self,
        )
        self.update_install_worker = worker
        worker.progress_changed.connect(self.on_update_install_progress)
        worker.installed.connect(self.on_update_installed)
        worker.error.connect(self.on_update_install_error)
        worker.cancelled.connect(self.on_update_install_cancelled)
        worker.finished.connect(lambda w=worker: self.release_update_worker("update_install_worker", w))
        progress.canceled.connect(worker.stop)
        self.btn_update.setEnabled(False)
        worker.start()

    def on_update_install_progress(self, value, stage):
        progress = self.update_progress_dialog
        if not progress:
            return
        progress.setValue(max(0, min(100, int(value))))
        if stage in ("download", "download_complete"):
            text = bg_l10n.text("Downloading update... {percent}%").format(percent=int(value))
        elif stage in ("verify_archive",):
            text = bg_l10n.text("Verifying update...")
        else:
            text = bg_l10n.text("Installing update...")
        progress.setLabelText(text)

    def close_update_progress(self):
        progress = self.update_progress_dialog
        self.update_progress_dialog = None
        if progress:
            try:
                progress.close()
                progress.deleteLater()
            except RuntimeError:
                pass

    def on_update_installed(self, result):
        self.close_update_progress()
        self._pending_update = None
        self._set_update_button_state("idle")
        message = bg_l10n.text("Update installed successfully.")
        if result.get("requires_restart", True):
            message += "\n\n" + bg_l10n.text("This update requires Maya to restart.")
        QtWidgets.QMessageBox.information(
            self,
            bg_l10n.text("Update Installed"),
            message,
        )

    def on_update_install_error(self, error):
        self.close_update_progress()
        self._set_update_button_state("available" if self._pending_update else "idle")
        QtWidgets.QMessageBox.warning(
            self,
            bg_l10n.text("Update Error"),
            bg_l10n.text("Update failed: {error}").format(error=error),
        )

    def on_update_install_cancelled(self):
        self.close_update_progress()
        self._set_update_button_state("available" if self._pending_update else "idle")

    def load_custom_session(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, bg_l10n.text("Select session file"), "", bg_l10n.text("JSON Files (*.json)"))
        if not file_path:
            return
        try:
            import json
            with open(file_path, 'r') as f:
                data = json.load(f)
            if isinstance(data, dict) and 'pairs' in data:
                pairs = data['pairs']
            elif isinstance(data, list):
                pairs = data
            else:
                cmds.warning(bg_l10n.text("Invalid session file format: expected a list or a 'pairs' key."))
                return

            if not isinstance(pairs, list):
                cmds.warning(bg_l10n.text("Invalid session file format: 'pairs' must be a list."))
                return

            seen_ids = set()
            for pair in pairs:
                if not isinstance(pair, dict):
                    continue
                if 'id' not in pair or pair['id'] in seen_ids:
                    pair['id'] = str(uuid.uuid4())
                if 'locked' not in pair:
                    pair['locked'] = []
                if 'final_smooth_states' not in pair or not isinstance(pair.get('final_smooth_states'), dict):
                    pair['final_smooth_states'] = {}
                if pair.get('grouping_mode') not in ('conflict_safe', 'one_lp_per_group', 'legacy_compact'):
                    pair['grouping_mode'] = 'legacy_compact'
                seen_ids.add(pair['id'])

            self.core._node_cache.clear()
            self.root_pairs = [p for p in pairs if isinstance(p, dict)]
            if hasattr(self.core, 'root_pairs'):
                self.core.root_pairs = self.root_pairs
            self.active_root_id = None
            self.active_subgroup_name = None
            self.is_isolated = False
            self.set_grouping_mode('conflict_safe')
            self.refresh_right_panel()
            self.refresh_left_panel()
            cmds.inViewMessage(amg=bg_l10n.text("Session successfully loaded from: {name}").format(name=os.path.basename(file_path)), pos='midCenter', fade=True)
        except Exception as e:
            cmds.warning(bg_l10n.text("Error loading session: {error}").format(error=e))

    def manual_save_session(self):
        bg_core.BakeSessionModel.save(self.root_pairs)
        cmds.inViewMessage(amg=bg_l10n.text("Session Saved Successfully"), pos='midCenter', fade=True)

    def confirm_action(self, text):
        if self.skip_delete_confirm:
            return True
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(bg_l10n.text("Confirm Action"))
        box.setText(text)
        cb = QtWidgets.QCheckBox(bg_l10n.text("Don't ask again in this session"))
        bg_l10n.localize_widget(cb)
        box.setCheckBox(cb)
        box.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        box.setDefaultButton(QtWidgets.QMessageBox.No)
        box.setStyleSheet("QMessageBox { background-color: #242424; color: white; } QPushButton { background-color: #333; padding: 5px; }")
        res = box.exec_() if hasattr(box, 'exec_') else box.exec_()
        if cb.isChecked():
            self.skip_delete_confirm = True
        return res == QtWidgets.QMessageBox.Yes

    def add_group_list_empty_state(self, text_key):
        label = QtWidgets.QLabel(bg_l10n.text(text_key))
        label.setObjectName("EmptyState")
        label.setAlignment(QtCore.Qt.AlignCenter)
        label.setWordWrap(True)
        self.subgroups_layout.addWidget(label)

    def refresh_left_panel(self):
        import re  # Гарантированная защита от NameError в среде Maya

        for i in reversed(range(self.subgroups_layout.count())):
            item = self.subgroups_layout.takeAt(i)
            if item.widget():
                item.widget().deleteLater()

        self.update_workspace_context()
        if hasattr(self, 'lbl_group_count'):
            self.lbl_group_count.setText(bg_l10n.text("{count} Groups").format(count=0))

        if getattr(self, 'is_final_view', False):
            self.render_final_view()
            bg_l10n.localize_widget_tree(self.subgroups_widget)
            self.schedule_dock_relayout()
            return

        if not self.active_root_id:
            self.add_group_list_empty_state("No Active Task")
            self.schedule_dock_relayout()
            return
        pair = next((p for p in self.root_pairs if p['id'] == self.active_root_id), None)
        if not pair:
            self.add_group_list_empty_state("No Active Task")
            self.schedule_dock_relayout()
            return
        hp_main, lp_main, _ = self.core.resolve_main_nodes(pair)
        if not hp_main or not lp_main:
            self.add_group_list_empty_state("HP or LP root groups not found.")
            self.schedule_dock_relayout()
            return

        hp_c = cmds.listRelatives(hp_main, children=True, type='transform', fullPath=True) or []
        lp_c = cmds.listRelatives(lp_main, children=True, type='transform', fullPath=True) or []

        groups = {}
        for child in hp_c:
            if not cmds.objExists(child):
                continue
            if cmds.listRelatives(child, shapes=True):
                continue
            sn = child.split('|')[-1]
            
            # Если включен Keep HP — доверяем вашей структуре и берем группу без проверок суффиксов
            if self.cb_keep_hp_structure.isChecked():
                is_hp = True
            else:
                is_hp = cmds.objExists(child + "." + bg_core.BakeConfig.ATTR_BAKE_GROUP) and cmds.getAttr(child + "." + bg_core.BakeConfig.ATTR_BAKE_GROUP) == "HP"
                if not is_hp and sn.endswith(bg_core.BakeConfig.SUFFIX_HP):
                    is_hp = True
            
            if not is_hp:
                continue
            ui_name = sn
            match = re.search(r'(_HP|_hp|HP|hp)(\d*)$', sn)
            if match:
                ui_name = sn[:match.start()] + match.group(2)
            groups[ui_name] = {'hp': child, 'lp': None}

        for child in lp_c:
            if not cmds.objExists(child):
                continue
            if cmds.listRelatives(child, shapes=True):
                continue
            sn = child.split('|')[-1]
            
            # Ослабляем фильтр для LP, если активен режим сохранения структуры
            if self.cb_keep_hp_structure.isChecked():
                is_lp = True
            else:
                is_lp = cmds.objExists(child + "." + bg_core.BakeConfig.ATTR_BAKE_GROUP) and cmds.getAttr(child + "." + bg_core.BakeConfig.ATTR_BAKE_GROUP) == "LP"
                if not is_lp and sn.endswith(bg_core.BakeConfig.SUFFIX_LP):
                    is_lp = True
            
            if not is_lp:
                continue
            ui_name = sn
            match = re.search(r'(_LP|_lp|LP|lp)(\d*)$', sn)
            if match:
                ui_name = sn[:match.start()] + match.group(2)
            if ui_name in groups:
                groups[ui_name]['lp'] = child
            elif self.cb_keep_hp_structure.isChecked():
                groups[ui_name] = {'hp': None, 'lp': child}

        locked_list = pair.get('locked', [])
        sorted_group_names = sorted(groups.keys())
        if hasattr(self, 'cb_color_subgroups') and self.cb_color_subgroups.isChecked():
            self.ensure_subgroup_color_indices(sorted_group_names)
        for ui_name in sorted_group_names:
            hp_node, lp_node = groups[ui_name]['hp'], groups[ui_name]['lp']
            is_active_subgroup = self.active_subgroup_name == ui_name
            frame = QtWidgets.QFrame()
            frame.setObjectName("SubgroupRow")
            frame.setStyleSheet(self.subgroup_row_style(ui_name, is_active_subgroup))
            layout = QtWidgets.QHBoxLayout(frame)
            layout.setContentsMargins(5, 4, 5, 4)
            layout.setSpacing(5)

            is_vis = self.subgroup_pair_is_visible(hp_node, lp_node)
            btn_vis = QtWidgets.QPushButton()
            self.set_compact_visibility_state(btn_vis, is_vis)
            btn_vis.clicked.connect(lambda checked=False, h=hp_node, l=lp_node, b=btn_vis: self.run_undoable_bg_action("Subgroup Visibility", self.toggle_subgroup_vis, h, l, b))
            btn_vis.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            btn_vis.customContextMenuRequested.connect(lambda pos, h=hp_node, l=lp_node: self.run_undoable_bg_action("Isolate Subgroup Visibility", self.isolate_subgroup_vis, h, l))
            layout.addWidget(btn_vis)

            btn_name = SubgroupButton(ui_name)
            btn_name.setStyleSheet(self.subgroup_name_style(ui_name, is_active_subgroup))
            btn_name.clicked.connect(lambda checked=False, n=ui_name: self.set_active_subgroup(n))
            btn_name.doubleClicked.connect(lambda checked=False, h=hp_node, l=lp_node: self.select_meshes_in_group(h, l))
            btn_name.rightClicked.connect(lambda checked=False, old_name=ui_name, h=hp_node, l=lp_node: self.run_undoable_bg_action("Rename Group", self.rename_subgroup_ui, old_name, h, l))
            btn_name.setMinimumHeight(32)
            layout.addWidget(btn_name, stretch=1)

            btn_plus = QtWidgets.QPushButton()
            configure_symbol_button(btn_plus, "+", "Add", size=32)
            btn_plus.setStyleSheet(self.subgroup_add_button_style(is_active_subgroup))
            btn_plus.clicked.connect(lambda checked=False, h=hp_node, l=lp_node, pm_hp=hp_main, pm_lp=lp_main: self.run_undoable_bg_action("Add to Group", self.add_to_groups_ui, h, l, pm_hp, pm_lp))
            layout.addWidget(btn_plus)

            btn_lock = QtWidgets.QPushButton()
            btn_lock.setProperty("compact", True)
            btn_lock.setFixedSize(32, 32)
            is_locked = ui_name in locked_list
            icon_name = "Look_Icon_Button.png" if is_locked else "Unlook_Icon_Button.png"
            icon_path = os.path.join(os.path.dirname(__file__), icon_name)
            if os.path.exists(icon_path):
                btn_lock.setIcon(QtGui.QIcon(icon_path))
                btn_lock.setIconSize(QtCore.QSize(17, 17))
            btn_lock.setStyleSheet("background-color: #643f45; padding: 0;" if is_locked else "background-color: #3b6752; padding: 0;")
            lock_tip_key = "Unlock subgroup" if is_locked else "Lock subgroup"
            btn_lock.setToolTip(bg_l10n.tooltip(lock_tip_key))
            btn_lock.setStatusTip(bg_l10n.tooltip(lock_tip_key))
            btn_lock.clicked.connect(lambda checked=False, n=ui_name: self.run_undoable_bg_action("Toggle Group Lock", self.toggle_lock, n))
            layout.addWidget(btn_lock)

            btn_del = QtWidgets.QPushButton()
            configure_symbol_button(btn_del, "X", "Delete Group", size=32)
            btn_del.setProperty("role", "danger")
            btn_del.clicked.connect(lambda checked=False, h=hp_node, l=lp_node, r_h=hp_main, r_l=lp_main: self.run_undoable_bg_action("Delete Group", self.safe_delete_subgroup_ui, h, l, r_h, r_l))
            layout.addWidget(btn_del)

            self.subgroups_layout.addWidget(frame)
        if hasattr(self, 'lbl_group_count'):
            self.lbl_group_count.setText(bg_l10n.text("{count} Groups").format(count=len(sorted_group_names)))
        if not sorted_group_names:
            self.add_group_list_empty_state("No Bake Master Yet")
        bg_l10n.localize_widget_tree(self.subgroups_widget)
        self.clear_disabled_tooltips(self.subgroups_widget)
        self.schedule_dock_relayout()

    def clear_disabled_tooltips(self, root):
        if not root:
            return
        widgets = [root] + root.findChildren(QtWidgets.QWidget)
        for widget in widgets:
            if widget.property("bg_no_tooltip"):
                widget.setToolTip("")
                widget.setStatusTip("")
                widget.setProperty("bg_status_tip", "")

    def activate_root(self, pair):
        self._syncing_grouping_mode = True
        try:
            self.set_grouping_mode(pair.get('grouping_mode', 'legacy_compact'))
        finally:
            self._syncing_grouping_mode = False

        hp_node, lp_node, _ = self.core.resolve_main_nodes(pair)
        if not hp_node or not lp_node:
            self.active_root_id = pair['id']
            return cmds.warning("Original groups were deleted.")

        if self.active_root_id == pair['id']:
            self.is_isolated = not self.is_isolated
        else:
            self.active_root_id = pair['id']
            self.active_subgroup_name = None
            self.is_isolated = True
            self.subgroup_color_index_map = {}

        panel = 'modelPanel4'
        if not cmds.modelEditor(panel, exists=True):
            panel = cmds.playblast(activeEditor=True)
        if cmds.modelEditor(panel, exists=True):
            cmds.isolateSelect(panel, state=self.is_isolated)
            if self.is_isolated:
                iso_set = cmds.isolateSelect(panel, q=True, viewObjects=True)
                cmds.isolateSelect(panel, addDagObject=hp_node)
                cmds.isolateSelect(panel, addDagObject=lp_node)
                if iso_set:
                    set_name = iso_set[0] if isinstance(iso_set, list) else iso_set
                    if cmds.objExists(set_name):
                        cmds.sets(clear=set_name)
                nodes_to_add = [n for n in [hp_node, lp_node] if n and cmds.objExists(n)]
                for node in nodes_to_add:
                    cmds.isolateSelect(panel, addDagObject=node)
            cmds.isolateSelect(panel, update=True)
        else:
            cmds.warning("Active modelPanel not found for isolation.")

        self.sync_toggle_buttons(hp_node, lp_node)
        self.refresh_right_panel()
        self.refresh_left_panel()
        skip_color_update = bool(getattr(self, '_skip_color_update_once', False))
        self._skip_color_update_once = False
        color_groups_enabled = hasattr(self, 'cb_color_subgroups') and self.cb_color_subgroups.isChecked()
        if skip_color_update and color_groups_enabled:
            self.restore_subgroup_colors()
        elif color_groups_enabled:
            self.update_subgroup_colors()

    def select_meshes_in_group(self, hp_grp, lp_grp):
        to_select = []
        for grp in [hp_grp, lp_grp]:
            if grp and cmds.objExists(grp):
                meshes = cmds.listRelatives(grp, allDescendents=True, type='mesh', fullPath=True) or []
                to_select.extend(list(set([cmds.listRelatives(m, parent=True, fullPath=True)[0] for m in meshes])))
        if to_select:
            cmds.select(to_select, replace=True)
        else:
            cmds.select(clear=True)

    def sync_toggle_buttons(self, hp_node, lp_node):
        self.btn_toggle_hp.blockSignals(True)
        self.btn_toggle_lp.blockSignals(True)
        self.btn_toggle_groups.blockSignals(True)

        hp_vis = cmds.getAttr("{}.visibility".format(hp_node)) if hp_node and cmds.objExists(hp_node) else False
        lp_vis = cmds.getAttr("{}.visibility".format(lp_node)) if lp_node and cmds.objExists(lp_node) else False

        self.btn_toggle_hp.setChecked(hp_vis)
        self.set_localized_button_state(self.btn_toggle_hp, "HP Visible" if hp_vis else "HP Hidden")
        self.btn_toggle_hp.setStyleSheet("background-color: #3b6752;" if hp_vis else "background-color: #643f45;")

        self.btn_toggle_lp.setChecked(lp_vis)
        self.set_localized_button_state(self.btn_toggle_lp, "LP Visible" if lp_vis else "LP Hidden")
        self.btn_toggle_lp.setStyleSheet("background-color: #3b6752;" if lp_vis else "background-color: #643f45;")

        material_filter = getattr(self, 'active_material_visibility_filter', None)
        group_state = self.get_active_subgroups_visibility_state(hp_node, lp_node, hp_vis, lp_vis)
        if material_filter:
            material_nodes = [
                node for node in self.get_active_subgroup_nodes(hp_node, lp_node)
                if self.material_slot_from_subgroup_name(node) == material_filter
            ]
            material_visible = sum(1 for node in material_nodes if cmds.objExists(node) and self.is_visible(node))
            material_state = "all" if material_nodes and material_visible == len(material_nodes) else "partial"
            self.btn_toggle_groups.setChecked(False)
            if material_state == "all":
                self.set_localized_button_state(self.btn_toggle_groups, "Groups Vis/M")
                self.btn_toggle_groups.setStyleSheet("background-color: #8b6a32; color: white; font-weight: bold;")
            else:
                self.set_localized_button_state(self.btn_toggle_groups, "Groups Hid/M")
                self.btn_toggle_groups.setStyleSheet("background-color: #8b6a32; color: white; font-weight: bold;")
            self.btn_toggle_hp.blockSignals(False)
            self.btn_toggle_lp.blockSignals(False)
            self.btn_toggle_groups.blockSignals(False)
            return

        if group_state == "all":
            self.btn_toggle_groups.setChecked(True)
            self.set_localized_button_state(self.btn_toggle_groups, "Groups Vis")
            self.btn_toggle_groups.setStyleSheet("background-color: #3b6752;")
        elif group_state == "partial":
            self.btn_toggle_groups.setChecked(False)
            self.set_localized_button_state(self.btn_toggle_groups, "Groups Hid")
            self.btn_toggle_groups.setStyleSheet("background-color: #8b6a32; color: white; font-weight: bold;")
        else:
            self.btn_toggle_groups.setChecked(False)
            self.set_localized_button_state(self.btn_toggle_groups, "Groups Hidden")
            self.btn_toggle_groups.setStyleSheet("background-color: #643f45;")

        self.btn_toggle_hp.blockSignals(False)
        self.btn_toggle_lp.blockSignals(False)
        self.btn_toggle_groups.blockSignals(False)

    def get_active_subgroup_nodes(self, hp_node, lp_node):
        nodes = []
        for p_node in [hp_node, lp_node]:
            if not p_node or not cmds.objExists(p_node):
                continue
            for child in (cmds.listRelatives(p_node, children=True, type='transform', fullPath=True) or []):
                if cmds.objExists(child):
                    nodes.append(child)
        return nodes

    def material_slot_from_subgroup_name(self, node_or_name):
        name = str(node_or_name or "").split('|')[-1]
        match = re.match(r"^(M\d{2,})(?:_|\.)", name)
        return match.group(1) if match else None

    def get_active_material_slots(self, hp_node, lp_node):
        slots = set()
        for node in self.get_active_subgroup_nodes(hp_node, lp_node):
            slot = self.material_slot_from_subgroup_name(node)
            if slot:
                slots.add(slot)
        return sorted(slots)

    def mesh_transform_nodes_under(self, node):
        if not node or not cmds.objExists(node):
            return []
        shapes = cmds.listRelatives(node, shapes=True, fullPath=True, type='mesh', noIntermediate=True) or []
        if shapes:
            return [node]
        mesh_shapes = cmds.listRelatives(node, allDescendents=True, fullPath=True, type='mesh', noIntermediate=True) or []
        transforms = []
        seen = set()
        for shape in mesh_shapes:
            parents = cmds.listRelatives(shape, parent=True, fullPath=True) or []
            if parents and parents[0] not in seen:
                seen.add(parents[0])
                transforms.append(parents[0])
        return transforms

    def lp_material_records_for_node(self, lp_node, include_faces=True):
        if not om or not lp_node or not cmds.objExists(lp_node):
            return []
        shapes = cmds.listRelatives(lp_node, shapes=True, fullPath=True, type='mesh', noIntermediate=True) or []
        if not shapes:
            return []
        try:
            sel = om.MSelectionList()
            sel.add(lp_node)
            dag = sel.getDagPath(0)
            if dag.hasFn(om.MFn.kTransform):
                dag.extendToShape()
            mesh_fn = om.MFnMesh(dag)
            shaders, face_shader_indices = mesh_fn.getConnectedShaders(dag.instanceNumber())
        except Exception:
            return []

        records = []
        for shader_index, shader_obj in enumerate(shaders):
            try:
                sg = om.MFnDependencyNode(shader_obj).name()
            except Exception:
                continue
            material = self.material_from_shading_engine(sg)
            key = material or sg
            faces = [
                face_id for face_id, assigned_index in enumerate(face_shader_indices)
                if int(assigned_index) == shader_index
            ] if include_faces else []
            if faces or not include_faces:
                records.append({
                    "key": key,
                    "material": material,
                    "faces": faces
                })
        return records

    def lp_material_slot_map_for_nodes(self, lp_nodes):
        records_by_key = {}
        for node in lp_nodes or []:
            for mesh_node in self.mesh_transform_nodes_under(node):
                for rec in self.lp_material_records_for_node(mesh_node, include_faces=False):
                    key = rec.get("key")
                    if key:
                        records_by_key.setdefault(key, rec)
        if len(records_by_key) <= 1:
            return {}
        ordered = sorted(records_by_key.values(), key=lambda rec: ((rec.get("material") or rec.get("key") or "").split('|')[-1].lower(), (rec.get("key") or "").split('|')[-1].lower()))
        return {rec.get("key"): "M{:02d}".format(index) for index, rec in enumerate(ordered, 1) if rec.get("key")}

    def lp_material_slots_for_node(self, lp_node, slot_by_key=None):
        slot_by_key = slot_by_key or {}
        records = self.lp_material_records_for_node(lp_node, include_faces=True)
        if len(records) <= 1:
            return {}
        if not slot_by_key:
            ordered = sorted(records, key=lambda rec: ((rec.get("material") or rec.get("key") or "").split('|')[-1].lower(), (rec.get("key") or "").split('|')[-1].lower()))
            slot_by_key = {rec.get("key"): "M{:02d}".format(index) for index, rec in enumerate(ordered, 1) if rec.get("key")}
        slots = {}
        for rec in records:
            slot = slot_by_key.get(rec.get("key"))
            if slot:
                slots.setdefault(slot, [])
                slots[slot].extend(rec.get("faces") or [])
        return slots

    def material_from_shading_engine(self, shading_engine):
        try:
            materials = cmds.listConnections("{}.surfaceShader".format(shading_engine), source=True, destination=False) or []
            return materials[0] if materials else None
        except Exception:
            return None

    def face_components_for_indices(self, node, faces):
        if not faces:
            return []
        faces = sorted(set(int(face) for face in faces))
        components = []
        start = faces[0]
        prev = faces[0]
        for face in faces[1:]:
            if face == prev + 1:
                prev = face
                continue
            components.append("{}.f[{}]".format(node, start) if start == prev else "{}.f[{}:{}]".format(node, start, prev))
            start = prev = face
        components.append("{}.f[{}]".format(node, start) if start == prev else "{}.f[{}:{}]".format(node, start, prev))
        return components

    def show_all_lp_material_faces(self, lp_nodes):
        for node in lp_nodes or []:
            if node and cmds.objExists(node):
                try:
                    cmds.showHidden(node)
                except Exception:
                    pass

    def isolate_lp_material_faces(self, lp_node, slot, slot_by_key=None):
        slots = self.lp_material_slots_for_node(lp_node, slot_by_key=slot_by_key)
        if not slots:
            return False
        self.show_all_lp_material_faces([lp_node])
        hide_faces = []
        for other_slot, faces in slots.items():
            if other_slot != slot:
                hide_faces.extend(faces)
        if not hide_faces:
            return True
        saved_selection = cmds.ls(selection=True, long=True) or []
        try:
            components = self.face_components_for_indices(lp_node, hide_faces)
            if components:
                cmds.select(components, replace=True)
                cmds.hide()
        finally:
            if saved_selection:
                cmds.select(saved_selection, replace=True)
            else:
                cmds.select(clear=True)
        return True

    def apply_lp_material_slot_visibility(self, lp_node, slot, slot_by_key=None):
        if not lp_node or not cmds.objExists(lp_node):
            return False
        mesh_nodes = self.mesh_transform_nodes_under(lp_node)
        if mesh_nodes and not (len(mesh_nodes) == 1 and mesh_nodes[0] == lp_node):
            any_visible = False
            for mesh_node in mesh_nodes:
                if self.apply_lp_material_slot_visibility(mesh_node, slot, slot_by_key=slot_by_key):
                    any_visible = True
            cmds.setAttr("{}.visibility".format(lp_node), any_visible)
            return any_visible

        records = self.lp_material_records_for_node(lp_node, include_faces=True)
        if not records:
            cmds.setAttr("{}.visibility".format(lp_node), False)
            return False

        if len(records) == 1:
            rec_slot = (slot_by_key or {}).get(records[0].get("key"))
            visible = rec_slot == slot or (not slot_by_key and slot == "M01")
            if visible:
                self.show_all_lp_material_faces([lp_node])
            cmds.setAttr("{}.visibility".format(lp_node), visible)
            return visible

        visible = self.isolate_lp_material_faces(lp_node, slot, slot_by_key=slot_by_key)
        cmds.setAttr("{}.visibility".format(lp_node), visible)
        return visible

    def show_groups_visibility_context_menu(self, pos):
        pair = next((p for p in self.root_pairs if p['id'] == self.active_root_id), None)
        if not pair:
            return
        hp_main, lp_main, _ = self.core.resolve_main_nodes(pair)
        menu = QtWidgets.QMenu(self)
        slots = self.get_active_material_slots(hp_main, lp_main)
        if slots:
            for slot in slots:
                action = menu.addAction(bg_l10n.text("Only Show {slot}").format(slot=slot))
                action.triggered.connect(lambda checked=False, s=slot: self.run_undoable_bg_action("Only Show Material Section", self.only_show_material_slot, s))
            menu.addSeparator()
        else:
            action = menu.addAction(bg_l10n.text("No material sections found"))
            action.setEnabled(False)
        show_all_action = menu.addAction(bg_l10n.text("Groups Vis"))
        show_all_action.triggered.connect(lambda checked=False: self.run_undoable_bg_action("Groups Visibility", self.set_all_subgroups_vis, True))
        menu.exec_(self.btn_toggle_groups.mapToGlobal(pos)) if hasattr(menu, 'exec_') else menu.exec(self.btn_toggle_groups.mapToGlobal(pos))

    def only_show_material_slot(self, slot):
        pair = next((p for p in self.root_pairs if p['id'] == self.active_root_id), None)
        if not pair:
            return
        self.active_material_visibility_filter = slot
        hp_main, lp_main, _ = self.core.resolve_main_nodes(pair)
        if hp_main and cmds.objExists(hp_main):
            cmds.setAttr("{}.visibility".format(hp_main), True)
        if lp_main and cmds.objExists(lp_main):
            cmds.setAttr("{}.visibility".format(lp_main), True)

        hp_children = cmds.listRelatives(hp_main, children=True, type='transform', fullPath=True) if hp_main and cmds.objExists(hp_main) else []
        lp_children = cmds.listRelatives(lp_main, children=True, type='transform', fullPath=True) if lp_main and cmds.objExists(lp_main) else []
        hp_children = hp_children or []
        lp_children = lp_children or []
        lp_has_slot_children = any(self.material_slot_from_subgroup_name(child) for child in lp_children)
        lp_slot_by_key = self.lp_material_slot_map_for_nodes(lp_children) if not lp_has_slot_children else {}
        self.show_all_lp_material_faces(lp_children)

        for child in hp_children:
            if cmds.objExists(child):
                cmds.setAttr("{}.visibility".format(child), self.material_slot_from_subgroup_name(child) == slot)

        for child in lp_children:
            if not cmds.objExists(child):
                continue
            child_slot = self.material_slot_from_subgroup_name(child)
            if lp_has_slot_children:
                cmds.setAttr("{}.visibility".format(child), child_slot == slot)
            else:
                self.apply_lp_material_slot_visibility(child, slot, slot_by_key=lp_slot_by_key)

        if getattr(self, 'is_final_view', False):
            for widget_data in getattr(self, 'final_mesh_widgets', []):
                subgroup_name = widget_data.get('subgroup_name') or ''
                state = self.material_slot_from_subgroup_name(subgroup_name) == slot
                for hp in widget_data.get('hp_nodes', []):
                    if cmds.objExists(hp):
                        cmds.setAttr(hp + ".visibility", state)

        self.sync_toggle_buttons(hp_main, lp_main)
        self.refresh_left_panel()
        if hasattr(self, 'record_user_action'):
            self.record_user_action("Only Show Material Section", slot)

    def get_active_subgroups_visibility_state(self, hp_node, lp_node, hp_vis=None, lp_vis=None):
        child_nodes = self.get_active_subgroup_nodes(hp_node, lp_node)
        if not child_nodes:
            if hp_vis is None:
                hp_vis = cmds.getAttr("{}.visibility".format(hp_node)) if hp_node and cmds.objExists(hp_node) else False
            if lp_vis is None:
                lp_vis = cmds.getAttr("{}.visibility".format(lp_node)) if lp_node and cmds.objExists(lp_node) else False
            return "all" if (hp_vis or lp_vis) else "none"
        visible_count = sum(1 for node in child_nodes if cmds.getAttr("{}.visibility".format(node)))
        if visible_count == len(child_nodes):
            return "all"
        if visible_count == 0:
            return "none"
        return "partial"

    def toggle_root_vis(self, type_str, state):
        pair = next((p for p in self.root_pairs if p['id'] == self.active_root_id), None)
        if not pair:
            return

        if type_str == "LP" and getattr(self, 'is_final_view', False):
            self.set_final_low_visibility(pair.get('base', ''), state)
            return

        hp_node, lp_node, _ = self.core.resolve_main_nodes(pair)
        parent_node = hp_node if type_str == "HP" else lp_node
        btn = self.btn_toggle_hp if type_str == "HP" else self.btn_toggle_lp
        self.set_localized_button_state(btn, "{} Visible".format(type_str) if state else "{} Hidden".format(type_str))
        btn.setStyleSheet("background-color: #3b6752;" if state else "background-color: #643f45;")
        if parent_node and cmds.objExists(parent_node):
            cmds.setAttr("{}.visibility".format(parent_node), state)

    def set_all_subgroups_vis(self, state):
        pair = next((p for p in self.root_pairs if p['id'] == self.active_root_id), None)
        if not pair:
            return
        if getattr(self, 'active_material_visibility_filter', None):
            self.active_material_visibility_filter = None
            state = True
        hp_main, lp_main, _ = self.core.resolve_main_nodes(pair)
        for p_node in [hp_main, lp_main]:
            if p_node and cmds.objExists(p_node):
                for child in (cmds.listRelatives(p_node, children=True, type='transform', fullPath=True) or []):
                    cmds.setAttr("{}.visibility".format(child), state)
                    if state and p_node == lp_main:
                        self.show_all_lp_material_faces([child])
        self.set_localized_button_state(self.btn_toggle_groups, "Groups Vis" if state else "Groups Hidden")
        self.btn_toggle_groups.setStyleSheet("background-color: #3b6752;" if state else "background-color: #643f45;")
        if getattr(self, 'is_final_view', False):
            for widget_data in getattr(self, 'final_mesh_widgets', []):
                for hp in widget_data.get('hp_nodes', []):
                    if cmds.objExists(hp):
                        cmds.setAttr(hp + ".visibility", state)
        self.sync_toggle_buttons(hp_main, lp_main)
        self.refresh_left_panel()

    def is_visible(self, node):
        return cmds.getAttr("{}.visibility".format(node)) if node and cmds.objExists(node) else False

    def subgroup_pair_is_visible(self, hp, lp):
        nodes = [node for node in (hp, lp) if node and cmds.objExists(node)]
        return bool(nodes) and all(self.is_visible(node) for node in nodes)

    def toggle_subgroup_vis(self, hp, lp, btn_widget):
        new_state = not self.subgroup_pair_is_visible(hp, lp)
        if hp and cmds.objExists(hp):
            cmds.setAttr("{}.visibility".format(hp), new_state)
        if lp and cmds.objExists(lp):
            cmds.setAttr("{}.visibility".format(lp), new_state)
        if btn_widget.property("bg_compact_visibility"):
            self.set_compact_visibility_state(btn_widget, new_state)
        else:
            btn_widget.setText(bg_l10n.text("Vis" if new_state else "Hid"))
            btn_widget.setStyleSheet("background-color: #3b6752;" if new_state else "background-color: #643f45;")
        if getattr(self, 'is_final_view', False):
            pair = next((p for p in self.root_pairs if p['id'] == self.active_root_id), None)
            if pair:
                hp_final = "Bake_Groups|{}|HP".format(pair['base'])
                lp_final = "Bake_Groups|{}|LP".format(pair['base'])
                if cmds.objExists(hp_final):
                    cmds.setAttr(hp_final + ".visibility", new_state)
                if cmds.objExists(lp_final):
                    cmds.setAttr(lp_final + ".visibility", new_state)
        pair = next((p for p in self.root_pairs if p['id'] == self.active_root_id), None)
        if pair:
            hp_main, lp_main, _ = self.core.resolve_main_nodes(pair)
            self.sync_toggle_buttons(hp_main, lp_main)

    def isolate_subgroup_vis(self, hp, lp):
        pair = next((p for p in self.root_pairs if p['id'] == self.active_root_id), None)
        if not pair:
            return
        hp_main, lp_main, _ = self.core.resolve_main_nodes(pair)
        material_filter = getattr(self, 'active_material_visibility_filter', None)
        subgroup_nodes = []
        for root in (hp_main, lp_main):
            if not root or not cmds.objExists(root):
                continue
            for child in (cmds.listRelatives(root, children=True, type='transform', fullPath=True) or []):
                if cmds.objExists(child) and not cmds.listRelatives(child, shapes=True, type='mesh', noIntermediate=True):
                    subgroup_nodes.append(child)
        target_nodes = set(cmds.ls([node for node in (hp, lp) if node and cmds.objExists(node)], long=True) or [])
        if material_filter:
            material_nodes = set(node for node in subgroup_nodes if self.material_slot_from_subgroup_name(node) == material_filter)
            target_nodes = target_nodes.intersection(material_nodes)
            if not target_nodes:
                return
            subgroup_nodes = [node for node in subgroup_nodes if node in material_nodes]
        visible_nodes = set(node for node in subgroup_nodes if self.is_visible(node))
        show_all = bool(target_nodes) and visible_nodes == target_nodes
        for node in subgroup_nodes:
            cmds.setAttr("{}.visibility".format(node), True if show_all else node in target_nodes)
        if material_filter and show_all:
            self.only_show_material_slot(material_filter)
            return
        self.sync_toggle_buttons(hp_main, lp_main)
        self.refresh_left_panel()
        if hasattr(self, 'record_user_action'):
            target_names = [node.split('|')[-1] for node in (hp, lp) if node and cmds.objExists(node)]
            action = "Show All Group Visibility" if show_all else "Isolate Group Visibility"
            self.record_user_action(action, ", ".join(target_names))

    def show_subgroups_context_menu(self, pos):
        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet(bg_core.BakeConfig.STYLE_CONTEXT_MENU)
        action_optimize = menu.addAction("Optimize subgroups (delete empty)")
        action_optimize.triggered.connect(lambda checked=False: self.run_undoable_bg_action("Optimize Groups", self.optimize_subgroups))
        action_select_by_mesh = menu.addAction("Group search by mesh (Ctrl+Shift+Z)")
        action_select_by_mesh.triggered.connect(self.select_subgroup_by_selected_mesh)
        menu.addSeparator()
        action_select_low = menu.addAction("Select all _low meshes (Active Chapter)")
        action_select_low.triggered.connect(self.select_all_combined_low_meshes)
        action_select_book_low = menu.addAction("Select all _low meshes (Entire Book)")
        action_select_book_low.triggered.connect(self.select_all_book_low_meshes)
        bg_l10n.localize_menu(menu)
        menu.exec_(self.subgroups_widget.mapToGlobal(pos))

    def select_subgroup_by_selected_mesh(self, checked=False):
        import re
        
        # Получаем полный (long) путь к выделенным объектам
        sel = cmds.ls(sl=True, long=True)
        if not sel:
            self.log("Nothing is highlighted in the scene.", "yellow")
            return

        if not self.active_root_id:
            self.log("There is no active bake group. First, select a group in the TOC.", "yellow")
            return

        pair = next((p for p in self.root_pairs if p['id'] == self.active_root_id), None)
        if not pair:
            return

        hp_main, lp_main, _ = self.core.resolve_main_nodes(pair)
        
        # Собираем все дочерние группы (трансформы подгрупп) текущего Root
        subgroup_nodes = []
        for main_node in [hp_main, lp_main]:
            if main_node and cmds.objExists(main_node):
                children = cmds.listRelatives(main_node, children=True, type='transform', fullPath=True) or []
                subgroup_nodes.extend(children)

        found_ui_name = None

        # Ищем, какому дочернему узлу принадлежит выделенный объект
        for obj in sel:
            for sub_node in subgroup_nodes:
                # Работает как для самого трансформа, так и для вложенных нод/компонентов (фейсов, вертексов)
                if obj == sub_node or obj.startswith(sub_node + "|"):
                    sn = sub_node.split('|')[-1]
                    ui_name = sn
                    
                    # Универсальная регулярка для отсечения суффиксов и HP, и LP
                    match = re.search(r'(_HP|_hp|HP|hp|_LP|_lp|LP|lp)(\d*)$', sn)
                    if match:
                        ui_name = sn[:match.start()] + match.group(2)
                    
                    found_ui_name = ui_name
                    break
            if found_ui_name:
                break

        if found_ui_name:
            self.set_active_subgroup(found_ui_name)
            if hasattr(self, 'refresh_left_panel'):
                self.refresh_left_panel()
                
            self.log("A subgroup has been found and is active: {}".format(found_ui_name), "green")
        else:
            self.log("The selected object does not belong to any subgroup of the active Root.", "yellow")

    def select_all_combined_low_meshes(self):
        pair = next((p for p in self.root_pairs if p['id'] == self.active_root_id), None)
        if not pair:
            cmds.warning("No active group for selection.")
            return
        base_name = pair.get('base', '')
        chapter_grp_path = "LP_Combine_BG|{}".format(base_name)
        _, lp_main, _ = self.core.resolve_main_nodes(pair)
        search_root = chapter_grp_path if cmds.objExists(chapter_grp_path) else lp_main
        if not search_root or not cmds.objExists(search_root):
            cmds.warning("LP root not found.")
            return
        to_select = []
        lp_children = cmds.listRelatives(search_root, children=True, fullPath=True) or []
        for child in lp_children:
            short_name = child.split('|')[-1]
            is_subgroup = not cmds.listRelatives(child, shapes=True)
            if "_low" in short_name and not is_subgroup:
                to_select.append(child)
        if to_select:
            cmds.select(to_select, replace=True)
            cmds.inViewMessage(amg="Selected {} final _low meshes".format(len(to_select)), pos='midCenter', fade=True)
        else:
            cmds.warning("Combined _low meshes not found. Run 'Combine Fin' first.")
            cmds.select(clear=True)

    def select_all_book_low_meshes(self):
        if not self.active_root_id:
            cmds.warning("No active chapter to determine book.")
            return
        active_pair = next((p for p in self.root_pairs if p['id'] == self.active_root_id), None)
        if not active_pair:
            return
        active_book = active_pair.get('book')
        if not active_book:
            cmds.warning("Active chapter is not linked to any Book.")
            return
        book_pairs = [p for p in self.root_pairs if p.get('book') == active_book]
        if not book_pairs:
            return
        to_select = []
        for pair in book_pairs:
            base_name = pair.get('base', '')
            chapter_grp_path = "LP_Combine_BG|{}".format(base_name)
            _, lp_main, _ = self.core.resolve_main_nodes(pair)
            search_root = chapter_grp_path if cmds.objExists(chapter_grp_path) else lp_main
            if not search_root or not cmds.objExists(search_root):
                continue
            lp_children = cmds.listRelatives(search_root, children=True, fullPath=True) or []
            for child in lp_children:
                short_name = child.split('|')[-1]
                is_subgroup = not cmds.listRelatives(child, shapes=True)
                if "_low" in short_name and not is_subgroup:
                    to_select.append(child)
        if to_select:
            cmds.select(to_select, replace=True)
            cmds.inViewMessage(amg="Selected {} _low meshes in book '{}'".format(len(to_select), active_book), pos='midCenter', fade=True)
        else:
            cmds.warning("Combined _low meshes in book '{}' not found.".format(active_book))
            cmds.select(clear=True)


def cleanup_stale_bake_manager_ui():
    app = QtWidgets.QApplication.instance()
    if app:
        for widget in list(app.allWidgets()):
            try:
                object_name = widget.objectName()
            except RuntimeError:
                continue
            if object_name not in ("BakeManagerUI", bg_core.BakeConfig.WORKSPACE_NAME):
                continue
            try:
                if hasattr(widget, "shutdown_for_reload"):
                    widget.shutdown_for_reload()
            except Exception:
                pass
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
        for _ in range(3):
            try:
                app.processEvents(QtCore.QEventLoop.AllEvents, 50)
            except Exception:
                try:
                    app.processEvents()
                except Exception:
                    break

    if cmds.workspaceControl(bg_core.BakeConfig.WORKSPACE_NAME, exists=True):
        try:
            cmds.deleteUI(bg_core.BakeConfig.WORKSPACE_NAME, control=True)
        except RuntimeError:
            pass
    try:
        if cmds.workspaceControlState(bg_core.BakeConfig.WORKSPACE_NAME, exists=True):
            cmds.workspaceControlState(bg_core.BakeConfig.WORKSPACE_NAME, remove=True)
    except Exception:
        pass


def main():
    cleanup_stale_bake_manager_ui()
    if cmds.workspaceControl(bg_core.BakeConfig.WORKSPACE_NAME, exists=True):
        cmds.deleteUI(bg_core.BakeConfig.WORKSPACE_NAME, control=True)

    global bake_manager_ui
    bake_manager_ui = BakeManagerUI()

    bake_manager_ui.show(
        dockable=True, floating=False, area='right', allowedArea='right', retain=False
    )

    dock_targets = ['AttributeEditor', 'ToolSettings', 'ChannelBoxLayerEditor']
    for target in dock_targets:
        try:
            cmds.workspaceControl(bg_core.BakeConfig.WORKSPACE_NAME, e=True, tabToControl=[target, -1])
            break
        except RuntimeError:
            continue

    try:
        cmds.workspaceControl(bg_core.BakeConfig.WORKSPACE_NAME, e=True, restore=True)
    except:
        pass


if __name__ == "__main__":
    main()
