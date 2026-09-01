"""
音频模块后台线程
扫描线程、mtime 迁移线程、曲目时长探测线程与 DLsite 信息抓取线程
"""
import os
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import QThread, pyqtSignal

from audio_manager import database as db
from audio_manager import scanner as audio_scanner
from audio_manager import dlsite, dlsite_db
from audio_manager.duration import get_duration
from resource_manager import config


class AudioScanThread(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, audio_roots):
        super().__init__()
        self.audio_roots = audio_roots if isinstance(audio_roots, (list, tuple)) else [audio_roots]
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            stats = audio_scanner.scan(
                audio_roots=self.audio_roots,
                progress_callback=self._emit_progress,
                cancel_event=self.cancel_event,
            )
            self.finished.emit(stats or {})
        except Exception as e:
            self.error.emit(str(e))

    def _emit_progress(self, current, total, msg):
        self.progress.emit(current, total, msg)


class MtimeMigrationThread(QThread):
    """后台批量补全旧歌单的 mtime，避免 UI 线程在时间排序时执行文件 IO"""

    migrated = pyqtSignal(int)  # 实际迁移的条目数

    def run(self):
        try:
            pending = db.list_playlists_missing_mtime()
            if not pending:
                self.migrated.emit(0)
                return
            updates = []
            for pid, path in pending:
                try:
                    m = os.path.getmtime(path)
                    updates.append((pid, m))
                except OSError:
                    # 路径不存在（已删除等），保留 mtime=0
                    continue
            if updates:
                db.update_mtimes_batch(updates)
            self.migrated.emit(len(updates))
        except Exception as e:
            print(f"[音频] mtime 迁移失败: {e}")
            traceback.print_exc()
            self.migrated.emit(0)


class DurationProbeThread(QThread):
    """后台并行探测曲目时长，逐个回调主线程更新列表

    由曲目详情页触发；gen 用于区分多次切换歌单，丢弃过期结果。
    """
    resolved = pyqtSignal(int, int, int)  # (gen, index, seconds)

    def __init__(self, tracks, gen=0, parent=None):
        super().__init__(parent)
        self._tracks = list(tracks)
        self._gen = gen
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            paths = [t.get("path") for t in self._tracks]
            with ThreadPoolExecutor(max_workers=4) as ex:
                futures = {ex.submit(get_duration, p): i for i, p in enumerate(paths)}
                for fut in futures:
                    if self._stop.is_set():
                        break
                    try:
                        secs = fut.result()
                    except Exception:
                        secs = 0
                    if secs > 0:
                        self.resolved.emit(self._gen, futures[fut], secs)
        except Exception as e:
            print(f"[音频] 时长探测失败: {e}")
            traceback.print_exc()


