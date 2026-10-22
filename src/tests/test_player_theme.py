"""
播放器「陶色 · 温润卡片」主题（网页版方案 C）守卫

设计来源：artifacts/player-ui-redesign/player-ui-3versions.html 的版本 C
（暖奶油底 + 卡片分区 + 赤陶强调）。这里锁四件事：
    1) 播放器窗口真的接上 player_theme，令牌与设计稿一致；
    2) 卡片类 QSS 必须带类型选择器 —— 裸声明会被子控件继承，
       曾导致标签被描上一圈边框、滑杆被套进方框；
    3) 播放键是自绘柔光按钮（GlowPlayButton），且圆面/柔光真的画出来了；
    4) 迷你态带 44px 封面缩略（网页版"共同修正"清单里的一条）。
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 必须在导入 Qt 之前设置
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

pytest.importorskip("PyQt6.QtWidgets")

import pathlib  # noqa: E402

from PyQt6.QtWidgets import QApplication  # noqa: E402

SRC = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _source(name):
    return (SRC / name).read_text(encoding="utf-8")


def test_player_window_wired_to_clay_theme():
    """player_window 必须引用 player_theme，且不残留上一版深青玻璃的字面量"""
    source = _source("ui/player_window.py")
    assert "from ui.player_theme import" in source, "播放器窗口没接上 player_theme"
    for leftover in ("#0C2A31", "#123E4A", "#4ADCE8"):  # 深青玻璃特征色
        assert leftover not in source, f"残留深青玻璃字面量: {leftover}"


def test_clay_tokens_match_design():
    """关键令牌必须与网页版 C 的 CSS 取值一致"""
    from ui import player_theme as pt

    assert pt.P_BG == "#F4EFE7", "窗体底应是暖奶油"
    assert pt.P_ACCENT == "#B85C3E", "强调色应是赤陶"
    assert pt.P_ACCENT_LIGHT == "#D4886A", "赤陶渐变亮端"
    assert pt.P_CARD == "#FBF7F0", "卡片面应是米白"


def test_text_is_dark_on_cream():
    """深咖正文压在奶油底上（防止改回浅字后对比度崩掉）"""

    def luminance(hex_color):
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    from ui import player_theme as pt

    assert luminance(pt.P_BG) - luminance(pt.P_TEXT) > 120, "正文与底色对比度不足"
    assert luminance(pt.P_ACCENT) < luminance(pt.P_BG), "赤陶强调色应比底色深"


def test_container_qss_use_type_selectors():
    """卡片/胶囊容器 QSS 必须带 QFrame/QLabel 选择器

    裸声明（无选择器）会被 Qt 级联给子控件：标签被描边框、滑杆被套方框。
    """
    from ui import player_theme as pt

    builders = [pt.card_qss, pt.header_qss, pt.transport_pill_qss,
                pt.vol_pill_qss, pt.cover_qss, pt.mini_cover_qss, pt.tag_qss]
    for build in builders:
        chunk = build().strip()
        assert chunk.startswith(("QFrame {", "QLabel {")), (
            f"{build.__name__} 必须用类型选择器开头，实际: {chunk[:24]!r}"
        )


def test_window_qss_must_use_selector():
    """window_qss 必须带选择器 + 挂到 #playerCentral

    裸 `background-color` 会被 Qt 级联给纯 QWidget 容器（meta_row / cover_wrap），
    把它们染成底色，在米白卡片上形成横贯浅色带（v1.8.4 实锤 bug）。
    """
    from ui import player_theme as pt

    chunk = pt.window_qss().strip()
    assert chunk.startswith("#playerCentral {"), (
        f"window_qss 必须用 #playerCentral 选择器，实际: {chunk[:32]!r}"
    )
    assert chunk.endswith("}"), "window_qss 必须是完整的 选择器 { ... } 块"
    source = _source("ui/player_window.py")
    assert 'setObjectName("playerCentral")' in source, (
        "central 没命名 playerCentral，#playerCentral 选择器会落空"
    )
    assert "central.setStyleSheet(window_qss())" in source


def test_clay_qss_uses_opaque_backgrounds():
    """QSS 的 background-color 不得带透明度（Qt 会静默丢弃整条声明）"""
    from ui import player_theme as pt

    chunks = [pt.window_qss(), pt.card_qss(), pt.pill_btn_qss(),
              pt.slider_qss(), pt.queue_list_qss()]
    for chunk in chunks:
        assert "rgba(" not in chunk and "#AARRGGBB" not in chunk


def test_queue_list_item_qss_has_no_padding():
    """QListWidget::item 的 QSS 不得带 padding

    Qt 会把 item padding 从 item 矩形里扣掉，再把 setItemWidget 的行控件塞进
    剩余内容区（34 − 7×2 = 20px），行内 margin 再吃 12px，标签只剩 8px——
    12px 字体墨盒 14px，字形头脚被裁（v1.8.6 用户实锤）。行内间距由行控件
    自己的 contentsMargins 负责。
    """
    import re

    from ui import player_theme as pt

    chunk = pt.queue_list_qss()
    for m in re.finditer(r"QListWidget::item[^{]*\{([^}]*)\}", chunk):
        body = m.group(1)
        assert "padding" not in body, (
            f"QListWidget::item 规则不得带 padding（会挤压 itemWidget 行高、裁掉字形）: {body!r}"
        )


def test_no_border_radius_exceeds_half_height():
    """禁止「CSS 式超大圆角」：Qt 对超过半高的 border-radius 不会钳成胶囊，
    而是直接退化成直角（写 border-radius: 100px 时完整态传输栏渲染成方角矩形）。
    本主题最大胶囊是 60px 高的传输栏（半高 30），任何 >40 的圆角值都必然是失效写法。
    """
    import re

    source = _source("ui/player_theme.py")
    bad = [m.group(0) for m in re.finditer(r"border-radius:\s*(\d+)px", source)
           if int(m.group(1)) > 40]
    bad += [m.group(0) for m in re.finditer(r"radius: int = (\d+)", source)
            if int(m.group(1)) > 40]
    assert not bad, f"发现超半高圆角（Qt 会退化成直角）: {bad}"


def test_play_button_is_glow_button():
    """播放键必须是自绘柔光按钮"""
    source = _source("ui/player_window.py")
    assert source.count("GlowPlayButton(") >= 2, "完整态与迷你态都要用 GlowPlayButton"
    media = _source("ui/media_icons.py")
    assert "class GlowPlayButton" in media
    assert "QRadialGradient" in media, "柔光要用径向渐变绘制"


def test_glow_button_actually_paints(qapp):
    """柔光按钮画到画布上必须同时出现圆面与外圈柔光像素"""
    from ui.media_icons import GlowPlayButton

    button = GlowPlayButton("play", "#FFFFFF", "#D4886A", "#B85C3E", "#B85C3E",
                            icon_size=20)
    button.setFixedSize(64, 64)
    button.show()
    qapp.processEvents()
    image = button.grab().toImage()

    def pixel(x, y):
        c = image.pixelColor(x, y)
        return c.red(), c.green(), c.blue(), c.alpha()

    face = pixel(24, 32)      # 圆面内、图标左侧空白：赤陶
    halo = pixel(32, 8)       # 圆面之外、按钮之内：柔光
    corner = pixel(2, 2)      # 角落：应接近透明
    assert face[3] > 200 and face[0] > face[1], "圆面应是赤陶实心（红大于绿）"
    assert halo[3] > 10, "圆面外圈应有柔光像素"
    assert corner[3] <= halo[3], "角落应比柔光更透明"
    # 必须收干净：残留的可见顶层窗口会污染依赖 topLevelWidgets 的关窗守卫
    button.close()
    button.deleteLater()
    qapp.processEvents()


def test_mini_layout_has_cover_thumbnail():
    """迷你态带 44px 封面缩略 + 曲名 + 进度条两端时间（网页版共同修正清单）"""
    source = _source("ui/player_window.py")
    for needle in ("self.mini_body", "self.mini_cover", "self.mini_title",
                   "self.mini_time_cur", "self.mini_time_total",
                   "self.mini_seek", "_MINI_COVER = 44"):
        assert needle in source, f"迷你态缺少 {needle}"


def test_subtitle_view_takes_clay_theme(qapp):
    """字幕面板接受陶色配色注入（默认浅色行为不变）"""
    from ui.player_theme import P_CARD, subtitle_theme
    from ui.subtitle_view import SubtitleView

    light = SubtitleView()
    assert "#E8DECB" in light.styleSheet(), "默认浅色卡片边（随 theme_base 陶色）"

    clay = SubtitleView(theme=subtitle_theme())
    assert P_CARD in clay.styleSheet(), "注入后应换成陶色卡片"
    clay.deleteLater()
    light.deleteLater()
