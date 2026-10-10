"""
统一的图片异步解码模块：
- 基于 QThreadPool + QRunnable，避免阻塞主线程
- 全局 LRU 缓存（path + mtime + 标准尺寸作为键，mtime 防止换图后显示旧图）
- LRU 按字节上限淘汰（全屏大图单张可达 MB 级，仅限条数会撑到 GB 级）
- 标准尺寸对齐：窗口 resize 时缓存不失效
- 磁盘缓存：重启后不重新解码（仅适用小尺寸缩略图），超容量时按写入时间清理
- token 机制支持请求取消（避免过期结果覆盖最新 UI）

注意：解码产物与缓存全程使用 QImage（可重入、跨线程安全）；
QPixmap 仅限 GUI 线程使用，由各消费方在主线程按需 QPixmap.fromImage 转换。
"""
import hashlib
import os
import threading
import traceback
from collections import OrderedDict
from contextlib import suppress

from PyQt6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader

from resource_manager.config import GPU_FRIENDLY_FORMAT, IMAGE_THUMBNAIL_DIR

# 标准尺寸（按 64 像素对齐）：所有请求尺寸向上取到最接近的标准尺寸
# 这样同一张图在 165x222、166x223、167x224 显示时都会对齐到同一个缓存键
_SIZE_STEP = 64

# 超过此尺寸的请求不写磁盘缓存（如全屏查看器，每次尺寸都不同，缓存收益低）
_DISK_CACHE_MAX_DIM = 512

# 内存缓存字节上限（解码产物 QImage，全屏大图单张数 MB，按条数限不住总量）
_CACHE_MAX_BYTES = 256 * 1024 * 1024

# 磁盘缓存总容量上限与清理频率（每写入 N 个文件检查一次）
_DISK_CACHE_MAX_BYTES = 200 * 1024 * 1024
_DISK_SWEEP_INTERVAL = 128


def _normalize_size(size: QSize) -> tuple[int, int]:
    """将尺寸对齐到 64 像素的倍数（向上取）
    例：(165, 222) -> (192, 224)；(200, 200) -> (256, 256)
    """
    w = ((size.width() + _SIZE_STEP - 1) // _SIZE_STEP) * _SIZE_STEP
    h = ((size.height() + _SIZE_STEP - 1) // _SIZE_STEP) * _SIZE_STEP
    # 限制最大值，避免超大图导致 OOM
    w = min(w, 1024)
    h = min(h, 1024)
    return (w, h)


def _file_sig(path: str) -> int:
    """文件修改时间签名：写入缓存键，文件被替换后自动失效旧缓存"""
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0


def _disk_cache_path(path: str, sig: int, w: int, h: int) -> str | None:
    """返回磁盘缓存文件路径。None 表示该尺寸不缓存到磁盘

    文件名包含 mtime：换图后生成新缓存文件，旧文件由容量清理回收。
    """
    if w > _DISK_CACHE_MAX_DIM or h > _DISK_CACHE_MAX_DIM:
        return None
    if not IMAGE_THUMBNAIL_DIR:
        return None
    h_md5 = hashlib.md5(path.encode("utf-8")).hexdigest()[:16]
    return os.path.join(IMAGE_THUMBNAIL_DIR, f"{h_md5}_{w}x{h}_{sig}.jpg")


# 全局 LRU 缓存：key = (path, mtime, w, h)，其中 w/h 是标准化后的尺寸
# 存 QImage 而非 QPixmap：缓存被多个解码线程与主线程并发读写，
# QPixmap 限定 GUI 线程使用，跨线程访问是崩溃源
_cache: "OrderedDict[tuple[str, int, int, int], QImage]" = OrderedDict()
_cache_bytes = 0

# 缓存被多个解码线程（QThreadPool）与主线程并发访问，必须加锁：
# get_cached 的「pop 再插回」与 _trim_cache 的淘汰可能交错，
# 未加锁时 _cache.pop(key) 会在 key 刚被淘汰后抛 KeyError —— 该异常发生在
# QRunnable.run 中，PyQt6 会直接 abort 整个进程（不是普通报错）。
# 用 RLock 以便 _trim_cache 被 put_cache 持锁调用时仍可重入。
_cache_lock = threading.RLock()


def _trim_cache():
    """按字节上限淘汰最久未使用的条目（调用方可直接在持锁状态下调用，RLock 可重入）"""
    global _cache_bytes
    with _cache_lock:
        while _cache and _cache_bytes > _CACHE_MAX_BYTES:
            _, img = _cache.popitem(last=False)
            _cache_bytes -= img.sizeInBytes()


def get_cached(path: str, target: QSize) -> QImage | None:
    """从内存缓存读取。未命中返回 None（不查磁盘，磁盘由 _decode 负责）"""
    key = (path, _file_sig(path), *_normalize_size(target))
    with _cache_lock:
        # pop(key, None)：并发淘汰下 key 可能已被移除，不能用 pop(key) 否则 KeyError
        img = _cache.pop(key, None)
        if img is None:
            return None
        _cache[key] = img  # 移到末尾，标记为最近使用
        return img


def put_cache(path: str, target: QSize, image: QImage):
    """写入内存缓存"""
    global _cache_bytes
    if image.isNull():
        return
    key = (path, _file_sig(path), *_normalize_size(target))
    with _cache_lock:
        old = _cache.pop(key, None)
        if old is not None:
            _cache_bytes -= old.sizeInBytes()
        _cache[key] = image
        _cache_bytes += image.sizeInBytes()
        _trim_cache()


def clear_cache():
    """清空内存缓存（不影响磁盘缓存）"""
    global _cache_bytes
    with _cache_lock:
        _cache.clear()
        _cache_bytes = 0


def _decode(path: str, target: QSize) -> QImage:
    """实际解码函数，在工作线程中调用（只产出 QImage，不碰 QPixmap）。
    优先级：内存缓存 > 磁盘缓存 > 原图解码（+写入磁盘缓存）
    """
    norm_w, norm_h = _normalize_size(target)
    decode_size = QSize(norm_w, norm_h)
    sig = _file_sig(path)

    # 1. 查磁盘缓存（仅小尺寸缩略图）
    disk_path = _disk_cache_path(path, sig, norm_w, norm_h)
    if disk_path and os.path.exists(disk_path):
        reader = QImageReader(disk_path)
        img = reader.read()
        if not img.isNull():
            if GPU_FRIENDLY_FORMAT:
                img = img.convertToFormat(QImage.Format.Format_RGBX8888)
            return img
        # 磁盘缓存损坏，删除重生成
        with suppress(OSError):
            os.remove(disk_path)

    # 2. 解码原图
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    orig = reader.size()
    if orig.isValid() and (
            orig.width() > decode_size.width() or orig.height() > decode_size.height()):
        reader.setScaledSize(
            orig.scaled(decode_size, Qt.AspectRatioMode.KeepAspectRatio)
        )
    img = reader.read()
    if img.isNull():
        return QImage()
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
            _note_disk_write()
        except Exception:
            pass  # 写盘失败不影响主流程

    return img


# 磁盘缓存容量管理（写入由多个解码线程并发执行）
_disk_lock = threading.Lock()
_disk_write_count = 0


def _note_disk_write():
    """记录一次磁盘缓存写入，按间隔触发容量清理"""
    global _disk_write_count
    with _disk_lock:
        _disk_write_count += 1
        due = _disk_write_count % _DISK_SWEEP_INTERVAL == 0
    if due:
        _sweep_disk_cache()


def _sweep_disk_cache():
    """磁盘缓存超容量时按写入时间清理（优先删最久未写入的文件）

    换图/删除原图会留下孤儿缓存文件，扫描器的缩略图 GC 不覆盖 img/ 子目录，
    这里是该目录唯一的回收入口。
    """
    if not IMAGE_THUMBNAIL_DIR or not os.path.isdir(IMAGE_THUMBNAIL_DIR):
        return
    try:
        entries = []
        for fname in os.listdir(IMAGE_THUMBNAIL_DIR):
            fpath = os.path.join(IMAGE_THUMBNAIL_DIR, fname)
            try:
                st = os.stat(fpath)
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, fpath))
    except OSError:
        return
    total = sum(size for _, size, _ in entries)
    if total <= _DISK_CACHE_MAX_BYTES:
        return
    for _, size, fpath in sorted(entries):  # 最久未写入的先删
        with suppress(OSError):
            os.remove(fpath)
            total -= size
        if total <= _DISK_CACHE_MAX_BYTES:
            break


