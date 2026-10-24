"""日志查看 + 崩溃分析对话框。

两个来源：
* 正在运行的游戏会话（实时追加）；
* 实例 logs 目录里最近一次启动日志（只读）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QPlainTextEdit, QVBoxLayout, QWidget

from ...core.crash import CrashFinding, analyze_lines
from ...core.instances import Instance
from ...utils.versions import required_java_major
from ..context import AppContext, GameSession
from ..dialogs.common import BaseDialog, MessageDialog
from ..widgets import Card, GlyphWidget, label, make_button
from ..workers import TaskManager


class FindingCard(Card):
    def __init__(self, finding: CrashFinding, parent: QWidget | None = None) -> None:
        super().__init__(parent, margins=(12, 10, 12, 10), spacing=5, flat=True)
        head = QHBoxLayout()
        head.setSpacing(8)
        color = {"error": "#E06064", "warning": "#D9A02B"}.get(finding.severity, "#5B7CFA")
        head.addWidget(GlyphWidget("warning" if finding.severity != "info" else "info", 16, color=color, width=1.7))
        head.addWidget(label(finding.title, "body", wrap=True), 1)
        self.body().addLayout(head)
        if finding.detail:
            self.body().addWidget(label(finding.detail, "muted", wrap=True))
        if finding.suggestion:
            suggestion = label(f"建议：{finding.suggestion}", "body", wrap=True)
            self.body().addWidget(suggestion)


class LogDialog(BaseDialog):
    def __init__(
        self,
        parent: QWidget | None,
        *,
        ctx: AppContext,
        tasks: TaskManager | None = None,
        instance: Instance | None = None,
        session: GameSession | None = None,
    ) -> None:
        super().__init__(parent, title="游戏日志", width=780)
        self.ctx = ctx
        self.tasks = tasks
        self.instance = instance
        self.session = session
        self._lines: list[str] = []

        self._layout.addWidget(label(self._source_text(), "muted"))

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self._follow = QCheckBox("自动滚动")
        self._follow.setChecked(True)
        tools.addWidget(self._follow)
        tools.addStretch(1)
        self._analyze_button = make_button("崩溃分析", kind="search", role="primary", handler=self._analyze)
        tools.addWidget(self._analyze_button)
        self._open_button = make_button("打开日志目录", kind="folder", handler=self._open_folder)
        tools.addWidget(self._open_button)
        self._copy_button = make_button("复制全部", kind="copy", handler=self._copy)
        tools.addWidget(self._copy_button)
        self._layout.addLayout(tools)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setMinimumHeight(300)
        self._view.setMaximumBlockCount(6000)
        self._view.setStyleSheet("font-family: Consolas, 'Cascadia Code', monospace; font-size: 12px;")
        self._layout.addWidget(self._view, 1)

        self._analysis_title = label("崩溃分析结果", "section")
        self._analysis_box = QWidget()
        self._analysis_layout = QVBoxLayout(self._analysis_box)
        self._analysis_layout.setContentsMargins(0, 0, 0, 0)
        self._analysis_layout.setSpacing(6)
        self._analysis_title.setVisible(False)
        self._analysis_box.setVisible(False)
        self._layout.addWidget(self._analysis_title)
        self._layout.addWidget(self._analysis_box)

        self.add_footer_button(make_button("关闭", handler=self.reject))
        self.finish()
        self.resize(820, 720)

        self._load()
        if session is not None:
            session.line.connect(self._on_line)
            session.exited.connect(lambda *_: self._on_exit())

    # ------------------------------------------------------------------ #
    def _source_text(self) -> str:
        if self.session is not None:
            return f"实时日志 · 实例 {self.ctx.current_instance().name if self.ctx.current_instance() else ''} · {self.session.log_file.name}"
        if self.instance is not None:
            return f"实例 {self.instance.name} 的最近日志"
        return "启动器日志"

    def _load(self) -> None:
        if self.session is not None:
            self._lines = list(self.session.recent_lines(2000))
        elif self.instance is not None:
            path = self._latest_log()
            if path is not None:
                try:
                    self._lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError as exc:
                    self._lines = [f"# 读取日志失败：{exc}"]
        else:
            from ...utils import log as log_utils

            self._lines = log_utils.recent(1200)
        self._view.setPlainText("\n".join(self._lines))
        self._scroll_to_end()

    def _latest_log(self) -> Path | None:
        if self.instance is None:
            return None
        logs_dir = self.instance.logs_dir
        if not logs_dir.is_dir():
            return None
        files = sorted(logs_dir.glob("launch-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0] if files else None

    def _on_line(self, text: str) -> None:
        self._lines.append(text)
        self._view.appendPlainText(text)
        if self._follow.isChecked():
            self._scroll_to_end()

    def _scroll_to_end(self) -> None:
        bar = self._view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_exit(self) -> None:
        self._view.appendPlainText("# 游戏进程已退出")
        self._analyze()

    # ------------------------------------------------------------------ #
    def _analyze(self) -> None:
        from ...utils.versions import required_java_major

        exit_code = None
        required = 0
        if self.instance is not None:
            required = required_java_major(self.instance.mc_version)
        findings = analyze_lines(
            self._lines or self._view.toPlainText().splitlines(),
            exit_code=exit_code,
            required_java=required or None,
        )
        while self._analysis_layout.count():
            item = self._analysis_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not findings:
            self._analysis_layout.addWidget(
                label("没有匹配到常见的崩溃特征。可以到 crash-reports 目录查看完整报告。", "muted", wrap=True)
            )
        else:
            for finding in findings:
                self._analysis_layout.addWidget(FindingCard(finding))
        self._analysis_title.setVisible(True)
        self._analysis_box.setVisible(True)

    def _open_folder(self) -> None:
        if self.instance is not None:
            self.ctx.open_instance_folder(self.instance, "logs")
        else:
            self.ctx.open_path(self.ctx.paths.logs_dir)

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self._view.toPlainText())
        self.ctx.toast.emit("日志已复制到剪贴板")
