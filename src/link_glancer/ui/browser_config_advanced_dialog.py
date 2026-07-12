from __future__ import annotations

from copy import deepcopy

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from link_glancer.tasks.models import BrowserConfig


class BrowserConfigAdvancedDialog(QDialog):
    def __init__(
        self,
        *,
        browser_config: BrowserConfig,
        browser_test_callback,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.browser_config = deepcopy(browser_config)
        self._browser_test_callback = browser_test_callback

        self.setWindowTitle(f"高级配置 - {self.browser_config.name}")
        self.resize(620, 320)

        root = QVBoxLayout(self)
        form = QFormLayout()

        self._path_label = QLabel(self.browser_config.executable_path)
        self._path_label.setWordWrap(True)
        self._test_url_edit = QLineEdit(self.browser_config.test_url)
        self._launch_args_edit = QLineEdit(" ".join(self.browser_config.launch_args))
        self._status_label = QLabel(_status_label(self.browser_config.last_test_status))

        form.addRow("浏览器名称", QLabel(self.browser_config.name))
        form.addRow("浏览器程序", self._path_label)
        form.addRow("测试页 URL", self._test_url_edit)
        form.addRow("启动参数", self._launch_args_edit)
        form.addRow("当前状态", self._status_label)
        root.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _save(self) -> None:
        test_url = self._test_url_edit.text().strip() or "about:blank"
        launch_args_text = self._launch_args_edit.text().strip()
        launch_args = launch_args_text.split() if launch_args_text else []

        needs_test = (
            test_url != self.browser_config.test_url
            or launch_args != self.browser_config.launch_args
        )
        if needs_test:
            result = QMessageBox.question(
                self,
                "确认重新测试",
                "测试页面或启动参数已修改，保存前将自动测试浏览器配置。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if result != QMessageBox.StandardButton.Yes:
                return

        updated_config = deepcopy(self.browser_config)
        updated_config.test_url = test_url
        updated_config.launch_args = launch_args

        if needs_test:
            testing_config = deepcopy(updated_config)
            ok, message = self._browser_test_callback(testing_config)
            if ok:
                updated_config.last_test_status = testing_config.last_test_status
                updated_config.last_tested_at = testing_config.last_tested_at
                self._status_label.setText(_status_label(updated_config.last_test_status))
            else:
                updated_config.last_test_status = "failed"
                self._status_label.setText(_status_label(updated_config.last_test_status))
                result = QMessageBox.question(
                    self,
                    "浏览器测试失败",
                    f"{message}\n\n是否仍然保存高级配置？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if result != QMessageBox.StandardButton.Yes:
                    return

        self.browser_config = updated_config
        self.accept()


def _status_label(status: str) -> str:
    if status == "passed":
        return "可用"
    if status == "failed":
        return "不可用"
    return "未测试"