class _Signals(QObject):
    loaded = pyqtSignal(str, QSize, QImage, int)  # path, target, image, token


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
        # QRunnable 里未捕获的 Python 异常在 PyQt6 下不会只打印，而是 qFatal → abort 整个进程。
        # 因此这里必须兜住所有异常：解码失败时发射空 image，调用方的 _on_loaded
        # 已经统一判 isNull() 并保持占位图（grid_view / image_viewer / work_images_view）。
        try:
            # 命中缓存直接返回
            cached = get_cached(self.path, self.target)
            if cached is not None:
                self.signals.loaded.emit(self.path, self.target, cached, self.token)
                return
            image = _decode(self.path, self.target)
            put_cache(self.path, self.target, image)
            self.signals.loaded.emit(self.path, self.target, image, self.token)
        except Exception:
            traceback.print_exc()
            # 连发射都失败（如 QObject 已析构）则彻底放弃该任务
            with suppress(Exception):
                self.signals.loaded.emit(self.path, self.target, QImage(), self.token)


class ImageLoader:
    """
    异步加载器：管理 token 序号与回调注册。
    用法：
        loader = ImageLoader()
        loader.request(path, target, on_loaded)
    """

    def __init__(self, pool: QThreadPool | None = None):
        self._pool = pool or QThreadPool.globalInstance()
        self._next_token = 0

    def request(self, path: str, target: QSize, on_loaded) -> int:
        """
        请求异步解码。返回 token。
        on_loaded 回调签名：on_loaded(path, target, image, token) -> bool
            image 为 QImage（QPixmap 转换由调用方在 GUI 线程自行完成）；
            返回 True 表示结果有效，False 表示已过期（调用方可丢弃）。
        """
        # 目标尺寸无效时直接跳过
        if target.width() <= 0 or target.height() <= 0:
            return -1

        self._next_token += 1
        token = self._next_token

        task = ImageLoadTask(path, target, token)
        # 直连回调即可（旧实现的 lambda 转发并非注释所说的弱引用，只是多余的中间层）；
        # 连接持有 on_loaded 的强引用，任务自动删除后连接随之释放
        task.signals.loaded.connect(on_loaded)
        self._pool.start(task)
        return token


# 全局单例
_loader: ImageLoader | None = None


def loader() -> ImageLoader:
    global _loader
    if _loader is None:
        _loader = ImageLoader()
        # 限制并发，避免线程过多抢主线程时间片
        QThreadPool.globalInstance().setMaxThreadCount(4)
    return _loader
