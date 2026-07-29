import os
import re
import hashlib
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from resource_manager import config
from resource_manager import database as db
from resource_manager import fingerprint_cache as fp


# 并行生成缩略图的线程数（IO 密集 + CPU 解码 JPEG）
_THUMB_WORKERS = 4


def _is_image(path):
    return Path(path).suffix.lower() in config.IMAGE_EXTENSIONS


def _collect_images_with_mtime(work_path):
    """递归收集作品目录下所有图片及其 mtime，按路径自然排序
    使用 os.scandir 替代 os.walk，DirEntry 自带 stat 缓存，减少系统调用
    返回 [(path, mtime), ...]
    """
    images = []
    stack = [work_path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False) and _is_image(entry.path):
                            # DirEntry.stat() 缓存了 stat 结果，无需再次系统调用
                            try:
                                mtime = int(entry.stat().st_mtime)
                            except OSError:
                                continue
                            images.append((entry.path, mtime))
                    except OSError:
                        continue
        except OSError:
            continue
    # 自然排序
    images.sort(key=lambda x: _natural_key_path(x[0]))
    return images


def _natural_key_path(s: str):
    """自然排序 key（与 database._natural_key 一致）"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


def _thumbnail_path(work_path):
    h = hashlib.md5(work_path.encode("utf-8")).hexdigest()
    return os.path.join(config.THUMBNAIL_DIR, f"{h}.jpg")


def _ensure_thumbnail(image_path, save_path):
    """生成缩略图，如果已存在则跳过"""
    if os.path.exists(save_path):
        return save_path
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    try:
        with Image.open(image_path) as im:
            im.thumbnail((config.THUMBNAIL_SIZE, config.THUMBNAIL_SIZE))
            if im.mode in ("RGBA", "P"):
                im = im.convert("RGB")
            im.save(save_path, "JPEG", quality=85)
        return save_path
    except Exception as e:
        print(f"[缩略图失败] {image_path}: {e}")
        return None


def count_authors():
    """统计写真根目录下的作者文件夹数量"""
    if not os.path.isdir(config.PHOTO_ROOT):
        return 0
    return sum(
        1 for name in os.listdir(config.PHOTO_ROOT)
        if os.path.isdir(os.path.join(config.PHOTO_ROOT, name))
    )


def _sync_work_images(work_id, current_images, stats):
    """同步单个作品的图片：增、改、删
    current_images: [(path, mtime), ...]
    stats: 累计统计 dict
    """
    current_map = {p: m for p, m in current_images}
    db_map = db.get_work_image_mtimes(work_id)

    current_paths = set(current_map.keys())
    db_paths = set(db_map.keys())

    # 删除：数据库有但磁盘没有
    to_delete = db_paths - current_paths
    if to_delete:
        db.delete_images_by_paths(list(to_delete))
        stats["deleted"] += len(to_delete)

    # 新增：磁盘有但数据库没有
    to_add = current_paths - db_paths
    if to_add:
        db.upsert_images(work_id, [(p, current_map[p]) for p in to_add])
        stats["added"] += len(to_add)

    # 更新：mtime 变化的
    to_update = []
    for p in current_paths & db_paths:
        if current_map[p] != db_map[p]:
            to_update.append((current_map[p], p))
    if to_update:
        db.update_image_mtimes(to_update)
        stats["updated"] += len(to_update)

    # 跳过：mtime 相同的
    skipped = len(current_paths & db_paths) - len(to_update)
    stats["skipped"] += max(skipped, 0)


def _scan_author(author_name, author_path, stats, progress_callback, cancel_event,
                 author_count, total_authors):
    """扫描单个作者（增量）：处理作品增、改、删"""
    # 数据库中该作者现有的作品 {path: (id, name, thumbnail)}
    # 先查作者 ID
    author_id = db.get_author_id(author_name)
    if author_id is None:
        author_id = db.add_author(author_name)

    existing_works = db.list_works_by_author(author_id)
    disk_work_paths = set()
    new_works = []  # 待插入的新作品

    for work_name in sorted(os.listdir(author_path)):
        if cancel_event and cancel_event.is_set():
            return

        work_path = os.path.join(author_path, work_name)
        if not os.path.isdir(work_path):
            continue

        try:
            current_images = _collect_images_with_mtime(work_path)
            if not current_images:
                continue
        except Exception as e:
            print(f"[扫描作品失败] {author_name}/{work_name}: {e}")
            traceback.print_exc()
            continue

        disk_work_paths.add(work_path)

        if work_path in existing_works:
            # 已存在作品，同步图片变化
            work_id, _, _ = existing_works[work_path]
            _sync_work_images(work_id, current_images, stats)
        else:
            # 新作品：暂存，缩略图稍后并行生成
            thumbnail = _thumbnail_path(work_path)
            new_works.append((work_name, work_path, thumbnail, current_images))
            stats["added"] += len(current_images)

        if progress_callback:
            progress_callback(
                "work", author_name, work_name, len(current_images),
                author_count, total_authors
            )

    # 删除数据库中存在但磁盘不存在的作品
    for work_path, (work_id, _, _) in existing_works.items():
        if work_path not in disk_work_paths:
            db.delete_work(work_id)
            stats["deleted_works"] += 1

    # 并行生成新作品的缩略图（4 线程并发解码 JPEG）
    if new_works:
        with ThreadPoolExecutor(max_workers=_THUMB_WORKERS) as executor:
            for _, _, thumbnail, images in new_works:
                if thumbnail and not os.path.exists(thumbnail):
                    executor.submit(_ensure_thumbnail, images[0][0], thumbnail)
        # with 退出时所有任务已完成

    # 批量插入新作品
    if new_works:
        def _cb(work_name, image_count):
            if progress_callback:
                progress_callback(
                    "work", author_name, work_name, image_count,
                    author_count, total_authors
                )
        db.add_author_with_works(author_name, new_works, progress_callback=_cb)


def _gc_thumbnails():
    """清理孤儿缩略图：删除 thumbnails/ 下数据库未引用的 .jpg 文件"""
    if not os.path.isdir(config.THUMBNAIL_DIR):
        return 0

    db_thumbs = set()
    for thumb_path in db.list_all_thumbnail_paths():
        if thumb_path:
            db_thumbs.add(os.path.basename(thumb_path))

    removed = 0
    for fname in os.listdir(config.THUMBNAIL_DIR):
        if not fname.endswith(".jpg"):
            continue
        if fname in db_thumbs:
            continue
        try:
            os.remove(os.path.join(config.THUMBNAIL_DIR, fname))
            removed += 1
        except OSError as e:
            print(f"[清理缩略图失败] {fname}: {e}")
    return removed


def scan(progress_callback=None, author_done_callback=None, cancel_event=None, total_authors=0,
         check_fingerprint=True):
    """增量扫描：检测文件增删改，同步数据库，清理孤儿缩略图
    使用 SQLite 长连接贯穿整个扫描，避免频繁 connect/close
    
    Args:
        check_fingerprint: 为 True 时先对比目录指纹，无变化则直接跳过扫描
    """
    db.init_db()
    if not os.path.isdir(config.PHOTO_ROOT):
        raise FileNotFoundError(f"写真目录不存在: {config.PHOTO_ROOT}")

    # 指纹预检：目录未变化则跳过整个扫描流程
    if check_fingerprint:
        unchanged, _ = fp.is_unchanged(config.PHOTO_ROOT)
        if unchanged:
            print("[扫描] 目录指纹未变化，跳过扫描")
            return {"unchanged": True, "skipped_all": True}

    stats = {
        "added": 0,        # 新增图片数
        "updated": 0,      # mtime 变化的图片数
        "deleted": 0,      # 删除的图片数
        "skipped": 0,      # 未变化的图片数
        "deleted_works": 0,  # 删除的作品数
        "deleted_authors": 0,  # 删除的作者数
    }

    # 开启长连接，所有 db 操作复用同一个 connection
    db.begin_persistent()
    try:
        # 1. 数据库现有作者 {name: id}
        existing_authors = {name: aid for aid, name in db.list_all_authors()}
        disk_author_names = set()

        author_count = 0

        for author_name in sorted(os.listdir(config.PHOTO_ROOT)):
            if cancel_event and cancel_event.is_set():
                print("扫描已取消")
                break

            author_path = os.path.join(config.PHOTO_ROOT, author_name)
            if not os.path.isdir(author_path):
                continue

            author_count += 1
            disk_author_names.add(author_name)

            try:
                _scan_author(
                    author_name, author_path, stats,
                    progress_callback, cancel_event,
                    author_count, total_authors,
                )
            except Exception as e:
                print(f"[扫描作者失败] {author_name}: {e}")
                traceback.print_exc()

            if author_done_callback:
                author_done_callback(author_name, 0)

            if progress_callback:
                progress_callback(
                    "author", author_name, None, 0, author_count, total_authors
                )

            print(f"[作者] {author_name} 已同步")

        # 2. 删除数据库中存在但磁盘不存在的作者
        for author_name, author_id in existing_authors.items():
            if author_name not in disk_author_names:
                db.delete_author(author_id)
                stats["deleted_authors"] += 1
                print(f"[删除作者] {author_name}")

    finally:
        # 提交事务并关闭长连接
        db.end_persistent()

    # 3. 缩略图垃圾回收（不需要长连接）
    removed_thumbs = _gc_thumbnails()
    stats["gc_thumbs"] = removed_thumbs

    if progress_callback:
        progress_callback("done", "", None, 0, author_count, total_authors)

    # 扫描完成后更新目录指纹缓存
    fp.update(config.PHOTO_ROOT)

    print(
        f"扫描完成: 新增 {stats['added']} 张, 更新 {stats['updated']} 张, "
        f"删除 {stats['deleted']} 张图片 / {stats['deleted_works']} 个作品 / "
        f"{stats['deleted_authors']} 个作者, 跳过 {stats['skipped']} 张, "
        f"清理孤儿缩略图 {removed_thumbs} 个"
    )

    return stats
