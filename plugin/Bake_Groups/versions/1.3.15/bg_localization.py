from __future__ import print_function, division, absolute_import

import io
import json
import os

try:
    from PySide6 import QtWidgets, QtGui, QtCore
    QAction = QtGui.QAction
except ImportError:
    from PySide2 import QtWidgets, QtGui, QtCore
    QAction = QtWidgets.QAction

try:
    import maya.cmds as cmds
except ImportError:
    cmds = None


_CACHE = {}
_REVERSE_CACHE = None
_DEFAULT_LANG = "en"
_CURRENT_LANG = None
_LANGUAGE_OPTION_VAR = "BakeGroupsLanguage"


def _localization_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "localization")


def current_language():
    if _CURRENT_LANG:
        return _CURRENT_LANG
    if cmds is not None:
        try:
            if cmds.optionVar(exists=_LANGUAGE_OPTION_VAR):
                saved = cmds.optionVar(query=_LANGUAGE_OPTION_VAR)
                if saved:
                    return str(saved)
        except Exception:
            pass
    return os.environ.get("BG_LANGUAGE", _DEFAULT_LANG)


def set_language(lang):
    global _CURRENT_LANG
    _CURRENT_LANG = lang or _DEFAULT_LANG
    os.environ["BG_LANGUAGE"] = _CURRENT_LANG
    if cmds is not None:
        try:
            cmds.optionVar(stringValue=(_LANGUAGE_OPTION_VAR, _CURRENT_LANG))
        except Exception:
            pass
    load_language(_CURRENT_LANG)
    return _CURRENT_LANG


def clear_cache():
    global _REVERSE_CACHE
    _CACHE.clear()
    _REVERSE_CACHE = None


def available_languages():
    path = os.path.join(_localization_dir(), "languages.json")
    result = []
    if os.path.exists(path):
        try:
            with io.open(path, "r", encoding="utf-8-sig") as stream:
                data = json.load(stream)
            for item in data.get("languages", []):
                label = item.get("label") or item.get("name") or item.get("code")
                file_name = item.get("file") or "{}.json".format(item.get("code", ""))
                code = item.get("code") or os.path.splitext(file_name)[0]
                if label and code:
                    result.append({"label": label, "code": code, "file": file_name})
        except Exception as exc:
            print("Bake Master language list load failed for '{}': {}".format(path, exc))

    if not result:
        result.append({"label": "Russian", "code": "ru", "file": "ru.json"})

    return result


def load_language(lang=None):
    lang = lang or current_language()
    if lang in _CACHE:
        return _CACHE[lang]

    path = os.path.join(_localization_dir(), "{}.json".format(lang))
    data = {"texts": {}, "tooltips": {}}

    if os.path.exists(path):
        try:
            with io.open(path, "r", encoding="utf-8-sig") as stream:
                loaded = json.load(stream)
            if isinstance(loaded, dict):
                data["texts"] = loaded.get("texts", {}) or {}
                data["tooltips"] = loaded.get("tooltips", {}) or {}
        except Exception as exc:
            print("Bake Master localization load failed for '{}': {}".format(path, exc))

    _CACHE[lang] = data
    return data


def _reverse_text_map():
    global _REVERSE_CACHE
    if _REVERSE_CACHE is not None:
        return _REVERSE_CACHE

    reverse = {}
    for lang in available_languages():
        code = lang.get("code")
        if not code:
            continue
        data = load_language(code)
        for key, value in (data.get("texts", {}) or {}).items():
            if value and value not in reverse:
                reverse[value] = key
    _REVERSE_CACHE = reverse
    return reverse


def source_key_from_value(value):
    if not value:
        return value
    reverse = _reverse_text_map()
    return reverse.get(value, value)


def text(key, default=None):
    data = load_language()
    return data.get("texts", {}).get(key, default if default is not None else key)


def _fallback_label(key):
    key = str(key)
    if key.startswith("placeholder:"):
        return key.split("placeholder:", 1)[1]
    if key.startswith("combo:"):
        return key.split("combo:", 1)[1]
    return key


