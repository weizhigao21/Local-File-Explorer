import argparse
import os
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox

import system_integration as si
from app_instance import SingleInstance
from audio_manager import database as audio_db
from resource_manager import config
from ui.hotkeys import GlobalHotkeys
from ui.launcher import LauncherWindow
from ui.playback_controller import get_playback_controller, reset_playback_controller
from ui.player_window import PlayerWindow, tracks_from_paths

# 说明：本程序不再挂系统托盘（右下角小图标区）常驻。
# 播放时是一个正常窗口，任务栏上有按钮，最小化后从任务栏点回来；
# 关掉最后一个窗口程序就退出，和普通播放器一样。


def _parse_args(argv):
    """解析本程序自己的命令行参数；Qt 自带参数忽略"""
    parser = argparse.ArgumentParser(
        prog="本地资源管理器",
        description="本地资源管理器 / 音频播放器",
    )
    parser.add_argument("--module", choices=("photo", "audio"), default=None,
                        help="启动后直接进入指定模块")
    parser.add_argument("--player", action="store_true",
                        help="只打开独立播放器窗口（完整尺寸）")
    parser.add_argument("--play", nargs="+", default=[], metavar="FILE",
                        help="播放指定的音频文件或文件夹（只开一个小播放窗）")
    parser.add_argument("--register", action="store_true",
                        help="注册为 Windows 可选音乐播放器后退出")
    parser.add_argument("--unregister", action="store_true",
                        help="取消 Windows 播放器注册后退出")
    parser.add_argument("files", nargs="*", help="要播放的音频文件（也可拖到 exe 上）")
    args, _unknown = parser.parse_known_args(argv)
    return args


def _input_files(args):
    """--play 与裸文件参数合并（文件关联传的是裸参数）"""
    return list(args.play) + list(args.files)


def _play_files(files, playback, player):
    """把文件/文件夹展开成队列并开始播放；返回是否有内容可播"""
    tracks = tracks_from_paths(files)
    if not tracks:
        return False
    playback.play_tracks(tracks, 0)
    player.show_player()
    return True


def _apply_args(args, launcher, player, playback):
    """按参数执行动作；返回是否有动作被执行"""
    if _play_files(_input_files(args), playback, player):
        return True
    if args.player:
        player.show_player()
        return True
    if args.module and launcher is not None:
        launcher.open_module(args.module)
        return True
    return False


def _install_helpers(app):
    """应用图标 + 全局字体"""
    icon_path = os.path.join(config.get_project_root(), "data", "ui.ico")
    if not os.path.exists(icon_path) and getattr(sys, "frozen", False):
        icon_path = os.path.join(sys._MEIPASS, "data", "ui.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # Windows 上优先雅黑 UI，回退到 Segoe UI
    font = QFont("Microsoft YaHei UI", 9)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)


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
    _install_helpers(app)

    # 注册/取消注册是独立动作：不需要单实例锁，也不启动主界面
    cli_args = _parse_args(sys.argv[1:])
    if cli_args.register or cli_args.unregister:
        if cli_args.unregister:
            ok, message = si.unregister()
        else:
            ok, message = si.register()
            if ok:
                si.open_default_apps_settings()
        QMessageBox.information(None, "系统集成", message)
        return 0

    # 先抢单实例锁：已有实例在跑时，本次启动只负责把文件/参数转发过去
    instance = SingleInstance()
    if not instance.start():
        return 0

    audio_db.init_db()
    playback = get_playback_controller()

    # 文件关联（--play）与 --player 走「独立播放」：
    # 只创建一个小播放窗口，不构造启动器、不初始化主界面，启动更快也更省内存。
    files = _input_files(cli_args)
    standalone = bool(files) or cli_args.player

    launcher = None
    if standalone:
        player = PlayerWindow(playback, standalone=True)
        if files and not cli_args.player:
            player.set_compact(True)  # 小窗口：只留封面信息 + 控制条
    else:
        player = PlayerWindow(playback)
        launcher = LauncherWindow()
        launcher.playerRequested.connect(player.show_player)

        def open_system_settings():
            from ui.system_dialog import SystemIntegrationDialog

            SystemIntegrationDialog(player).exec()

        launcher.systemRequested.connect(open_system_settings)

    # 关掉最后一个窗口就退出（不再挂托盘常驻进程）。
    app.setQuitOnLastWindowClosed(True)

    hotkeys = GlobalHotkeys(playback)
    if not hotkeys.register():
        print("[热键] 全局热键注册失败（可能被其他程序占用），窗口内快捷键仍可用")

    def handle_remote(argv):
        _apply_args(_parse_args(argv), launcher, player, playback)

    instance.messageReceived.connect(handle_remote)

    def cleanup():
        instance.shutdown()
        hotkeys.unregister()
        player.shutdown()
        reset_playback_controller()

    app.aboutToQuit.connect(cleanup)

    if not _apply_args(cli_args, launcher, player, playback):
        launcher.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
