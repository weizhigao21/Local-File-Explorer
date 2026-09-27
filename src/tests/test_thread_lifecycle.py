"""
后台线程生命周期与图片加载器线程安全回归测试（离屏，无需显示器）

覆盖两类缺陷：

1) image_loader 全局 LRU 缓存被 QThreadPool 多线程与主线程无锁并发访问：
   - get_cached 的「key in _cache 判断 → pop」之间存在窗口，若该 key 恰被
     _trim_cache 淘汰，pop 会抛 KeyError；它发生在 QRunnable.run 里，
     PyQt6 不是打印日志而是 abort 整个进程。
   - ImageLoadTask.run 没有任何 try/except，解码异常同样会 abort 进程。

2) 两个模块窗口的 closeEvent 都没停扫描线程（音频窗口只停了 mtime/DLsite 线程）：
   扫描线程持有数据库长连接并向即将关闭的窗口发信号；退出再进入模块会再起一条
   扫描线程，两条线程同时持有长连接。

用离屏 QApplication + 假线程/假数据层验证，不启动真实扫描、不触碰真实数据库。
"""
import os
import threading
from collections import OrderedDict

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 必须在导入 Qt 之前设置

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QSize  # noqa: E402
from PyQt6.QtGui import QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from audio_manager import database as audio_db  # noqa: E402
from audio_manager import dlsite_db  # noqa: E402
from ui import image_loader  # noqa: E402
from ui import main_window as mw_mod  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    """离屏 QApplication（QPixmap 需要 QGuiApplication 存在）"""
    app = QApplication.instance() or QApplication([])
    yield app


# ─────────────────────── 图片加载器 ───────────────────────

def test_get_cached_survives_concurrent_eviction(qapp, monkeypatch):
    """模拟：读取命中判断通过后、真正取值的瞬间，该 key 已被 _trim_cache 淘汰

    真实交错时序：`if key in _cache`（或 pop 的查表）通过 → 另一线程执行 LRU 淘汰
    → 取值时 key 已不在。旧实现写 `_cache.pop(key)`（无默认值），此时抛 KeyError；
    异常出现在 QRunnable.run 中，PyQt6 会直接 abort 整个进程，而不是打印一条日志。

    这里用 _EvictOnPop 在 pop 内部先淘汰再取值来**确定性复现**该时序：
    无默认值的 pop 会 KeyError，带默认值的 pop 只会得到 None。
    （注：不能用「覆盖 __contains__ 返回 True」来模拟 —— OrderedDict 子类覆盖
     __contains__ 会让 pop(key, default) 也抛 KeyError，模型会失真。）
    """

    class _EvictOnPop(OrderedDict):
        """pop 执行的瞬间，key 已被并发淘汰"""

        def pop(self, key, *args):
            super().pop(key, None)   # 模拟另一线程抢先淘汰
            return super().pop(key, *args)

    monkeypatch.setattr(image_loader, "_cache", _EvictOnPop())
    image_loader._cache[("/x.jpg", 64, 64)] = QPixmap(2, 2)  # 先放入，模拟"命中判断通过"

    # 不应抛异常，应视为缓存未命中
    assert image_loader.get_cached("/x.jpg", QSize(64, 64)) is None


def test_image_load_task_swallows_decode_error(qapp, monkeypatch):
    """解码抛异常时任务必须兜住并发射空 pixmap（而不是让异常逃出 run()）"""
    def _boom(path, target):
        raise RuntimeError("模拟解码失败")

    monkeypatch.setattr(image_loader, "_decode", _boom)

    task = image_loader.ImageLoadTask("/tmp/broken.jpg", QSize(64, 64), token=7)
    got = []
    task.signals.loaded.connect(lambda p, t, pix, tk: got.append((p, t, pix, tk)))

    task.run()  # 旧实现下这里会把 RuntimeError 抛出 run() → PyQt6 abort

    assert len(got) == 1, "解码失败时也应以空 pixmap 回调一次，调用方才能收尾"
    path, target, pixmap, token = got[0]
    assert path == "/tmp/broken.jpg"
    assert token == 7
    assert pixmap.isNull(), "失败时应回调空 pixmap（调用方统一判 isNull 并保持占位）"


def test_cache_thread_safety_stress(qapp):
    """多线程并发 put/get 不应抛异常，且缓存长度受 _CACHE_MAX 约束"""
    pixmap = QPixmap(4, 4)  # 在主线程创建，避免 QPixmap 跨线程构造
    size = QSize(128, 128)
    errors = []

    def worker(base):
        try:
            for i in range(300):
                path = f"/tmp/stress_{base}_{i}.jpg"
                image_loader.put_cache(path, size, pixmap)
                image_loader.get_cached(path, size)
                # 故意访问可能已被淘汰的旧 key
                image_loader.get_cached(f"/tmp/stress_{base}_{i - 5}.jpg", size)
        except Exception as e:  # noqa: BLE001 - 收集任意异常用于断言
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], f"并发访问缓存出现异常: {errors[:3]}"
    assert len(image_loader._cache) <= image_loader._CACHE_MAX

    image_loader.clear_cache()
    assert len(image_loader._cache) == 0


# ─────────────────────── 窗口关闭 ───────────────────────

