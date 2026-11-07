"""视频模块 UI 守卫

三件事必须钉住：
1. 启动器里的「视频」卡真的可用了（不是再来一次「开发中」弹窗）；
2. 作品卡标题走共享的 20 字截断规则，且标题/meta 不越出卡片；
3. 播放入口只有一个（`play_list`），换源只有一处（`setSource(fromLocalFile)`）——
   音频模块当年四条入口各写一遍 `play_tracks+show_player`，重复打开就冲掉进度。
"""
import os
import re

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

from resource_manager import config  # noqa: E402
from ui import video_widgets  # noqa: E402
from ui.launcher import MODULES  # noqa: E402
from ui.text_utils import TITLE_MAX_CHARS, truncate_title  # noqa: E402
from ui.video_widgets import CARD_H, CARD_W, COVER_H, COVER_W, VideoCard  # noqa: E402

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(SRC, "ui")

LONG_TITLE = (
    "【简体中文版】某社团超长标题作品合集 第01-12话 完整版 "
    "1080P 高码率 附特典与字幕"
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _read(name):
    with open(os.path.join(UI_DIR, name), encoding="utf-8") as f:
        return f.read()


def _card(name=LONG_TITLE):
    return VideoCard({
        "id": 1,
        "name": name,
        "path": "",
        "author": "某社团",
        "thumbnail": "",
        "video_count": 12,
        "total_duration_ms": 3_600_000,
    })


# ==================== 启动器接线 ====================

def test_launcher_video_module_is_ready():
    video = next(m for m in MODULES if m["id"] == "video")
    assert video["status"] == "ready", "视频模块还是 coming_soon，点开只会弹「开发中」"
    assert "内置播放" in video["description"]


def test_launcher_opens_video_module():
    source = _read("launcher.py")
    assert 'mid == "video"' in source
    assert "VideoMainWindow" in source


def test_cli_accepts_video_module():
    with open(os.path.join(SRC, "main.py"), encoding="utf-8") as f:
        source = f.read()
    assert '"photo", "audio", "video"' in source


# ==================== 卡片 ====================

def test_card_uses_shared_truncate_rule():
    """与音频/写真同一份规则，不得各写一套数字"""
    assert TITLE_MAX_CHARS == 20
    assert video_widgets.truncate_title is truncate_title


def test_cover_is_16_9():
    assert abs(COVER_W / COVER_H - 16 / 9) < 0.02, f"封面 {COVER_W}x{COVER_H} 不是 16:9"


def test_card_fits_its_fixed_size(app):
    """标题只占两行、meta 和封面都在卡片内（坑：长标题把 meta 顶出卡片）"""
    card = _card()
    card.show()
    app.processEvents()

    title = card.findChild(QLabel, "videoTitle")
    cover = card.findChild(QLabel, "videoCover")
    meta = card.findChild(QLabel, "videoMeta")
    assert title is not None and cover is not None and meta is not None

    assert title.text() == LONG_TITLE[:TITLE_MAX_CHARS] + "…"
    assert title.toolTip() == LONG_TITLE, "全名不能丢"
    assert title.y() >= cover.geometry().bottom(), "标题压住了封面"
    assert meta.geometry().bottom() < CARD_H, "meta 行被挤出卡片"
    assert card.width() == CARD_W and card.height() == CARD_H

    card.close()


def test_duration_badge_stays_inside_cover(app):
    card = _card()
    card.show()
    app.processEvents()

    badge = card.findChild(QLabel, "videoBadge")
    assert badge is not None and badge.isVisible(), "有时长的作品应显示角标"
    assert badge.geometry().right() <= COVER_W
    assert badge.geometry().bottom() <= COVER_H
    # 角标是真胶囊：圆角取半高（Qt 超过半高会退化成直角）
    assert "border-radius: 9px" in badge.styleSheet()

    card.close()


def test_card_without_thumbnail_says_so(app):
    card = _card()
    card.load_thumbnail()
    assert card.cover.text() == "无封面"
    card.close()


# ==================== 唯一入口 / 换源唯一 ====================

def test_player_has_single_open_entry():
    source = _read("video_view.py")
    assert source.count(".play_list(") == 1, "播放入口必须只有一个（统一的 play_list）"


def test_player_switches_source_in_one_place():
    source = _read("video_player.py")
    assert source.count("setSource(QUrl.fromLocalFile(") == 1, "换源只允许在 _load 里发生一次"
    assert "should_keep_current_session(" in source, "同视频判重必须走共享规则"


def test_player_saves_and_restores_progress():
    source = _read("video_player.py")
    assert "db.get_progress(" in source
    assert "db.set_progress(" in source
    assert "db.clear_progress(" in source, "看完要清掉续播点，否则一进去就结束"
    assert "resume_position(" in source


def test_player_has_external_fallback():
    source = _read("video_player.py")
    assert "open_with_system" in source, "编码不支持时必须能给外部播放器兜底"


# ==================== QSS 选择器 ====================

def test_window_css_uses_object_selectors():
    """裸 background-color 会被级联给所有子控件（v1.8.4 色带、v1.8.11 白洞）"""
    for name, selector in (
        ("video_view.py", "#videoCentral"),
        ("video_player.py", "#videoPlayerRoot"),
        ("video_widgets.py", "#videoSettings"),
    ):
        source = _read(name)
        assert selector in source, f"{name} 缺少 {selector} 选择器"

    for name in ("video_view.py", "video_player.py", "video_widgets.py", "video_detail.py"):
        source = _read(name)
        assert "QFrame {" not in source, f"{name} 用了 QFrame{{}}，会级联给 QLabel（描边框/上底色）"


def test_video_theme_is_single_sourced():
    from ui import theme_base, video_theme

    for name in ("ACCENT", "ACCENT_HOVER", "ACCENT_TINT", "BG_MAIN", "CARD_BG", "TEXT_PRIMARY"):
        assert getattr(video_theme, name) == getattr(theme_base, name), f"{name} 出现漂移"


def test_detail_row_tall_enough_for_13px_font():
    source = _read("video_detail.py")
    row_height = int(re.search(r"ROW_HEIGHT = (\d+)", source).group(1))
    # 13px 字体的墨盒约 18px，行高必须留出上下留白（坑：item padding 挤掉行控件）
    assert row_height >= 36
    assert "#videoList::item" in source
    assert "padding" not in re.search(r"#videoList::item \{[^}]*\}", source).group(0)


# ==================== 冒烟：窗口能建起来 ====================

def test_main_window_builds(app, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VIDEO_DB_PATH", str(tmp_path / "video.db"))
    monkeypatch.setattr(config, "VIDEO_THUMB_DIR", str(tmp_path / "thumbs"))
    monkeypatch.setattr(config, "VIDEO_ROOT", str(tmp_path / "视频"))
    (tmp_path / "视频").mkdir()
    monkeypatch.setattr(config, "USER_CONFIG_PATH", str(tmp_path / "config.json"))

    from ui.video_view import VideoMainWindow

    window = VideoMainWindow()
    assert "视频" in window.windowTitle()
    assert window.search_box.placeholderText().startswith("搜索作品")
    # 根目录为空时给出可操作提示，而不是一片空白
    assert window.hint_label.isVisible() is False or "还没有作品" in window.hint_label.text()
    window.close()


def test_player_rejects_empty_playlist(app, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "VIDEO_DB_PATH", str(tmp_path / "video.db"))

    from ui.video_player import VideoPlayerWindow

    player = VideoPlayerWindow()
    assert player.play_list([], 0) is False
    assert player.current_path() == ""
    player.shutdown()
    player.close()
