import os
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import QApplication

from resource_manager import config
from ui.launcher import LauncherWindow


def main():
    # 应用 UI 缩放（必须在 QApplication 创建之前设置）
    scale = getattr(config, "UI_SCALE", 100) / 100.0
    os.environ["QT_SCALE_FACTOR"] = str(scale)

    # 解决高 DPI 缩放问题
    if hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 设置应用图标（优先 exe 同级 data/，回退到 PyInstaller 内置资源）
    icon_path = os.path.join(config.get_project_root(), "data", "ui.ico")
    if not os.path.exists(icon_path) and getattr(sys, "frozen", False):
        icon_path = os.path.join(sys._MEIPASS, "data", "ui.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # 全局字体：Windows 上优先雅黑 UI，回退到 Segoe UI
    font = QFont("Microsoft YaHei UI", 9)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)

    window = LauncherWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
