"""
单实例锁 + 二次启动参数转发

用户把音频文件「用本程序打开」时会再启动一个进程。这里让第二个进程只负责
把命令行参数通过本地 socket 传给已有实例，然后立刻退出——避免两个实例抢
同一个数据库和音频设备。
"""
import json
import sys
import time

from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

_DEFAULT_KEY = "local-resource-manager-single-instance"


class SingleInstance(QObject):
    """messageReceived(list[str])：收到另一个实例转发的命令行参数"""

    messageReceived = pyqtSignal(list)

    def __init__(self, key=_DEFAULT_KEY, parent=None):
        super().__init__(parent)
        self._key = key
        self._server = None

    def start(self):
        """尝试成为主实例；若已有实例在运行则转发参数并返回 False"""
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
            return False

        # 没有活着的实例：清掉残留的 socket 文件（崩溃后常见）再监听
        QLocalServer.removeServer(self._key)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)
        self._server.listen(self._key)
        # listen 失败也按主实例继续运行（单实例保护不可用，但不阻塞使用）
        return True

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


__all__ = ["SingleInstance"]
