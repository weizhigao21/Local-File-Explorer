"""子歌单路径名称及其详情展示。"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from audio_manager import database as db  # noqa: E402
from audio_manager import dlsite  # noqa: E402
from audio_manager.catalog import build_audio_catalog
from resource_manager import config  # noqa: E402
from ui.audio_view import AudioMainWindow  # noqa: E402
from ui.audio_widgets import SubPlaylistCard  # noqa: E402


def test_folder_name_dot_is_distinct_from_path_separator(tmp_path):
    root = str(tmp_path)
    dotted = os.path.join(root, "A.B")
    nested = os.path.join(root, "A", "B")
    playlists = [
        {"id": 1, "name": "A.B", "path": dotted, "cover": None, "tags": ""},
        {"id": 2, "name": "B", "path": nested, "cover": None, "tags": ""},
    ]

    catalog = build_audio_catalog(playlists, {1: 1, 2: 1}, [root])
    assert len(catalog) == 2
    assert len({pl["name"] for pl in catalog}) == 2
    assert {pl["name"] for pl in catalog} == {"A.B", "A > B"}


def test_long_subplaylist_path_stays_inside_card_and_keeps_full_tooltip():
    app = QApplication.instance() or QApplication([])
    name = "非常非常长的上级文件夹名称 > 另一个很长的中间文件夹 > 最终文件夹名称"
    card = SubPlaylistCard({"id": 1, "name": name, "track_count": 12})
    card.resize(300, 32)
    card.show()
    app.processEvents()

    label = card.name_label
    assert label.toolTip() == name
    assert label.text() != name
    assert "最终文件夹" in label.text()
    assert label.fontMetrics().horizontalAdvance(label.text()) <= label.width()
    assert card.layout().itemAt(1).widget().geometry().right() < card.width()

    card.resize(1100, 32)
    app.processEvents()
    assert label.text() == name
    card.close()


def test_main_playlist_contains_flat_audio_subplaylists(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    assert app is QApplication.instance()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "audio.db"))
    monkeypatch.setattr(config, "AUDIO_ROOTS", [str(tmp_path)])
    monkeypatch.setattr(dlsite, "AVAILABLE", False)
    monkeypatch.setattr(AudioMainWindow, "start_scan", lambda self: None)
    db.end_persistent()
    db.init_db()

    group_path = os.path.join(str(tmp_path), "歌单")
    first_path = os.path.join(group_path, "文件夹1")
    second_path = os.path.join(first_path, "文件夹2")
    empty_path = os.path.join(second_path, "文件夹3")
    fourth_path = os.path.join(empty_path, "文件夹4")
    group_id = db.add_playlist("歌单", group_path)
    first_id = db.add_playlist("文件夹1", first_path, parent_path=group_path)
    second_id = db.add_playlist("文件夹2", second_path, parent_path=first_path)
    db.add_playlist("文件夹3", empty_path, parent_path=second_path)
    fourth_id = db.add_playlist("文件夹4", fourth_path, parent_path=empty_path)
    db.add_tracks(group_id, [("主曲目", os.path.join(group_path, "00.mp3"), 0, 1)])
    db.add_tracks(first_id, [("第一首", os.path.join(first_path, "01.mp3"), 0, 1)])
    db.add_tracks(second_id, [("第二首", os.path.join(second_path, "02.mp3"), 0, 1)])
    db.add_tracks(fourth_id, [("第四首", os.path.join(fourth_path, "04.mp3"), 0, 1)])

    win = AudioMainWindow()
    assert [pl["name"] for pl in win.browser._all_playlists] == ["歌单"]
    win._open_playlist(group_id)
    assert win.detail.playlist_title.text() == "歌单"
    assert [track["title"] for track in win._tracks_data] == ["主曲目"]
    assert win.detail.playlist_count.text() == "本目录 1 首曲目 · 3 个子歌单"
    assert {pl["name"] for pl in win.detail._sub_playlists} == {
        "文件夹1", "文件夹1 > 文件夹2", "文件夹1 > 文件夹2 > 文件夹3 > 文件夹4",
    }
    win._open_playlist(first_id, history=[group_id, first_id])
    assert win.detail.playlist_title.text() == "文件夹1"
    assert [track["title"] for track in win._tracks_data] == ["第一首"]
    assert win.detail._sub_playlists == []
    win._open_playlist(second_id, history=[group_id, second_id])
    assert win.detail.playlist_title.text() == "文件夹1 > 文件夹2"
    assert [track["title"] for track in win._tracks_data] == ["第二首"]
    win._detail_back()
    assert win.detail.playlist_title.text() == "歌单"
    win.close()
    db.end_persistent()
