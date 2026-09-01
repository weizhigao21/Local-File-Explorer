"""
音频模块主窗口
组装歌单浏览器、歌单详情页、播放条与扫描逻辑
"""
import os
from datetime import datetime

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFrame, QMessageBox, QStackedWidget,
    QDialog, QPlainTextEdit, QProgressBar,
)
from PyQt6.QtCore import QUrl, QTimer
from PyQt6.QtGui import QShortcut, QKeySequence
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaDevices

from audio_manager import database as db
from audio_manager import dlsite, dlsite_db
from resource_manager import config
from ui.audio_theme import (
    BG_MAIN, BG_SIDEBAR, TEXT_PRIMARY, BTN_QSS,
)
from ui.audio_widgets import AudioSettingsDialog
from ui.audio_browser import PlaylistBrowser
from ui.audio_player import AudioPlayerBar
from ui.audio_detail import PlaylistDetailPage
from ui.audio_threads import AudioScanThread, MtimeMigrationThread, DlsiteWorker


class AudioMainWindow(QMainWindow):
    def __init__(self, on_back_to_launcher=None):
        super().__init__()
        self._on_back_to_launcher = on_back_to_launcher
        self.setWindowTitle("本地资源管理器 - 音频")
        self.resize(1080, 700)

        self._current_playlist_id = None
        self._current_track_index = -1
        self._detail_history = []  # 详情页导航栈
        self._tracks_data = []
        self._play_queue = []  # 独立播放队列（导航/切歌单时保持播放）
        self._mtime_thread = None  # 后台 mtime 迁移线程
        self._dlsite_thread = None  # DLsite 信息后台抓取线程
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
        self.page_stack.addWidget(self.browser)  # index 0

        # — 页面 1: 曲目详情页
        self.detail = PlaylistDetailPage()
        self.detail.backClicked.connect(self._detail_back)
        self.detail.subPlaylistClicked.connect(self._open_sub_playlist)
        self.detail.tagClicked.connect(self._on_tag_clicked)
        self.detail.trackDoubleClicked.connect(self._on_track_double_clicked)
        self.page_stack.addWidget(self.detail)  # index 1

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

        # 扫描状态栏
        self._scan_bar = self._build_scan_bar()
        layout.addWidget(self._scan_bar)
        self._scan_bar.setVisible(False)

        # DLsite 抓取状态栏（进度 + 日志入口）
        self._dlsite_bar = self._build_dlsite_bar()
        layout.addWidget(self._dlsite_bar)
        self._dlsite_bar.setVisible(False)
        self._dlsite_logs = []            # 抓取日志缓冲（带时间戳）
        self._dlsite_log_dialog = None    # 日志窗口（懒创建）
        self._dlsite_hide_timer = QTimer(self)
        self._dlsite_hide_timer.setSingleShot(True)
        self._dlsite_hide_timer.timeout.connect(self._dlsite_bar.hide)

    def _build_dlsite_bar(self):
        bar = QFrame()
        bar.setFixedHeight(36)
        bar.setStyleSheet(f"""
            QFrame {{ background-color: {BG_SIDEBAR}; border-top: 1px solid #EDE6DA; }}
            QLabel {{ color: #777; font-size: 11px; padding: 2px; }}
            QProgressBar {{
                border: none; background: #EDE6DA; border-radius: 2px;
                text-align: center; color: {TEXT_PRIMARY}; font-size: 10px; height: 12px;
            }}
            QProgressBar::chunk {{ background-color: #8FA9C2; border-radius: 2px; }}
        """)
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(8)
        self.dlsite_status_label = QLabel("DLsite 信息抓取中...")
        self.dlsite_status_label.setFixedWidth(340)
        self.dlsite_progress_bar = QProgressBar()
        self.dlsite_progress_bar.setFixedWidth(260)
        self.dlsite_progress_bar.setRange(0, 1)
        self.dlsite_progress_bar.setValue(0)
        log_btn = QPushButton("日志")
        log_btn.setStyleSheet(BTN_QSS)
        log_btn.setFixedWidth(50)
        log_btn.clicked.connect(self._show_dlsite_log)
        row.addWidget(self.dlsite_status_label)
        row.addWidget(self.dlsite_progress_bar)
        row.addStretch()
        row.addWidget(log_btn)
        return bar

    def _build_scan_bar(self):
        bar = QFrame()
        bar.setFixedHeight(36)
        from PyQt6.QtWidgets import QHBoxLayout, QProgressBar
        from ui.audio_theme import ACCENT
        bar.setStyleSheet(f"""
            QFrame {{ background-color: {BG_SIDEBAR}; border-top: 1px solid #EDE6DA; }}
            QLabel {{ color: #777; font-size: 11px; padding: 2px; }}
            QProgressBar {{
                border: none; background: #EDE6DA; border-radius: 2px;
                text-align: center; color: {TEXT_PRIMARY}; font-size: 10px; height: 12px;
            }}
            QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 2px; }}
        """)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)
        self.scan_label = QLabel("准备扫描...")
        self.scan_label.setFixedWidth(340)
        self.scan_progress = QProgressBar()
        self.scan_progress.setFixedWidth(260)
        self.scan_progress.setRange(0, 100)
        self.scan_progress.setValue(0)
        self.scan_cancel = QPushButton("取消")
        self.scan_cancel.setStyleSheet(BTN_QSS)
        self.scan_cancel.setFixedWidth(50)
        self.scan_cancel.clicked.connect(self._cancel_scan)
        layout.addWidget(self.scan_label)
        layout.addWidget(self.scan_progress)
        layout.addStretch()
        layout.addWidget(self.scan_cancel)
        return bar

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
        return (child_path.startswith(parent_path + os.sep)
                or child_path.startswith(parent_path + "/"))

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
        # 导航历史：默认首次从浏览器进入时重置、子歌单点击时追加；
        # 传 history 时（如"跳转曲目所属歌单"）直接用预设的祖先链，保证层级完整
        if history is not None:
            self._detail_history = list(history)
        elif self.page_stack.currentIndex() != 1:
            self._detail_history = [pl_id]
        elif pl_id != self._detail_history[-1]:
            self._detail_history.append(pl_id)

        self._current_playlist_id = pl_id
        # 注意：这里不再 stop() —— 播放队列与展示列表解耦，导航/返回时保持播放

        pl = db.get_playlist(pl_id)
        if not pl:
            return

        # 更新返回按钮文字
        if len(self._detail_history) <= 1:
            self.detail.set_back_label("← 返回歌单列表")
        else:
            self.detail.set_back_label("← 返回上级")

        # 标签和封面：子歌单使用主歌单的，主歌单用自己的
        main_pl = db.get_playlist(self._detail_history[0]) if len(self._detail_history) > 1 else pl
        tags_str = main_pl.get("tags", "") or ""
        # 网络分类优先：主歌单已有 DLsite 分类时，直接用它替代本地标签显示
        if dlsite.AVAILABLE:
            main_rj = dlsite.extract_rj_code(main_pl.get("name", ""))
            if main_rj:
                try:
                    main_info = dlsite_db.get_work(main_rj)
                except Exception:
                    main_info = None
                if main_info and main_info.get("genres"):
                    tags_str = "，".join(str(g) for g in main_info["genres"])

        cover_path = main_pl.get("cover")
        if not (cover_path and os.path.exists(cover_path)):
            cover_path = self._find_first_cover(main_pl)

        # 加载曲目：有直接音频则用自身的，否则递归聚合所有子歌单曲目
        direct_tracks = db.list_tracks(pl_id)
        if direct_tracks:
            self._tracks_data = direct_tracks
        else:
            raw = self._collect_tracks_recursive(pl_id)
            self._tracks_data = []
            seen = set()
            for tr in raw:
                if tr["path"] not in seen:
                    seen.add(tr["path"])
                    self._tracks_data.append(tr)

        # 后代歌单列表
        descendants = self._collect_descendant_playlists(pl["path"])
        self.detail_display(pl, tags_str, cover_path, descendants)
        self.page_stack.setCurrentIndex(1)
        # 若当前正在播放的曲目属于刚展示的列表，高亮它
        self._highlight_current_in_list()

    def detail_display(self, pl, tags_str, cover_path, descendants):
        """把组装好的数据交给详情页渲染"""
        # DLsite 信息：歌单名含 RJ 码时先查缓存；封面缺失时用 DLsite 封面回退
        rj = dlsite.extract_rj_code(pl["name"]) if dlsite.AVAILABLE else None
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

    def _sync_dlsite_info(self, rj=None):
        """把 dlsite.db 缓存的信息同步到主库（网络信息统一管理）

        - 封面：本地无封面文件的歌单回填 DLsite 封面（本地封面优先，已有则不覆盖）
        - 标签：有网络分类的歌单统一使用网络标签（覆盖本地 标签.txt），全半角逗号分隔
        - rj=None 时全量同步（扫描完成后调用一次）；指定 rj 时只精准同步该 RJ 的歌单
          （每次抓取完成后调用，避免全表扫描+逐条写库阻塞 UI 线程）
        重扫描后扫描器会把 tags 重置为本地值，全量同步在每次扫描后都会再次执行，保证收敛。
        返回是否有任何写入。
        """
        try:
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
        """更新底部 DLsite 抓取进度条"""
        if total <= 0:
            return
        self._dlsite_bar.setVisible(True)
        if done >= total:
            self.dlsite_status_label.setText(f"DLsite 信息全部完成（{total} 个）")
            self.dlsite_progress_bar.setRange(0, 1)
            self.dlsite_progress_bar.setValue(1)
            self._dlsite_hide_timer.start(4000)  # 完成后停留 4 秒再隐藏
        else:
            self._dlsite_hide_timer.stop()
            text = f"DLsite 信息: {done}/{total}"
            if action:
                text += f"  {action}"
            self.dlsite_status_label.setText(text)
            self.dlsite_progress_bar.setRange(0, total)
            self.dlsite_progress_bar.setValue(done)

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
        if ok and self._sync_dlsite_info(rj):
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
        self.page_stack.setCurrentIndex(0)
        self.browser.filter_by_tag(tag)

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

        # 1) 曲目属于当前展示歌单（或其子孙）→ 切回详情页保持上下文，不重建导航
        if self._current_playlist_id:
            current = db.get_playlist(self._current_playlist_id)
            if current and self._is_path_within(target["path"], current["path"]):
                self.page_stack.setCurrentIndex(1)
                return

        # 2) 跨歌单 → 按祖先链逐层展开（根→...→叶子），封面与返回行为同正常浏览
        chain = self._build_ancestor_chain(target)
        self._open_playlist(target["id"], history=chain)

    # ==================== 扫描 ====================
    def start_scan(self):
        if getattr(self, "scan_thread", None) and self.scan_thread.isRunning():
            return
        auto = getattr(self, "_auto_scanning", False)
        # auto 模式延迟显示扫描条：仅当检测到真实进度（current<total）时才显示；
        # unchanged 时扫描条从头到尾不出现，用户无感。manual 模式立即显示。
        self.scan_progress.setValue(0)
        self.scan_label.setText("准备扫描...")
        self.scan_cancel.setEnabled(True)
        if not auto:
            self._scan_bar.setVisible(True)

        self.scan_thread = AudioScanThread(config.AUDIO_ROOTS)
        self.scan_thread.finished.connect(self._on_scan_finished)
        self.scan_thread.error.connect(self._on_scan_error)
        self.scan_thread.progress.connect(self._on_scan_progress)
        self.scan_thread.start()

    def _cancel_scan(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.cancel()
            self.scan_cancel.setEnabled(False)
            self.scan_label.setText("正在取消...")
            self._scan_cancelling = True
            # 等待扫描线程真正结束，期间"正在取消..."文本保持可见
            self.scan_thread.wait(3000)
        self._scan_bar.setVisible(False)

    def _on_scan_progress(self, current, total, msg):
        auto = getattr(self, "_auto_scanning", False)
        # auto 模式且扫描条未显示：仅真实进度（current<total）才弹出扫描条；
        # unchanged 收尾信号 (1,1) 等无效信号直接吞掉，保持静默。
        if auto and not self._scan_bar.isVisible():
            if total <= 0 or current >= total:
                return
            self._scan_bar.setVisible(True)
        if total > 0:
            self.scan_progress.setValue(int(current / total * 100))
        # 截断过长的文件/文件夹名称，保持标签宽度稳定
        if len(msg) > 42:
            msg = msg[:39] + "..."
        self.scan_label.setText(msg)

    def _on_scan_finished(self, stats):
        auto = getattr(self, "_auto_scanning", False)
        self._auto_scanning = False
        cancelling = getattr(self, "_scan_cancelling", False)
        self._scan_cancelling = False

        # 指纹未变化，跳过扫描
        if stats.get("unchanged"):
            if auto:
                # auto 模式：扫描条从未显示，保持隐藏，完全静默
                pass
            else:
                # manual 模式：扫描条已可见，显示"目录无变化"2秒后隐藏
                self.scan_progress.setValue(100)
                self.scan_label.setText("目录无变化，已跳过扫描")
                QTimer.singleShot(2000, self._scan_bar.hide)
            self._start_mtime_migration()
            self._start_dlsite_prefetch()
            return

        self._scan_bar.setVisible(False)
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
        self._scan_bar.setVisible(False)
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
        if self._player_initialized:
            self._player.stop()
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

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Space"), self, self.toggle_play)
        QShortcut(QKeySequence("Left"), self, self.play_previous)
        QShortcut(QKeySequence("Right"), self, self.play_next)
        QShortcut(QKeySequence("Ctrl+S"), self, self.start_scan)