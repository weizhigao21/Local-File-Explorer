"""
系统集成对话框：可选播放器注册 + 音频文件格式图标

只负责展示状态与调用 system_integration / format_icon_export；
注册表写入全部发生在 system_integration 里，且可一键还原。
"""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

import system_integration as si
from ui.format_icon_export import export_icons
from ui.theme_base import (
    ACCENT,
    BG_SIDEBAR,
    BORDER_COLOR,
    CARD_BG,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
)

_BTN = f"""
QPushButton {{
    background-color: #EDE2CE; color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_COLOR}; border-radius: 5px;
    padding: 7px 14px; font-size: 12px;
}}
QPushButton:hover {{ border-color: {ACCENT}; background-color: #F3EFE8; }}
QPushButton:disabled {{ color: {TEXT_DIM}; border-color: {BORDER_COLOR}; }}
"""

_PRIMARY_BTN = f"""
QPushButton {{
    background-color: {ACCENT}; color: #FFFFFF; border: none;
    border-radius: 5px; padding: 8px 18px; font-size: 12px;
}}
QPushButton:hover {{ background-color: #D4886A; }}
QPushButton:disabled {{ background-color: #B9DDE1; }}
"""

_SECTION = f"color: {TEXT_PRIMARY}; font-size: 13px; font-weight: bold;"
_HINT = f"color: {TEXT_MUTED}; font-size: 11px;"


class SystemIntegrationDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("系统集成 · 播放器与文件图标")
        self.setMinimumSize(620, 660)
        self.resize(620, 680)
        self.setStyleSheet(f"SystemIntegrationDialog {{ background-color: {BG_SIDEBAR}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(8)

        title = QLabel("接入 Windows：默认播放器 与 文件图标")
        title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 17px; font-weight: bold;")
        layout.addWidget(title)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # ---------------- 播放器 ----------------
        layout.addSpacing(4)
        layout.addWidget(self._section("播放器"))
        player_hint = QLabel(
            "注册后本程序会出现在右键「打开方式」和「设置 → 默认应用」。"
            "Windows 不允许程序静默抢占默认（UserChoice 带哈希保护），最后一步要你点一次。"
        )
        player_hint.setWordWrap(True)
        player_hint.setStyleSheet(_HINT)
        layout.addWidget(player_hint)

        player_row = QHBoxLayout()
        player_row.setSpacing(8)
        self.register_btn = QPushButton("注册为可选播放器")
        self.register_btn.setStyleSheet(_PRIMARY_BTN)
        self.register_btn.clicked.connect(self._register)
        player_row.addWidget(self.register_btn)

        self.settings_btn = QPushButton("打开默认应用设置")
        self.settings_btn.setStyleSheet(_BTN)
        self.settings_btn.clicked.connect(si.open_default_apps_settings)
        player_row.addWidget(self.settings_btn)

        player_row.addStretch()
        self.unregister_btn = QPushButton("取消注册")
        self.unregister_btn.setStyleSheet(_BTN)
        self.unregister_btn.clicked.connect(self._unregister)
        player_row.addWidget(self.unregister_btn)
        layout.addLayout(player_row)

        # ---------------- 格式图标 ----------------
        layout.addSpacing(8)
        layout.addWidget(self._section("文件图标"))
        icon_hint = QLabel(
            "让资源管理器里的 .mp3 / .flac 直接显示本程序的格式图标。\n"
            "Android 式的“换扩展名图标”在 Windows 上不成立：图标挂在 ProgID 上，"
            "而用哪个 ProgID 由该格式的默认程序决定。所以两种做法：\n"
            "  · 应用格式图标：把这些格式交给本程序打开，图标一定生效；\n"
            "  · 仅换图标：保留原来的播放器，只覆盖它的图标（它得独占这一种格式）。"
        )
        icon_hint.setWordWrap(True)
        icon_hint.setStyleSheet(_HINT)
        layout.addWidget(icon_hint)

        icon_row = QHBoxLayout()
        icon_row.setSpacing(8)
        self.apply_btn = QPushButton("应用格式图标")
        self.apply_btn.setStyleSheet(_PRIMARY_BTN)
        self.apply_btn.clicked.connect(lambda: self._apply_icons(True))
        icon_row.addWidget(self.apply_btn)

        self.icon_only_btn = QPushButton("仅换图标")
        self.icon_only_btn.setStyleSheet(_BTN)
        self.icon_only_btn.clicked.connect(lambda: self._apply_icons(False))
        icon_row.addWidget(self.icon_only_btn)

        icon_row.addStretch()
        self.restore_btn = QPushButton("还原图标")
        self.restore_btn.setStyleSheet(_BTN)
        self.restore_btn.clicked.connect(self._restore_icons)
        icon_row.addWidget(self.restore_btn)
        layout.addLayout(icon_row)

        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setMinimumHeight(150)
        self.report.setStyleSheet(
            f"QPlainTextEdit {{ background-color: {CARD_BG}; color: {TEXT_MUTED};"
            f" border: 1px solid {BORDER_COLOR}; border-radius: 6px;"
            f" padding: 8px; font-size: 11px; }}"
        )
        layout.addWidget(self.report, 1)

        refresh_row = QHBoxLayout()
        refresh_row.setSpacing(8)
        refresh_note = QLabel("图标没立刻变？先点「刷新图标」，不必重启资源管理器。")
        refresh_note.setStyleSheet(_HINT)
        refresh_row.addWidget(refresh_note)
        refresh_row.addStretch()
        self.cache_btn = QPushButton("刷新图标")
        self.cache_btn.setStyleSheet(_BTN)
        self.cache_btn.clicked.connect(self._clear_cache)
        refresh_row.addWidget(self.cache_btn)
        self.explorer_btn = QPushButton("重启资源管理器")
        self.explorer_btn.setStyleSheet(_BTN)
        self.explorer_btn.clicked.connect(self._restart_explorer)
        refresh_row.addWidget(self.explorer_btn)
        layout.addLayout(refresh_row)

        if not si.is_supported():
            self.status_label.setText("当前系统不是 Windows，无法注册系统默认应用与文件图标。")
            for btn in (self.register_btn, self.settings_btn, self.unregister_btn,
                        self.apply_btn, self.icon_only_btn, self.restore_btn,
                        self.cache_btn, self.explorer_btn):
                btn.setEnabled(False)
            self.report.setPlainText("系统集成仅支持 Windows。")
        else:
            self._refresh_status()
            self.report.setPlainText(
                f"图标目录：{si.format_icon_dir()}\n"
                f"可应用格式：{len(si.supported_extensions())} 种\n\n"
                "点「应用格式图标」生成 .ico 并写入注册表，随时可以用「还原图标」撤销。"
            )

    # ---------------- 视觉小工具 ----------------

    @staticmethod
    def _section(text):
        label = QLabel(text)
        label.setStyleSheet(_SECTION)
        return label

    def _refresh_status(self):
        parts = []
        if si.is_registered():
            parts.append("播放器：已注册")
        else:
            parts.append("播放器：未注册")
        parts.append("图标：已应用" if si.format_icons_installed() else "图标：未应用")
        self.status_label.setText("状态 · " + " ／ ".join(parts))
        self.status_label.setStyleSheet(
            f"color: {ACCENT}; font-size: 12px;" if si.is_registered()
            else f"color: {TEXT_MUTED}; font-size: 12px;"
        )
        self.register_btn.setEnabled(not si.is_registered())
        self.unregister_btn.setEnabled(si.is_registered())

    # ---------------- 动作 ----------------

    def _register(self):
        ok, message = si.register()
        QMessageBox.information(self, "注册完成" if ok else "注册失败", message)
        self._refresh_status()
        if ok:
            si.open_default_apps_settings()

    def _unregister(self):
        reply = QMessageBox.question(
            self, "取消注册",
            "确定要取消注册吗？\n已设为默认的关联需要你在系统设置里改回其他播放器。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        ok, message = si.unregister()
        QMessageBox.information(self, "已取消" if ok else "操作失败", message)
        self._refresh_status()

    def _apply_icons(self, take_over: bool):
        icon_dir = si.format_icon_dir()
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            export_icons(icon_dir)
            report = si.apply_format_icons(icon_dir, take_over=take_over)
        except Exception as exc:  # noqa: BLE001 - 注册表/磁盘异常都直接告诉用户
            QGuiApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "应用失败", f"写入图标或注册表时出错：\n{exc}")
            return
        QGuiApplication.restoreOverrideCursor()
        self.report.setPlainText(self._format_report(report))
        self._refresh_status()

    def _restore_icons(self):
        reply = QMessageBox.question(
            self, "还原图标",
            "确定要还原吗？\n会把被替换的图标与关联恢复成操作前的样子。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        result = si.remove_format_icons()
        lines = ["已还原图标与关联。"] if result.get("ok") else ["还原失败。"]
        lines.append(f"恢复 {result.get('restored', 0)} 项备份，删除 {result.get('removed', 0)} 项。")
        lines.append("如果图标没立刻变，点下面「刷新图标」。")
        self.report.setPlainText("\n".join(lines))
        self._refresh_status()

    def _clear_cache(self):
        if si.refresh_icon_cache():
            self.report.appendPlainText("\n已让资源管理器重读图标（没有结束任何进程）。")
        else:
            self.report.appendPlainText("\n刷新图标失败。")

    def _restart_explorer(self):
        reply = QMessageBox.question(
            self, "重启资源管理器",
            "只有「刷新图标」无效时才需要这一步。\n\n"
            "执行后桌面和任务栏会闪断一次，已打开的资源管理器窗口会全部关闭。\n"
            "确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        ok, message = si.restart_explorer()
        self.report.appendPlainText("\n" + message + ("，图标应已刷新。" if ok else "。"))

    # ---------------- 报告 ----------------

    @staticmethod
    def _format_report(report) -> str:
        if not report.get("ok", True):
            return f"操作失败：{report.get('error', '未知错误')}"

        lines = []
        applied = report.get("applied", [])
        pending = report.get("pending", [])
        skipped = report.get("skipped", [])
        taken = report.get("taken_over", [])

        lines.append(f"已生效 {len(applied)} 种格式：")
        if applied:
            lines.append("  " + "  ".join(ext.upper() for ext, _ in applied))
        lines.append("")

        if pending:
            lines.append(f"待你在系统设置里确认 {len(pending)} 种：")
            for ext, why in pending:
                lines.append(f"  · {ext.upper()} — {why}")
            lines.append("")

        if skipped:
            lines.append(f"跳过 {len(skipped)} 种：")
            for ext, why in skipped:
                lines.append(f"  · {ext.upper()} — {why}")
            lines.append("")

        if taken:
            lines.append(
                f"其中 {len(taken)} 种已把打开方式改成本程序；"
                "不想这样就在下面点「还原图标」。"
            )
        lines.append(f"图标文件：{report.get('icon_dir', '')}")
        lines.append("图标没立刻变，是系统缓存的问题，点「重启资源管理器」即可。")
        return "\n".join(lines)


__all__ = ["SystemIntegrationDialog"]
