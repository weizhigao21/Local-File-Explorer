"""
设计令牌（调色板）与导入完整性守卫

1) 令牌单一来源：launcher / photo_theme / audio_theme 曾各自复制同名配色，
   取值一旦漂移就会出现"改一处、漏两处"。现在统一到 ui.theme_base，
   这里断言三者取值与 theme_base 完全一致。
2) 导入烟测：逐个导入 ui 包下所有模块，捕捉重构（拆分/改名/删文件）造成的
   导入链断裂与拼写错误 —— 这类问题在离屏测试里比在真机点击才发现便宜得多。
"""
import importlib
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 必须在导入 Qt 之前设置

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")

# 三个模块都曾各自定义过的令牌
SHARED_TOKENS = [
    "ACCENT", "ACCENT_HOVER", "ACCENT_TINT",
    "BG_MAIN", "BG_SIDEBAR", "CARD_BG", "CARD_HOVER",
    "TEXT_PRIMARY", "TEXT_MUTED", "TEXT_DIM",
]


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_palette_single_sourced():
    """三个 UI 模块的共享配色令牌必须与 theme_base 取值一致（不得各写一份）"""
    from ui import audio_theme, launcher, photo_theme, theme_base

    mismatches = []
    for name in SHARED_TOKENS:
        expected = getattr(theme_base, name)
        for mod in (audio_theme, photo_theme, launcher):
            if not hasattr(mod, name):
                continue
            actual = getattr(mod, name)
            if actual != expected:
                mismatches.append(
                    f"{mod.__name__}.{name} = {actual!r}，theme_base 为 {expected!r}"
                )
    assert mismatches == [], "调色板出现漂移（应只在 ui/theme_base.py 定义）:\n" + "\n".join(mismatches)


def test_theme_modules_reexport_tokens():
    """主题模块必须继续对外导出令牌（既有 `from ui.photo_theme import ACCENT` 不能断）"""
    from ui import audio_theme, launcher, photo_theme, theme_base

    for mod in (audio_theme, photo_theme, launcher):
        assert hasattr(mod, "ACCENT"), f"{mod.__name__} 不再导出 ACCENT"
        assert mod.ACCENT == theme_base.ACCENT

    # photo_theme 的模块独有令牌
    assert photo_theme.ACCENT_PRESSED == "#3DA0AE"
    # audio_theme 的模块独有样式
    assert "QPushButton" in audio_theme.BTN_QSS


def test_all_ui_modules_import(qapp):
    """ui 包下每个模块都必须能被干净导入"""
    failures = []
    for name in sorted(os.listdir(UI_DIR)):
        if not name.endswith(".py") or name == "__init__.py":
            continue
        mod_name = f"ui.{name[:-3]}"
        try:
            importlib.import_module(mod_name)
        except Exception as e:  # noqa: BLE001 - 收集所有失败用于一次性报告
            failures.append(f"{mod_name}: {type(e).__name__}: {e}")

    assert failures == [], "以下 UI 模块导入失败:\n" + "\n".join(failures)


def test_core_layers_do_not_import_qt():
    """核心层（resource_manager / audio_manager）不得依赖 Qt —— 保持算法层与 GUI 解耦"""
    offenders = []
    for pkg in ("resource_manager", "audio_manager"):
        pkg_dir = os.path.join(os.path.dirname(UI_DIR), pkg)
        for name in sorted(os.listdir(pkg_dir)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(pkg_dir, name), encoding="utf-8") as f:
                src = f.read()
            if "PyQt6" in src or "PySide6" in src:
                offenders.append(f"{pkg}/{name}")
    assert offenders == [], f"核心层出现 Qt 依赖（分层被破坏）: {offenders}"
