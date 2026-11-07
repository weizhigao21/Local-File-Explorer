"""视频库增量扫描（根目录/作者/作品/视频文件，纯 Python）

与写真模块的扫描同一套心智，差别只有两点：
1. 视频的「一张图」是多个视频文件，缩略图要等 ffmpeg 抽帧，所以扫描阶段**只记
   封面路径不生成图**（生成交给 UI 的懒加载池，滚动到才抽，首扫不再卡几分钟）；
2. 新增视频顺手用 ffprobe 读时长/分辨率，读不到就留 0（界面显示 --:--），
   不影响入库，下次播放时播放器会回填真实时长。
"""
import hashlib
import os
import traceback

from resource_manager import config
from resource_manager.utils import natural_key
from video_manager import database as db
from video_manager import probe


def is_video(path):
    return os.path.splitext(path)[1].lower() in config.VIDEO_EXTENSIONS


def thumbnail_path(work_path):
    """作品封面路径：按作品目录 md5 命名，目录改名后重新抽帧不会撞车"""
    digest = hashlib.md5(work_path.encode("utf-8")).hexdigest()
    return os.path.join(config.VIDEO_THUMB_DIR, f"{digest}.jpg")


def first_video_path(work_path):
    """作品目录下自然排序的第一个视频（抽帧做封面用），找不到返回 None"""
    try:
        videos, _ = _collect_videos(work_path)
    except OSError:
        return None
    return videos[0][0] if videos else None


def count_authors():
    root = config.VIDEO_ROOT
    if not os.path.isdir(root):
        return 0
    return sum(
        1 for name in os.listdir(root)
        if os.path.isdir(os.path.join(root, name))
    )


def _collect_videos(work_path):
    """递归收集作品目录下的视频文件

    返回 ([(path, name, mtime, size), ...], read_errors)。
    根目录读取失败时抛 OSError —— 调用方据此区分「目录不可读」与「目录已空」，
    否则网络盘/权限抖动会把作品连同记录整批误删。
    """
    videos = []
    read_errors = 0
    stack = [work_path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False) and is_video(entry.path):
                            stat = entry.stat()
                            videos.append(
                                (entry.path, entry.name, int(stat.st_mtime), int(stat.st_size))
                            )
                    except OSError:
                        read_errors += 1
                        continue
        except OSError:
            if current == work_path:
                raise
            read_errors += 1
            continue
    videos.sort(key=lambda item: natural_key(item[0]))
    return videos, read_errors


def _probe_new_videos(rows):
    """给刚入库的视频补时长/分辨率（ffprobe 缺失时整体跳过）"""
    if not rows or not probe.has_ffprobe():
        return 0
    filled = 0
    for path, _name, _mtime, _size in rows:
        info = probe.probe(path)
        if not info:
            continue
        db.update_video_meta(
            path,
            info.get("duration_ms", 0),
            info.get("width", 0),
            info.get("height", 0),
            info.get("codec", ""),
        )
        filled += 1
    return filled


def _sync_work_videos(work_id, current_videos, stats, allow_delete=True):
    """同步单个作品的视频：增、改、删"""
    current_map = {path: (mtime, size) for path, _name, mtime, size in current_videos}
    db_map = db.get_video_mtimes(work_id)

    current_paths = set(current_map)
    db_paths = set(db_map)

    if allow_delete:
        to_delete = db_paths - current_paths
        if to_delete:
            db.delete_videos_by_paths(list(to_delete))
            stats["deleted"] += len(to_delete)

    to_add = current_paths - db_paths
    if to_add:
        rows = []
        for path, name, mtime, size in current_videos:
            if path in to_add:
                rows.append((path, name, mtime, size))
        db.upsert_videos(work_id, rows)
        stats["added"] += len(rows)
        stats["probed"] += _probe_new_videos(rows)

    to_update = [
        (current_map[path][0], current_map[path][1], path)
        for path in current_paths & db_paths
        if current_map[path] != db_map[path]
    ]
    for mtime, size, path in to_update:
        db.update_video_file(path, mtime, size)
    stats["updated"] += len(to_update)

    stats["skipped"] += max(len(current_paths & db_paths) - len(to_update), 0)


