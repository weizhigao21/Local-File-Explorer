"""播放器封面自动发现 + 「音频设置 → 播放器」选项守卫。

背景：用户反馈"播放器里的封面没有自动使用"。原实现只认歌单 cover 与
`封面/cover.jpg` 两个来源，于是
  ① 文件夹里叫 folder.jpg / front.jpg 的封面看不见
  ② 单文件播放（无 playlist_id）时，已下载的 DLsite 封面拿不到
  ③ 音频文件内嵌的封面（ID3 APIC / FLAC PICTURE）完全没用
现在按 歌单 → 文件夹命名 → DLsite 缓存 → 内嵌封面 四级兜底。
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from audio_manager import database as adb  # noqa: E402
from resource_manager import config  # noqa: E402
from ui import player_window as pw  # noqa: E402
from ui.audio_widgets import AudioSettingsDialog  # noqa: E402
from ui.playback_controller import PlaybackController  # noqa: E402
from ui.player_window import PlayerWindow, dlsite_cover, folder_cover  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def temp_audio_db(tmp_path, monkeypatch):
    monkeypatch.setattr(adb, "DB_PATH", str(tmp_path / "audio.db"))
    adb.end_persistent()
    adb.init_db()
    yield adb
    adb.end_persistent()


@pytest.fixture
def window(qapp, temp_audio_db):
    playback = PlaybackController()
    win = PlayerWindow(playback=playback)
    win._animate = False
    yield win, playback
    win.shutdown()


def _image(color="#B85C3E", side=160):
    img = QImage(side, side, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    return img


def _write_cover(path, color="#B85C3E"):
    assert _image(color).save(str(path))
    return str(path)


# ==================== 文件夹命名封面 ====================

@pytest.mark.parametrize("name", ["封面.jpg", "cover.png", "folder.jpg",
                                  "front.jpeg", "albumart.png", "Cover.PNG"])
def test_folder_cover_recognizes_common_names(tmp_path, name):
    """下载站/自抓的封面命名五花八门，这些常见名都要认（大小写无关）"""
    expected = _write_cover(tmp_path / name)
    assert folder_cover(str(tmp_path)) == os.path.normpath(expected)


def test_folder_cover_ignores_random_images(tmp_path):
    """只有详情页截图/特典图时不认——随便挑一张当封面比"无封面"更糟"""
    _write_cover(tmp_path / "01.jpg")
    _write_cover(tmp_path / "特典.png")
    assert folder_cover(str(tmp_path)) is None


def test_folder_cover_missing_dir_is_none():
    assert folder_cover("") is None
    assert folder_cover(r"Z:\definitely\not\here") is None


# ==================== DLsite 封面兜底 ====================

def test_dlsite_cover_from_folder_name(tmp_path, monkeypatch):
    """文件夹名带 RJ 码 → data/dlsite_covers/<RJ>.jpg"""
    covers = tmp_path / "dlsite_covers"
    covers.mkdir()
    _write_cover(covers / "RJ01116988.jpg")
    monkeypatch.setattr(config, "DLSITE_COVER_DIR", str(covers))

    track = {"path": r"F:\hhh\asmr\双语\RJ01116988-某作品\01.mp3", "title": "01"}
    assert dlsite_cover(track) == str(covers / "RJ01116988.jpg")


def test_dlsite_cover_from_title(tmp_path, monkeypatch):
    covers = tmp_path / "covers"
    covers.mkdir()
    _write_cover(covers / "RJ01234567.jpg")
    monkeypatch.setattr(config, "DLSITE_COVER_DIR", str(covers))

    track = {"path": r"F:\asmr\某目录\01.mp3", "title": "双语-RJ01234567-某作品"}
    assert dlsite_cover(track) == str(covers / "RJ01234567.jpg")


def test_dlsite_cover_absent_returns_none(tmp_path, monkeypatch):
    covers = tmp_path / "covers"
    covers.mkdir()
    monkeypatch.setattr(config, "DLSITE_COVER_DIR", str(covers))
    assert dlsite_cover({"path": r"F:\asmr\无RJ码\01.mp3", "title": "x"}) is None


# ==================== 装载优先级 ====================

def test_load_cover_prefers_playlist_cover(window, tmp_path, monkeypatch):
    """歌单封面（作品封面）优先于文件夹里的同名图

    两张图用不同颜色，靠像素判断最终用的是哪一张——只断言"有图"无法区分优先级。
    """
    win, _pc = window
    sub = tmp_path / "a"
    sub.mkdir()
    playlist_cover = _write_cover(tmp_path / "作品封面.jpg", "#B85C3E")  # 赤陶（偏红）
    _write_cover(sub / "cover.jpg", "#42B4C2")                          # 青蓝（偏蓝）
    monkeypatch.setattr(pw.db, "get_playlist", lambda pid: {"cover": playlist_cover})

    win._load_cover({"path": str(sub / "01.mp3"), "playlist_id": 1})
    assert win._cover_from_file is True
    center = win.cover_label.pixmap().toImage().pixelColor(104, 104)
    assert center.red() > center.blue(), "应使用歌单封面（赤陶），而不是文件夹里的 cover.jpg"


def test_load_cover_falls_back_to_folder(window, tmp_path):
    win, _pc = window
    _write_cover(tmp_path / "cover.png")
    win._load_cover({"path": os.path.join(tmp_path, "01.mp3"), "title": "01"})
    assert win._cover_from_file is True
    assert not win.cover_label.pixmap().isNull()


def test_load_cover_uses_dlsite_cache_for_single_file(window, tmp_path, monkeypatch):
    """单文件播放没有 playlist_id，歌单封面拿不到 → 用已下载的 DLsite 封面

    这是原实现最明显的缺口：明明抓了封面，从播放队列/文件对话框播放时却不显示。
    """
    win, _pc = window
    covers = tmp_path / "dlsite_covers"
    covers.mkdir()
    _write_cover(covers / "RJ01116988.jpg")
    monkeypatch.setattr(config, "DLSITE_COVER_DIR", str(covers))

    win._load_cover({"path": r"F:\asmr\RJ01116988-某作品\01.mp3", "title": "01"})
    assert win._cover_from_file is True
    assert not win.cover_label.pixmap().isNull()


def test_load_cover_clears_when_nothing_found(window, tmp_path):
    win, _pc = window
    win._load_cover({"path": os.path.join(tmp_path, "01.mp3"), "title": "01"})
    assert win._cover_from_file is False
    assert win.cover_label.text() == "无封面"
    assert win.cover_label.pixmap().isNull()


# ==================== 内嵌封面 ====================

def test_embedded_cover_fills_when_no_file_cover(window):
    """文件夹里没封面时，播放中解析出的内嵌封面自动补上"""
    win, _pc = window
    win._load_cover({"path": "/no/such/dir/01.mp3", "title": "01"})
    assert win.cover_label.text() == "无封面"

    win._on_metadata({"title": "01", "artist": "", "album": "", "cover": _image()})
    assert win.cover_label.text() == ""
    assert not win.cover_label.pixmap().isNull()
    assert not win.mini_cover.pixmap().isNull()


def test_embedded_cover_never_overrides_file_cover(window, tmp_path):
    """已经有作品封面时，内嵌封面（往往是降采样小图）不许覆盖"""
    win, _pc = window
    _write_cover(tmp_path / "封面.jpg")
    win._load_cover({"path": os.path.join(tmp_path, "01.mp3"), "title": "01"})
    assert win._cover_from_file is True

    win._on_metadata({"title": "01", "cover": _image("#42B4C2")})
    assert win._cover_from_file is True          # 标记未被改动
    assert not win.cover_label.pixmap().isNull()


def test_embedded_cover_respects_switch(window, monkeypatch):
    """设置里关掉后不再用内嵌封面"""
    win, _pc = window
    monkeypatch.setattr(config, "PLAYER_EMBEDDED_COVER", False)
    win._load_cover({"path": "/no/such/dir/01.mp3", "title": "01"})
    win._on_metadata({"title": "01", "cover": _image()})
    assert win.cover_label.text() == "无封面"
    assert win.cover_label.pixmap().isNull()


def test_metadata_cover_empty_does_not_break(window):
    """后端没解析出封面时给 None，不能崩也不能清掉已有封面"""
    win, _pc = window
    win._load_cover({"path": "/no/such/dir/01.mp3", "title": "01"})
    win._on_metadata({"title": "01", "cover": None})
    assert win.cover_label.text() == "无封面"


# ==================== 播放器选项 ====================

def test_auto_queue_expands_on_open(window, monkeypatch):
    win, _pc = window
    monkeypatch.setattr(config, "PLAYER_AUTO_QUEUE", False)
    win.queue_btn.setChecked(False)
    win._apply_player_prefs()
    assert win.queue_btn.isChecked() is False

    monkeypatch.setattr(config, "PLAYER_AUTO_QUEUE", True)
    win._apply_player_prefs()
    assert win.queue_btn.isChecked() is True
    assert win.queue_panel.isVisible() or not win.isVisible()  # 未显示时 isVisible 恒 False


def test_auto_queue_off_keeps_user_expanded_queue(window, monkeypatch):
    """设置关掉时不能把用户手动展开的队列收回去"""
    win, _pc = window
    monkeypatch.setattr(config, "PLAYER_AUTO_QUEUE", False)
    win.queue_btn.setChecked(True)
    win._apply_player_prefs()
    assert win.queue_btn.isChecked() is True


def test_on_top_flag_follows_setting(window, monkeypatch):
    from PyQt6.QtCore import Qt

    win, _pc = window
    monkeypatch.setattr(config, "PLAYER_ON_TOP", True)
    win._apply_player_prefs()
    assert bool(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
    assert win.on_top_btn.isChecked()

    monkeypatch.setattr(config, "PLAYER_ON_TOP", False)
    win._apply_player_prefs()
    assert not (win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
    assert not win.on_top_btn.isChecked()


def test_settings_dialog_shows_player_options(qapp, monkeypatch):
    monkeypatch.setattr(config, "PLAYER_AUTO_QUEUE", True)
    monkeypatch.setattr(config, "PLAYER_ON_TOP", True)
    monkeypatch.setattr(config, "PLAYER_EMBEDDED_COVER", False)
    dialog = AudioSettingsDialog()
    assert dialog.auto_queue_check.isChecked() is True
    assert dialog.on_top_check.isChecked() is True
    assert dialog.embedded_cover_check.isChecked() is False
    dialog.deleteLater()


def test_player_options_are_persisted(tmp_path, monkeypatch):
    """三个键必须真的写进 config.json（否则重启就丢）"""
    monkeypatch.setattr(config, "USER_CONFIG_PATH", str(tmp_path / "config.json"))
    before = dict(config._user_config)
    try:
        config.set_user_config("PLAYER_AUTO_QUEUE", True)
        config.set_user_config("PLAYER_ON_TOP", True)
        config.set_user_config("PLAYER_EMBEDDED_COVER", False)
        config.save_user_config()
        data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert data["PLAYER_AUTO_QUEUE"] is True
        assert data["PLAYER_ON_TOP"] is True
        assert data["PLAYER_EMBEDDED_COVER"] is False
    finally:
        for key, value in before.items():
            config._user_config[key] = value
            setattr(config, key, value)