def tooltip(key, default=None):
    data = load_language()
    return data.get("tooltips", {}).get(key, default or "")


def _source_key(obj, current):
    existing = obj.property("bg_i18n_key") if hasattr(obj, "property") else None
    if existing:
        return existing
    current = source_key_from_value(current)
    if hasattr(obj, "setProperty"):
        obj.setProperty("bg_i18n_key", current)
    return current


def _set_common_help(obj, key):
    tip = tooltip(key)
    if hasattr(obj, "setToolTip"):
        obj.setToolTip(tip)
    if hasattr(obj, "setStatusTip"):
        obj.setStatusTip(tip)
    if hasattr(obj, "setProperty"):
        obj.setProperty("bg_status_tip", tip)


def localize_action(action):
    if not action:
        return action
    try:
        current = action.text()
    except Exception:
        return action
    if not current:
        return action
    key = _source_key(action, current)
    action.setText(text(key, _fallback_label(key)))
    _set_common_help(action, key)
    return action


def localize_widget(widget):
    if not widget:
        return widget

    if hasattr(widget, "windowTitle"):
        current = widget.windowTitle()
        if current:
            key = _source_key(widget, current)
            widget.setWindowTitle(text(key, _fallback_label(key)))
            _set_common_help(widget, key)

    if hasattr(widget, "title") and hasattr(widget, "setTitle"):
        try:
            current = widget.title()
            if current:
                key = _source_key(widget, current)
                widget.setTitle(text(key, _fallback_label(key)))
                _set_common_help(widget, key)
        except Exception:
            pass

    if isinstance(widget, (QtWidgets.QPushButton, QtWidgets.QToolButton, QtWidgets.QCheckBox, QtWidgets.QLabel)):
        current = widget.text()
        preserve_text = bool(widget.property("bg_preserve_text")) if hasattr(widget, "property") else False
        if preserve_text:
            existing = widget.property("bg_i18n_key") if hasattr(widget, "property") else None
            if existing:
                _set_common_help(widget, existing)
            return widget
        if current:
            key = _source_key(widget, current)
            widget.setText(text(key, _fallback_label(key)))
            _set_common_help(widget, key)
        else:
            existing = widget.property("bg_i18n_key") if hasattr(widget, "property") else None
            if existing:
                _set_common_help(widget, existing)

    if isinstance(widget, QtWidgets.QLineEdit):
        current = widget.placeholderText()
        if current:
            existing = widget.property("bg_i18n_key") if hasattr(widget, "property") else None
            if existing:
                key = existing
            else:
                current_key = source_key_from_value(current)
                key = current_key if str(current_key).startswith("placeholder:") else "placeholder:" + current_key
                if hasattr(widget, "setProperty"):
                    widget.setProperty("bg_i18n_key", key)
            widget.setPlaceholderText(text(key, _fallback_label(key)))
            _set_common_help(widget, key)

    if isinstance(widget, QtWidgets.QComboBox):
        for idx in range(widget.count()):
            current = widget.itemText(idx)
            if not current:
                continue
            current_key = source_key_from_value(current)
            key = current_key if str(current_key).startswith("combo:") else "combo:" + current_key
            widget.setItemText(idx, text(key, _fallback_label(key)))
            item_tip = tooltip(key)
            if item_tip:
                widget.setItemData(idx, item_tip, QtCore.Qt.ToolTipRole)
        _set_common_help(widget, widget.objectName() or widget.__class__.__name__)

    if isinstance(widget, QtWidgets.QMenu):
        for action in widget.actions():
            localize_action(action)

    for action in getattr(widget, "actions", lambda: [])():
        localize_action(action)

    return widget


def localize_widget_tree(root):
    if not root:
        return root
    localize_widget(root)
    for widget in root.findChildren(QtWidgets.QWidget):
        localize_widget(widget)
    for action in root.findChildren(QAction):
        localize_action(action)
    return root


def localize_menu(menu):
    return localize_widget_tree(menu)