class FakeSignal:
    """模拟 pyqtSignal：可 connect/disconnect

    真实 PyQt 在「无连接时 disconnect()」会抛 TypeError，
    raise_on_disconnect=True 用来覆盖异常分支。
    """

    def __init__(self, raise_on_disconnect=False):
        self.connections = []
        self.disconnected = False
        self._raise = raise_on_disconnect

    def connect(self, slot):
        self.connections.append(slot)

    def disconnect(self):
        if self._raise:
            raise TypeError("signal has no connections")
        self.disconnected = True
        self.connections.clear()


class FakeScanThread:
    """模拟扫描线程：只实现 _stop_scan_thread 依赖的接口"""

    def __init__(self, audio_roots=None, total_authors=0, *,
                 running=True, wait_result=True):
        self._running = running
        self._wait_result = wait_result
        self.cancelled = False
        self.waits = []
        # finished 是 pyqtSignal(dict)，槽必须能接住参数
        self.finished = FakeSignal()
        self.error = FakeSignal()
        self.progress = FakeSignal()
        self.author_done = FakeSignal(raise_on_disconnect=True)
        self.started = False

    def isRunning(self):
        return self._running

    def cancel(self):
        self.cancelled = True

    def wait(self, ms):
        self.waits.append(ms)
        return self._wait_result

    def start(self):
        self.started = True


def _make_photo_window(monkeypatch):
    """构造写真主窗口：用假数据层 + 假扫描线程，绝不启动真实扫描"""
    monkeypatch.setattr(mw_mod.db, "init_db", lambda: None)
    monkeypatch.setattr(mw_mod.db, "list_authors", lambda: [])
    monkeypatch.setattr(mw_mod.db, "list_works", lambda author_id=None: [])
    monkeypatch.setattr(mw_mod.scanner, "count_authors", lambda: 0)
    # 关键：拦截 ScanThread，防止 500ms 后的自动扫描触碰真实写真目录
    monkeypatch.setattr(mw_mod, "ScanThread", FakeScanThread)
    return mw_mod.MainWindow()


def test_photo_close_cancels_and_waits_scan_thread(qapp, monkeypatch):
    """关闭写真窗口必须取消并等待扫描线程"""
    win = _make_photo_window(monkeypatch)
    fake = FakeScanThread()
    win.scan_thread = fake

    win.close()

    assert fake.cancelled is True, "关闭窗口未取消扫描线程"
    assert fake.waits == [3000], "关闭窗口未等待扫描线程退出"
    assert mw_mod._detached_threads == set(), "线程已正常退出，不应进入悬挂集合"


def test_photo_close_detaches_thread_that_outlives_wait(qapp, monkeypatch):
    """等待超时（线程仍在收尾）时：断开信号 + 持有引用，避免崩溃"""
    win = _make_photo_window(monkeypatch)
    fake = FakeScanThread(wait_result=False)
    win.scan_thread = fake

    win.close()

    assert fake.cancelled is True
    assert fake.finished.disconnected is True, "未断开 finished，仍会回调已关闭的窗口"
    assert fake.error.disconnected is True
    assert fake.author_done.disconnected is False, "无连接时抛的 TypeError 应被吞掉"
    assert fake in mw_mod._detached_threads, "未持有引用，QThread 可能被 GC 导致崩溃"

    # 线程真正结束后应从悬挂集合中移除（验证接上的回收槽带了参数占位）
    fake.finished.connections[0]({})
    assert fake not in mw_mod._detached_threads
    assert mw_mod._detached_threads == set()


def test_photo_close_without_scan_is_safe(qapp, monkeypatch):
    """从未启动过扫描就关闭窗口：不应报错（幂等）"""
    win = _make_photo_window(monkeypatch)
    assert win.scan_thread is None
    win._stop_scan_thread()
    win.close()


def _make_audio_window(monkeypatch):
    """构造音频主窗口：用假数据层 + 假扫描线程"""
    monkeypatch.setattr(audio_db, "init_db", lambda: None)
    monkeypatch.setattr(dlsite_db, "init_db", lambda: None)
    monkeypatch.setattr(audio_db, "list_playlists_by_parent", lambda parent: [])
    monkeypatch.setattr(audio_db, "list_playlists", lambda: [])
    monkeypatch.setattr(audio_db, "count_tracks_by_playlist", lambda: {})
    monkeypatch.setattr(audio_db, "get_child_counts_batch", lambda paths: {})
    monkeypatch.setattr(audio_db, "get_all_tags", lambda: [])
    monkeypatch.setattr(dlsite_db, "get_circle_counts", lambda: {})
    monkeypatch.setattr(dlsite_db, "get_cv_counts", lambda: {})
    monkeypatch.setattr(dlsite_db, "get_cover_map", lambda: {})
    monkeypatch.setattr(dlsite_db, "get_genre_map", lambda: {})
    monkeypatch.setattr(dlsite_db, "find_rjs_by", lambda circles=None, cvs=None: set())

    from ui import audio_view as av_mod

    monkeypatch.setattr(av_mod, "AudioScanThread", FakeScanThread)
    win = av_mod.AudioMainWindow()
    return win, av_mod


def test_audio_close_cancels_and_waits_scan_thread(qapp, monkeypatch):
    """关闭音频窗口同样必须取消并等待扫描线程"""
    win, av_mod = _make_audio_window(monkeypatch)
    fake = FakeScanThread()
    win.scan_thread = fake

    win.close()

    assert fake.cancelled is True, "关闭音频窗口未取消扫描线程"
    assert fake.waits == [3000]
    assert av_mod._detached_threads == set()
