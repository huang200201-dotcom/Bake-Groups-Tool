# -*- coding: utf-8 -*-
from __future__ import print_function, division, absolute_import

import maya.cmds as cmds
import os
import bg_localization as bg_l10n
import re

try:
    from PySide6 import QtWidgets, QtCore, QtGui
    QAction = QtGui.QAction
except ImportError:
    from PySide2 import QtWidgets, QtCore, QtGui
    QAction = QtWidgets.QAction

import bg_core

# ==========================================
# ICON UTILITY
# ==========================================
CURRENT_DIR = os.path.dirname(__file__)
ICONS_DIR = os.path.join(CURRENT_DIR, "icons")
ICON_SEARCH_DIRS = [
    ICONS_DIR,
    CURRENT_DIR,
    os.path.join(CURRENT_DIR, "Icon_dop"),
    os.path.join(os.path.dirname(CURRENT_DIR), "Icon_dop"),
    os.path.join(os.path.dirname(os.path.dirname(CURRENT_DIR)), "Icon_dop"),
]
_ICON_CACHE = {}

def get_icon(icon_name):
    """Returns QIcon by filename from the icon search paths."""
    cache_key = os.path.normcase(os.path.normpath(str(icon_name)))
    cached = _ICON_CACHE.get(cache_key)
    if cached is not None:
        return cached

    icon_path = None
    if os.path.isabs(icon_name) and os.path.exists(icon_name):
        icon_path = icon_name
    else:
        for directory in ICON_SEARCH_DIRS:
            candidate = os.path.join(directory, icon_name)
            if os.path.exists(candidate):
                icon_path = candidate
                break
    icon = QtGui.QIcon(icon_path) if icon_path else QtGui.QIcon()
    _ICON_CACHE[cache_key] = icon
    return icon


def configure_square_icon_button(button, icon_name, label_key=None, size=42, icon_size=30):
    label_key = label_key or button.text().strip()
    if label_key:
        button.setProperty("bg_i18n_key", label_key)
        tip = bg_l10n.tooltip(label_key)
        if tip:
            button.setToolTip(tip)
            button.setStatusTip(tip)
            button.setProperty("bg_status_tip", tip)
    button.setText("")
    button.setProperty("compact", True)
    button.setIcon(get_icon(icon_name))
    button.setIconSize(QtCore.QSize(icon_size, icon_size))
    button.setFixedSize(size, size)
    button.setMinimumSize(size, size)
    button.setMaximumSize(size, size)
    button.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
    return button


def configure_symbol_button(button, symbol, label_key, size=32):
    """Configure a compact, familiar-symbol action while localizing its help."""
    button.setText(symbol)
    button.setProperty("bg_i18n_key", label_key)
    button.setProperty("bg_preserve_text", True)
    button.setProperty("compact", True)
    button.setFixedSize(size, size)
    button.setMinimumSize(size, size)
    button.setMaximumSize(size, size)
    button.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
    tip = bg_l10n.tooltip(label_key)
    if tip:
        button.setToolTip(tip)
        button.setStatusTip(tip)
        button.setProperty("bg_status_tip", tip)
    return button


# ==========================================
# COLLAPSIBLE SECTION
# ==========================================
class CollapsibleSection(QtWidgets.QWidget):
    """Collapsible widget acting like a QGroupBox with a clickable header."""
    def __init__(self, title="", parent=None):
        super(CollapsibleSection, self).__init__(parent)
        self.setObjectName("CollapsibleSection")
        
        # Header button
        self.toggle_button = QtWidgets.QToolButton(text=title, checkable=True, checked=False)
        self.toggle_button.setObjectName("SectionToggle")
        self.toggle_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(QtCore.Qt.RightArrow)
        self.toggle_button.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.toggle_button.toggled.connect(self.set_expanded)
        
        # Content container
        self.content_area = QtWidgets.QWidget()
        self.content_area.setObjectName("SectionContent")
        self.content_layout = QtWidgets.QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(4, 4, 4, 4)
        
        # Header layout
        self.header_widget = QtWidgets.QWidget()
        self.header_widget.setObjectName("SectionHeader")
        self.header_layout = QtWidgets.QHBoxLayout(self.header_widget)
        self.header_layout.setContentsMargins(0, 0, 0, 0)
        self.header_layout.setSpacing(6)
        self.header_layout.addWidget(self.toggle_button)

        # Main Layout
        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self.header_widget)
        main_layout.addWidget(self.content_area)
        self.content_area.setVisible(False)
        
    def set_expanded(self, checked):
        self.toggle_button.setArrowType(QtCore.Qt.RightArrow if not checked else QtCore.Qt.DownArrow)
        self.content_area.setVisible(checked)

    def on_pressed(self):
        self.set_expanded(self.toggle_button.isChecked())
        
    def addWidget(self, widget):
        self.content_layout.addWidget(widget)
        
    def addLayout(self, layout):
        self.content_layout.addLayout(layout)

    def addHeaderWidget(self, widget, stretch=0):
        self.header_layout.addWidget(widget, stretch)


