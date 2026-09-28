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

_WORKFLOW_TEXTS = {
    'en': {
        'Automatic Bake Groups': 'Automatic Bake Groups',
        'Automatic Bake Groups: grouping high-poly meshes, then matching low-poly meshes.': 'Automatic Bake Groups: grouping high-poly meshes, then matching low-poly meshes.',
        'Automatic Bake Groups: matching low-poly meshes.': 'Automatic Bake Groups: matching low-poly meshes.',
        'Automatic Bake Groups complete.': 'Automatic Bake Groups complete.',
        'Wait for automatic grouping to finish before switching asset tasks.': 'Wait for automatic grouping to finish before switching asset tasks.',
        'No unlocked HP groups are available for low-poly matching.': 'No unlocked HP groups are available for low-poly matching.',
        'Run the check now, or continue automatic grouping without checking?': 'Run the check now, or continue automatic grouping without checking?',
        'No HP subgroups found. Run Automatic Bake Groups first.': 'No HP subgroups found. Run Automatic Bake Groups first.',
        'Combine Fin: no LP subgroups were combined. Run Automatic Bake Groups first or check LP subgroup names.': 'Combine Fin: no LP subgroups were combined. Run Automatic Bake Groups first or check LP subgroup names.',
        'Run Automatic Bake Groups or rename/create matching LP subgroups before Combine Fin.': 'Run Automatic Bake Groups or rename/create matching LP subgroups before Combine Fin.',
        'No debug log to save yet. Run Automatic Bake Groups first.': 'No debug log to save yet. Run Automatic Bake Groups first.',
    },
    'zh-CN': {
        'Automatic Bake Groups': '自动划分烘焙组',
        'Automatic Bake Groups: grouping high-poly meshes, then matching low-poly meshes.': '自动划分烘焙组：先划分高模，再匹配低模。',
        'Automatic Bake Groups: matching low-poly meshes.': '自动划分烘焙组：正在匹配低模。',
        'Automatic Bake Groups complete.': '自动划分烘焙组完成。',
        'Wait for automatic grouping to finish before switching asset tasks.': '请等待自动划分完成后再切换资产任务。',
        'No unlocked HP groups are available for low-poly matching.': '没有可供低模匹配的未锁定高模组。',
        'Run the check now, or continue automatic grouping without checking?': '立即检查，还是跳过检查继续自动划分烘焙组？',
        'No HP subgroups found. Run Automatic Bake Groups first.': '未找到高模子组，请先运行“自动划分烘焙组”。',
        'Combine Fin: no LP subgroups were combined. Run Automatic Bake Groups first or check LP subgroup names.': '最终生成未合并任何低模子组。请先运行“自动划分烘焙组”，或检查低模子组名称。',
        'Run Automatic Bake Groups or rename/create matching LP subgroups before Combine Fin.': '最终生成前，请运行“自动划分烘焙组”，或重命名／创建相匹配的低模子组。',
        'No debug log to save yet. Run Automatic Bake Groups first.': '暂无可保存的调试日志。请先运行“自动划分烘焙组”。',
    },
    'ja': {'Automatic Bake Groups': 'ベイクグループの自動作成'},
    'ru': {'Automatic Bake Groups': 'Автоматические группы запекания'},
}
_WORKFLOW_TOOLTIP = {
    'en': 'Group high-poly meshes, then automatically match low-poly meshes in the same asset task. Each completed stage can be undone separately.',
    'zh-CN': '一次点击，先划分高模，再自动匹配同一资产任务的低模。已完成的两个阶段可分别撤销。',
    'ja': '高ポリゴンを分類し、同じアセットの低ポリゴンを自動照合します。完了した各段階は個別に取り消せます。',
    'ru': 'Сгруппировать высокополигональные модели, затем сопоставить низкополигональные в той же задаче. Каждый этап отменяется отдельно.',
}

