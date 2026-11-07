"""视频库增量扫描（根目录/作者/作品/视频文件，纯 Python）

层级判定规则（v1.9.1）：
    **顶层目录自己直接躺着视频 → 它本身就是「作品」**；否则它当「作者」，往下找作品。
    所以三种库结构都能用：
      根/剧集/第01集.mp4          → 剧集 = 作品（归到以根目录名命的「归集作者」下）
      根/社团/作品/第01集.mp4      → 社团 = 作者，作品 = 作品
      根/第01集.mp4               → 根目录自己当一个作品
    老逻辑只认三层，两层库（很常见）里的视频会被整批静默丢弃。
    代价：若某个「作者」目录里既有子文件夹、又直接躺着视频，会被合并成一个作品
    （宁可在卡片上看到全部，也不要悄悄少几个文件）。

多根目录（v1.10.0）：
    VIDEO_ROOTS 可以配多个根，逐个扫描、各自算自己的「归集作者」；
    两棵库出现同名作者时，后来的自动加根目录后缀（「某某（写真@F）」），
    避免 works 表的 UNIQUE(author_id, name) 让两家的同名作品互相顶掉。

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
from resource_manager.utils import natural_key, root_labels, unique_author_name
from video_manager import database as db
from video_manager import probe


def is_video(path):
    return os.path.splitext(path)[1].lower() in config.VIDEO_EXTENSIONS


def video_roots():
    """当前生效的视频根目录列表（运行时读，便于测试 monkeypatch）

    VIDEO_ROOTS 是 v1.10.0 起的多根配置；VIDEO_ROOT 仍是「第一个根目录」的别名。
    """
    roots = [p for p in (getattr(config, "VIDEO_ROOTS", None) or [])
             if isinstance(p, str) and p.strip()]
    if roots:
        return roots
    first = getattr(config, "VIDEO_ROOT", "")
    return [first] if first else []


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
    """统计所有视频根目录下的扫描单元数（顶层目录 + 根目录自己含视频时 +1）"""
    total = 0
    for root in video_roots():
        if not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        total += sum(
            1 for name in names
            if os.path.isdir(os.path.join(root, name))
        )
        if _loose_videos(root):
            total += 1  # 根目录自己躺着的视频也算一个扫描单元
    return total


def _loose_videos(path):
    """目录**顶层**的视频文件（不进子目录），按路径自然排序

    用途只有一个：判断「这个目录本身是不是一个作品」。
    常见于「根/剧集名/第01集.mp4」这种两层库——顶层目录下面直接就是视频，
    它没有子文件夹可以当作品，按老逻辑整批视频会被静默丢掉。
    """
    videos = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False) and is_video(entry.path):
                        stat = entry.stat()
                        videos.append(
                            (entry.path, entry.name, int(stat.st_mtime), int(stat.st_size))
                        )
                except OSError:
                    continue
    except OSError:
        return []
    videos.sort(key=lambda item: natural_key(item[0]))
    return videos


def _flat_author_name(root):
    """两层库没有「作者」这一层，用一个归集作者把作品挂住

    名字取根目录自己的名字（如「动漫」），比「未分类」更有信息量；
    根目录是盘符（"F:\\"）时 basename 为空，退化成「未分类」。
    """
    name = os.path.basename(os.path.normpath(root)).strip()
    return name or "未分类"


def _root_work_name(root):
    """根目录自身作为作品时的名字（根目录直接放了一堆视频的情况）"""
    return f"{_flat_author_name(root)}（根目录）"


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


def _sync_flat_works(author_name, items, stats, progress_callback, cancel_event,
                     author_count, total_authors):
    """把「本身就是作品」的顶层目录收进归集作者下

    items: [(work_name, work_path), ...]
    与 `_scan_author` 同一套增量语义，差别只在作品不是「作者目录的子目录」，
    而是顶层目录本身。
    """
    author_id = db.get_author_id(author_name)
    if author_id is None:
        author_id = db.add_author(author_name)

    existing_works = db.list_works_by_author(author_id)
    disk_work_paths = set()
    new_works = []

    for work_name, work_path in items:
        if cancel_event and cancel_event.is_set():
            return

        try:
            current_videos, read_errors = _collect_videos(work_path)
        except OSError as e:
            print(f"[扫描作品跳过] 无法读取 {work_path}: {e}")
            disk_work_paths.add(work_path)
            continue
        except Exception as e:  # noqa: BLE001 - 单个作品异常不能中断整轮扫描
            print(f"[扫描作品失败] {work_path}: {e}")
            traceback.print_exc()
            disk_work_paths.add(work_path)
            continue

        if read_errors:
            disk_work_paths.add(work_path)
        if not current_videos:
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
    configured = video_roots()
    roots = [r for r in configured if os.path.isdir(r)]
    if not roots:
        raise FileNotFoundError(
            "视频目录不存在: " + "、".join(configured or ["（未配置）"])
        )
    # 配了但当前读不到的根目录（移动硬盘没插、NAS 掉线）：本轮不做作者清理
    missing_roots = [r for r in configured if r not in roots]

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
    skipped_roots = []      # 读不动的根目录：本轮不拿它当删除基准
    claimed = {}            # 作者名 → 归属根目录，用于跨根同名去重
    labels = root_labels(roots)
    author_count = 0
    existing_authors = {}

    try:
        # 扫描前先取作者快照：必须是 dict 而不是查一次用一次，
        # 取消时要拿「扫描开始时的全集」做基准才安全（见下方注释）
        existing_authors = {name: aid for aid, name in db.list_all_authors()}
        disk_author_names = set()

        # 根目录按配置顺序处理：顺序决定同名作者谁保留原名（先到先得）
        for root in roots:
            if cancel_event and cancel_event.is_set():
                cancelled = True
                break

            try:
                author_dirs = sorted(os.listdir(root), key=natural_key)
            except OSError as e:
                print(f"[扫描跳过] 无法读取视频目录 {root}: {e}")
                skipped_roots.append(root)
                continue

            # 先把每个顶层目录分类：自己就躺着视频的算「作品」，否则算「作者往下找作品」。
            # 不分类直接当作者，两层库（根/剧集/第01集.mp4）会一个视频都进不了库。
            units = []
            for author_name in author_dirs:
                author_path = os.path.join(root, author_name)
                if not os.path.isdir(author_path):
                    continue
                units.append(("work" if _loose_videos(author_path) else "author",
                              author_name, author_path))

            # 归集作者（两层库的作品挂这里）也要跨根去重
            flat_author = unique_author_name(
                _flat_author_name(root), root, claimed, labels.get(root)
            )
            if _loose_videos(root):
                units.append(("work", _root_work_name(root), root))

            flat_items = []
            for kind, unit_name, unit_path in units:
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    break

                author_count += 1

                if kind == "author":
                    # 作者目录单独成作者；跨根同名时同样要区分，否则两家作品会混进同一条记录
                    display_name = unique_author_name(
                        unit_name, root, claimed, labels.get(root)
                    )
                    disk_author_names.add(display_name)
                    try:
                        _scan_author(
                            display_name, unit_path, stats,
                            progress_callback, cancel_event, author_count, total_authors,
                        )
                    except Exception as e:  # noqa: BLE001 - 单作者失败不能中断整轮
                        print(f"[扫描作者失败] {display_name}: {e}")
                        traceback.print_exc()
                else:
                    # 归集作者必须提前登记，否则会被下面的「作者已消失」清理误删
                    disk_author_names.add(flat_author)
                    flat_items.append((unit_name, unit_path))

                if author_done_callback:
                    author_done_callback(unit_name, 0)
                if progress_callback:
                    progress_callback("author", unit_name, None, 0, author_count, total_authors)

            if flat_items and not cancelled:
                try:
                    _sync_flat_works(
                        flat_author, flat_items, stats,
                        progress_callback, cancel_event, author_count, total_authors,
                    )
                except Exception as e:  # noqa: BLE001 - 单作者失败不能中断整轮
                    print(f"[扫描作者失败] {flat_author}: {e}")
                    traceback.print_exc()

        # 删除磁盘上已消失的作者：取消时不做，disk_author_names 只含遍历到的部分；
        # 有根目录读不动时也不做，那是"没读到"而不是"已删除"。
        if cancelled or skipped_roots or missing_roots:
            if skipped_roots:
                print(f"[扫描] 有 {len(skipped_roots)} 个根目录不可读，跳过作者清理")
            if missing_roots:
                print(f"[扫描] 有 {len(missing_roots)} 个根目录不存在，跳过作者清理")
        else:
            # 两层库的归集作者也在这里一并消失（最后一个剧集文件夹被删掉时）。
            # 它的作品是被 delete_author 级联清掉的，不会经过 _sync_flat_works，
            # 所以先按现有作品数把统计补上，否则界面提示永远是「删除 0 个作品」。
            for author_name, author_id in existing_authors.items():
                if author_name in disk_author_names:
                    continue
                stats["deleted_works"] += len(db.list_works_by_author(author_id))
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