# ==========================================
# FINAL MESH LINE EDIT
# ==========================================
class FinalMeshLineEdit(QtWidgets.QLineEdit):
    def __init__(self, mesh_path, parent=None):
        super(FinalMeshLineEdit, self).__init__(parent)
        self.mesh_path = mesh_path
        self.setText(mesh_path.split('|')[-1])
        self.setStyleSheet("""
            QLineEdit {
                background: #2b2b2b; 
                border: 1px solid #444444; 
                border-radius: 4px;
                color: #dddddd;
                padding: 4px;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 13px;
            }
            QLineEdit:focus {
                border: 1px solid #5c85d6;
                background: #333333;
            }
        """)
        self.editingFinished.connect(self.on_rename)

    def mouseDoubleClickEvent(self, event):
        if cmds.objExists(self.mesh_path):
            cmds.select(self.mesh_path, replace=True)
        super(FinalMeshLineEdit, self).mouseDoubleClickEvent(event)

    def on_rename(self):
        new_name = self.text().strip().replace(".", "_")
        old_name = self.mesh_path.split('|')[-1]
        
        if new_name and new_name != old_name and cmds.objExists(self.mesh_path):
            try:
                new_node = cmds.rename(self.mesh_path, new_name)
                parts = self.mesh_path.split('|')
                parts[-1] = new_node
                self.mesh_path = '|'.join(parts)
                self.clearFocus()
            except Exception as e:
                cmds.warning("Rename failed: {}".format(e))
                self.setText(old_name)


# ==========================================
# SUBGROUP BUTTON
# ==========================================
class SubgroupButton(QtWidgets.QPushButton):
    doubleClicked = QtCore.Signal()
    rightClicked = QtCore.Signal()
    
    def __init__(self, *args, **kwargs):
        super(SubgroupButton, self).__init__(*args, **kwargs)
        self.setMinimumWidth(30)
        sp = self.sizePolicy()
        sp.setHorizontalPolicy(QtWidgets.QSizePolicy.Ignored)
        self.setSizePolicy(sp)
        
    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.RightButton:
            self.rightClicked.emit()
        super(SubgroupButton, self).mousePressEvent(event)
        
    def mouseDoubleClickEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.doubleClicked.emit()
        super(SubgroupButton, self).mouseDoubleClickEvent(event)


# ==========================================
# RESOLVE NAME DIALOG
# ==========================================
class ResolveNameDialog(QtWidgets.QDialog):
    def __init__(self, hp_base, lp_base, parent=None):
        super(ResolveNameDialog, self).__init__(parent)
        self.setWindowTitle("Name Mismatch")
        self.setStyleSheet("""
            QDialog {
                background-color: #242424;
                color: #e6e6e6;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 13px;
            }
            QPushButton { background-color: #333333; border: 1px solid #444; border-radius: 4px; padding: 6px; color: #ddd; }
            QPushButton:hover { background-color: #444; }
            QLineEdit { background: #2b2b2b; border: 1px solid #444; border-radius: 4px; color: #ddd; padding: 4px; }
        """)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel("Base names differ.\nChoose name for pair:"))
        self.btn_group = QtWidgets.QButtonGroup(self)
        self.rb_hp = QtWidgets.QRadioButton("HP:  {}".format(hp_base))
        self.rb_hp.setChecked(True)
        self.btn_group.addButton(self.rb_hp)
        layout.addWidget(self.rb_hp)
        self.rb_lp = QtWidgets.QRadioButton("LP:  {}".format(lp_base))
        self.btn_group.addButton(self.rb_lp)
        layout.addWidget(self.rb_lp)
        custom_layout = QtWidgets.QHBoxLayout()
        self.rb_custom = QtWidgets.QRadioButton("Custom:")
        self.btn_group.addButton(self.rb_custom)
        self.input_custom = QtWidgets.QLineEdit()
        self.input_custom.textChanged.connect(lambda: self.rb_custom.setChecked(True))
        custom_layout.addWidget(self.rb_custom)
        custom_layout.addWidget(self.input_custom)
        layout.addLayout(custom_layout)
        btn_layout = QtWidgets.QHBoxLayout()
        btn_ok = QtWidgets.QPushButton("Apply")
        btn_ok.clicked.connect(self.accept)
        btn_layout.addWidget(btn_ok)
        btn_cancel = QtWidgets.QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout)
        bg_l10n.localize_widget_tree(self)

    def get_chosen_name(self):
        if self.rb_hp.isChecked():
            return self.rb_hp.text().replace("HP:  ", "").strip()
        if self.rb_lp.isChecked():
            return self.rb_lp.text().replace("LP:  ", "").strip()
        return self.input_custom.text().strip() if self.rb_custom.isChecked() else "BakeGroup"