# Update controls ship with the runtime so staged Python updates can introduce
# their labels even when a user's optional translation files are older.
_UPDATE_TEXTS = {
    'en': {
        'Updates': 'Updates',
        'Automatic Updates': 'Automatic Updates',
        'Manual Update': 'Manual Update',
        'Visit Repository Website': 'Visit Repository Website',
        'Checking for updates...': 'Checking for updates...',
        'Downloading update {version}...': 'Downloading update {version}...',
        'Bake Master is up to date.': 'Bake Master is up to date.',
        'Update failed: {error}': 'Update failed: {error}',
        'An update is already in progress.': 'An update is already in progress.',
        'Automatic update cancelled.': 'Automatic update cancelled.',
        'Update {version} is ready; waiting until Bake Master is idle.': 'Update {version} is ready; waiting until Bake Master is idle.',
        'Update is ready. Restart Maya, then open Bake Master to finish installing.': 'Update is ready. Restart Maya, then open Bake Master to finish installing.',
    },
    'zh-CN': {
        'Updates': '更新',
        'Automatic Updates': '自动更新',
        'Manual Update': '手动更新',
        'Visit Repository Website': '访问仓库网站',
        'Checking for updates...': '正在检查更新…',
        'Downloading update {version}...': '正在下载更新 {version}…',
        'Bake Master is up to date.': 'Bake Master 已是最新版本。',
        'Update failed: {error}': '更新失败：{error}',
        'An update is already in progress.': '更新正在进行中。',
        'Automatic update cancelled.': '已取消自动更新。',
        'Update {version} is ready; waiting until Bake Master is idle.': '更新 {version} 已准备好，等待 Bake Master 空闲后应用。',
        'Update is ready. Restart Maya, then open Bake Master to finish installing.': '更新已准备好。请重启 Maya，然后打开 Bake Master 完成安装。',
    },
    'ja': {
        'Updates': '更新',
        'Automatic Updates': '自動更新',
        'Manual Update': '手動更新',
        'Visit Repository Website': 'リポジトリを開く',
        'Checking for updates...': '更新を確認しています…',
        'Downloading update {version}...': '更新 {version} をダウンロードしています…',
        'Bake Master is up to date.': 'Bake Master は最新です。',
        'Update failed: {error}': '更新に失敗しました: {error}',
        'An update is already in progress.': '更新は既に進行中です。',
        'Automatic update cancelled.': '自動更新をキャンセルしました。',
        'Update {version} is ready; waiting until Bake Master is idle.': '更新 {version} の準備が完了しました。処理が終了するまで待機します。',
        'Update is ready. Restart Maya, then open Bake Master to finish installing.': '更新の準備が完了しました。Maya を再起動し、Bake Master を開いてインストールを完了してください。',
    },
    'ru': {
        'Updates': 'Обновления',
        'Automatic Updates': 'Автообновление',
        'Manual Update': 'Обновить вручную',
        'Visit Repository Website': 'Открыть репозиторий',
        'Checking for updates...': 'Проверка обновлений…',
        'Downloading update {version}...': 'Загрузка обновления {version}…',
        'Bake Master is up to date.': 'Установлена последняя версия Bake Master.',
        'Update failed: {error}': 'Ошибка обновления: {error}',
        'An update is already in progress.': 'Обновление уже выполняется.',
        'Automatic update cancelled.': 'Автообновление отменено.',
        'Update {version} is ready; waiting until Bake Master is idle.': 'Обновление {version} готово; ожидание завершения работы Bake Master.',
        'Update is ready. Restart Maya, then open Bake Master to finish installing.': 'Обновление готово. Перезапустите Maya и откройте Bake Master для завершения установки.',
    },
}
_UPDATE_TOOLTIPS = {
    'en': ('Check, download and apply updates automatically when Bake Master is idle.',
           'Check for a new version now, download it and apply it when idle.',
           'Open the Bake Master GitHub repository.'),
    'zh-CN': ('自动检查、下载更新，并在 Bake Master 空闲时应用。',
              '立即检查新版本、下载更新，并在空闲时应用。', '打开 Bake Master 的 GitHub 仓库首页。'),
    'ja': ('更新を自動確認・ダウンロードし、処理が終了したら適用します。',
           '新しいバージョンを確認・ダウンロードし、処理が終了したら適用します。', 'Bake Master の GitHub リポジトリを開きます。'),
    'ru': ('Автоматически проверять и загружать обновления; применять после завершения работы.',
           'Проверить и загрузить новую версию; применить после завершения работы.', 'Открыть репозиторий Bake Master на GitHub.'),
}


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

    data['texts'].update(_UPDATE_TEXTS.get(lang, _UPDATE_TEXTS['en']))
    data['texts'].update(_WORKFLOW_TEXTS['en'])
    data['texts'].update(_WORKFLOW_TEXTS.get(lang, {}))
    data['tooltips']['Automatic Bake Groups'] = _WORKFLOW_TOOLTIP.get(lang, _WORKFLOW_TOOLTIP['en'])
    tips = _UPDATE_TOOLTIPS.get(lang, _UPDATE_TOOLTIPS['en'])
    data['tooltips'].update(dict(zip(
        ('Automatic Updates', 'Manual Update', 'Visit Repository Website'), tips)))
    data['tooltips']['Updates'] = data['texts']['Updates']
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
