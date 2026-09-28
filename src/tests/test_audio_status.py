"""后台任务共用分页栏，状态与分页不得重叠。"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QProgressBar  # noqa: E402

from audio_manager import database as db  # noqa: E402
from audio_manager import dlsite  # noqa: E402
from ui.audio_view import AudioMainWindow  # noqa: E402


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "audio.db"))
    monkeypatch.setattr(dlsite, "AVAILABLE", False)
    monkeypatch.setattr(AudioMainWindow, "start_scan", lambda self: None)
    monkeypatch.setattr(AudioMainWindow, "_start_mtime_migration", lambda self: None)
    monkeypatch.setattr(AudioMainWindow, "_start_dlsite_prefetch", lambda self: None)
    db.end_persistent()
    win = AudioMainWindow()
    yield win, app
    win.close()
    win.deleteLater()
    app.processEvents()
    db.end_persistent()


@pytest.mark.parametrize("width", [640, 1080])
def test_scan_and_fetch_share_footer_without_overlapping_pagination(window, width):
    win, app = window
    win.resize(width, 700)
    win.show()
    app.processEvents()
    footer_height = win.browser.page_bar.height()
    win._on_scan_progress(3, 10, "正在扫描: " + "长文件夹名称" * 30)
    win._on_dlsite_progress(2, 10, "正在获取 RJ123456")
    app.processEvents()

    status = win.background_status
    assert status.ring.value() == 30
    assert status.label.text() == "正在扫描"
    assert "长文件夹名称" in status.toolTip()
    assert "RJ123456" in status.toolTip()
    assert status.geometry().right() < win.browser.pagination_controls.geometry().left()
    assert win.browser.page_bar.height() == footer_height == 40
    assert not win.findChildren(QProgressBar)
    if width == 1080:
        assert abs(win.browser.pagination_controls.geometry().center().x()
                   - win.browser.page_bar.rect().center().x()) <= 1

    win._on_scan_finished({})
    assert status.ring.value() == 20
    assert status.label.text() == "拉取信息"
    win._on_dlsite_progress(10, 10, "")
    assert status.ring.value() == 100
    assert status.label.text() == "信息就绪"


def test_background_status_remains_available_in_detail_and_hides_when_idle(window):
    win, app = window
    win.show()
    win._on_scan_progress(1, 4, "正在扫描: 文件夹")
    win.page_stack.setCurrentIndex(1)
    app.processEvents()
    assert win.browser.page_bar.isVisible()
    assert not win.browser.pagination_controls.isVisible()
    win.background_status.clear_task("scan")
    assert not win.browser.page_bar.isVisible()
    win.page_stack.setCurrentIndex(0)
    assert win.browser.page_bar.isVisible()
    assert win.browser.pagination_controls.isVisible()
