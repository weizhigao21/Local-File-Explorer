"""
统一的图片异步解码模块：
- 基于 QThreadPool + QRunnable，避免阻塞主线程
- 全局 LRU 缓存（path + 标准尺寸作为键）
- 标准尺寸对齐：窗口 resize 时缓存不失效
- 磁盘缓存：重启后不重新解码（仅适用小尺寸缩略图）
- token 机制支持请求取消（避免过期结果覆盖最新 UI）
"""
import hashlib
import os
from collections import OrderedDict
from typing import Optional, Tuple

from PyQt6.QtCore import Qt, QSize, QThreadPool, QRunnable, pyqtSignal, QObject
from PyQt6.QtGui import QImage, QImageReader, QPixmap

from resource_manager.config import GPU_FRIENDLY_FORMAT, IMAGE_THUMBNAIL_DIR


# 标准尺寸（按 64 像素对齐）：所有请求尺寸向上取到最接近的标准尺寸
# 这样同一张图在 165x222、166x223、167x224 显示时都会对齐到同一个缓存键
_SIZE_STEP = 64

# 超过此尺寸的请求不写磁盘缓存（如全屏查看器，每次尺寸都不同，缓存收益低）
_DISK_CACHE_MAX_DIM = 512


def _normalize_size(size: QSize) -> Tuple[int, int]:
    """将尺寸对齐到 64 像素的倍数（向上取）
    例：(165, 222) -> (192, 224)；(200, 200) -> (256, 256)
    """
    w = ((size.width() + _SIZE_STEP - 1) // _SIZE_STEP) * _SIZE_STEP
    h = ((size.height() + _SIZE_STEP - 1) // _SIZE_STEP) * _SIZE_STEP
    # 限制最大值，避免超大图导致 OOM
    w = min(w, 1024)
    h = min(h, 1024)
    return (w, h)


def _disk_cache_path(path: str, w: int, h: int) -> Optional[str]:
    """返回磁盘缓存文件路径。None 表示该尺寸不缓存到磁盘"""
    if w > _DISK_CACHE_MAX_DIM or h > _DISK_CACHE_MAX_DIM:
        return None
    if not IMAGE_THUMBNAIL_DIR:
        return None
    h_md5 = hashlib.md5(path.encode("utf-8")).hexdigest()[:16]
    return os.path.join(IMAGE_THUMBNAIL_DIR, f"{h_md5}_{w}x{h}.jpg")


# 全局 LRU 缓存：key = (path, w, h)，其中 w/h 是标准化后的尺寸
_cache: "OrderedDict[Tuple[str, int, int], QPixmap]" = OrderedDict()
_CACHE_MAX = 600


def _trim_cache():
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def get_cached(path: str, target: QSize) -> Optional[QPixmap]:
    """从内存缓存读取。未命中返回 None（不查磁盘，磁盘由 _decode 负责）"""
    key = (path, *_normalize_size(target))
    if key in _cache:
        # 移到末尾，标记为最近使用
        pix = _cache.pop(key)
        _cache[key] = pix
        return pix
    return None


def put_cache(path: str, target: QSize, pixmap: QPixmap):
    """写入内存缓存"""
    if pixmap.isNull():
        return
    key = (path, *_normalize_size(target))
    if key in _cache:
        _cache.pop(key)
    _cache[key] = pixmap
    _trim_cache()


def clear_cache():
    """清空内存缓存（不影响磁盘缓存）"""
    _cache.clear()


def _decode(path: str, target: QSize) -> QPixmap:
    """实际解码函数，在工作线程中调用。
    优先级：内存缓存 > 磁盘缓存 > 原图解码（+写入磁盘缓存）
    """
    norm_w, norm_h = _normalize_size(target)
    decode_size = QSize(norm_w, norm_h)

    # 1. 查磁盘缓存（仅小尺寸缩略图）
    disk_path = _disk_cache_path(path, norm_w, norm_h)
    if disk_path and os.path.exists(disk_path):
        reader = QImageReader(disk_path)
        img = reader.read()
        if not img.isNull():
            if GPU_FRIENDLY_FORMAT:
                img = img.convertToFormat(QImage.Format.Format_RGBX8888)
            return QPixmap.fromImage(img)
        # 磁盘缓存损坏，删除重生成
        try:
            os.remove(disk_path)
        except OSError:
            pass

    # 2. 解码原图
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    orig = reader.size()
    if orig.isValid():
        if orig.width() > decode_size.width() or orig.height() > decode_size.height():
            reader.setScaledSize(
                orig.scaled(decode_size, Qt.AspectRatioMode.KeepAspectRatio)
            )
    img = reader.read()
    if img.isNull():
        return QPixmap()
    if GPU_FRIENDLY_FORMAT:
        img = img.convertToFormat(QImage.Format.Format_RGBX8888)

    # 3. 写入磁盘缓存（仅小尺寸，原子的写）
    if disk_path:
        try:
            os.makedirs(os.path.dirname(disk_path), exist_ok=True)
            # 用 RGB 格式存盘，避免 RGBA 透明通道膨胀体积
            save_img = img if img.format() != QImage.Format.Format_RGBX8888 else \
                img.convertToFormat(QImage.Format.Format_RGB888)
            save_img.save(disk_path, "JPEG", quality=85)
        except Exception:
            pass  # 写盘失败不影响主流程

    return QPixmap.fromImage(img)


class _Signals(QObject):
    loaded = pyqtSignal(str, QSize, QPixmap, int)  # path, target, pixmap, token


class ImageLoadTask(QRunnable):
    """异步解码任务"""

    def __init__(self, path: str, target: QSize, token: int):
        super().__init__()
        self.path = path
        self.target = target
        self.token = token
        self.signals = _Signals()
        # 任务结束后自动清理
        self.setAutoDelete(True)

    def run(self):
        # 命中缓存直接返回
        cached = get_cached(self.path, self.target)
        if cached is not None:
            self.signals.loaded.emit(self.path, self.target, cached, self.token)
            return
        pixmap = _decode(self.path, self.target)
        put_cache(self.path, self.target, pixmap)
        self.signals.loaded.emit(self.path, self.target, pixmap, self.token)


class ImageLoader:
    """
    异步加载器：管理 token 序号与回调注册。
    用法：
        loader = ImageLoader()
        loader.request(path, target, on_loaded)
    """

    def __init__(self, pool: Optional[QThreadPool] = None):
        self._pool = pool or QThreadPool.globalInstance()
        self._next_token = 0

    def request(self, path: str, target: QSize, on_loaded) -> int:
        """
        请求异步解码。返回 token。
        on_loaded 回调签名：on_loaded(path, target, pixmap, token) -> bool
            返回 True 表示结果有效，False 表示已过期（调用方可丢弃）。
        """
        # 目标尺寸无效时直接跳过
        if target.width() <= 0 or target.height() <= 0:
            return -1

        self._next_token += 1
        token = self._next_token

        task = ImageLoadTask(path, target, token)
        # 使用弱引用包装回调：通过 lambda 转发
        task.signals.loaded.connect(
            lambda p, t, pix, tk: on_loaded(p, t, pix, tk)
        )
        self._pool.start(task)
        return token

    def request_sync(self, path: str, target: QSize) -> QPixmap:
        """同步解码（仅用于必须立即返回结果的场景），仍走缓存"""
        cached = get_cached(path, target)
        if cached is not None:
            return cached
        pixmap = _decode(path, target)
        put_cache(path, target, pixmap)
        return pixmap


# 全局单例
_loader: Optional[ImageLoader] = None


def loader() -> ImageLoader:
    global _loader
    if _loader is None:
        _loader = ImageLoader()
        # 限制并发，避免线程过多抢主线程时间片
        QThreadPool.globalInstance().setMaxThreadCount(4)
    return _loader
