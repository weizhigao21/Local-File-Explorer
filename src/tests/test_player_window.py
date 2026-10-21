"""播放器窗口 / 字幕视图 / 单实例测试（离屏）"""
import os
import sys
import time
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from app_instance import SingleInstance  # noqa: E402
from audio_manager import database as adb  # noqa: E402
from ui.playback_controller import PlaybackController  # noqa: E402
from ui.player_window import PlayerWindow, tracks_from_paths  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def temp_audio_db(tmp_path, monkeypatch):
    monkeypatch.setattr(adb, "DB_PATH", str(tmp_path / "audio.db"))
    adb.end_persistent()
    adb.init_db()
    yield adb
    adb.end_persistent()


@pytest.fixture
def window(qapp, temp_audio_db):
    playback = PlaybackController()
    win = PlayerWindow(playback=playback)
    yield win, playback
    win.shutdown()


def _make_track(tmp_path, with_subtitle=True):
    audio = tmp_path / "03.mp3"
    audio.write_bytes(b"")
    if with_subtitle:
        (tmp_path / "03.srt").write_text(
            "1\n00:00:01,000 --> 00:00:03,000\n第一句\n\n"
            "2\n00:00:04,000 --> 00:00:06,000\n第二句\n",
            encoding="utf-8",
        )
    return {"title": "第 03 轨", "path": str(audio), "playlist_id": None}


def test_window_construction_keeps_player_lazy(window):
    win, playback = window
    assert playback._player is None, "打开播放器窗口不应初始化音频设备"
    assert win.title_label.text() == "未播放"
    assert win.queue_list.count() == 0


def test_media_buttons_use_sized_vector_icons(window):
    """播放控制必须是绘制图标：图标非空、尺寸足够，播放/暂停可切换"""
    win, _playback = window
    assert not win.play_btn.icon().isNull()
    assert not win.prev_btn.icon().isNull()
    assert not win.next_btn.icon().isNull()
    assert win.play_btn.iconSize().width() >= 20
    assert win.prev_btn.iconSize().width() >= 18
    assert win.play_btn.kind == "play"
    win._on_state_changed(True)
    assert win.play_btn.kind == "pause"
    win._on_state_changed(False)
    assert win.play_btn.kind == "play"


def test_track_change_loads_subtitle_and_title(window, tmp_path):
    win, _playback = window
    track = _make_track(tmp_path)
    win._on_track_changed(track)

    assert win.title_label.text() == "第 03 轨"
    assert "03.srt" in win.subtitle_view.file_label.text()
    assert win.subtitle_view.list.count() == 2
    assert win.meta_label.text().startswith("MP3")


def test_subtitle_highlight_follows_position(window, tmp_path):
    win, _playback = window
    win._on_track_changed(_make_track(tmp_path))
    win.subtitle_view.set_position(1500)
    assert win.subtitle_view._current_row == 0
    win.subtitle_view.set_position(4500)
    assert win.subtitle_view._current_row == 1
    # 句间空隙保持上一句，避免滚动闪烁
    win.subtitle_view.set_position(3500)
    assert win.subtitle_view._current_row == 1


def test_subtitle_offset_is_persisted(window, temp_audio_db, tmp_path):
    win, _playback = window
    track = _make_track(tmp_path)
    playlist_id = temp_audio_db.add_playlist("歌单", str(tmp_path))
    temp_audio_db.add_tracks(playlist_id, [("第 03 轨", track["path"], 0, 1)])

    win._on_track_changed(track)
    win.subtitle_view.set_offset(500)
    assert temp_audio_db.get_subtitle_offset(track["path"]) == 500

    win.subtitle_view.set_offset(-250)
    assert temp_audio_db.get_subtitle_offset(track["path"]) == -250


def test_queue_panel_and_mini_mode(window, tmp_path):
    win, playback = window
    tracks = []
    for index in range(3):
        path = tmp_path / f"{index}.mp3"
        path.write_bytes(b"")
        tracks.append({"title": f"曲{index}", "path": str(path), "playlist_id": None})
    playback.set_queue(tracks)
    assert win.queue_list.count() == 3
    assert win.queue_btn.text() == "队列 3"

    win.queue_btn.setChecked(True)
    assert win.queue_panel.isVisibleTo(win)

    win.mini_btn.setChecked(True)
    assert win.body.isHidden()
    assert win.secondary_row.isHidden()
    win.mini_btn.setChecked(False)
    assert not win.body.isHidden()


