"""音频格式图标：扩展名归一、有损/无损配色、文字自适应、渲染健全性"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from ui import file_format_icons as icons  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


# ==================== 覆盖范围 ====================
def test_covers_every_scanner_extension():
    """守卫：扫描器认得的音频格式，必须全都有图标"""
    from audio_manager.scanner import AUDIO_EXTENSIONS as scanner_exts

    scanner = {str(e).lstrip(".").lower() for e in scanner_exts}
    assert scanner == icons.AUDIO_EXTENSIONS, (
        f"图标缺少：{sorted(scanner - icons.AUDIO_EXTENSIONS)}；"
        f"图标多余：{sorted(icons.AUDIO_EXTENSIONS - scanner)}"
    )


def test_lossy_and_lossless_are_disjoint():
    assert not (icons.LOSSY_EXTENSIONS & icons.LOSSLESS_EXTENSIONS)


# ==================== 扩展名归一 ====================
@pytest.mark.parametrize(
    "raw,expected",
    [
        (".MP3", "mp3"),
        ("mp3", "mp3"),
        ("07 曲名.FLAC", "flac"),
        ("C:/音乐/RJ01234567/01 曲名.FLAC", "flac"),
        ("D:\\音频\\某专辑\\02.m4a", "m4a"),
        ("noext", "noext"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_ext(raw, expected):
    assert icons.normalize_ext(raw) == expected


# ==================== 有损 / 无损配色 ====================
@pytest.mark.parametrize("ext", ["mp3", "mp2", "aac", "m4a", "m4b", "m4r", "ogg", "oga",
                                 "opus", "spx", "wma", "ac3", "amr"])
def test_lossy_formats_use_accent(ext):
    assert icons.is_lossless(ext) is False
    assert icons.color_for(ext) == icons.COLOR_LOSSY


@pytest.mark.parametrize("ext", ["flac", "wav", "ape", "wv", "aiff", "aif", "dsf", "dff", "mka"])
def test_lossless_formats_use_deeper_color(ext):
    assert icons.is_lossless(ext) is True
    assert icons.color_for(ext) == icons.COLOR_LOSSLESS


def test_lossy_color_matches_theme_accent():
    """有损是绝大多数文件，必须直接沿用主题强调色，否则新图标会和界面脱节"""
    from ui.theme_base import ACCENT

    assert icons.COLOR_LOSSY.upper() == ACCENT.upper()


def test_unknown_format_falls_back_to_lossy_color():
    assert icons.color_for("qzq") == icons.COLOR_LOSSY
    assert icons.color_for("") == icons.COLOR_LOSSY


def test_uppercase_and_mixed_case_are_equivalent():
    assert icons.color_for("FLAC") == icons.color_for("flac")
    assert icons.is_lossless("Flac") is True


# ==================== 图标文字 ====================
@pytest.mark.parametrize(
    "ext,expected",
    [("mp3", "MP3"), ("flac", "FLAC"), ("m4a", "M4A"), ("aiff", "AIFF"), ("mp2", "MP2")],
)
def test_label_for(ext, expected):
    assert icons.label_for(ext) == expected


def test_empty_label_is_a_placeholder():
    assert icons.label_for("") == "AUDIO"


def test_every_supported_format_yields_a_short_label():
    for ext in icons.AUDIO_EXTENSIONS:
        label = icons.label_for(ext)
        assert 2 <= len(label) <= icons._MAX_LABEL, f"{ext} → {label}"
        assert label == label.upper()


def test_tiny_icons_use_short_codes():
    """16px（资源管理器列表视图）必须画短码

    只留色块的话 22 种音频在列表里长得一模一样，用户根本分不出格式；
    写完整格式名又会糊成一道白线。短码是这两者之间唯一可行的取值。
    """
    assert icons.short_code_for("mp3") == "M3"
    assert icons.short_code_for("flac") == "FL"
    assert icons.short_code_for("opus") == "OP"
    assert icons.short_code_for(".M4A") == "4A"


def test_short_codes_are_unique_across_all_formats():
    """两种格式撞码，等于 16px 下这两者无法区分"""
    codes = [icons.short_code_for(ext) for ext in icons.AUDIO_EXTENSIONS]
    duplicates = {c for c in codes if codes.count(c) > 1}
    assert not duplicates, f"短码重复：{duplicates}"


def test_short_codes_are_always_two_chars():
    for ext in icons.AUDIO_EXTENSIONS:
        assert len(icons.short_code_for(ext)) == 2


def test_short_code_falls_back_for_unknown_extension():
    assert icons.short_code_for("xyz") == "XZ"


def test_16px_icon_actually_draws_white_text():
    """光有映射不算数，画到 16px 画布上必须真的出现白色笔画"""
    from PyQt6.QtGui import QImage

    image = icons.format_pixmap("flac", 16).toImage().convertToFormat(
        QImage.Format.Format_ARGB32)
    data = bytes(image.constBits().asstring(image.sizeInBytes()))
    whites = 0
    for i in range(0, len(data), 4):
        b, g, r, a = data[i], data[i + 1], data[i + 2], data[i + 3]
        if a > 200 and r > 225 and g > 225 and b > 225:
            whites += 1
    assert whites >= 6, f"16px 只有 {whites} 个白色像素，说明字没画出来"


def test_20px_still_uses_full_name():
    """20px 起字号够大，应该回到完整格式名而不是短码"""
    assert icons._MIN_TEXT_SIZE == 20
    assert icons.label_for("flac") == "FLAC"


def test_font_shrinks_for_long_labels():
    short_font, _ = icons._label_font(64, "MP3")
    long_font, _ = icons._label_font(64, "AIFF")
    assert long_font.pixelSize() < short_font.pixelSize()


def test_long_label_fits_inside_tile(qapp):
    """4~5 字符格式名的实际宽度必须落在方块内，不能溢出"""
    size = 64.0
    for label in ("MP3", "FLAC", "AIFF", "OPUS"):
        _font, metrics = icons._label_font(size, label)
        assert metrics.horizontalAdvance(label) <= size * 0.80


# ==================== 渲染 ====================
def test_pixmap_has_requested_size(qapp):
    assert icons.format_pixmap("mp3", 64).size().width() == 64
    assert icons.format_pixmap("mp3", 32).size().width() == 32


def test_pixmap_honours_device_pixel_ratio(qapp):
    pixmap = icons.format_pixmap("flac", 48, dpr=2.0)
    assert (pixmap.width(), pixmap.height()) == (96, 96)
    assert pixmap.devicePixelRatio() == 2.0


def test_pixmap_corners_are_transparent(qapp):
    """圆角外必须透明，否则贴到卡片上会出现白角"""
    image = icons.format_pixmap("mp3", 64).toImage()
    assert image.pixelColor(0, 0).alpha() == 0
    assert image.pixelColor(63, 0).alpha() == 0


def test_pixmap_center_is_filled(qapp):
    image = icons.format_pixmap("mp3", 64).toImage()
    assert image.pixelColor(32, 32).alpha() == 255

    # 圆角只吃掉四个角，方块本体应覆盖绝大部分画布
    side, step = 64, 2
    points = [(x, y) for y in range(0, side, step) for x in range(0, side, step)]
    opaque = sum(1 for x, y in points if image.pixelColor(x, y).alpha() == 255)
    assert opaque / len(points) > 0.70


def test_lossy_and_lossless_render_differently(qapp):
    lossy = icons.format_pixmap("mp3", 64).toImage().pixelColor(12, 12)
    lossless = icons.format_pixmap("flac", 64).toImage().pixelColor(12, 12)
    assert lossy != lossless


def test_format_icon_contains_multiple_sizes(qapp):
    icon = icons.format_icon("mp3")
    sizes = {size.width() for size in icon.availableSizes()}
    assert {16, 24, 32, 48, 64} <= sizes


def test_every_format_renders(qapp):
    """22 种格式都必须能画出非空图标（防止某个 label 触发绘制异常）"""
    for ext in icons.AUDIO_EXTENSIONS:
        icon = icons.format_icon(ext, sizes=(32,))
        assert not icon.pixmap(32, 32).isNull(), ext


def test_save_png_writes_file(qapp, tmp_path):
    target = tmp_path / "mp3.png"
    icons.save_png("mp3", str(target), 128)
    assert target.is_file() and target.stat().st_size > 0
