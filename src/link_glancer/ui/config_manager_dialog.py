from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from link_glancer.browser.detector import list_detected_browsers
from link_glancer.tasks.models import BrowserConfig, BrowserProfile
from link_glancer.ui.browser_config_advanced_dialog import BrowserConfigAdvancedDialog


class ConfigManagerDialog(QDialog):
    def __init__(
        self,
        *,
        browser_configs: list[BrowserConfig],
        browser_test_callback,
        browser_launch_callback,
        save_browser_config_callback,
        save_browser_profile_callback,
        delete_browser_config_callback,
        delete_browser_profile_callback,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.browser_configs = [deepcopy(config) for config in browser_configs]
        self._browser_test_callback = browser_test_callback
        self._browser_launch_callback = browser_launch_callback
        self._save_browser_config_callback = save_browser_config_callback
        self._save_browser_profile_callback = save_browser_profile_callback
        self._delete_browser_config_callback = delete_browser_config_callback
        self._delete_browser_profile_callback = delete_browser_profile_callback
        self._config_ids: list[str] = []

        self.setWindowTitle("浏览器配置")
        self.resize(920, 560)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("浏览器配置列表"))

        self._config_table = QTableWidget(0, 3)
        self._config_table.setHorizontalHeaderLabels(["浏览器名称", "程序位置", "状态"])
        self._config_table.verticalHeader().setVisible(False)
        self._config_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._config_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._config_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._config_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._config_table.setAlternatingRowColors(True)
        self._config_table.setStyleSheet(
            "QTableView { outline: 0; } "
            "QTableView::item:selected { "
            "background-color: #2563eb; "
            "color: #ffffff; "
            "border: 0px; "
            "}"
        )
        self._config_table.doubleClicked.connect(self._open_advanced_config)
        header = self._config_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self._config_table, stretch=1)

        actions = QHBoxLayout()
        add_button = QPushButton("新增")
        delete_button = QPushButton("删除")
        launch_button = QPushButton("启动浏览器")
        advanced_button = QPushButton("高级配置")
        for button in (add_button, delete_button, launch_button, advanced_button):
            button.setAutoDefault(False)
            button.setDefault(False)
        add_button.clicked.connect(self._add_browser_config)
        delete_button.clicked.connect(self._delete_selected_browser_config)
        launch_button.clicked.connect(self._launch_selected_browser)
        advanced_button.clicked.connect(self._open_advanced_config)
        self._delete_button = delete_button
        self._launch_button = launch_button
        self._advanced_button = advanced_button

        actions.addStretch(1)
        actions.addWidget(add_button)
        actions.addWidget(delete_button)
        actions.addWidget(launch_button)
        actions.addWidget(advanced_button)
        root.addLayout(actions)

        self._config_table.itemSelectionChanged.connect(self._update_actions)
        self._refresh_table()

    def _refresh_table(self, *, selected_id: str | None = None) -> None:
        current_id = selected_id or self._selected_config_id()
        self._config_ids = [config.config_id for config in self.browser_configs]
        self._config_table.setRowCount(len(self.browser_configs))
        for row, config in enumerate(self.browser_configs):
            values = [
                config.name,
                config.executable_path,
                _status_label(config.last_test_status),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._config_table.setItem(row, column, item)
        if current_id is None and self._config_ids:
            current_id = self._config_ids[0]
        self._restore_selection(current_id)
        self._update_actions()

    def _restore_selection(self, config_id: str | None) -> None:
        self._config_table.clearSelection()
        if config_id is None:
            return
        for row, existing_id in enumerate(self._config_ids):
            if existing_id == config_id:
                self._config_table.selectRow(row)
                return

    def _selected_config_id(self) -> str | None:
        selected_ranges = self._config_table.selectedRanges()
        if not selected_ranges:
            return None
        row = selected_ranges[0].topRow()
        if row < 0 or row >= len(self._config_ids):
            return None
        return self._config_ids[row]

    def _selected_config(self) -> BrowserConfig | None:
        selected_id = self._selected_config_id()
        if selected_id is None:
            return None
        for config in self.browser_configs:
            if config.config_id == selected_id:
                return config
        return None

    def _update_actions(self) -> None:
        has_selection = self._selected_config() is not None
        self._delete_button.setEnabled(has_selection)
        self._launch_button.setEnabled(has_selection)
        self._advanced_button.setEnabled(has_selection)

    def _add_browser_config(self) -> None:
        dialog = AddBrowserConfigDialog(parent=self)
        if dialog.exec() != int(QDialog.DialogCode.Accepted):
            return
        path = dialog.executable_path
        if not path:
            return

        browser_name = _infer_browser_label(path) or "Browser"
        config_id = _unique_browser_config_id(
            browser_name,
            {existing.config_id for existing in self.browser_configs},
        )
        config_name = _unique_browser_name(
            browser_name,
            {config.name for config in self.browser_configs},
        )
        config = BrowserConfig(
            config_id=config_id,
            name=config_name,
            profile_id=_profile_id_for_config_id(config_id),
            executable_path=path,
            test_url="about:blank",
            last_test_status="untested",
        )
        profile = BrowserProfile(profile_id=config.profile_id, name=config.name)

        ok, message = self._run_browser_test(config)
        if ok:
            config.last_test_status = "passed"
        else:
            config.last_test_status = "failed"
            result = QMessageBox.question(
                self,
                "浏览器测试失败",
                f"{message}\n\n是否仍然添加这个浏览器配置？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if result != QMessageBox.StandardButton.Yes:
                return
        self.browser_configs.append(config)
        self._save_browser_profile_callback(profile)
        self._save_browser_config_callback(config)
        self._refresh_table(selected_id=config.config_id)

    def _delete_selected_browser_config(self) -> None:
        config = self._selected_config()
        if config is None:
            return
        result = QMessageBox.question(
            self,
            "确认删除浏览器配置",
            f"将删除浏览器配置“{config.name}”，是否继续？",
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        self.browser_configs = [
            existing for existing in self.browser_configs if existing.config_id != config.config_id
        ]
        self._delete_browser_config_callback(config.config_id)
        self._delete_browser_profile_callback(config.profile_id)
        next_selected_id = self.browser_configs[0].config_id if self.browser_configs else None
        self._refresh_table(selected_id=next_selected_id)

    def _open_advanced_config(self) -> None:
        config = self._selected_config()
        if config is None:
            return
        dialog = BrowserConfigAdvancedDialog(
            browser_config=config,
            browser_test_callback=self._browser_test_callback,
            parent=self,
        )
        if dialog.exec() != int(QDialog.DialogCode.Accepted):
            return
        updated_config = dialog.browser_config
        for index, existing in enumerate(self.browser_configs):
            if existing.config_id == updated_config.config_id:
                self.browser_configs[index] = updated_config
                break
        self._save_browser_config_callback(updated_config)
        self._refresh_table(selected_id=updated_config.config_id)

    def _run_browser_test(self, config: BrowserConfig) -> tuple[bool, str]:
        testing_config = deepcopy(config)
        ok, message = self._browser_test_callback(testing_config)
        if ok:
            config.last_test_status = testing_config.last_test_status
            config.last_tested_at = testing_config.last_tested_at
        return ok, message

    def _launch_selected_browser(self) -> None:
        config = self._selected_config()
        if config is None:
            return
        ok, message = self._browser_launch_callback(deepcopy(config))
        if not ok:
            QMessageBox.warning(self, "启动浏览器失败", message)
            return


def _status_label(status: str) -> str:
    if status == "passed":
        return "可用"
    if status == "failed":
        return "不可用"
    return "未测试"


def _infer_browser_label(executable_path: str) -> str:
    lowered = executable_path.lower()
    if "chrome" in lowered:
        return "Chrome"
    if "edge" in lowered or "msedge" in lowered:
        return "Edge"
    if "thorium" in lowered:
        return "Thorium"
    path = Path(executable_path)
    if path.stem:
        return path.stem[:1].upper() + path.stem[1:]
    return ""


def _unique_browser_name(base_name: str, existing_names: set[str]) -> str:
    if base_name not in existing_names:
        return base_name
    for index in range(2, 1000):
        candidate = f"{base_name} {index}"
        if candidate not in existing_names:
            return candidate
    return f"{base_name} Browser"


def _unique_browser_config_id(base_name: str, existing_ids: set[str]) -> str:
    normalized = "".join(char.lower() if char.isalnum() else "-" for char in base_name).strip("-")
    normalized = normalized or "browser"
    if normalized not in existing_ids:
        return normalized
    for index in range(2, 1000):
        candidate = f"{normalized}-{index}"
        if candidate not in existing_ids:
            return candidate
    return f"{normalized}-browser"


def _profile_id_for_config_id(config_id: str) -> str:
    return config_id


class AddBrowserConfigDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.executable_path = ""

        self.setWindowTitle("新增浏览器配置")
        self.resize(680, 180)

        root = QVBoxLayout(self)
        form = QFormLayout()

        self._path_combo = QComboBox()
        self._path_combo.setEditable(True)
        self._path_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._load_presets()
        path_row = QHBoxLayout()
        path_row.addWidget(self._path_combo, stretch=1)
        browse_button = QPushButton("浏览...")
        browse_button.clicked.connect(self._browse_path)
        path_row.addWidget(browse_button)
        path_widget = QWidget()
        path_widget.setLayout(path_row)

        form.addRow("浏览器程序", path_widget)
        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        test_button = QPushButton("测试并添加")
        test_button.setAutoDefault(False)
        test_button.setDefault(False)
        buttons.addButton(test_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        test_button.clicked.connect(self._accept_with_path)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _load_presets(self) -> None:
        detected = list_detected_browsers()
        self._path_combo.clear()
        if not detected:
            self._path_combo.setEditText("")
            return
        for candidate in detected:
            path = str(candidate.executable_path)
            self._path_combo.addItem(path, path)

    def _browse_path(self) -> None:
        if sys.platform == "darwin":
            selected = QFileDialog.getExistingDirectory(
                self,
                "选择浏览器应用或目录",
                "",
            )
            if selected:
                self._path_combo.setEditText(selected)
            return

        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择浏览器程序",
            "",
            "Executable (*.exe);;All Files (*)",
        )
        if selected:
            self._path_combo.setEditText(selected)

    def _accept_with_path(self) -> None:
        path = self._path_combo.currentText().strip()
        if not path:
            QMessageBox.warning(self, "新增浏览器配置", "请选择浏览器程序路径。")
            return
        self.executable_path = path
        self.accept()
