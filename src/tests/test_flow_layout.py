"""FlowLayout 排布守卫：固定尺寸的卡片不能因为 sizeHint 偏小而被排到上一行里。

历史 bug（v1.8.11）：PlaylistCard `setFixedSize(180, 240)`，但内容布局的
sizeHint 只有 196（名字两行时 202）。FlowLayout 当年直接用 sizeHint 累加行距，
控件却被 min/max 顶回 240 → 相邻两行重叠 20~28px，视觉上是「两个作品压在一起」。
"""

import pytest
from PyQt6.QtWidgets import QApplication, QPushButton, QWidget, QWidgetItem

from ui.audio_widgets import PlaylistCard
from ui.flow_layout import FlowLayout, _item_box
from ui.grid_view import WorkCard


@pytest.fixture(scope="session")
def qapp():
    """离屏 QApplication（整个测试会话共用一个实例）"""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def host(qapp):
    """排布用的宿主容器，结束时统一销毁（不在测试间复用 QWidget）"""
    boxes = []

    def make(width=600, height=900):
        box = QWidget()
        box.resize(width, height)
        boxes.append(box)
        return box

    yield make
    for box in boxes:
        box.close()


def _fill(box, cards, h_spacing=16, v_spacing=16, margin=16):
    flow = FlowLayout(box, margin=margin, h_spacing=h_spacing, v_spacing=v_spacing)
    for card in cards:
        flow.addWidget(card)
    box.show()
    QApplication.processEvents()
    return flow


def _audio_cards(count=6):
    return [
        PlaylistCard({"id": i, "name": f"作品名 {i}", "path": f"/p{i}", "track_count": 6})
        for i in range(count)
    ]


def _photo_cards(count=4):
    return [
        WorkCard({"name": f"作品 {i}", "author": "社团", "image_count": 40, "thumbnail": ""})
        for i in range(count)
    ]


def test_playlist_cards_do_not_overlap(host):
    """音频作品卡两行之间必须留出完整间距，不能叠在一起"""
    cards = _audio_cards(6)
    _fill(host(600, 900), cards)

    first_row = [c for c in cards if c.y() == cards[0].y()]
    assert len(first_row) == 2, f"600 宽应每行 2 张，实际 {len(first_row)} 张"

    first_bottom = cards[0].y() + cards[0].height()
    assert cards[2].y() >= first_bottom + 16 - 1, (
        f"第二行顶 {cards[2].y()} 与第一行底 {first_bottom} 重叠了 "
        f"{first_bottom - cards[2].y()}px（FlowLayout 用了偏小的 sizeHint）")

    for card in cards:
        assert card.height() == 240, f"卡片被压成 {card.height()}px"
    gap = cards[1].x() - (cards[0].x() + cards[0].width())
    assert gap == 16, f"行内水平间隙 {gap}px != 16"


def test_work_cards_do_not_overlap(host):
    """写真作品卡（220x280）同样不能重叠"""
    cards = _photo_cards(4)
    _fill(host(520, 900), cards)

    first_bottom = cards[0].y() + cards[0].height()
    assert cards[2].y() >= first_bottom + 16 - 1, (
        f"写真卡第二行顶 {cards[2].y()} 与第一行底 {first_bottom} 重叠")


def test_item_box_returns_fixed_size():
    """排布尺寸必须等于 setFixedSize 设的值，而不是内容 sizeHint"""
    card = _audio_cards(1)[0]
    box = _item_box(QWidgetItem(card))
    assert (box.width(), box.height()) == (180, 240), f"排布尺寸 {box} 与固定尺寸不符"

    photo = _photo_cards(1)[0]
    box = _item_box(QWidgetItem(photo))
    assert (box.width(), box.height()) == (220, 280)


def test_normal_widget_still_uses_size_hint():
    """没设固定尺寸的控件仍按 sizeHint 排（详情页标签胶囊依赖这个行为）"""
    button = QPushButton("标签")
    button.ensurePolished()
    assert _item_box(QWidgetItem(button)) == button.sizeHint()
