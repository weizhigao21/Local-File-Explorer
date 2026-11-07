"""
单实例锁 + 二次启动参数转发

用户把音频文件「用本程序打开」时会再启动一个进程。这里让第二个进程只负责
把命令行参数通过本地 socket 传给已有实例，然后立刻退出——避免两个实例抢
同一个数据库和音频设备。

冷启动竞态（用户连着双击同一个音频最容易撞上）：
打包版解压启动要 1~3 秒，两个进程可能几乎同时走到「探测 socket → 监听」。
只用 QLocalSocket 探测时，两边都可能没探到对方、都自认主实例，各开一个播放窗。
因此再加一道 Windows 命名互斥体：由内核保证同一会话只有一个进程能成为主实例，
进程崩溃时句柄自动释放，不留陈旧锁。
"""
import ctypes
import json
import sys
import time

from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

_DEFAULT_KEY = "local-resource-manager-single-instance"
_ERROR_ALREADY_EXISTS = 183


def _acquire_primary_mutex(key):
    """返回 (handle, is_primary)；非 Windows 或 API 失败时退回「无锁」

    CreateMutexW 的对象由内核按名字持有，主进程存活期间一直存在；进程退出
    （含崩溃）时句柄关闭、对象销毁，不会像锁文件那样留下陈旧残留。
    """
    if sys.platform != "win32":
        return None, True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, 0, f"Local\\{key}-primary")
        if not handle:
            return None, True
        return handle, ctypes.get_last_error() != _ERROR_ALREADY_EXISTS
    except (AttributeError, OSError):
        # 拿不到互斥体就退回原来的 socket 探测，绝不让单实例保护阻塞启动
        return None, True


def _close_mutex(handle):
    if not handle or sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        pass


class SingleInstance(QObject):
    """messageReceived(list[str])：收到另一个实例转发的命令行参数"""

    messageReceived = pyqtSignal(list)

    def __init__(self, key=_DEFAULT_KEY, parent=None):
        super().__init__(parent)
        self._key = key
        self._server = None
        self._mutex = None

    def start(self):
        """尝试成为主实例；若已有实例在运行则转发参数并返回 False"""
        self._mutex, is_primary = _acquire_primary_mutex(self._key)
        if not is_primary:
            # 已有进程持锁，但它可能还在解压启动、socket 尚未 listen：
            # 轮询等它把服务建好，把文件参数送过去后本次启动直接退出。
            if self._forward(retries=30, wait_ms=100):
                # 自己只是「借」了那个互斥体的句柄，立刻还回去，免得主进程
                # 退出后对象还被本进程吊着，挡住下一次启动。
                _close_mutex(self._mutex)
                self._mutex = None
                return False
            # 持锁进程像是崩了（3 秒都没起来）：别让用户的打开动作落空，
            # 继续往下当主实例，socket 探测会再兜一层。
            _close_mutex(self._mutex)
            self._mutex, _ = _acquire_primary_mutex(self._key)

        # 持锁的主实例只探一次（保持原来的启动速度）；没拿到互斥体的平台靠
        # 这一探 + 下面 listen 失败后的重试兜住竞态。
        if self._forward():
            return False

        # 没有活着的实例：清掉残留的 socket 文件（崩溃后常见）再监听
        QLocalServer.removeServer(self._key)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)
        if not self._server.listen(self._key):
            # 竞态兜底：另一个进程刚抢先监听了同名管道。旧逻辑在这里「listen
            # 失败也当主实例继续」，两边就各开一个播放窗（重复打开的根因）。
            self._server.close()
            self._server.deleteLater()
            self._server = None
            if self._forward(retries=10, wait_ms=100):
                return False
            # 实在连不上（对方崩了/权限问题）：退回主实例继续运行，
            # 单实例保护不可用，但不阻塞使用。
            self._server = QLocalServer(self)
            self._server.newConnection.connect(self._on_connection)
            self._server.listen(self._key)
        return True

    def _forward(self, retries=1, wait_ms=0):
        """连到已有实例把命令行参数送过去；成功返回 True"""
        for attempt in range(retries):
            socket = QLocalSocket()
            socket.connectToServer(self._key)
            if socket.waitForConnected(300):
                payload = json.dumps(sys.argv[1:], ensure_ascii=False).encode("utf-8")
                socket.write(payload)
                socket.flush()
                # Windows 命名管道下 write 只是进缓冲，需要让事件循环把数据真正送出去；
                # 否则紧接着 disconnectFromServer() 会把尚未发送的参数丢弃。
                deadline = time.monotonic() + 1.0
                while socket.bytesToWrite() > 0 and time.monotonic() < deadline:
                    QCoreApplication.processEvents()
                    time.sleep(0.01)
                socket.disconnectFromServer()
                socket.waitForDisconnected(500)
                return True
            socket.abort()
            if attempt + 1 < retries:
                time.sleep(wait_ms / 1000.0)
        return False

    def _on_connection(self):
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            connection = self._server.nextPendingConnection()
            if connection is None:
                continue
            # 对端可能已经写完并断开：先看缓冲区，再等一次新数据
            if connection.bytesAvailable() == 0:
                connection.waitForReadyRead(500)
            raw = bytes(connection.readAll())
            if not raw:
                connection.waitForReadyRead(200)
                raw = bytes(connection.readAll())
            if not raw:
                connection.disconnectFromServer()
                connection.deleteLater()
                continue
            try:
                argv = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                argv = []
            if isinstance(argv, list):
                self.messageReceived.emit([str(item) for item in argv])
            connection.disconnectFromServer()
            connection.deleteLater()

    def shutdown(self):
        if self._server is not None:
            self._server.close()
            self._server = None
        _close_mutex(self._mutex)
        self._mutex = None


__all__ = ["SingleInstance"]
