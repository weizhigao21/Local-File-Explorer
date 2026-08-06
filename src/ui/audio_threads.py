"""
音频模块后台线程
扫描线程与 mtime 后台迁移线程
"""
import os
import threading
import traceback

from PyQt6.QtCore import QThread, pyqtSignal

from audio_manager import database as db
from audio_manager import scanner as audio_scanner


class AudioScanThread(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, audio_root):
        super().__init__()
        self.audio_root = audio_root
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            stats = audio_scanner.scan(
                audio_root=self.audio_root,
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