class DlsiteWorker(QThread):
    """DLsite 作品信息后台抓取线程（多线程并行）

    架构：QThread 作为管理器，内部启动 WORKER_COUNT 个抓取线程共享同一队列。
    带优先级队列：预取队列（扫描后补齐缺失）+ 用户点击插队。
    缓存策略：入队前由主线程批量校验已缓存 ID（不入队）；抓取线程处理前二次校验
    （防并发竞态 + 覆盖点击路径）；失败记录 24 小时内不重试。
    每处理完一个 RJ 码（无论成功/失败/跳过）都发出 fetched 信号，主线程自查数据库。
    progress/log 信号供主窗口显示抓取进度与日志。
    """
    fetched = pyqtSignal(str)             # rj_code
    progress = pyqtSignal(int, int, str)  # (已处理数, 总数, 当前动作)
    log = pyqtSignal(str)                 # 一行日志

    ERROR_RETRY_SECONDS = 24 * 3600  # 失败记录的最低重试间隔
    REQUEST_DELAY = 2.0              # 每个工作线程对 DLsite 的请求间隔（秒）
    WORKER_COUNT = 4                 # 并行抓取线程数（4线程×2s间隔 ≈ 2个/秒）

    def __init__(self, rj_codes=None, parent=None):
        super().__init__(parent)
        self._pri = deque()          # 用户点击的优先队列
        self._norm = deque()         # 启动预取的普通队列
        self._queued = set()         # 队列内去重
        self._done = 0               # 已处理计数（含跳过）
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        for rj in rj_codes or []:
            self._norm.append(rj)
            self._queued.add(rj)

    # ── 请求入口（线程安全） ──
    def request(self, rj, priority=False):
        """请求抓取一个 RJ 码；priority=True 时插队（用户点击触发）"""
        if not rj:
            return
        with self._lock:
            if rj in self._queued and not priority:
                return
            if priority:
                self._pri.append(rj)
            else:
                self._norm.append(rj)
            self._queued.add(rj)
        self._wake.set()
        self._emit_progress("入队")

    def request_many(self, rj_codes):
        """批量入队普通预取（重扫描后增量补齐）"""
        with self._lock:
            for rj in rj_codes:
                if rj and rj not in self._queued:
                    self._queued.add(rj)
                    self._norm.append(rj)
        self._wake.set()
        self._emit_progress("入队")

    def _emit_progress(self, action):
        """向主线程汇报进度（done/total + 当前动作）"""
        with self._lock:
            done = self._done
            total = self._done + len(self._pri) + len(self._norm)
        self.progress.emit(done, total, action)

    def stop(self):
        self._stop.set()
        self._wake.set()

    def run(self):
        if not dlsite.AVAILABLE:
            return
        try:
            dlsite_db.init_db()
        except Exception as e:
            print(f"[DLsite] 数据库初始化失败: {e}")
            return
        threads = [
            threading.Thread(target=self._fetch_loop, name=f"dlsite-{i}", daemon=True)
            for i in range(self.WORKER_COUNT)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()  # stop() 后各线程很快退出

    def _fetch_loop(self):
        """工作线程主循环：共享队列取任务，逐个处理"""
        while not self._stop.is_set():
            with self._lock:
                if self._pri:
                    rj = self._pri.popleft()
                elif self._norm:
                    rj = self._norm.popleft()
                else:
                    rj = None
            if rj is None:
                self._wake.wait(timeout=5)
                self._wake.clear()
                continue
            with self._lock:
                self._queued.discard(rj)  # 出队后允许再次请求
            self._emit_progress(f"正在获取 {rj}")
            self._process(rj)

    # ── 处理单个 RJ 码 ──
    def _process(self, rj):
        did_network = False
        try:
            now = time.time()
            info = dlsite_db.get_work(rj)
            if info:
                if info.get("title") and not info.get("error"):
                    self.log.emit(f"{rj} 缓存命中，跳过")
                    return
                if info.get("error") and now - (info.get("fetched_at") or 0) < self.ERROR_RETRY_SECONDS:
                    self.log.emit(f"{rj} 近期已失败（{info.get('error')}），24h 内不重试")
                    return
            html = dlsite.fetch(rj)
            did_network = True
            if not html:
                dlsite_db.set_error(rj, "网络请求失败")
                self.log.emit(f"{rj} 抓取失败：网络请求失败")
            else:
                d = dlsite.parse(html, rj)
                if d.get("cover"):
                    cover_path = os.path.join(config.DLSITE_COVER_DIR, f"{rj}.jpg")
                    if dlsite.download_cover(d["cover"], cover_path):
                        d["cover_path"] = cover_path
                d["fetched_at"] = now
                dlsite_db.upsert_work(d)
                title = d.get("title")
                self.log.emit(f"{rj} 抓取成功：{title}" if title else f"{rj} 抓取成功（页面无标题）")
        except Exception as e:
            print(f"[DLsite] 抓取 {rj} 失败: {e}")
            traceback.print_exc()
            try:
                dlsite_db.set_error(rj, str(e))
            except Exception:
                pass
            self.log.emit(f"{rj} 抓取异常：{e}")
        finally:
            with self._lock:
                self._done += 1
            self.fetched.emit(rj)
            self._emit_progress("")
            if did_network:
                # 礼貌延迟，分片睡眠便于及时响应停止
                for _ in range(int(self.REQUEST_DELAY / 0.2)):
                    if self._stop.is_set():
                        break
                    time.sleep(0.2)