def test_mini_mode_hides_controls_container(window):
    """迷你态必须把整个 controls 容器（含上下边距）一起藏掉

    只藏 seek_card/secondary_row 时容器边距仍占 32px，把 mini_body 压到 56px，
    42px 播放键溢出 32px 高的传输胶囊，光晕被齐底截成「方形光斑」（v1.8.4）。
    """
    win, _playback = window
    win.mini_btn.setChecked(True)
    assert win.controls.isHidden(), "controls 容器必须整体隐藏"
    assert win.seek_card.isHidden()
    assert win.secondary_row.isHidden()
    # 退出迷你态要全部恢复
    win.mini_btn.setChecked(False)
    assert not win.controls.isHidden()
    assert not win.seek_card.isHidden()
    assert not win.secondary_row.isHidden()


def _pill_fill_profile(image, rect, fill_rgb):
    """返回 rect 内最左填充列的填充起点 y 偏移。
    胶囊形起点 ≈ 高/2；直角 ≈ 0；普通圆角矩形 = 圆角 < 半高。"""
    x0, y0, w, h = rect.x(), rect.y(), rect.width(), rect.height()

    def is_fill(x, y):
        p = image.pixel(x, y)
        return ((p >> 16) & 255, (p >> 8) & 255, p & 255) == fill_rgb

    xs = []
    for y in range(y0, y0 + h):
        for x in range(x0, x0 + w):
            if is_fill(x, y):
                xs.append(x)
    if not xs:
        return h, None
    left = min(xs)
    top = min(y for y in range(y0, y0 + h) if is_fill(left, y)) - y0
    return h, top


def test_pills_render_as_true_capsules(window, qapp):
    """传输/音量/倍速胶囊必须真胶囊（两端半圆），不能是方角或圆角矩形

    Qt 对超过半高的 border-radius 不钳成胶囊而是退化成直角——写 100px 时
    完整态传输栏渲染成方角矩形（用户截图取证，v1.8.5）。按真实控件像素验型：
    最左填充列约在边框内侧 x0+1 处，胶囊弧顶起点 = r-√(2r-1) ≈ h/2-√h，
    显著高于直角（≈1）与小圆角矩形（≈r-√(2r)），据此判型。
    """
    import math

    win, _playback = window
    win.resize(900, 620)
    win.show()
    qapp.processEvents()

    from PyQt6.QtCore import QPoint

    card_fill = (251, 247, 240)  # P_CARD #FBF7F0

    def assert_capsule(widget, name):
        top_left = widget.mapTo(win, QPoint(0, 0))
        rect = widget.rect()
        rect.moveTopLeft(top_left)
        image = win.grab().toImage()
        height, start = _pill_fill_profile(image, rect, card_fill)
        assert start is not None, f"{name} 没采到填充色"
        # 胶囊弧在最左填充列的起点下界：r - √(2r) - 3（r=半高，√(2r)=√h）
        capsule_min = height / 2 - math.sqrt(height) - 3
        assert start >= capsule_min, (
            f"{name} 不是胶囊形：高 {height}，最左填充起点 {start}"
            f"（胶囊应 ≥{capsule_min:.0f}；直角 ≈1，圆角矩形/拉伸后圆角比例"
            "失效都会偏低——border-radius 必须 ≤ 半高且取半高）"
        )

    assert_capsule(win.play_btn.parent(), "传输胶囊")
    assert_capsule(win.vol_pill, "音量胶囊")
    assert_capsule(win.speed_btn, "倍速胶囊")

    win.mini_btn.setChecked(True)
    qapp.processEvents()
    mini = win.mini_play.parent()
    assert_capsule(mini, "迷你传输胶囊")
    assert mini.height() == mini.sizeHint().height(), (
        f"迷你胶囊被布局拉伸: {mini.height()} != {mini.sizeHint().height()}"
        "（拉高后半高圆角不再是胶囊）"
    )


def test_queue_row_elides_long_titles(window, tmp_path):
    """超长曲名必须省略号截断，不能把行撑出队列卡（QLabel 不自动 ellipsis）"""
    win, playback = window
    long_title = "超长曲名" * 30  # 120 个字符，远超 240px 队列卡
    path = tmp_path / "long.mp3"
    path.write_bytes(b"")
    playback.set_queue([{"title": long_title, "path": str(path), "playlist_id": None}])

    row = win.queue_list.itemWidget(win.queue_list.item(0))
    name_label = None
    for label in row.findChildren(type(win.title_label)):
        if label.text().endswith("…"):
            name_label = label
            break
    assert name_label is not None, "队列曲名 label 应带省略号"
    assert len(name_label.text()) < len(long_title), "省略号版本必须短于原曲名"
    assert name_label.toolTip() == long_title, "悬停应能看到完整曲名"