def _scan_author(author_name, author_path, stats, progress_callback, cancel_event,
                 author_count, total_authors):
    author_id = db.get_author_id(author_name)
    if author_id is None:
        author_id = db.add_author(author_name)

    existing_works = db.list_works_by_author(author_id)
    disk_work_paths = set()
    new_works = []

    for work_name in sorted(os.listdir(author_path), key=natural_key):
        if cancel_event and cancel_event.is_set():
            return

        work_path = os.path.join(author_path, work_name)
        if not os.path.isdir(work_path):
            continue

        try:
            current_videos, read_errors = _collect_videos(work_path)
        except OSError as e:
            print(f"[扫描作品跳过] 无法读取 {author_name}/{work_name}: {e}")
            disk_work_paths.add(work_path)
            continue
        except Exception as e:  # noqa: BLE001 - 单个作品异常不能中断整轮扫描
            print(f"[扫描作品失败] {author_name}/{work_name}: {e}")
            traceback.print_exc()
            disk_work_paths.add(work_path)
            continue

        if read_errors:
            disk_work_paths.add(work_path)
        if not current_videos:
            # 空目录（或只有非视频文件）不入库，避免卡片墙上出现空作品
            continue

        disk_work_paths.add(work_path)

        if work_path in existing_works:
            work_id, _, _ = existing_works[work_path]
            _sync_work_videos(work_id, current_videos, stats, allow_delete=(read_errors == 0))
        else:
            new_works.append(
                (work_name, work_path, thumbnail_path(work_path), current_videos)
            )
            stats["added"] += len(current_videos)

        if progress_callback:
            progress_callback(
                "work", author_name, work_name, len(current_videos),
                author_count, total_authors,
            )

    for work_path, (work_id, _, _) in existing_works.items():
        if work_path not in disk_work_paths:
            db.delete_work(work_id)
            stats["deleted_works"] += 1

    if new_works:
        db.add_author_with_works(author_name, new_works)
        stats["probed"] += _probe_new_videos(
            [row for _, _, _, videos in new_works for row in videos]
        )


def _gc_thumbnails():
    """清理孤儿封面：VIDEO_THUMB_DIR 下数据库未引用的 .jpg"""
    if not os.path.isdir(config.VIDEO_THUMB_DIR):
        return 0

    referenced = {os.path.basename(p) for p in db.list_all_thumbnail_paths() if p}
    removed = 0
    for fname in os.listdir(config.VIDEO_THUMB_DIR):
        if not fname.endswith(".jpg") or fname in referenced:
            continue
        try:
            os.remove(os.path.join(config.VIDEO_THUMB_DIR, fname))
            removed += 1
        except OSError as e:
            print(f"[清理视频封面失败] {fname}: {e}")
    return removed


def scan(progress_callback=None, author_done_callback=None, cancel_event=None,
         total_authors=0):
    """增量扫描视频根目录，返回 stats

    stats 键：added / updated / deleted / skipped / probed /
             deleted_works / deleted_authors / gc_thumbs / cancelled
    """
    db.init_db()
    root = config.VIDEO_ROOT
    if not os.path.isdir(root):
        raise FileNotFoundError(f"视频目录不存在: {root}")

    stats = {
        "added": 0,
        "updated": 0,
        "deleted": 0,
        "skipped": 0,
        "probed": 0,
        "deleted_works": 0,
        "deleted_authors": 0,
    }

    db.begin_persistent()
    cancelled = False
    author_count = 0
    existing_authors = {}

    try:
        # 扫描前先取作者快照：必须是 dict 而不是查一次用一次，
        # 取消时要拿「扫描开始时的全集」做基准才安全（见下方注释）
        existing_authors = {name: aid for aid, name in db.list_all_authors()}
        disk_author_names = set()

        try:
            author_dirs = sorted(os.listdir(root), key=natural_key)
        except OSError as e:
            raise FileNotFoundError(f"无法读取视频目录 {root}: {e}") from e

        for author_name in author_dirs:
            if cancel_event and cancel_event.is_set():
                cancelled = True
                break

            author_path = os.path.join(root, author_name)
            if not os.path.isdir(author_path):
                continue

            author_count += 1
            disk_author_names.add(author_name)

            try:
                _scan_author(
                    author_name, author_path, stats,
                    progress_callback, cancel_event, author_count, total_authors,
                )
            except Exception as e:  # noqa: BLE001 - 单作者失败不能中断整轮
                print(f"[扫描作者失败] {author_name}: {e}")
                traceback.print_exc()

            if author_done_callback:
                author_done_callback(author_name, 0)
            if progress_callback:
                progress_callback("author", author_name, None, 0, author_count, total_authors)

        # 删除磁盘上已消失的作者：取消时不做，disk_author_names 只含遍历到的部分
        if not cancelled:
            for author_name, author_id in existing_authors.items():
                if author_name not in disk_author_names:
                    db.delete_author(author_id)
                    stats["deleted_authors"] += 1
    finally:
        db.end_persistent()

    if cancelled:
        stats["cancelled"] = True
        if progress_callback:
            progress_callback("done", "", None, 0, author_count, total_authors)
        return stats

    stats["gc_thumbs"] = _gc_thumbnails()
    if progress_callback:
        progress_callback("done", "", None, 0, author_count, total_authors)
    return stats
