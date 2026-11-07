"""网页端数据视图（纯 Python，禁止 Qt）

把三个模块的 SQLite 读成前端能直接渲染的 JSON。

两条硬规矩：

1. **媒体只按数据库 id 暴露**。前端拿不到、也传不进任何文件路径，
   目录穿越在协议层就不存在（`http_util.static_file_path` 只负责前端静态资源）。
2. **缺封面就地补**。桌面端是「滚动到才抽帧」，手机端第一次访问很可能
   遇到还没生成封面的作品；这里按需生成并落到与桌面**同一路径**，
   桌面下次直接用，不会重复算。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading

from audio_manager import database as audio_db
from audio_manager import subtitles as audio_subtitles
from resource_manager import config
from resource_manager import database as photo_db
from video_manager import database as video_db
from video_manager import probe
from video_manager import scanner as video_scanner
from video_manager.progress import decide_progress
from web_server import http_util

# ==================== 初始化 ====================

def init_all():
    """确保三个库的表都存在：否则首次访问会撞 `no such table: works`"""
    errors = []
    for name, module in (("photo", photo_db), ("audio", audio_db), ("video", video_db)):
        try:
            module.init_db()
        except (sqlite3.Error, OSError) as e:
            errors.append(f"{name}: {e}")
    return errors


# ==================== 通用小工具 ====================

_lock_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}

# 同时进行的缩图解码数（大图解码很吃内存，并发太高会把机器拖慢）
_DECODE_SLOTS = threading.Semaphore(2)


def _lock_for(key):
    """按缓存文件路径取锁：同一个封面并发请求只生成一次，不同封面互不排队"""
    with _lock_guard:
        if len(_locks) > 512:
            _locks.clear()
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def _rows(db_module, sql, params=()):
    with db_module.get_conn() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _row(db_module, sql, params=()):
    with db_module.get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def _count(db_module, table):
    row = _row(db_module, f"SELECT COUNT(*) AS c FROM {table}")
    return int(row["c"]) if row else 0


def _basename(path):
    return os.path.basename(str(path or "").replace("\\", "/"))


# ==================== 分页 ====================

# 网页端列表默认每页条数 / 上限（page_size 参数超出会被钳回来）
PAGE_DEFAULT = 60
PAGE_MAX = 200


def _paged(items, page, page_size):
    """内存分页：返回 items / total / page / pages，page_size<=0 时按默认值"""
    total = len(items)
    try:
        page = max(int(page or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(page_size or PAGE_DEFAULT)
    except (TypeError, ValueError):
        page_size = PAGE_DEFAULT
    page_size = min(max(page_size, 1), PAGE_MAX)
    start = (page - 1) * page_size
    return {
        "items": items[start:start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        # ceil：4 页 × 60 = 240 条也能在第 4 页装下 241 条
        "pages": max(1, -(-total // page_size)),
    }


def _exists(path):
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


# ==================== 总览 ====================

def summary():
    """首页统计：三个模块各自的数量 + 版本号（前端用来显示「库里还没东西」）"""
    photo_roots = list(getattr(config, "PHOTO_ROOTS", None) or [config.PHOTO_ROOT])
    video_roots = list(getattr(config, "VIDEO_ROOTS", None) or [config.VIDEO_ROOT])
    return {
        "version": config.APP_VERSION,
        "photo": {
            "works": _count(photo_db, "works"),
            "images": _count(photo_db, "images"),
            "roots": photo_roots,
            "root_exists": any(os.path.isdir(p) for p in photo_roots),
        },
        "audio": {
            "playlists": _count(audio_db, "playlists"),
            "tracks": _count(audio_db, "tracks"),
            "roots": list(config.AUDIO_ROOTS),
            "root_exists": any(os.path.isdir(p) for p in config.AUDIO_ROOTS),
        },
        "video": {
            "works": _count(video_db, "works"),
            "videos": _count(video_db, "videos"),
            "roots": video_roots,
            "root_exists": any(os.path.isdir(p) for p in video_roots),
        },
    }


# ==================== 写真 ====================

def photo_authors():
    authors = photo_db.list_authors()
    return [
        {"id": a["id"], "name": a["name"], "work_count": int(a.get("work_count") or 0)}
        for a in authors
    ]


def photo_works(author_id=None, keyword=""):
    works = photo_db.list_works(author_id)
    keyword = (keyword or "").strip().lower()
    if keyword:
        works = [
            w for w in works
            if keyword in (w.get("name") or "").lower()
            or keyword in (w.get("author") or "").lower()
        ]
    return [
        {
            "id": w["id"],
            "name": w["name"],
            "author": w["author"],
            "image_count": int(w.get("image_count") or 0),
            "cover": f"/media/photo-cover/{w['id']}",
        }
        for w in works
    ]


def photo_works_page(author_id=None, keyword="", page=1, page_size=0):
    """分页版写真作品列表（网页端用）：切到写真页只拉一页，不再一次全量"""
    return _paged(photo_works(author_id, keyword), page, page_size)


def photo_work(work_id):
    return _row(photo_db, "SELECT id, name, path, thumbnail FROM works WHERE id = ?", (work_id,))


def photo_cover(work_id):
    """作品封面文件路径；没有就现场缩一张（与桌面同一命名，避免重复生成）"""
    work = photo_work(work_id)
    if not work:
        return ""
    if _exists(work.get("thumbnail")):
        return work["thumbnail"]

    images = photo_db.get_images_by_work(work_id)
    if not images:
        return ""

    target = work.get("thumbnail") or _photo_thumb_path(work["path"])
    with _lock_for(target):
        if _exists(target):
            return target
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        try:
            from PIL import Image

            with Image.open(images[0]["path"]) as im:
                # 桌面端按 240 生成；网页卡片在 2 倍屏上明显发虚，这里取 480，
                # 桌面复用时只会更清楚，不会被降级。
                im.thumbnail((config.THUMBNAIL_SIZE * 2, config.THUMBNAIL_SIZE * 2))
                if im.mode in ("RGBA", "P"):
                    im = im.convert("RGB")
                im.save(target, "JPEG", quality=85)
            return target
        except Exception as e:  # noqa: BLE001 - 单张封面失败不该让整个列表 500
            print(f"[网页封面] 生成失败 {images[0]['path']}: {e}")
            return ""


def _photo_thumb_path(work_path):
    digest = hashlib.md5(str(work_path).encode("utf-8")).hexdigest()
    return os.path.join(config.THUMBNAIL_DIR, f"{digest}.jpg")


def photo_work_images(work_id):
    work = photo_work(work_id)
    if not work:
        return None
    images = photo_db.get_images_by_work(work_id)
    return {
        "work": {
            "id": work["id"],
            "name": work["name"],
            "cover": f"/media/photo-cover/{work['id']}",
        },
        "images": [
            {
                "id": img["id"],
                "name": _basename(img["path"]),
                "thumb": f"/media/photo/{img['id']}?w=640",
                # 看图用的主图：1600 宽在手机/平板上已经超过屏幕像素密度所需，
                # 体积却只有原图（常见 10~40MB）的几十分之一
                "full": f"/media/photo/{img['id']}?w=1600",
                # 真要 100% 放大看细节时才点「原图」，按需加载
                "orig": f"/media/photo/{img['id']}",
            }
            for img in images
        ],
    }


def photo_image_path(image_id):
    row = _row(photo_db, "SELECT path FROM images WHERE id = ?", (image_id,))
    return row["path"] if row else ""


def scaled_photo(path, width):
    """按宽度返回缓存缩图路径；宽度不在白名单或缩放失败时回原图

    相册网格一次要几十张原图（每张 3-8MB），手机上根本滚不动，
    所以统一在这里缩：按 md5(路径)_宽.jpg 落盘，第二次直接命中。

    解码走 `im.draft()`：JPEG 可以按 1/2、1/4、1/8 直接解码，
    6000×4000 的原图缩到 1600 宽只需解 1/4 的像素，比全解再缩快 3~5 倍，
    否则第一次打开一本写真时 CPU 会被单张图吃满好几秒。
    """
    try:
        width = int(width or 0)
    except (TypeError, ValueError):
        return path
    if width not in config.WEB_THUMB_WIDTHS or not _exists(path):
        return path

    digest = hashlib.md5(os.path.abspath(path).encode("utf-8")).hexdigest()
    target = os.path.join(config.WEB_CACHE_DIR, f"{digest}_{width}.jpg")
    if _exists(target):
        return target

    with _lock_for(target):
        if _exists(target):
            return target
        os.makedirs(config.WEB_CACHE_DIR, exist_ok=True)
        # 全局限流：同时只解两张。一本写真里连点十几张时，
        # 无限制并发解码会把内存和 CPU 一起打满，反而全都变慢。
        with _DECODE_SLOTS:
            try:
                from PIL import Image

                with Image.open(path) as im:
                    # draft 必须在 load()/thumbnail() 之前调用；非 JPEG 会静默无效
                    im.draft("RGB", (width, width * 4))
                    im.thumbnail((width, width * 4))
                    if im.mode in ("RGBA", "P"):
                        im = im.convert("RGB")
                    tmp = f"{target}.tmp.jpg"
                    # 渐进式：手机先看到模糊轮廓再逐步清晰，比「空白等到齐全」感受快得多
                    im.save(tmp, "JPEG", quality=84, progressive=True)
                    os.replace(tmp, target)
                return target
            except Exception as e:  # noqa: BLE001 - 缩图失败就退回原图，不影响看图
                print(f"[网页缩图] 失败 {path}: {e}")
                return path


# ==================== 音频 ====================

def _playlist_cover(playlist, by_path, depth=0):
    """歌单封面：自己没有就用最近一层父歌单的（与桌面 catalog 的继承规则一致）"""
    cover = playlist.get("cover") or ""
    if _exists(cover):
        return cover
    if depth >= 6:
        return ""
    parent = by_path.get(playlist.get("parent_path") or "")
    if not parent:
        return ""
    return _playlist_cover(parent, by_path, depth + 1)


def _playlist_brief(playlist, by_path, child_counts, track_counts):
    # 以 tracks 表的实际行数为准：老库的 track_count 可能是 0（扫描器没回填），
    # 直接用会显示成「空歌单」，而点进去明明有歌。
    return {
        "id": playlist["id"],
        "name": playlist["name"],
        "track_count": int(track_counts.get(playlist["path"], playlist.get("track_count") or 0)),
        "child_count": int(child_counts.get(playlist["path"], 0)),
        "tags": playlist.get("tags") or "",
        "cover": f"/media/audio-cover/{playlist['id']}",
        "has_cover": bool(_playlist_cover(playlist, by_path)),
        "parent_path": playlist.get("parent_path") or "",
    }


def audio_playlists(parent=""):
    """指定层级下的歌单；parent="" 即顶层"""
    playlists = audio_db.list_playlists()
    by_path = {p["path"]: p for p in playlists}
    child_counts = {}
    for p in playlists:
        key = p.get("parent_path") or ""
        child_counts[key] = child_counts.get(key, 0) + 1

    parent = parent or ""
    items = [p for p in playlists if (p.get("parent_path") or "") == parent]
    items.sort(key=lambda p: p["name"])
    track_counts = _track_counts_by_path()
    return [_playlist_brief(p, by_path, child_counts, track_counts) for p in items]


def audio_playlists_page(parent="", page=1, page_size=0):
    """分页版歌单列表（网页端用）"""
    return _paged(audio_playlists(parent), page, page_size)


def _track_counts_by_path():
    """{歌单路径: 曲目数}（一次 GROUP BY，避免每个歌单查一遍）"""
    rows = _rows(
        audio_db,
        "SELECT p.path AS path, COUNT(t.id) AS c FROM playlists p "
        "LEFT JOIN tracks t ON t.playlist_id = p.id GROUP BY p.id",
    )
    return {row["path"]: int(row["c"] or 0) for row in rows}


def audio_playlist(playlist_id):
    playlist = audio_db.get_playlist(playlist_id)
    if not playlist:
        return None

    playlists = audio_db.list_playlists()
    by_path = {p["path"]: p for p in playlists}
    child_counts = {}
    for p in playlists:
        key = p.get("parent_path") or ""
        child_counts[key] = child_counts.get(key, 0) + 1

    track_counts = _track_counts_by_path()
    children = [
        _playlist_brief(p, by_path, child_counts, track_counts)
        for p in playlists
        if (p.get("parent_path") or "") == playlist["path"]
    ]
    children.sort(key=lambda p: p["name"])

    tracks = audio_db.list_tracks(playlist_id)
    return {
        "playlist": {
            "id": playlist["id"],
            "name": playlist["name"],
            "tags": playlist.get("tags") or "",
            "cover": f"/media/audio-cover/{playlist['id']}",
            "has_cover": bool(_playlist_cover(playlist, by_path)),
            "parent_path": playlist.get("parent_path") or "",
            "track_count": int(track_counts.get(playlist["path"], 0)),
        },
        "children": children,
        "tracks": [
            {
                "id": t["id"],
                "title": t.get("title") or _basename(t["path"]),
                "url": f"/media/audio/{t['id']}",
                "duration_ms": int(float(t.get("duration") or 0) * 1000),
                "index": index,
            }
            for index, t in enumerate(tracks)
        ],
    }


def audio_track(track_id):
    return _row(
        audio_db,
        "SELECT id, playlist_id, title, path, duration FROM tracks WHERE id = ?",
        (track_id,),
    )


def audio_cover_path(playlist_id):
    playlist = audio_db.get_playlist(playlist_id)
    if not playlist:
        return ""
    playlists = audio_db.list_playlists()
    by_path = {p["path"]: p for p in playlists}
    return _playlist_cover(playlist, by_path)


def audio_subtitle_cues(track_id):
    """曲目字幕（同目录 srt/vtt/lrc/ass → 统一 JSON cue 列表）

    找不到字幕返回 None，前端据此不显示字幕开关。
    """
    track = audio_track(track_id)
    if not track:
        return None
    subtitle_path = audio_subtitles.find_subtitle(track["path"])
    if not subtitle_path:
        return None
    loaded = audio_subtitles.load_subtitle(subtitle_path)
    cues = [
        {"start_ms": int(c.start_ms), "end_ms": int(c.end_ms), "text": c.text}
        for c in loaded.cues[:5000]
    ]
    if not cues:
        return None
    return {
        "name": _basename(subtitle_path),
        "format": loaded.format,
        "cues": cues,
        "vtt": http_util.cues_to_vtt(loaded.cues),
    }


# ==================== 视频 ====================

def video_authors():
    return [
        {
            "id": a["id"],
            "name": a["name"],
            "work_count": int(a.get("work_count") or 0),
            "video_count": int(a.get("video_count") or 0),
        }
        for a in video_db.list_authors()
    ]


def video_works(author_id=None, keyword=""):
    works = video_db.list_works(author_id)
    keyword = (keyword or "").strip().lower()
    if keyword:
        works = [
            w for w in works
            if keyword in (w.get("name") or "").lower()
            or keyword in (w.get("author") or "").lower()
        ]
    return [
        {
            "id": w["id"],
            "name": w["name"],
            "author": w["author"],
            "video_count": int(w.get("video_count") or 0),
            "total_duration_ms": int(w.get("total_duration_ms") or 0),
            "cover": f"/media/video-cover/{w['id']}",
        }
        for w in works
    ]


def video_works_page(author_id=None, keyword="", page=1, page_size=0):
    """分页版视频作品列表（网页端用）"""
    return _paged(video_works(author_id, keyword), page, page_size)


def video_work(work_id):
    return video_db.get_work(work_id)


def video_cover(work_id):
    """作品封面：没生成过就现场 ffmpeg 抽一帧（可能与桌面同时发生，用锁挡）"""
    work = video_work(work_id)
    if not work:
        return ""
    target = work.get("thumbnail") or video_scanner.thumbnail_path(work["path"])
    if _exists(target):
        return target

    videos = video_db.get_work_videos(work_id)
    if not videos:
        return ""
    with _lock_for(target):
        if _exists(target):
            return target
        try:
            return probe.make_thumbnail(videos[0]["path"], target) or ""
        except Exception as e:  # noqa: BLE001 - 抽帧失败只显示占位图
            print(f"[网页视频封面] 失败 {videos[0]['path']}: {e}")
            return ""


def video_work_videos(work_id):
    work = video_work(work_id)
    if not work:
        return None
    videos = video_db.get_work_videos(work_id)
    progress = video_db.get_progress_map([v["path"] for v in videos])

    items = []
    for index, video in enumerate(videos):
        ext = os.path.splitext(video["path"])[1].lower()
        playable = http_util.browser_playable(ext, video.get("codec") or "")
        items.append(
            {
                "id": video["id"],
                "name": video["name"],
                "index": index,
                "duration_ms": int(video.get("duration_ms") or 0),
                "size": int(video.get("size") or 0),
                "width": int(video.get("width") or 0),
                "height": int(video.get("height") or 0),
                "codec": video.get("codec") or "",
                "ext": ext,
                "playable": playable,
                "url": f"/media/video/{video['id']}",
                "remux_url": f"/media/video-remux/{video['id']}",
                "progress_ms": int(progress.get(video["path"], 0)),
            }
        )

    return {
        "work": {
            "id": work["id"],
            "name": work["name"],
            "author": work["author"],
            "cover": f"/media/video-cover/{work['id']}",
        },
        "videos": items,
    }


def video_by_id(video_id):
    return _row(
        video_db,
        "SELECT id, work_id, path, name, duration_ms, width, height, codec, size "
        "FROM videos WHERE id = ?",
        (video_id,),
    )


def video_play_info(video_id):
    """播放前信息：真实路径只留在服务端，前端只拿到续播点与可播性"""
    video = video_by_id(video_id)
    if not video:
        return None
    position, duration = video_db.get_progress(video["path"])
    ext = os.path.splitext(video["path"])[1].lower()
    return {
        "id": video["id"],
        "name": video["name"],
        "duration_ms": int(video.get("duration_ms") or duration or 0),
        "position_ms": int(position),
        "codec": video.get("codec") or "",
        "ext": ext,
        "playable": http_util.browser_playable(ext, video.get("codec") or ""),
        "url": f"/media/video/{video['id']}",
        "remux_url": f"/media/video-remux/{video['id']}",
    }


def save_video_progress(video_id, position_ms, duration_ms):
    """网页端上报播放进度；规则与桌面播放器完全一致（看完就清空）"""
    video = video_by_id(video_id)
    if not video:
        return False
    action, position = decide_progress(position_ms, duration_ms)
    if action == "clear":
        video_db.clear_progress(video["path"])
    elif action == "set":
        video_db.set_progress(video["path"], position, duration_ms)
    # 时长未知时顺手把播放器读到的真实时长补进库，桌面端列表也能跟着变准
    if duration_ms:
        video_db.update_video_duration(video["path"], int(duration_ms))
    return True
