"""
看图缩放：解码上限与缩放倍率的纯函数边界

这里刻意不建 QWidget —— 只测两个纯函数：
    - `clamp_decode_size`：超大图必须夹住（否则翻两张就打满内存）、
      小图不能放大（否则一张 200px 图标被拉成满屏马赛克）
    - `fit_scale`：100% 必须正好适应窗口，缩放只是在这个基准上叠加

前两个上限（`_MAX_DECODE_DIM` / `VIEWER_MAX_DIM`）是两处独立常量，
必须保持一致，否则会出现「请求 2560 却按 1024 解码」的糊图。
"""
from PyQt6.QtCore import QSize

from ui.image_loader import _MAX_DECODE_DIM, _normalize_size
from ui.image_viewer import VIEWER_MAX_DIM, clamp_decode_size, fit_scale

# ==================== 上限一致性 ====================

def test_decode_limit_matches_viewer_limit():
    assert _MAX_DECODE_DIM == VIEWER_MAX_DIM, (
        "image_loader 的解码上限与 image_viewer 的请求上限必须相同；"
        "只改一个会让放大后的图被静默降级成 1024px"
    )


def test_normalize_size_no_longer_clamps_to_1024():
    """旧上限 1024 在 2K 屏上放大一倍就糊，这里锁死已经放宽"""
    w, h = _normalize_size(QSize(2000, 1400))
    assert (w, h) == (2048, 1408)


def test_normalize_size_still_has_an_upper_bound():
    w, h = _normalize_size(QSize(9999, 9999))
    assert w <= VIEWER_MAX_DIM
    assert h <= VIEWER_MAX_DIM


# ==================== 解码尺寸 ====================

def test_clamp_never_exceeds_max_dim():
    got = clamp_decode_size(QSize(8000, 6000), QSize(3840, 2160))
    assert max(got.width(), got.height()) <= VIEWER_MAX_DIM


def test_clamp_keeps_aspect_ratio():
    assert clamp_decode_size(QSize(4000, 2000), QSize(2000, 2000)) == QSize(2000, 1000)


def test_clamp_does_not_upscale_small_images():
    assert clamp_decode_size(QSize(400, 300), QSize(3000, 3000)) == QSize(400, 300)


def test_clamp_falls_back_to_want_without_original_size():
    """QImageReader 读不到头信息时 orig 是无效尺寸，直接用视口×2"""
    got = clamp_decode_size(QSize(), QSize(1600, 900))
    assert got == QSize(1600, 900)


# ==================== 缩放倍率 ====================

def test_fit_scale_fits_large_image_to_viewport():
    assert abs(fit_scale(1000, 800, 2000, 1600, 1.0) - 0.5) < 1e-6


def test_fit_scale_never_enlarges_small_image():
    assert fit_scale(1000, 800, 200, 100, 1.0) == 1.0


def test_fit_scale_applies_zoom_on_top_of_fit():
    assert abs(fit_scale(1000, 800, 500, 400, 2.0) - 2.0) < 1e-6


def test_fit_scale_returns_neutral_for_empty_viewport():
    """窗口还没显示时视口是 0×0，不能因此算出 0 倍缩放（图会消失）"""
    assert fit_scale(0, 0, 100, 100, 1.5) == 1.0
