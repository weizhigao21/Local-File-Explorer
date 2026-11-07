"""「手机 / 平板访问」窗口：启停内置网页服务、显示访问地址

这是唯一需要 Qt 的服务器相关界面；真正的 HTTP 服务在 `web_server/`（纯 Python），
窗口只做三件事：开/关、把带令牌的地址给用户、把端口/局域网开关落到配置里。

关闭窗口**不会**停服——常见用法是「电脑放这儿，手机看片」，
窗口只是控制台，服务活到程序退出（`main.py` 退出时统一 stop）。
"""
from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from resource_manager import config
from ui.theme_base import (
    ACCENT,
    BG_MAIN,
    BG_SIDEBAR,
    CARD_BG,
    CARD_BORDER,
    CARD_BORDER_HOVER,
    TEXT_DIM,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from web_server import WebServerError, get_server, lan_addresses

RUNNING_COLOR = "#4F8A5B"


class ServerWindow(QMainWindow):
    def __init__(self, on_back_to_launcher=None):
        super().__init__()
        self._on_back_to_launcher = on_back_to_launcher
        self._server = get_server()
        self._addresses = []

        self.setWindowTitle(f"本地资源管理器 - 手机访问 v{config.APP_VERSION}")
        self.resize(880, 640)
        self.setMinimumSize(760, 580)
        self.setStyleSheet(f"QMainWindow {{ background-color: {BG_MAIN}; }}")

        central = QWidget()
        central.setObjectName("serverCentral")
        central.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCentralWidget(central)
        central.setStyleSheet(f"#serverCentral {{ background-color: {BG_MAIN}; }}")

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(24, 18, 24, 22)
        body_layout.setSpacing(14)

        body_layout.addWidget(self._build_status_card())
        body_layout.addWidget(self._build_settings_card())
        body_layout.addWidget(self._build_tips())
        body_layout.addStretch(0)

        root.addWidget(body, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._sync_state)
        self._timer.start()

        self._sync_state()

    # ---------- 顶栏 ----------

    def _build_header(self):
        header = QFrame()
        header.setObjectName("serverHeader")
        header.setFixedHeight(86)
        header.setStyleSheet(
            f"#serverHeader {{ background-color: {BG_SIDEBAR}; border: none; "
            f"border-bottom: 1px solid {CARD_BORDER}; }}"
        )

        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(12)

        back = QPushButton("← 返回")
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.setStyleSheet(self._btn_qss(compact=True))
        back.clicked.connect(self._back_to_launcher)
        layout.addWidget(back)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)

        title = QLabel("手机 / 平板访问")
        title.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 17px; font-weight: bold; "
            f"background: transparent; border: none;"
        )
        subtitle = QLabel("同一个 Wi-Fi 下用浏览器打开，无需安装 App")
        subtitle.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 12px; background: transparent; border: none;"
        )
        title_box.addWidget(title)
        title_box.addWidget(subtitle)

        layout.addLayout(title_box)
        layout.addStretch(1)

        self.state_pill = QLabel("未启动")
        self.state_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_pill.setFixedSize(78, 26)
        layout.addWidget(self.state_pill)

        return header

    # ---------- 状态卡 ----------

    def _build_status_card(self):
        card = QFrame()
        card.setObjectName("serverCard")
        card.setStyleSheet(
            f"#serverCard {{ background-color: {CARD_BG}; "
            f"border: 1px solid {CARD_BORDER}; border-radius: 12px; }}"
        )

        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(8)

        self.status_title = QLabel("服务未启动")
        self.status_title.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 15px; font-weight: bold; "
            f"background: transparent; border: none;"
        )
        row.addWidget(self.status_title)
        row.addStretch(1)

        self.start_btn = QPushButton("启动服务")
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.setFixedHeight(34)
        self.start_btn.setStyleSheet(self._primary_qss())
        self.start_btn.clicked.connect(self._toggle_server)
        row.addWidget(self.start_btn)

        layout.addLayout(row)

        self.url_label = QLabel("启动后这里会显示手机要访问的地址")
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url_label.setWordWrap(True)
        mono = QFont("Consolas", 12)
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.url_label.setFont(mono)
        self.url_label.setStyleSheet(
            f"color: {TEXT_SECONDARY}; font-size: 13px; background: #F4EEE3; "
            f"border: 1px solid {CARD_BORDER}; border-radius: 8px; padding: 10px 12px;"
        )
        layout.addWidget(self.url_label)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        self.copy_btn = QPushButton("复制链接")
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setFixedHeight(32)
        self.copy_btn.setStyleSheet(self._btn_qss())
        self.copy_btn.clicked.connect(self._copy_url)
        self.copy_btn.setEnabled(False)
        action_row.addWidget(self.copy_btn)

        self.open_btn = QPushButton("在本机浏览器预览")
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.setFixedHeight(32)
        self.open_btn.setStyleSheet(self._btn_qss())
        self.open_btn.clicked.connect(self._open_in_browser)
        self.open_btn.setEnabled(False)
        action_row.addWidget(self.open_btn)

        action_row.addStretch(1)

        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 12px; background: transparent; border: none;"
        )
        action_row.addWidget(self.hint_label)

        layout.addLayout(action_row)

        self.addr_label = QLabel("")
        self.addr_label.setWordWrap(True)
        self.addr_label.setStyleSheet(
            f"color: {TEXT_DIM}; font-size: 12px; background: transparent; border: none;"
        )
        layout.addWidget(self.addr_label)

        return card

    # ---------- 设置卡 ----------

    def _build_settings_card(self):
        card = QFrame()
        card.setObjectName("serverSettings")
        card.setStyleSheet(
            f"#serverSettings {{ background-color: {CARD_BG}; "
            f"border: 1px solid {CARD_BORDER}; border-radius: 12px; }}"
        )

        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(10)

        title = QLabel("设置")
        title.setStyleSheet(
            f"color: {TEXT_SECONDARY}; font-size: 13px; font-weight: bold; "
            f"background: transparent; border: none;"
        )
        layout.addWidget(title)

        row = QHBoxLayout()
        row.setSpacing(10)

        port_label = QLabel("端口")
        port_label.setStyleSheet(self._label_qss())
        row.addWidget(port_label)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setFixedWidth(96)
        self.port_spin.setValue(int(getattr(config, "WEB_PORT", 8735)))
        self.port_spin.setStyleSheet(self._spin_qss())
        self.port_spin.valueChanged.connect(self._on_settings_changed)
        row.addWidget(self.port_spin)

        self.lan_check = QCheckBox("允许局域网访问（关闭后只有本机能打开）")
        self.lan_check.setChecked(bool(getattr(config, "WEB_ALLOW_LAN", True)))
        self.lan_check.setStyleSheet(self._check_qss())
        self.lan_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lan_check.toggled.connect(self._on_settings_changed)
        row.addWidget(self.lan_check)

        row.addStretch(1)

        self.auto_check = QCheckBox("随程序启动")
        self.auto_check.setChecked(bool(getattr(config, "WEB_AUTO_START", False)))
        self.auto_check.setStyleSheet(self._check_qss())
        self.auto_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_check.toggled.connect(self._on_auto_start_changed)
        row.addWidget(self.auto_check)

        layout.addLayout(row)

        token_row = QHBoxLayout()
        token_row.setSpacing(10)

        token_label = QLabel("访问令牌")
        token_label.setStyleSheet(self._label_qss())
        token_row.addWidget(token_label)

        self.token_label = QLabel("—")
        self.token_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.token_label.setStyleSheet(
            f"color: {TEXT_SECONDARY}; font-size: 12px; background: transparent; border: none;"
        )
        token_row.addWidget(self.token_label)

        self.reset_btn = QPushButton("重置")
        self.reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_btn.setFixedHeight(26)
        self.reset_btn.setStyleSheet(self._btn_qss(compact=True))
        self.reset_btn.setToolTip("换了设备要收回旧链接时用：重置后旧链接立即失效")
        self.reset_btn.clicked.connect(self._reset_token)
        token_row.addWidget(self.reset_btn)

        token_row.addStretch(1)
        layout.addLayout(token_row)

        return card

    # ---------- 说明 ----------

    def _build_tips(self):
        card = QFrame()
        card.setObjectName("serverTips")
        card.setStyleSheet(
            f"#serverTips {{ background-color: #F8F3EA; "
            f"border: 1px solid {CARD_BORDER}; border-radius: 12px; }}"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(6)

        lines = [
            "手机和电脑要连同一个 Wi-Fi（或同一路由器下的有线/无线混连）。",
            "第一次启动如果手机打不开，检查 Windows 防火墙是否允许本程序访问「专用网络」。",
            "关闭这个窗口不会停止服务，手机可以继续用；退出程序才会停止。",
            "地址里的 t=… 是访问令牌，等于钥匙：发给别人的截图请打码。",
        ]
        for text in lines:
            label = QLabel(f"· {text}")
            label.setWordWrap(True)
            label.setStyleSheet(
                f"color: {TEXT_MUTED}; font-size: 12px; line-height: 1.6; "
                f"background: transparent; border: none;"
            )
            layout.addWidget(label)

        return card

    # ---------- 状态同步 ----------

    def _sync_state(self):
        running = self._server.running
        # 地址探测要走 getaddrinfo，2 秒一次没必要：状态没变时每 5 拍才刷新一次
        key = (running, self._server.port)
        self._tick = getattr(self, "_tick", 0) + 1
        refresh_address = key != getattr(self, "_state_key", None) or self._tick % 5 == 0
        self._state_key = key

        self.start_btn.setText("停止服务" if running else "启动服务")
        self.start_btn.setStyleSheet(
            self._btn_qss() if running else self._primary_qss()
        )
        self.copy_btn.setEnabled(running)
        self.open_btn.setEnabled(running)

        if running:
            self.state_pill.setText("运行中")
            self.state_pill.setStyleSheet(
                f"color: #FFFFFF; background-color: {RUNNING_COLOR}; border: none; "
                f"border-radius: 13px; font-size: 12px;"
            )
            port = self._server.port
            self.status_title.setText(f"服务已启动 · 端口 {port}")
            if refresh_address:
                self._addresses = lan_addresses()
                primary = self._addresses[0] if self._addresses else "127.0.0.1"
                self.url_label.setText(self._server.url(primary, with_token=True))
                if len(self._addresses) > 1:
                    others = "、".join(self._addresses[1:])
                    self.addr_label.setText(f"其他可用地址：{others}")
                elif not self._addresses:
                    self.addr_label.setText("没有检测到局域网地址：确认已连 Wi-Fi / 网线")
                else:
                    self.addr_label.setText("")
        else:
            self.state_pill.setText("未启动")
            self.state_pill.setStyleSheet(
                f"color: {TEXT_MUTED}; background-color: #EDE2CE; border: none; "
                f"border-radius: 13px; font-size: 12px;"
            )
            self.status_title.setText("服务未启动")
            self.url_label.setText("点击右侧「启动服务」，然后把这里出现的地址在手机上打开")
            self.addr_label.setText("")

        token = getattr(config, "WEB_TOKEN", "") or ""
        self.token_label.setText(token or "首次启动时自动生成")

    # ---------- 操作 ----------

    def _toggle_server(self):
        if self._server.running:
            self._server.stop()
            self.hint_label.setText("已停止，手机上的页面会断开")
            self._sync_state()
            return

        try:
            port = self._server.start(
                port=self.port_spin.value(),
                lan=self.lan_check.isChecked(),
            )
        except WebServerError as e:
            QMessageBox.warning(self, "启动失败", str(e))
            return
        except OSError as e:
            QMessageBox.warning(self, "启动失败", f"无法监听端口：{e}")
            return

        self.hint_label.setText(f"已启动（端口 {port}）")
        self._sync_state()

    def _copy_url(self):
        if not self._server.running:
            return
        url = self.url_label.text().strip()
        QApplication.clipboard().setText(url)
        self.hint_label.setText("链接已复制，发给手机或粘到浏览器")
        QTimer.singleShot(2600, lambda: self.hint_label.setText(""))

    def _open_in_browser(self):
        if not self._server.running:
            return
        QDesktopServices.openUrl(
            QUrl(self._server.url("127.0.0.1", with_token=True))
        )

    def _reset_token(self):
        answer = QMessageBox.question(
            self,
            "重置访问令牌",
            "重置后，所有已保存旧链接的手机都会失效，需要重新打开新链接。继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        config.set_user_config("WEB_TOKEN", "")
        config.save_user_config()
        was_running = self._server.running
        self._server.stop()
        if was_running:
            try:
                self._server.start(port=self.port_spin.value(), lan=self.lan_check.isChecked())
            except WebServerError as e:
                QMessageBox.warning(self, "重启失败", str(e))
        self.hint_label.setText("令牌已重置")
        self._sync_state()

    def _on_settings_changed(self):
        """改端口/局域网开关：落盘；如果服务在跑就重启让它立刻生效"""
        port = self.port_spin.value()
        lan = self.lan_check.isChecked()

        if port != getattr(config, "WEB_PORT", port):
            config.set_user_config("WEB_PORT", port)
        if lan != getattr(config, "WEB_ALLOW_LAN", lan):
            config.set_user_config("WEB_ALLOW_LAN", lan)
        config.save_user_config()

        if self._server.running:
            self._server.stop()
            try:
                self._server.start(port=port, lan=lan)
                self.hint_label.setText(f"设置已生效，端口 {port}")
            except WebServerError as e:
                QMessageBox.warning(self, "重启失败", f"{e}\n\n服务已停止。")

        self._sync_state()

    def _on_auto_start_changed(self, checked):
        config.set_user_config("WEB_AUTO_START", bool(checked))
        config.save_user_config()
        self.hint_label.setText(
            "下次启动程序时自动开服" if checked else "下次启动程序时不再自动开服"
        )

    def _back_to_launcher(self):
        self.close()

    def closeEvent(self, event):
        # 服务不清停：窗口只是控制台，关窗后手机照样能访问
        self._timer.stop()
        super().closeEvent(event)
        # 通知启动器回到前台：不接这一步，点 ✕ 关窗后启动器还藏着，用户以为程序没了。
        # 先取走回调再调用，避免 _back_to_launcher() 里 close() 造成二次触发。
        if self._on_back_to_launcher:
            callback = self._on_back_to_launcher
            self._on_back_to_launcher = None
            callback()

    # ---------- QSS ----------

    def _label_qss(self):
        return (
            f"color: {TEXT_SECONDARY}; font-size: 12px; "
            f"background: transparent; border: none;"
        )

    def _btn_qss(self, compact=False):
        padding = "4px 10px" if compact else "6px 14px"
        font = "12px" if compact else "13px"
        return f"""
            QPushButton {{
                background-color: {CARD_BG};
                color: {TEXT_PRIMARY};
                border: 1px solid {CARD_BORDER};
                border-radius: 8px;
                padding: {padding};
                font-size: {font};
            }}
            QPushButton:hover {{
                border: 1px solid {ACCENT};
                background-color: #FFFCF7;
            }}
            QPushButton:disabled {{
                color: {TEXT_DIM};
                border: 1px solid {CARD_BORDER};
                background-color: #F5EFE4;
            }}
        """

    def _primary_qss(self):
        return f"""
            QPushButton {{
                background-color: {ACCENT};
                color: #FFF8F2;
                border: 1px solid {ACCENT};
                border-radius: 8px;
                padding: 6px 16px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: #C96C4C;
                border: 1px solid #C96C4C;
            }}
        """

    def _spin_qss(self):
        return f"""
            QSpinBox {{
                background-color: #F4EEE3;
                color: {TEXT_PRIMARY};
                border: 1px solid {CARD_BORDER};
                border-radius: 7px;
                padding: 4px 8px;
                font-size: 13px;
            }}
            QSpinBox:hover {{ border: 1px solid {CARD_BORDER_HOVER}; }}
            QSpinBox:focus {{ border: 1px solid {ACCENT}; }}
        """

    def _check_qss(self):
        return f"""
            QCheckBox {{
                color: {TEXT_SECONDARY};
                font-size: 12.5px;
                background: transparent;
                spacing: 7px;
            }}
            QCheckBox::indicator {{
                width: 15px;
                height: 15px;
                border: 1px solid {CARD_BORDER_HOVER};
                border-radius: 4px;
                background-color: #FFFFFF;
            }}
            QCheckBox::indicator:checked {{
                background-color: {ACCENT};
                border: 1px solid {ACCENT};
            }}
        """