def test_no_visible_label_clips_glyphs(window, qapp):
    """可见 QLabel 不得裁掉字形头脚：标签高必须 ≥ 字体墨盒（ascent+descent）

    队列行曾被 QListWidget::item 的 QSS padding 从 34px 挤压到 20px（行内
    margin 再吃 12px，标签只剩 8px < 12px 字体墨盒 14px），曲名头脚被砍掉
    （用户截图实锤，v1.8.6）；17px 标题也因 word-wrap 的 sizeHint 向下取整
    （报 18px < 墨盒 19px）顶部触边。像素取证：曲名墨迹不得触碰标签上下边。
    """
    from PyQt6.QtWidgets import QLabel

    win, playback = window
    track = {"title": "曲名裁切守卫", "path": "a.mp3", "playlist_id": None}
    playback.set_queue([track, dict(track, title="b", path="b.mp3")])
    win.resize(900, 620)
    win.show()
    # 必须展开队列面板：收起时行标签不可见会被扫描跳过，裁切根本测不到
    win.queue_btn.setChecked(True)
    qapp.processEvents()

    def clipped(tag):
        bad = []
        for label in win.findChildren(QLabel):
            if not label.isVisible():
                continue
            fm = label.fontMetrics()
            ink = fm.ascent() + fm.descent()
            if label.height() < ink:
                bad.append(
                    f"{tag} {label.text()[:16]!r}: h={label.height()} < 墨盒={ink}")
        return bad

    bad = clipped("完整态")

    # 像素取证：队列曲名字形头脚不得触边（触边 = 被裁掉）
    row = win.queue_list.itemWidget(win.queue_list.item(0))
    name_label = next(
        lbl for lbl in row.findChildren(QLabel)
        if "…" in lbl.text() or lbl.text() in ("b", "曲名裁切守卫"))
    img = name_label.grab().toImage()
    bg = img.pixelColor(0, 0)
    ink_rows = [y for y in range(img.height())
                if any(img.pixelColor(x, y) != bg for x in range(img.width()))]
    assert ink_rows, "曲名应有可见墨迹"
    top, bottom = min(ink_rows), max(ink_rows)
    assert top >= 1 and bottom <= img.height() - 2, (
        f"队列曲名字形触边被裁: 墨迹 y{top}..{bottom} / 标签高 {img.height()}"
        "（字形上下应各留 ≥1px）"
    )

    win.mini_btn.setChecked(True)
    qapp.processEvents()
    bad += clipped("迷你态")
    win.mini_btn.setChecked(False)
    win.close()  # 清场：别把可见顶层窗口留给后续用例
    qapp.processEvents()
    assert not bad, "可见标签裁掉字形头脚:\n" + "\n".join(bad)


def test_embedded_window_close_only_hides(window, qapp):
    """嵌在主界面里的播放器：关窗只隐藏，主界面还在，随时能再打开"""
    from PyQt6.QtWidgets import QWidget

    win, _playback = window
    main_ui = QWidget()
    main_ui.show()  # 模拟主界面在场（有别的可见窗口时才允许隐藏语义）
    qapp.processEvents()
    win.show_player()
    qapp.processEvents()
    win.close()
    qapp.processEvents()
    assert not win.isVisible()
    assert win._allow_close is False, "这时的关闭必须是「隐藏」，不能真的销毁"
    win.show_player()
    assert win.isVisible()
    main_ui.close()
    main_ui.deleteLater()


def test_last_visible_window_closes_for_real(window, qapp):
    """最后一个可见窗口关闭必须真关，不能只隐藏

    只 hide 时 quitOnLastWindowClosed 永不触发：窗口从任务栏消失但进程驻留，
    单实例锁挡住下次启动，只能进任务管理器杀进程（用户反馈，v1.8.5）。
    """
    win, _playback = window
    # 测试隔离：前序用例可能残留可见顶层窗口（如 show 后只 deleteLater 的控件），
    # 会干扰「是否还有别的可见窗口」的判断。先清场，只留播放器自己。
    from PyQt6.QtWidgets import QApplication

    for stray in QApplication.topLevelWidgets():
        if stray is not win and stray.isVisible():
            stray.hide()
    win.show_player()
    qapp.processEvents()
    assert win.isVisible()
    win.close()
    qapp.processEvents()
    assert not win.isVisible()
    assert win._allow_close is True, (
        "最后一个可见窗口必须真关，否则进程驻留（无任务栏、重开被单实例锁挡住）"
    )
    win.shutdown()


