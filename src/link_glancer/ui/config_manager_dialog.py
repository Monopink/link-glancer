from __future__ import annotations

import sys
from copy import deepcopy

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from link_glancer.browser.detector import browser_path_presets
from link_glancer.tasks.models import BrowserConfig


class ConfigManagerDialog(QDialog):
    def __init__(
        self,
        *,
        browser_configs: list[BrowserConfig],
        browser_test_callback,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.browser_configs = [deepcopy(config) for config in browser_configs]
        self.browser_config = (
            self.browser_configs[0] if self.browser_configs else _new_browser_config(1)
        )
        if not self.browser_configs:
            self.browser_configs.append(self.browser_config)
        self._browser_test_callback = browser_test_callback

        self.setWindowTitle("浏览器配置")
        self.resize(840, 520)

        root = QHBoxLayout(self)
        left = QVBoxLayout()
        left.addWidget(QLabel("浏览器配置列表"))
        self._config_list = QListWidget()
        self._config_list.currentRowChanged.connect(self._select_browser_config)
        left.addWidget(self._config_list, stretch=1)
        list_actions = QHBoxLayout()
        new_button = QPushButton("新增")
        delete_button = QPushButton("删除")
        new_button.clicked.connect(self._create_browser_config)
        delete_button.clicked.connect(self._delete_browser_config)
        list_actions.addWidget(new_button)
        list_actions.addWidget(delete_button)
        left.addLayout(list_actions)
        root.addLayout(left, stretch=1)

        right_panel = QWidget()
        right = QVBoxLayout(right_panel)
        form = QFormLayout()
        self._name_edit = QLineEdit()
        self._path_edit = QLineEdit()
        path_row = QHBoxLayout()
        path_row.addWidget(self._path_edit, stretch=1)
        browse_button = QPushButton("浏览...")
        browse_button.clicked.connect(self._browse_browser_path)
        path_row.addWidget(browse_button)
        path_widget = QWidget()
        path_widget.setLayout(path_row)

        self._test_url_edit = QLineEdit()
        self._launch_args_edit = QLineEdit()
        form.addRow("名称", self._name_edit)
        form.addRow("浏览器程序", path_widget)
        form.addRow("测试页面", self._test_url_edit)
        form.addRow("启动参数", self._launch_args_edit)
        right.addLayout(form)

        presets_row = QHBoxLayout()
        self._preset_combo = QComboBox()
        self._preset_paths: list[str] = []
        self._preset_combo.addItem("选择扫描到的浏览器...")
        for browser_name, paths in browser_path_presets().items():
            for path in paths:
                self._preset_combo.addItem(f"{browser_name}: {path}")
                self._preset_paths.append(path)
        preset_use_button = QPushButton("使用")
        preset_use_button.clicked.connect(self._apply_preset_path)
        presets_row.addWidget(self._preset_combo, stretch=1)
        presets_row.addWidget(preset_use_button)
        right.addWidget(QLabel("扫描结果"))
        right.addLayout(presets_row)

        test_row = QHBoxLayout()
        test_button = QPushButton("测试浏览器配置")
        test_button.clicked.connect(self._test_browser)
        self._test_status_label = QLabel()
        self._test_status_label.setWordWrap(True)
        test_row.addWidget(test_button)
        test_row.addWidget(self._test_status_label, stretch=1)
        right.addWidget(QLabel("测试结果"))
        right.addLayout(test_row)
        right.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        right.addWidget(buttons)
        root.addWidget(right_panel, stretch=2)

        self._refresh_list()
        self._config_list.setCurrentRow(0)

    def _refresh_list(self) -> None:
        current_id = self.browser_config.config_id
        self._config_list.clear()
        for config in self.browser_configs:
            item = QListWidgetItem(f"{config.name} ({config.config_id})")
            item.setData(1, config.config_id)
            self._config_list.addItem(item)
        for row in range(self._config_list.count()):
            if self._config_list.item(row).data(1) == current_id:
                self._config_list.setCurrentRow(row)
                return

    def _select_browser_config(self, row: int) -> None:
        if row < 0 or row >= len(self.browser_configs):
            return
        self._store_form(validate=False)
        self.browser_config = self.browser_configs[row]
        self._load_form(self.browser_config)

    def _load_form(self, config: BrowserConfig) -> None:
        self._name_edit.setText(config.name)
        self._path_edit.setText(config.executable_path)
        self._test_url_edit.setText(config.test_url)
        self._launch_args_edit.setText(" ".join(config.launch_args))
        self._test_status_label.setText(config.last_test_status)

    def _store_form(self, *, validate: bool) -> BrowserConfig:
        name = self._name_edit.text().strip()
        executable_path = self._path_edit.text().strip()
        if validate and not name:
            raise ValueError("浏览器配置名称不能为空。")
        if validate and not executable_path:
            raise ValueError("浏览器程序路径不能为空。")
        self.browser_config.name = name or self.browser_config.name
        self.browser_config.executable_path = executable_path
        self.browser_config.test_url = self._test_url_edit.text().strip() or "about:blank"
        launch_args = self._launch_args_edit.text().strip()
        self.browser_config.launch_args = launch_args.split() if launch_args else []
        return self.browser_config

    def _create_browser_config(self) -> None:
        self._store_form(validate=False)
        config = _new_browser_config(len(self.browser_configs) + 1)
        self.browser_configs.append(config)
        self.browser_config = config
        self._refresh_list()
        self._load_form(config)

    def _delete_browser_config(self) -> None:
        if len(self.browser_configs) <= 1:
            QMessageBox.warning(self, "浏览器配置", "至少保留一个浏览器配置。")
            return
        index = self._config_list.currentRow()
        if index < 0:
            return
        removed = self.browser_configs.pop(index)
        self.browser_config = self.browser_configs[max(0, index - 1)]
        self._refresh_list()
        self._load_form(self.browser_config)
        self._test_status_label.setText(f"已删除：{removed.name}")

    def _browse_browser_path(self) -> None:
        if sys.platform == "darwin":
            selected = QFileDialog.getExistingDirectory(
                self,
                "选择浏览器应用或目录",
                "",
            )
            if selected:
                self._path_edit.setText(selected)
            return

        selected, _ = QFileDialog.getOpenFileName(
            self, "选择浏览器程序", "", "Executable (*.exe);;All Files (*)"
        )
        if selected:
            self._path_edit.setText(selected)

    def _apply_preset_path(self) -> None:
        index = self._preset_combo.currentIndex()
        if index <= 0:
            return
        self._path_edit.setText(self._preset_paths[index - 1])

    def _test_browser(self) -> None:
        config = deepcopy(self._store_form(validate=True))
        ok, message = self._browser_test_callback(config)
        self._test_status_label.setText(message)
        if ok:
            self.browser_config.last_test_status = config.last_test_status
            self.browser_config.last_tested_at = config.last_tested_at
        else:
            QMessageBox.warning(self, "测试失败", message)

    def _save(self) -> None:
        try:
            self._store_form(validate=True)
            _validate_browser_configs(self.browser_configs)
        except ValueError as exc:
            QMessageBox.warning(self, "浏览器配置无效", str(exc))
            return
        self.accept()


def _new_browser_config(index: int) -> BrowserConfig:
    return BrowserConfig(
        config_id=f"browser-{index}",
        name=f"浏览器配置 {index}",
        test_url="about:blank",
        last_test_status="untested",
    )


def _validate_browser_configs(configs: list[BrowserConfig]) -> None:
    seen_ids: set[str] = set()
    for config in configs:
        if not config.config_id.strip():
            raise ValueError("浏览器配置 ID 不能为空。")
        if config.config_id in seen_ids:
            raise ValueError(f"浏览器配置 ID 重复：{config.config_id}")
        if not config.name.strip():
            raise ValueError("浏览器配置名称不能为空。")
        if not config.executable_path.strip():
            raise ValueError(f"浏览器配置 {config.name} 缺少浏览器程序路径。")
        seen_ids.add(config.config_id)
