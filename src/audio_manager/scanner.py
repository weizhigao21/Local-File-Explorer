"""
音频模块目录扫描（递归）
每个包含音频文件的文件夹视为一个歌单
支持嵌套目录结构，根目录中的音频也会被收录
"""
import os
import re

from audio_manager import database as db
from resource_manager import fingerprint_cache as fp
from resource_manager.config import AUDIO_ROOT

AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".aac", ".ogg", ".wma", ".m4a"}


def _natural_key(s: str):
    """自然排序 key：将字符串中的数字部分按整数比较
    使 '10.mp3' 排在 '2.mp3' 之后，而非字典序
    """
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]

# 根目录歌单名称
ROOT_PLAYLIST_NAME = "音频根目录"


def _is_audio(path):
    return os.path.splitext(path)[1].lower() in AUDIO_EXTENSIONS


def _read_tags_file(tags_path):
    if not os.path.exists(tags_path):
        return ""
    try:
        with open(tags_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _sync_playlist(folder_path, name, parent_path, existing, stats,
                   progress_callback=None, idx=None, total=None,
                   allow_empty=False):
    """同步单个文件夹的歌单和曲目"""
    if progress_callback and idx is not None and total:
        progress_callback(idx, total, f"正在扫描: {name}")

    norm_folder = os.path.normpath(folder_path)

    cover = os.path.join(norm_folder, "封面.jpg")
    if not os.path.exists(cover):
        cover = os.path.join(norm_folder, "cover.jpg")
        if not os.path.exists(cover):
            cover = None

    tags = _read_tags_file(os.path.join(norm_folder, "标签.txt"))

    # 收集音频文件
    tracks = []
    for fname in sorted(os.listdir(norm_folder), key=_natural_key):
        fpath = os.path.normpath(os.path.join(norm_folder, fname))
        if os.path.isfile(fpath) and _is_audio(fpath):
            title = os.path.splitext(fname)[0]
            tracks.append((title, fpath, 0, len(tracks) + 1))

    # 无音频文件且不允许空歌单则跳过（容器歌单 allow_empty=True 时会创建）
    if not tracks and not allow_empty:
        return

    if norm_folder in existing:
        pid = existing[norm_folder]
        db.update_playlist(pid, cover=cover, tags=tags,
                           track_count=len(tracks),
                           name=name, parent_path=parent_path,
                           mtime=os.path.getmtime(norm_folder))
        # 增量同步曲目
        current_paths = {t[1] for t in tracks}
        existing_tracks = {t["path"] for t in db.list_tracks(pid)}
        to_add = [t for t in tracks if t[1] not in existing_tracks]
        if to_add:
            db.add_tracks(pid, to_add)
            stats["added_tracks"] += len(to_add)
        removed = len(existing_tracks - current_paths)
        if removed:
            db.delete_tracks_not_in(pid, list(current_paths))
            stats["removed_tracks"] += removed
        stats["skipped"] += 1
    else:
        pid = db.add_playlist(name, norm_folder, cover=cover,
                              tags=tags, parent_path=parent_path,
                              mtime=os.path.getmtime(norm_folder))
        if pid and tracks:
            db.add_tracks(pid, tracks)
            db.update_playlist(pid, track_count=len(tracks))
            stats["added_tracks"] += len(tracks)
        stats["added_playlists"] += 1


def _scan_dir(root, current_dir, parent_path, existing, disk_paths, stats,
              progress_callback, cancel_event, counter):
    """
    递归扫描目录
    root: 音频根目录
    current_dir: 当前待扫描目录
    parent_path: 父目录路径（数据库中的 parent_path）
    """
    current_dir = os.path.normpath(current_dir)
    if cancel_event and cancel_event.is_set():
        return

    # 将该目录记入磁盘路径，确保后续清理不会误删
    disk_paths.add(current_dir)

    # 先收集本级内容，再递归子目录
    subdirs = []
    audio_files = []

    for name in sorted(os.listdir(current_dir), key=_natural_key):
        if cancel_event and cancel_event.is_set():
            return
        path = os.path.normpath(os.path.join(current_dir, name))
        if os.path.isfile(path):
            if _is_audio(path):
                audio_files.append((name, path))
        elif os.path.isdir(path):
            subdirs.append((name, path))

    # 如果本级有音频文件，创建歌单
    has_audio = bool(audio_files)
    if has_audio:
        # 判断是否是根目录
        if current_dir == root:
            pl_name = ROOT_PLAYLIST_NAME
        else:
            pl_name = os.path.basename(current_dir)

        counter["idx"] += 1
        _sync_playlist(
            current_dir, pl_name, parent_path, existing, stats,
            progress_callback, counter["idx"], counter["total"],
        )

    # 更新 total（每次发现新子目录就增加 progress 总数）
    for sname, spath in subdirs:
        counter["total"] += 1

    # 递归子目录
    # parent_path 规则：root 的直接子目录用 ""（根层级），深层子目录用当前目录路径
    for sname, spath in subdirs:
        if cancel_event and cancel_event.is_set():
            return
        disk_paths.add(spath)
        child_parent = "" if current_dir == root else current_dir
        _scan_dir(root, spath, child_parent, existing, disk_paths, stats,
                  progress_callback, cancel_event, counter)

    # 递归完成后：本级无音频但有子歌单 → 创建容器歌单
    if not has_audio:
        child_playlists = db.list_playlists_by_parent(current_dir)
        if child_playlists:
            if current_dir == root:
                pl_name = ROOT_PLAYLIST_NAME
            else:
                pl_name = os.path.basename(current_dir)
            counter["idx"] += 1
            counter["total"] += 1
            _sync_playlist(
                current_dir, pl_name, parent_path, existing, stats,
                progress_callback, counter["idx"], counter["total"],
                allow_empty=True,
            )


def _update_all_container_track_counts():
    """自底向上更新所有歌单的展示曲目数

    展示曲目数 = 自身直接曲目数 + 所有后代子歌单的展示曲目数之和。
    无论歌单自身是否有直接音频，只要它有子歌单就聚合后代数量。
    直接曲目数从 tracks 表实时查询，避免依赖 playlists.track_count 字段
    （指纹未变跳过扫描时，track_count 可能是上一轮的聚合值，直接用作"直接数"会重复累加）。
    """
    all_pl = {pl["path"]: pl for pl in db.list_playlists()}
    if not all_pl:
        return

    # 构建 parent_path -> [child_path, ...] 索引
    children_map = {}
    for path, pl in all_pl.items():
        pp = pl["parent_path"]
        if pp not in children_map:
            children_map[pp] = []
        children_map[pp].append(path)

    # 一次性查询每个歌单的直接曲目数（从 tracks 表，避免依赖 track_count 是否已被刷新）
    direct_counts = {}
    with db._active_conn() as conn:
        rows = conn.execute(
            "SELECT playlist_id, COUNT(*) AS cnt FROM tracks GROUP BY playlist_id"
        ).fetchall()
        for row in rows:
            direct_counts[row["playlist_id"]] = row["cnt"]

    audio_root_norm = os.path.normpath(AUDIO_ROOT)

    # 按深度排序(深->浅)，从叶子向根处理
    # 对两种路径分隔符都计数，兼容 Windows/Unix 风格的路径字符串
    sorted_paths = sorted(
        all_pl.keys(),
        key=lambda p: p.count(os.sep) + p.count('/'),
        reverse=True,
    )

    aggregate_total = {}  # 父容器聚合用（始终包含子歌单）
    card_count = {}       # 卡片上显示的 track_count

    for path in sorted_paths:
        pl = all_pl[path]
        own_count = direct_counts.get(pl["id"], 0)

        # 获取直接子歌单路径
        # 根目录特殊处理：根的直接子歌单 parent_path=""，但根本身也是 parent_path=""，
        # 需要把根本身从 children_map[""] 中排除，避免自引用
        if path == audio_root_norm:
            child_paths = [p for p in children_map.get("", []) if p != path]
        else:
            child_paths = children_map.get(path, [])

        children_total = sum(aggregate_total.get(cp, 0) for cp in child_paths)
        aggregate_total[path] = own_count + children_total

        # 卡片显示：有直接音频仅显示本级曲目，纯容器歌单显示聚合值
        display = own_count if own_count > 0 else children_total
        card_count[path] = display

        # 与数据库现有值不一致才更新，减少无谓写入
        if display != pl["track_count"]:
            db.update_playlist(pl["id"], track_count=display)


def scan(audio_root=None, progress_callback=None, cancel_event=None,
         check_fingerprint=True):
    """
    递归扫描音频目录
    每层文件夹（含音频）都是一个歌单，支持层级嵌套
    
    Args:
        check_fingerprint: 为 True 时先对比目录指纹，无变化则直接跳过扫描
    """
    root = os.path.normpath(audio_root or AUDIO_ROOT)
    db.init_db()

    if not os.path.isdir(root):
        raise FileNotFoundError(f"音频目录不存在: {root}")

    # 指纹预检：目录未变化则跳过整个扫描流程
    # 需要追踪封面图(.jpg/.png)和标签(.txt)，确保这些文���变更也能触发重新扫描
    _FP_EXTENSIONS = AUDIO_EXTENSIONS | {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".txt"}
    if check_fingerprint:
        unchanged, _ = fp.is_unchanged(root, extensions=_FP_EXTENSIONS)
        if unchanged:
            # 即使目录指纹未变，如果数据库为空（被手动删除等），仍需重新扫描
            existing_playlists = db.list_playlists()
            if len(existing_playlists) > 0:
                print("[音频扫描] 目录指纹未变化，跳过扫描")
                # 即使跳过扫描，仍需运行容器歌单聚合，确保 track_count 与代码逻辑一致
                _update_all_container_track_counts()
                if progress_callback:
                    progress_callback(1, 1, "目录无变化，已跳过扫描")
                return {"unchanged": True, "skipped_all": True}
            else:
                print("[音频扫描] 目录指纹未变化，但数据库为空，强制执行扫描")

    stats = {"added_playlists": 0, "removed_playlists": 0,
             "added_tracks": 0, "removed_tracks": 0, "skipped": 0}

    existing_raw = db.get_playlist_paths()
    existing = {os.path.normpath(k): v for k, v in existing_raw.items()}
    disk_paths = set()

    # 开启持久连接，扫描期间所有 DB 操作复用同一连接
    db.begin_persistent()
    try:
        # 先粗算总数：根直接子目录作为初始 total
        initial_total = sum(
            1 for name in os.listdir(root)
            if os.path.isdir(os.path.join(root, name))
        )
        # 如果根目录本身可能有音频，也需要占位
        root_has_audio = any(
            _is_audio(os.path.join(root, f))
            for f in os.listdir(root)
            if os.path.isfile(os.path.join(root, f))
        )
        if root_has_audio:
            initial_total += 1

        counter = {"idx": 0, "total": max(initial_total, 1)}

        # 递归扫描所有目录
        _scan_dir(root, root, "", existing, disk_paths, stats,
                  progress_callback, cancel_event, counter)

        # 删除不存在的歌单（包含子歌单递归删除）
        for path, pid in existing.items():
            if path not in disk_paths:
                db.delete_playlist(pid)
                stats["removed_playlists"] += 1
    finally:
        db.end_persistent()

    if progress_callback:
        progress_callback(counter["total"], counter["total"], "扫描完成")

    # 聚合容器歌单（仅有子歌单、无直接音频文件的歌单）的曲目数
    _update_all_container_track_counts()

    # 扫描完成后更新目录指纹缓存（需与预检使用相同的扩展名集合）
    fp.update(root, extensions=_FP_EXTENSIONS)

    print(
        f"音频扫描完成: 新增 {stats['added_playlists']} 个歌单, "
        f"删除 {stats['removed_playlists']} 个歌单, "
        f"新增 {stats['added_tracks']} 首曲目, "
        f"删除 {stats['removed_tracks']} 首曲目, "
        f"跳过 {stats['skipped']} 个歌单"
    )

    return stats
