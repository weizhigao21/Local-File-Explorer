"""应用日志：把 print / logging / 未捕获异常都落到 data/app.log

**为什么要有这个模块**：打包成 exe 后是 windowed 程序，没有控制台，
`sys.stdout` 直接是 `None`。项目里散落的 `print("[网页封面] 生成失败 ...")`
这类线索全部蒸发——用户来报「封面不显示」，谁也查不出原因。

**为什么接管 stdout 而不是把 print 全改成 logging**：
    print 分散在 scanner / web_server / dlsite / main 等十几处，一次性替换
    风险大、漏一个就少一条线索。重定向 `sys.stdout`/`sys.stderr` 能一次性
    覆盖所有现有输出（含第三方库的打印和未捕获异常的 traceback），零侵入。
    新代码仍然推荐直接用 `logging.getLogger("app")`。

纯标准库，不依赖 Qt，核心层与网页层都可以安全引入。
"""
import logging
import os
import sys
import threading
from contextlib import suppress
from logging.handlers import RotatingFileHandler

__all__ = ["setup", "get_logger", "log_path", "LOG_FILENAME"]

LOG_FILENAME = "app.log"

# 单文件 1MB × 4 份：一个月的日常使用也撑不满，出问题时又不会只剩末尾几行
_MAX_BYTES = 1024 * 1024
_BACKUP_COUNT = 3

logger = logging.getLogger("app")
logger.setLevel(logging.INFO)
logger.propagate = False  # 不往 root 传播，避免和第三方库的 handler 重复写

_setup_done = False
_log_file = ""
_write_lock = threading.Lock()


def _default_log_dir() -> str:
    """默认落在 data/（与数据库、缩略图同一目录，卸载程序时一起清）"""
    from resource_manager import config

    return os.path.join(config.get_project_root(), "data")


def _ensure_handler(path: str) -> None:
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename == path:
            return

    handler = RotatingFileHandler(
        path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)


class _Tee:
    """把写入同时转发给原流与日志文件，按整行记录（带时间戳）

    只在遇到换行时落一条，避免 `print("a", end="")` 之类的半截输出把日志
    切成碎片；进程退出前由 `flush()` 把残留的半行补上。
    """

    def __init__(self, stream, is_err: bool = False):
        self._stream = stream
        self._is_err = is_err
        self._pending = ""

    def write(self, text):
        if not text:
            return 0
        if self._stream is not None:
            with suppress(OSError, ValueError):
                self._stream.write(text)
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._emit(line)
        return len(text)

    def _emit(self, line: str):
        if not line.strip():
            return
        with _write_lock, suppress(OSError, ValueError):
            (logger.warning if self._is_err else logger.info)(line)

    def flush(self):
        if self._stream is not None:
            with suppress(OSError, ValueError):
                self._stream.flush()
        if self._pending:
            self._emit(self._pending)
            self._pending = ""

    def isatty(self):
        return False

    # 某些库会读这些属性（如 tqdm 判断终端宽度）
    @property
    def encoding(self):
        return "utf-8"

    @property
    def errors(self):
        return "replace"


def setup(log_dir: str | None = None) -> str:
    """初始化日志（幂等）。返回日志文件路径。

    应在 `main()` 最开头调用：之后所有 print、第三方库输出、未捕获异常的
    traceback 都会同时进文件。
    """
    global _setup_done, _log_file
    if _setup_done:
        return _log_file

    directory = log_dir or _default_log_dir()
    os.makedirs(directory, exist_ok=True)

    path = os.path.join(directory, LOG_FILENAME)
    _ensure_handler(path)
    _log_file = path
    _setup_done = True

    # 打包后 stdout/stderr 是 None，_Tee 会跳过转发只写文件
    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr, is_err=True)

    # 未捕获异常也要留痕（默认走 excepthook → stderr → 上面已接管，这里补上类型）
    original_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        logger.error("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))
        if original_hook is not None:
            original_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook

    logger.info("=" * 60)
    logger.info("会话开始 pid=%s frozen=%s", os.getpid(), bool(getattr(sys, "frozen", False)))
    return path


def get_logger(name: str | None = None) -> logging.Logger:
    """取子 logger（仍在 `app` 命名空间下，共用同一个文件 handler）"""
    return logger.getChild(name) if name else logger


def log_path() -> str:
    """当前日志文件路径；未初始化时按默认目录推算（供 UI 直接打开）"""
    if _log_file:
        return _log_file
    return os.path.join(_default_log_dir(), LOG_FILENAME)


def _reset_for_tests() -> None:
    """仅测试用：摘掉文件 handler 并复位标志

    刻意不碰 `sys.stdout`/`sys.stderr`——测试里用 monkeypatch 换的流由
    monkeypatch 自己恢复，这里再动一次会把 pytest 的捕获流搞乱。
    """
    global _setup_done, _log_file
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        with suppress(Exception):
            handler.close()
    _setup_done = False
    _log_file = ""
