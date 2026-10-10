"""
关窗时的后台扫描线程兜底（写真 / 音频窗口共用）

关闭窗口时若扫描线程来不及退出，仍需持有其 Python 引用：
QThread 没有 Qt parent，失去引用后被 GC 会销毁正在运行的线程对象，导致崩溃。
"""
from contextlib import suppress

# 超时后被悬挂持有的线程引用（线程真正结束后由回收槽移除）
detached_threads = set()


def stop_scan_thread(thread, signal_names, log_name="扫描线程"):
    """取消并等待扫描线程退出（幂等：线程未启动 / 已结束时安全）

    等待超时（线程仍在收尾）时的兜底：断开信号避免回调到已关闭的窗口，
    并用模块级集合持有引用防止 QThread 被 GC。

    signal_names: 该线程需要断开的自定义信号名（scan_finished / error / progress 等）。
    """
    if thread is None or not thread.isRunning():
        return
    thread.cancel()
    if thread.wait(3000):
        return
    # 没有连接的信号 disconnect() 会抛 TypeError，逐个吞掉
    for name in signal_names:
        with suppress(TypeError):
            getattr(thread, name).disconnect()
    detached_threads.add(thread)
    # 注意：scan_finished 是 pyqtSignal(dict)，槽必须能接住这个参数
    thread.scan_finished.connect(lambda *_: detached_threads.discard(thread))
    print(f"[关闭] {log_name}未在 3 秒内退出，已断开其信号并保持引用直到结束")
