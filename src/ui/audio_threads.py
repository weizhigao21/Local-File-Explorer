"""
音频模块后台线程
扫描线程、mtime 迁移线程与曲目时长探测线程
"""
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import QThread, pyqtSignal

from audio_manager import database as db
from audio_manager import scanner as audio_scanner
from audio_manager.duration import get_duration


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