def test_standalone_window_closes_for_real(qapp, temp_audio_db):
    """文件关联拉起的小播放窗：关掉它程序就退出（不常驻托盘）"""
    playback = PlaybackController()
    win = PlayerWindow(playback=playback, standalone=True)
    win.show_player()
    qapp.processEvents()
    assert win.isVisible()
    win.close()
    qapp.processEvents()
    assert not win.isVisible()
    assert win._allow_close is True
    win.shutdown()


def test_standalone_compact_layout(qapp, temp_audio_db):
    """独立播放默认迷你布局：只有封面信息 + 控制条，不铺满屏幕"""
    win = PlayerWindow(playback=PlaybackController(), standalone=True)
    win.set_compact(True)
    assert win.mini_btn.isChecked()
    assert win.body.isHidden()
    assert win.secondary_row.isHidden()
    assert win.width() <= 600
    win.shutdown()


def test_seek_preview_never_touches_media(window, monkeypatch):
    """拖动预览只更新时间与字幕，必须一次都不调用 seek"""
    win, playback = window
    calls = []
    monkeypatch.setattr(playback, "seek", lambda ms: calls.append(ms))

    win._on_seek_preview(42)
    assert calls == []
    assert win.time_current.text() == "00:42"

    win._seek_to_second(42)
    assert calls == [42000]


def test_main_module_has_no_tray_resident():
    """静态守卫：启动流程不得再创建系统托盘常驻进程"""
    source = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
    )
    with open(source, encoding="utf-8") as f:
        text = f.read()
    assert "tray" not in text.lower()
    assert "QSystemTrayIcon" not in text
    assert "setQuitOnLastWindowClosed(True)" in text


def test_tracks_from_paths_expands_folder_and_filters(tmp_path):
    folder = tmp_path / "专辑"
    folder.mkdir()
    (folder / "01.mp3").write_bytes(b"")
    (folder / "02.flac").write_bytes(b"")
    (folder / "说明.txt").write_text("not audio", encoding="utf-8")

    tracks = tracks_from_paths([str(folder)])
    assert [os.path.basename(track["path"]) for track in tracks] == ["01.mp3", "02.flac"]
    assert tracks_from_paths([str(tmp_path / "nope.mp3")]) == []


def test_player_window_does_not_own_media_player():
    """静态守卫：播放器窗口同样只通过控制器操作媒体"""
    source = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "ui", "player_window.py",
    )
    with open(source, encoding="utf-8") as f:
        text = f.read()
    assert "QMediaPlayer" not in text
    assert "PlaybackController" in text or "get_playback_controller" in text


def test_single_instance_forwards_argv(qapp, monkeypatch):
    key = f"wb-test-{os.getpid()}-{uuid.uuid4().hex}"
    primary = SingleInstance(key)
    assert primary.start() is True

    received = []
    primary.messageReceived.connect(received.append)

    monkeypatch.setattr(sys, "argv", ["prog", "--play", "a.mp3"])
    secondary = SingleInstance(key)
    assert secondary.start() is False

    for _ in range(30):
        qapp.processEvents()
        if received:
            break
        time.sleep(0.02)
    assert received == [["--play", "a.mp3"]]

    secondary.shutdown()
    primary.shutdown()


# ==================== 进度条：一次拖动只提交一次 seek ====================
# 回归背景：旧实现拖动时按 100ms 节流连续 seek，每次 setPosition 都会让解码器
# 重开音频管线，听感上就是「同一小段被重复播放」。现在拖动只发 seekPreview
# （纯 UI 预览），松手瞬间才发一次 clickedSeek。
# 说明：跳转行为用离屏脚本手工验证（见 CHANGELOG v1.8.2），
# 这里只留静态守卫——在 pytest 里反复创建/销毁 QWidget 会让离屏平台
# 在 FlowLayout 上撞到 Qt 的悬空指针，属于测试环境问题，不适合作为门禁。


def test_player_window_commits_seek_only_on_release():
    """静态守卫：播放器窗口不得把 sliderMoved 接到真实 seek"""
    source = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "ui", "player_window.py",
    )
    with open(source, encoding="utf-8") as f:
        text = f.read()
    assert "seekPreview.connect" in text
    assert "sliderMoved.connect(self._seek" not in text
    assert "sliderMoved.connect(self.playback" not in text

    bar = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "ui", "audio_player.py",
    )
    with open(bar, encoding="utf-8") as f:
        bar_text = f.read()
    assert "seekPreview = pyqtSignal(int)" in bar_text
    assert "self.clickedSeek.emit(self.value())" in bar_text
