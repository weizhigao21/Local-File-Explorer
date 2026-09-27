"""歌单封面应在首次绘制前就可用，并按卡片尺寸解码。"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from audio_manager import database as db  # noqa: E402
from audio_manager import dlsite, dlsite_db  # noqa: E402
from ui.audio_view import AudioMainWindow  # noqa: E402
from ui.audio_widgets import PlaylistCard  # noqa: E402


def test_cover_is_ready_before_event_loop_and_is_thumbnail(tmp_path):
    app = QApplication.instance() or QApplication([])
    assert app is QApplication.instance()
    path = tmp_path / "cover.jpg"
    image = QImage(2400, 1800, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    assert image.save(str(path))

    card = PlaylistCard({"id": 1, "name": "歌单", "path": str(tmp_path),
                         "track_count": 1, "cover": str(path)})
    pix = card.cover_label.pixmap()
    assert not pix.isNull()
    assert pix.width() <= 160
    assert pix.height() <= 140
    assert card.cover_label.text() == ""
    card.close()


def test_cached_dlsite_cover_is_applied_before_first_list_render(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    assert app is QApplication.instance()
    path = tmp_path / "cached.jpg"
    image = QImage(400, 400, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.blue)
    assert image.save(str(path))

    playlist = {"id": 1, "name": "RJ123456", "path": str(tmp_path),
                "parent_path": "", "track_count": 1, "cover": str(tmp_path / "gone.jpg"),
                "tags": "", "mtime": 1}
    monkeypatch.setattr(db, "init_db", lambda: None)
    monkeypatch.setattr(db, "list_playlists", lambda: [playlist])
    monkeypatch.setattr(db, "list_playlists_by_parent", lambda parent: [playlist])
    monkeypatch.setattr(db, "count_tracks_by_playlist", lambda: {1: 1})
    monkeypatch.setattr(db, "get_child_counts_batch", lambda paths: {})
    monkeypatch.setattr(db, "update_playlists_batch",
                        lambda updates: playlist.update(updates[0][1]))
    monkeypatch.setattr(dlsite, "AVAILABLE", True)
    monkeypatch.setattr(dlsite, "extract_rj_code", lambda name: "RJ123456")
    monkeypatch.setattr(dlsite_db, "init_db", lambda: None)
    monkeypatch.setattr(dlsite_db, "get_cover_map", lambda: {"RJ123456": str(path)})
    monkeypatch.setattr(dlsite_db, "get_genre_map", lambda: {})
    monkeypatch.setattr(AudioMainWindow, "start_scan", lambda self: None)

    win = AudioMainWindow()
    card = win.browser.flow.itemAt(0).widget()
    assert playlist["cover"] == str(path)
    assert not card.cover_label.pixmap().isNull()
    win.close()


def test_single_dlsite_update_uses_only_the_fetched_work(tmp_path, monkeypatch):
    path = tmp_path / "cached.jpg"
    path.write_bytes(b"cached cover")
    playlist = {"id": 1, "name": "RJ123456", "cover": None, "tags": ""}
    updates = []
    monkeypatch.setattr(db, "list_playlists_by_rj", lambda rj: [playlist])
    monkeypatch.setattr(db, "update_playlists_batch", updates.extend)
    monkeypatch.setattr(dlsite, "extract_rj_code", lambda name: "RJ123456")
    monkeypatch.setattr(dlsite_db, "get_cover_map", lambda: pytest.fail("full cover map queried"))
    monkeypatch.setattr(dlsite_db, "get_genre_map", lambda: pytest.fail("full genre map queried"))
    monkeypatch.setattr(dlsite_db, "get_work", lambda rj: pytest.fail("work fetched twice"))

    info = {"title": "作品", "error": None, "cover_path": str(path), "genres": ["剧情"]}
    assert AudioMainWindow._sync_dlsite_info(None, "RJ123456", info)
    assert updates == [(1, {"cover": str(path), "tags": "剧情"})]
