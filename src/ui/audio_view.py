"""
音频模块主窗口
组装歌单浏览器、歌单详情页、播放条与扫描逻辑
"""
import os
from contextlib import suppress
from datetime import datetime

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from audio_manager import database as db
from audio_manager import dlsite, dlsite_db
from audio_manager.catalog import build_audio_catalog
from resource_manager import config
from ui.audio_browser import PlaylistBrowser
from ui.audio_detail import PlaylistDetailPage
from ui.audio_player import AudioPlayerBar
from ui.audio_theme import (
    BG_MAIN,
    BG_SIDEBAR,
    BTN_QSS,
)
from ui.audio_threads import AudioScanThread, DlsiteWorker, MtimeMigrationThread
from ui.audio_widgets import AudioSettingsDialog

# 关闭窗口时若扫描线程来不及退出，仍需持有其 Python 引用：
# QThread 没有 Qt parent，失去引用后被 GC 会销毁正在运行的线程对象，导致崩溃。
_detached_threads = set()


class AudioMainWindow(QMainWindow):
    def __init__(self, on_back_to_launcher=None):
        super().__init__()
        self._on_back_to_launcher = on_back_to_launcher
        self.setWindowTitle(f"本地资源管理器 - 音频 v{config.APP_VERSION}")
        self.resize(1080, 700)

        self._current_playlist_id = None
        self._current_track_index = -1
        self._detail_history = []  # 详情页导航栈
        self._detail_group_id = None
        self._detail_entries = {}  # 当前主歌单下含直接音频的子目录
        self._filter_origin = None  # 从详情页进入筛选视图时的导航历史快照（返回时还原）
        self._tracks_data = []
        self._play_queue = []  # 独立播放队列（导航/切歌单时保持播放）
        self._mtime_thread = None  # 后台 mtime 迁移线程
        self._dlsite_thread = None  # DLsite 信息后台抓取线程
        self.scan_thread = None  # 音频扫描线程（沿用既有命名）
        self._current_dlsite_rj = None  # 当前详情页歌单对应的 RJ 码
        self._browser_refresh_deferred = False  # 详情页期间推迟的列表刷新

        db.init_db()
        if dlsite.AVAILABLE:
            try:
                dlsite_db.init_db()
            except Exception as e:
                print(f"[DLsite] 数据库初始化失败: {e}")

        # 播放器（延迟初始化，避免阻塞窗口显示）
        self._player = None
        self._audio_output = None
        self._player_initialized = False

        self.setStyleSheet(f"QMainWindow {{ background-color: {BG_MAIN}; }}")
        self._setup_ui()
        self._setup_shortcuts()
        # 已下载的 DLsite 封面先同步到歌单库，再创建首屏卡片。
        # 否则失效的旧路径要等自动扫描结束及延迟刷新后才会被替换。
        if dlsite.AVAILABLE:
            self._sync_dlsite_info()
        self._refresh_playlists()

        # 自动后台扫描（延迟 500ms，让 UI 先渲染）
        self._auto_scanning = True
        QTimer.singleShot(500, self.start_scan)

    # ==================== 播放器（延迟初始化） ====================
    @property
    def player(self):
        self._ensure_player()
        return self._player

    @property
    def audio_output(self):
        self._ensure_player()
        return self._audio_output

    def _ensure_player(self):
        """延迟初始化 QMediaPlayer（首次使用时创建，避免阻塞窗口显示）"""
        if self._player_initialized:
            return
        self._player_initialized = True
        self._player = QMediaPlayer()
        self._audio_output = QAudioOutput()
        self._player.setAudioOutput(self._audio_output)
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_player_error)
        self._audio_output.setVolume(0.8)
        # 记录当前绑定的播放设备，并监听系统音频设备变化（切换默认设备后重建输出）
        self._bound_device_id = QMediaDevices.defaultAudioOutput().id()
        self._media_devices = QMediaDevices(self)
        self._media_devices.audioOutputsChanged.connect(self._on_audio_devices_changed)

    def _on_audio_devices_changed(self):
        """系统音频设备列表变化（拔插/切换设备）：重建音频输出以跟随新设备"""
        if not self._player_initialized:
            return
        self._rebind_audio_output(force=True)

    def _rebind_audio_output(self, force=False):
        """把 QAudioOutput 重绑到系统当前默认播放设备

        Windows 上 Qt 的 WMF 后端会把音频流绑定到开播时的设备端点，
        不会跟随系统默认播放设备切换，导致切换设备后声音仍发往旧设备（表现为无声）。
        因此在设备列表变化或每次开播前，显式把输出重绑到当前默认设备。
        注：不能通过新建 QAudioOutput 替换（Qt 6.11 实测替换会丢绑定），
        直接对现有输出 setDevice 即可，播放中重绑不中断。
        """
        self._ensure_player()  # 首次开播时播放器可能尚未初始化
        default_dev = QMediaDevices.defaultAudioOutput()
        if not force and default_dev.id() == self._bound_device_id:
            return
        self._audio_output.setDevice(default_dev)
        self._bound_device_id = default_dev.id()

    # ==================== UI 构建 ====================
    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---- 顶部栏 ----
        toolbar = QFrame()
        toolbar.setFixedHeight(44)
        toolbar.setStyleSheet(f"background-color: {BG_SIDEBAR}; border: none;")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(8, 4, 8, 4)
        toolbar_layout.setSpacing(8)

        back_btn = QPushButton("← 返回主界面")
        back_btn.setStyleSheet(BTN_QSS)
        back_btn.clicked.connect(self._back_to_launcher)
        toolbar_layout.addWidget(back_btn)
        toolbar_layout.addSpacing(12)

        scan_btn = QPushButton("扫描")
        scan_btn.setStyleSheet(BTN_QSS)
        scan_btn.clicked.connect(self.start_scan)
        toolbar_layout.addWidget(scan_btn)

        settings_btn = QPushButton("设置")
        settings_btn.setStyleSheet(BTN_QSS)
        settings_btn.clicked.connect(self.open_settings)
        toolbar_layout.addWidget(settings_btn)
        toolbar_layout.addStretch()
        layout.addWidget(toolbar)

        # ---- 页面切换（歌单浏览 / 曲目详情） ----
        self.page_stack = QStackedWidget()
        layout.addWidget(self.page_stack, 1)

        # — 页面 0: 歌单浏览器
        self.browser = PlaylistBrowser()
        self.browser.openPlaylist.connect(self._open_playlist)
        self.browser.returnToPlaylist.connect(self._return_from_filter_view)
        self.page_stack.addWidget(self.browser)  # index 0

        # — 页面 1: 曲目详情页
        self.detail = PlaylistDetailPage()
        self.detail.backClicked.connect(self._detail_back)
        self.detail.subPlaylistClicked.connect(self._open_sub_playlist)
        self.detail.tagClicked.connect(self._on_tag_clicked)
        self.detail.dlsiteFieldClicked.connect(self._on_dlsite_field_clicked)
        self.detail.trackDoubleClicked.connect(self._on_track_double_clicked)
        self.page_stack.addWidget(self.detail)  # index 1

        # 复用浏览器的分页栏，详情页也能在同一位置显示后台任务。
        layout.addWidget(self.browser.page_bar)
        self.background_status = self.browser.background_status
        self.background_status.cancelRequested.connect(self._cancel_scan)
        self.background_status.logRequested.connect(self._show_dlsite_log)
        self.background_status.activityChanged.connect(self._update_footer_visibility)
        self.page_stack.currentChanged.connect(self._update_footer_visibility)

        # ---- 播放栏 ----
        self.player_bar = AudioPlayerBar()
        self.player_bar.prevClicked.connect(self.play_previous)
        self.player_bar.toggleClicked.connect(self.toggle_play)
        self.player_bar.nextClicked.connect(self.play_next)
        self.player_bar.seekRequested.connect(self._seek)
        self.player_bar.volumeChanged.connect(self._on_volume_changed)
        self.player_bar.nowPlayingClicked.connect(self._on_now_playing_clicked)
        layout.addWidget(self.player_bar)
        self.player_bar.setVisible(False)

        self._dlsite_logs = []            # 抓取日志缓冲（带时间戳）
        self._dlsite_log_dialog = None    # 日志窗口（懒创建）
        self._scan_hide_timer = QTimer(self)
        self._scan_hide_timer.setSingleShot(True)
        self._scan_hide_timer.timeout.connect(self._clear_scan_status)
        self._dlsite_hide_timer = QTimer(self)
        self._dlsite_hide_timer.setSingleShot(True)
        self._dlsite_hide_timer.timeout.connect(self._clear_dlsite_status)

    def _clear_scan_status(self):
        self.background_status.clear_task("scan")

    def _clear_dlsite_status(self):
        self.background_status.clear_task("dlsite")

    def _update_footer_visibility(self):
        browsing = self.page_stack.currentIndex() == 0
        self.browser.pagination_controls.setVisible(browsing)
        self.browser.page_bar.setVisible(browsing or self.background_status.has_tasks())

    # ==================== 数据加载 ====================
    def _refresh_playlists(self):
        self.browser.reset()  # 回到根目录刷新

    def _detail_back(self):
        """详情页返回按钮：返回上一级详情或歌单列表"""
        if len(self._detail_history) > 1:
            self._detail_history.pop()
            self._open_playlist(self._detail_history[-1])
        else:
            self._detail_history = []
            self.page_stack.setCurrentIndex(0)
            # 返回列表时补上详情页期间推迟的刷新（避免用户正要看列表时卡片被重绘）
            if getattr(self, "_browser_refresh_deferred", False):
                self._browser_refresh_deferred = False
                self._schedule_browser_refresh()

    @staticmethod
    def _is_path_within(child_path: str, parent_path: str) -> bool:
        """child_path 是否等于 parent_path 或位于其子级（兼容 / 与 \\ 分隔符）"""
        if child_path == parent_path:
            return True
        return child_path.startswith((parent_path + os.sep, parent_path + "/"))

    def _build_ancestor_chain(self, pl):
        """从叶子歌单沿 parent_path 回溯到根，返回 [根, ..., 叶子] 的歌单 id 链"""
        chain = [pl["id"]]
        parent_path = pl.get("parent_path") or ""
        guard = 0
        while parent_path and guard < 64:
            parent = db.get_playlist_by_path(parent_path)
            if not parent:
                break
            chain.insert(0, parent["id"])
            parent_path = parent.get("parent_path") or ""
            guard += 1
        return chain

    @staticmethod
    def _collect_tracks_recursive(pl_id):
        """收集歌单及其所有后代歌单的全部曲目（一次CTE + 一次批量查询替代递归N+1）"""
        pl = db.get_playlist(pl_id)
        if not pl:
            return []
        descendants = db.get_descendant_playlists(pl["path"])
        if not descendants:
            return db.list_tracks(pl_id)
        all_ids = [pl_id] + [d["id"] for d in descendants]
        return db.list_tracks_by_playlist_ids(all_ids)

    @staticmethod
    def _find_first_cover(pl):
        """查找歌单或其子歌单的第一个可用封面（CTE后代查询替代递归N+1）"""
        cover = pl.get("cover")
        if cover and os.path.exists(cover):
            return cover
        descendants = db.get_descendant_playlists(pl["path"])
        for child in descendants:
            cover = child.get("cover")
            if cover and os.path.exists(cover):
                return cover
        return None

    def _open_playlist(self, pl_id, history=None):
        # 主列表进入时先打开歌单分组；在其详情页点子歌单时只追加该子目录。
        if history is not None:
            self._detail_history = list(history)
        elif self.page_stack.currentIndex() != 1:
            self._detail_history = [pl_id]
        elif pl_id != self._detail_history[-1]:
            self._detail_history.append(pl_id)

        self._current_playlist_id = pl_id
        # 注意：这里不再 stop() —— 播放队列与展示列表解耦，导航/返回时保持播放

        group_id = self._detail_history[0] if self._detail_history else pl_id
        group = db.get_playlist(group_id)
        if not group:
            return
        if self._detail_group_id != group_id or pl_id == group_id:
            descendants = db.get_descendant_playlists(group["path"])
            direct_counts = db.count_tracks_by_playlist()
            direct_counts.pop(group_id, None)  # 主歌单本身不重复出现在自己的子歌单列表里
            entries = build_audio_catalog(
                [group, *descendants], direct_counts, [group["path"]],
            )
            self._detail_entries = {entry["id"]: entry for entry in entries}
            self._detail_group_id = group_id
        pl = group if pl_id == group_id else self._detail_entries.get(pl_id) or db.get_playlist(pl_id)
        if not pl:
            return

        # 更新返回按钮文字
        if len(self._detail_history) <= 1:
            self.detail.set_back_label("← 返回歌单列表")
        else:
            self.detail.set_back_label("← 返回上级")

        # 子歌单展示记录已沿目录路径继承最近的封面、标签和 RJ 码。
        tags_str = pl.get("tags", "") or ""
        # 网络分类优先：已缓存的 DLsite 分类直接替代数据库标签。
        if dlsite.AVAILABLE:
            main_rj = pl.get("rj_code") or dlsite.extract_rj_code(pl.get("name", ""))
            if main_rj:
                try:
                    main_info = dlsite_db.get_work(main_rj)
                except Exception:
                    main_info = None
                if main_info and main_info.get("genres"):
                    tags_str = "，".join(str(g) for g in main_info["genres"])

        cover_path = pl.get("cover")
        if pl_id == group_id and not (cover_path and os.path.exists(cover_path)):
            cover_path = next((entry["cover"] for entry in self._detail_entries.values()
                               if entry.get("cover")), None)
        self._tracks_data = db.list_tracks(pl_id)
        sub_playlists = (sorted(self._detail_entries.values(), key=lambda entry: entry["name"].lower())
                         if pl_id == group_id else [])
        self.detail_display(pl, tags_str, cover_path, sub_playlists)
        self.page_stack.setCurrentIndex(1)
        # 若当前正在播放的曲目属于刚展示的列表，高亮它
        self._highlight_current_in_list()

    def detail_display(self, pl, tags_str, cover_path, descendants):
        """把组装好的数据交给详情页渲染"""
        # DLsite 信息：歌单名含 RJ 码时先查缓存；封面缺失时用 DLsite 封面回退
        rj = (pl.get("rj_code") or dlsite.extract_rj_code(pl["name"])) if dlsite.AVAILABLE else None
        self._current_dlsite_rj = rj
        info = None
        if rj:
            try:
                info = dlsite_db.get_work(rj)
            except Exception:
                info = None
        cached_ok = bool(info and info.get("title") and not info.get("error"))
        if cached_ok and not (cover_path and os.path.exists(cover_path)):
            cp = info.get("cover_path")
            if cp and os.path.exists(cp):
                cover_path = cp

        self.detail.display(
            pl["name"], cover_path, tags_str,
            self._tracks_data, descendants,
        )

        # DLsite 信息区（display 内部会 clear，需在其后填充）
        if cached_ok:
            self.detail.show_dlsite_info(info)
        elif rj:
            # 未命中缓存：显示占位并插队优先抓取，完成后 _on_dlsite_fetched 回调刷新
            self.detail.show_dlsite_pending(rj)
            self._ensure_dlsite_thread()
            self._dlsite_thread.request(rj, priority=True)

    # ==================== DLsite 后台抓取 ====================
    def _ensure_dlsite_thread(self):
        if self._dlsite_thread and self._dlsite_thread.isRunning():
            return
        self._dlsite_thread = DlsiteWorker()
        self._dlsite_thread.fetched.connect(self._on_dlsite_fetched)
        self._dlsite_thread.progress.connect(self._on_dlsite_progress)
        self._dlsite_thread.log.connect(self._on_dlsite_log)
        self._dlsite_thread.start()

    def _start_dlsite_prefetch(self):
        """扫描完成后：先同步缓存信息（封面+标签），再把未获取过信息的歌单加入并行预取队列

        本地批量校验：与 dlsite.db 已成功缓存的全量 RJ 码集合做差集，
        只把缺失的 ID 入队——已获取过的绝不重复请求。
        """
        if not dlsite.AVAILABLE:
            return
        # 启动即同步：dlsite.db 里已有的封面/标签立即应用到主列表（不依赖抓取信号）
        if self._sync_dlsite_info():
            self._schedule_browser_refresh()
        try:
            codes = set()
            for pl in db.list_playlists():
                rj = dlsite.extract_rj_code(pl.get("name", ""))
                if rj:
                    codes.add(rj)
        except Exception as e:
            print(f"[DLsite] 预取收集失败: {e}")
            return
        if not codes:
            return
        # 批量校验已缓存 ID，只抓缺失的
        try:
            missing = codes - dlsite_db.get_cached_rjs()
        except Exception as e:
            print(f"[DLsite] 缓存校验失败，全量入队: {e}")
            missing = codes
        if not missing:
            return
        self._ensure_dlsite_thread()
        self._dlsite_thread.request_many(missing)

    def _sync_dlsite_info(self, rj=None, info=None):
        """把 dlsite.db 缓存的信息同步到主库（网络信息统一管理）

        - 封面：本地无封面文件的歌单回填 DLsite 封面（本地封面优先，已有则不覆盖）
        - 标签：有网络分类的歌单统一使用网络标签（覆盖本地 标签.txt），全半角逗号分隔
        - rj=None 时全量同步（扫描完成后调用一次）；指定 rj 时只精准同步该 RJ 的歌单
          （每次抓取完成后调用，避免全表扫描+逐条写库阻塞 UI 线程）
        重扫描后扫描器会把 tags 重置为本地值，全量同步在每次扫描后都会再次执行，保证收敛。
        返回是否有任何写入。
        """
        try:
            if rj:
                work = info if info is not None else dlsite_db.get_work(rj)
                cover_path = (work or {}).get("cover_path")
                cover_map = {rj: cover_path} if cover_path and os.path.exists(cover_path) else {}
                genres = (work or {}).get("genres")
                valid = bool(work and work.get("title") and not work.get("error"))
                genre_map = {rj: genres} if valid and isinstance(genres, list) and genres else {}
            else:
                cover_map = dlsite_db.get_cover_map()
                genre_map = dlsite_db.get_genre_map()
            if not cover_map and not genre_map:
                return False
            pls = db.list_playlists_by_rj(rj) if rj else db.list_playlists()
            updates = []
            for pl in pls:
                pl_rj = dlsite.extract_rj_code(pl.get("name", ""))
                if rj and pl_rj != rj:
                    continue
                fields = {}
                # 封面回填
                cover = pl.get("cover")
                if not (cover and os.path.exists(cover)):
                    cp = cover_map.get(pl_rj)
                    if cp:
                        fields["cover"] = cp
                # 网络标签统一（网络分类替代本地标签）
                genres = genre_map.get(pl_rj)
                if genres:
                    tags_str = "，".join(str(g) for g in genres)
                    if (pl.get("tags") or "") != tags_str:
                        fields["tags"] = tags_str
                elif pl_rj and (pl.get("tags") or ""):
                    # 有 RJ 码但还没有网络数据：清掉残留的本地 标签.txt 内容
                    fields["tags"] = ""
                if fields:
                    updates.append((pl["id"], fields))
            if updates:
                db.update_playlists_batch(updates)  # 单连接单事务，UI 无感
            return bool(updates)
        except Exception as e:
            print(f"[DLsite] 信息同步失败: {e}")
            return False

    # ── DLsite 进度与日志 ──
    def _on_dlsite_progress(self, done, total, action):
        """在分页栏更新信息抓取进度。"""
        if total <= 0:
            return
        if done >= total:
            self.background_status.set_task(
                "dlsite", 100, "信息就绪", f"DLsite 信息全部完成（{total} 个）",
            )
            self._dlsite_hide_timer.start(4000)  # 完成后停留 4 秒再隐藏
        else:
            self._dlsite_hide_timer.stop()
            self.background_status.set_task(
                "dlsite", int(done * 100 / total), "拉取信息",
                f"DLsite 信息: {done}/{total}\n{action}",
            )

    def _on_dlsite_log(self, line):
        """接收 worker 日志行（带时间戳缓冲，日志窗口打开时实时追加）"""
        self._dlsite_logs.append(f"{datetime.now():%H:%M:%S}  {line}")
        if len(self._dlsite_logs) > 1000:
            del self._dlsite_logs[:-1000]
        if self._dlsite_log_dialog and self._dlsite_log_dialog.isVisible():
            self._dlsite_log_text.appendPlainText(self._dlsite_logs[-1])

    def _show_dlsite_log(self):
        """打开抓取日志窗口（懒创建，单实例）"""
        if self._dlsite_log_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("DLsite 抓取日志")
            dlg.resize(580, 400)
            v = QVBoxLayout(dlg)
            txt = QPlainTextEdit()
            txt.setReadOnly(True)
            txt.setMaximumBlockCount(5000)
            txt.setStyleSheet("QPlainTextEdit { background: #FFFFFF; color: #555; font-size: 12px; border: none; }")
            v.addWidget(txt)
            self._dlsite_log_dialog = dlg
            self._dlsite_log_text = txt
        self._dlsite_log_text.setPlainText("\n".join(self._dlsite_logs))
        sb = self._dlsite_log_text.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._dlsite_log_dialog.show()
        self._dlsite_log_dialog.raise_()

    def _on_dlsite_fetched(self, rj):
        """后台抓取完成回调：封面回填主列表 + 刷新当前详情页"""
        try:
            info = dlsite_db.get_work(rj)
        except Exception:
            info = None
        ok = bool(info and info.get("title") and not info.get("error"))

        # 新抓到的封面/标签立即精准同步到主列表（只处理该 RJ，批量写库不卡 UI）
        if ok and self._sync_dlsite_info(rj, info):
            self._schedule_browser_refresh()

        # 若抓取的正是当前展示的歌单，立即刷新详情页信息区与封面
        if rj != self._current_dlsite_rj or self.page_stack.currentIndex() != 1:
            return
        if ok:
            self.detail.show_dlsite_info(info)
            self.detail.try_dlsite_cover(info.get("cover_path"))
        else:
            self.detail.show_dlsite_error()

    def _schedule_browser_refresh(self):
        """合并短时间内的多次刷新请求（预取期间会连续回填多个封面）

        用户停留在详情页时推迟刷新：列表不可见时重绘纯属浪费，且卡片重绘
        发生在用户返回列表的瞬间容易造成点击落点偏移误开歌单；返回列表时统一补刷。
        """
        if self.page_stack.currentIndex() == 1:
            self._browser_refresh_deferred = True
            return
        if getattr(self, "_browser_refresh_pending", False):
            return
        self._browser_refresh_pending = True

        def _do():
            self._browser_refresh_pending = False
            self.browser.reload_current()

        QTimer.singleShot(1000, _do)

    def _collect_descendant_playlists(self, path):
        """收集指定路径下的所有后代歌单（递归CTE，一次SQL查询）"""
        return db.get_descendant_playlists(path)

    def _open_sub_playlist(self, pl_id):
        """点击详情页内的子歌单卡片"""
        self._open_playlist(pl_id)

    def _on_tag_clicked(self, tag):
        """点击标签按钮 → 返回歌单浏览器并追加标签过滤（与已有标签叠加 AND 逻辑）"""
        self._enter_filter_view()
        self.browser.filter_by_tag(tag)

    def _on_dlsite_field_clicked(self, field, value):
        """点击社团/CV 胶囊 → 返回浏览器并按该维度过滤（同值再点取消，异值替换）"""
        self._enter_filter_view()
        self.browser.filter_by_dlsite(field, value)

    def _enter_filter_view(self):
        """从详情页进入筛选视图：记录来源歌单，供列表页返回按钮/擦除筛选后回退

        筛选视图是详情页之上的一层临时状态，必须记住"从哪个歌单来的"，
        否则返回按钮只能按目录层级回退（根目录下甚至没有返回入口）。
        """
        if self.page_stack.currentIndex() == 1 and self._detail_history:
            pl_id = self._detail_history[-1]
            self._filter_origin = list(self._detail_history)
            self.browser.set_return_target(pl_id, self.detail.playlist_title.text())
        else:
            self._filter_origin = None
            self.browser.clear_return_target()
        self.page_stack.setCurrentIndex(0)

    def _return_from_filter_view(self, pl_id):
        """筛选视图返回：还原来源歌单详情页及其原有层级导航历史"""
        history = self._filter_origin or [pl_id]
        self._filter_origin = None
        self._open_playlist(pl_id, history=history)

    # ==================== 播放控制 ====================
    def _on_track_double_clicked(self, index):
        self._play_track(index)

    def _play_track(self, index):
        # 用户显式双击某首歌：以当前展示列表快照为独立播放队列起点
        if index < 0 or index >= len(self._tracks_data):
            return
        self._play_queue = list(self._tracks_data)
        self._play_from_queue(index)

    def _play_from_queue(self, index):
        """从独立播放队列播放指定位置的曲目（供双击/上一首/下一首/恢复共用）"""
        if index < 0 or index >= len(self._play_queue):
            return
        self._current_track_index = index
        track = self._play_queue[index]
        path = track["path"]
        if not os.path.exists(path):
            QMessageBox.warning(self, "文件不存在", f"找不到文件: {path}")
            return
        self._rebind_audio_output()  # 开播前校验默认播放设备是否已切换
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()
        self.player_bar.set_playing(True)
        title = track["title"] or os.path.basename(path)
        display = title[:10] + "..." if len(title) > 10 else title
        self.player_bar.set_now_playing(f"♪ {display}")
        self.player_bar.setVisible(True)
        self._highlight_current_in_list()

    def _highlight_current_in_list(self):
        """高亮展示列表中当前正在播放的曲目"""
        if not self._play_queue or self._current_track_index < 0 \
                or self._current_track_index >= len(self._play_queue):
            return
        playing_path = self._play_queue[self._current_track_index]["path"]
        self.detail.set_highlight_by_path(playing_path)

    def toggle_play(self):
        state = self.player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.player_bar.set_playing(False)
        elif state == QMediaPlayer.PlaybackState.PausedState:
            self.player.play()
            self.player_bar.set_playing(True)
        else:
            if self._play_queue and self._current_track_index >= 0:
                self._play_from_queue(self._current_track_index)
            elif self._tracks_data:
                self._play_track(0)

    def play_next(self):
        if not self._play_queue:
            return
        idx = self._current_track_index + 1
        if idx >= len(self._play_queue):
            idx = 0
        self._play_from_queue(idx)

    def play_previous(self):
        if not self._play_queue:
            return
        idx = self._current_track_index - 1
        if idx < 0:
            idx = len(self._play_queue) - 1
        self._play_from_queue(idx)

    def _on_position_changed(self, pos_ms):
        self.player_bar.set_position(pos_ms // 1000)

    def _on_duration_changed(self, dur_ms):
        self.player_bar.set_duration(int(dur_ms // 1000))

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.play_next()

    def _on_player_error(self, error, error_string):
        if error != QMediaPlayer.Error.NoError:
            self.player_bar.set_now_playing(f"播放错误: {error_string}")

    def _seek(self, pos):
        """拖动/点击进度条时跳转"""
        self.player.setPosition(pos * 1000)

    def _on_volume_changed(self, val):
        self.audio_output.setVolume(val / 100.0)

    def _on_now_playing_clicked(self):
        """点击播放条曲目名 → 回到当前歌单，或按层级跳转到曲目所属歌单"""
        if not self._play_queue or self._current_track_index < 0 \
                or self._current_track_index >= len(self._play_queue):
            return
        track = self._play_queue[self._current_track_index]
        target = db.get_playlist(track.get("playlist_id")) if track.get("playlist_id") else None
        if not target:
            return  # 歌单已被删除等脏数据，忽略

        # 每张卡片只对应本目录曲目；即使目标位于当前目录下，也要打开它自己的歌单。
        if self._current_playlist_id == target["id"]:
            self.page_stack.setCurrentIndex(1)
            return

        chain = self._build_ancestor_chain(target)
        history = [chain[0], target["id"]] if chain[0] != target["id"] else [target["id"]]
        self._open_playlist(target["id"], history=history)

    # ==================== 扫描 ====================
    def start_scan(self):
        if getattr(self, "scan_thread", None) and self.scan_thread.isRunning():
            return
        self._scan_hide_timer.stop()
        self._scan_cancelling = False
        self._scan_percent = 0
        self.background_status.set_task("scan", 0, "准备扫描", "正在检查音频目录", True)

        self.scan_thread = AudioScanThread(config.AUDIO_ROOTS)
        self.scan_thread.finished.connect(self._on_scan_finished)
        self.scan_thread.error.connect(self._on_scan_error)
        self.scan_thread.progress.connect(self._on_scan_progress)
        self.scan_thread.start()

    def _cancel_scan(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.cancel()
            self._scan_cancelling = True
            self.background_status.set_task(
                "scan", self._scan_percent, "正在取消", "等待扫描线程结束",
            )

    def _on_scan_progress(self, current, total, msg):
        if getattr(self, "_scan_cancelling", False):
            return
        self._scan_percent = int(current * 100 / total) if total > 0 else 0
        # 收尾仍有数据库整理工作，线程完成前保留最后 1%。
        self._scan_percent = max(0, min(99, self._scan_percent))
        text = "正在整理" if "整理" in msg or "扫描完成" in msg else "正在扫描"
        self.background_status.set_task("scan", self._scan_percent, text, msg, True)

    def _on_scan_finished(self, stats):
        auto = getattr(self, "_auto_scanning", False)
        self._auto_scanning = False
        cancelling = getattr(self, "_scan_cancelling", False)
        self._scan_cancelling = False

        # 指纹未变化，跳过扫描
        if stats.get("unchanged"):
            if auto:
                self.background_status.clear_task("scan")
            else:
                self.background_status.set_task("scan", 100, "目录未变", "目录无变化，已跳过扫描")
                self._scan_hide_timer.start(2000)
            self._start_mtime_migration()
            self._start_dlsite_prefetch()
            return

        if cancelling:
            self.background_status.clear_task("scan")
        else:
            self.background_status.set_task("scan", 100, "扫描完成", "音频目录扫描完成")
            self._scan_hide_timer.start(2000)
        self._refresh_playlists()
        self._start_mtime_migration()
        self._start_dlsite_prefetch()
        if auto or cancelling:
            return  # 自动扫描或用户取消，静默完成
        msg_parts = []
        for k, v in [("added_playlists", "新增歌单"), ("removed_playlists", "删除歌单"),
                     ("added_tracks", "新增曲目"), ("removed_tracks", "删除曲目")]:
            if stats.get(k):
                msg_parts.append(f"{v}: {stats[k]}")
        msg = "扫描完成\n\n" + "\n".join(msg_parts) if msg_parts else "扫描完成\n\n无变化"
        QMessageBox.information(self, "扫描完成", msg)

    def _on_scan_error(self, msg):
        self.background_status.set_task("scan", 0, "扫描失败", msg)
        self._scan_hide_timer.start(4000)
        auto = getattr(self, "_auto_scanning", False)
        self._auto_scanning = False
        if auto:
            return  # 自动扫描，静默处理错误
        QMessageBox.critical(self, "扫描失败", msg)

    # ==================== mtime 后台迁移 ====================
    def _start_mtime_migration(self):
        """启动后台 mtime 迁移线程（若已在运行则跳过）"""
        if self._mtime_thread and self._mtime_thread.isRunning():
            return
        self._mtime_thread = MtimeMigrationThread()
        self._mtime_thread.migrated.connect(self._on_mtime_migrated)
        self._mtime_thread.start()

    def _on_mtime_migrated(self, count):
        """mtime 迁移完成后静默刷新当前视图（仅当有迁移发生时）"""
        if count > 0:
            self.browser.reload_current()

    # ==================== 设置 / 返回 ====================
    def open_settings(self):
        AudioSettingsDialog(self).exec()

    def _back_to_launcher(self):
        if self._player_initialized:
            self._player.stop()
        self.close()

    def closeEvent(self, event):
        self._scan_hide_timer.stop()
        self._dlsite_hide_timer.stop()
        if self._player_initialized:
            self._player.stop()
        # 先停扫描线程：它持有数据库长连接，且会向即将关闭的窗口发信号
        self._stop_scan_thread()
        # 等待后台 mtime 迁移线程结束，避免向已删除的窗口发射信号
        if self._mtime_thread and self._mtime_thread.isRunning():
            self._mtime_thread.wait(2000)
        # 停止 DLsite 后台抓取线程（可能正在礼貌延迟中，wait 足够覆盖一个延迟周期）
        if self._dlsite_thread and self._dlsite_thread.isRunning():
            self._dlsite_thread.stop()
            self._dlsite_thread.wait(3000)
        super().closeEvent(event)
        if self._on_back_to_launcher:
            cb = self._on_back_to_launcher
            self._on_back_to_launcher = None
            cb()

    def _stop_scan_thread(self):
        """取消并等待扫描线程退出（幂等：线程未启动/已结束时安全）"""
        t = self.scan_thread
        if t is None or not t.isRunning():
            return
        t.cancel()
        if t.wait(3000):
            return
        # 兜底：扫描仍未在 3 秒内收尾。断开信号避免回调到已关闭的窗口，
        # 并用模块级集合持有引用防止 QThread 被 GC（见 _detached_threads 注释）。
        # 没有连接的信号 disconnect() 会抛 TypeError，逐个吞掉
        for sig in (t.finished, t.error, t.progress):
            with suppress(TypeError):
                sig.disconnect()
        _detached_threads.add(t)
        # 注意：AudioScanThread.finished 是 pyqtSignal(dict)，槽必须能接住这个参数
        t.finished.connect(lambda *_: _detached_threads.discard(t))
        print("[关闭] 音频扫描线程未在 3 秒内退出，已断开其信号并保持引用直到结束")

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Space"), self, self.toggle_play)
        QShortcut(QKeySequence("Left"), self, self.play_previous)
        QShortcut(QKeySequence("Right"), self, self.play_next)
        QShortcut(QKeySequence("Ctrl+S"), self, self.start_scan)
