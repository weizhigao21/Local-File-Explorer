"""卡片标题：截到 20 字 + 省略号，不得溢出卡片或压住封面。

两个模块各有一张作品卡，宽度都容纳不下完整 DLsite 标题：
- 音频 PlaylistCard：180x240，名区 160px
- 写真 WorkCard：220x280，名区 204px

「双语-RJ01588371-【W乳首責め×W中出し×W爆乳バニー】～VIP会員限定サービス～
W爆乳バニーの金玉空っぽ裏カジノ【CV秋山はるる】【CV大山チロル】」有 80 字，
不截断会换 6 行：音频卡把「12 首」顶出框架，写真卡把「某社团 · 42 张」挤出去、
标题还压住封面（用户截图反馈 2026-10-22）。
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

from ui import audio_widgets, grid_view  # noqa: E402
from ui.audio_widgets import PlaylistCard  # noqa: E402
from ui.grid_view import WorkCard  # noqa: E402
from ui.text_utils import TITLE_MAX_CHARS, truncate_title  # noqa: E402

LONG_JP_TITLE = (
    "双语-RJ01588371-【W乳首責め×W中出し×W爆乳バニー】"
    "～VIP会員限定サービス～W爆乳バニーの金玉空っぽ裏カジノ"
    "【CV秋山はるる】【CV大山チロル】"
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _audio_card(name):
    return PlaylistCard({"id": 1, "name": name, "path": "", "track_count": 12})


def _audio_name_label(card):
    label = card.findChild(QLabel, "cardName")
    assert label is not None, "音频卡片里找不到名字标签"
    return label


def test_both_modules_share_the_same_limit():
    """音频与写真共用 ui/text_utils 的规则，不能各写一套数字/函数。"""
    assert TITLE_MAX_CHARS == 20
    assert audio_widgets.truncate_title is truncate_title
    assert grid_view.truncate_title is truncate_title


def test_long_title_is_truncated_with_ellipsis():
    assert len(LONG_JP_TITLE) > TITLE_MAX_CHARS
    short = truncate_title(LONG_JP_TITLE)
    assert short == LONG_JP_TITLE[:TITLE_MAX_CHARS] + "…"
    assert len(short) == TITLE_MAX_CHARS + 1


def test_short_title_is_untouched():
    assert truncate_title("RJ01588371") == "RJ01588371"
    assert truncate_title("") == ""
    assert truncate_title(None) == ""


def test_audio_card_shows_truncated_title_but_keeps_tooltip(app):
    card = _audio_card(LONG_JP_TITLE)
    label = _audio_name_label(card)
    assert label.text() == LONG_JP_TITLE[:TITLE_MAX_CHARS] + "…"
    assert label.toolTip() == LONG_JP_TITLE      # 全名不能丢
    card.close()


def test_audio_card_title_stays_inside_frame(app):
    """核心判据：名字最多两行，且名字与「N 首」都在卡片内。"""
    card = _audio_card(LONG_JP_TITLE)
    card.show()
    app.processEvents()

    label = _audio_name_label(card)
    metrics = label.fontMetrics()
    assert label.height() <= metrics.lineSpacing() * 2 + 4, (
        f"名字占了 {label.height()}px，超过两行（{metrics.lineSpacing()}px/行）")
    assert label.geometry().bottom() < card.height(), (
        f"名字标签底边 {label.geometry().bottom()} 越出卡片高度 {card.height()}")

    sub = [lbl for lbl in card.findChildren(QLabel)
           if lbl is not label and lbl.text().endswith("首")]
    assert sub, "找不到「N 首」子标题"
    assert sub[0].isVisible(), "子标题被名字挤出可视区"
    assert sub[0].geometry().bottom() < card.height(), (
        f"子标题底边 {sub[0].geometry().bottom()} 越出卡片高度 {card.height()}")
    card.close()


def test_photo_card_title_does_not_cover_cover_or_push_meta(app):
    """写真卡：标题不得压住封面，meta 行必须完整留在卡片内。"""
    card = WorkCard({"name": LONG_JP_TITLE, "author": "某社团",
                     "image_count": 42, "thumbnail": ""})
    card.show()
    app.processEvents()

    title = card.findChild(QLabel, "workTitle")
    assert title is not None, "写真卡片里找不到标题标签"
    assert title.text() == LONG_JP_TITLE[:TITLE_MAX_CHARS] + "…"
    assert title.toolTip() == LONG_JP_TITLE

    cover = card.img_label
    assert title.y() >= cover.geometry().bottom(), (
        f"标题 y={title.y()} 压住封面（封面底 {cover.geometry().bottom()}）")

    meta = [lbl for lbl in card.findChildren(QLabel)
            if lbl is not title and lbl is not cover and "张" in lbl.text()]
    assert meta, "找不到「作者 · N 张」meta 行"
    assert meta[0].isVisible(), "meta 行被标题挤出可视区"
    assert meta[0].geometry().bottom() < card.height(), (
        f"meta 底边 {meta[0].geometry().bottom()} 越出卡片高度 {card.height()}")
    card.close()
