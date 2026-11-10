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
from resource_manager.utils import natural_key as photo_natural_key
from video_manager import align as skip_align
from video_manager import database as video_db
from video_manager import probe
from video_manager import scanner as video_scanner
from video_manager import skip as skip_logic
from video_manager.progress import decide_progress
from web_server import http_util, transcode

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


_local = threading.local()

# 一个请求要发好几条 SQL，而「新建连接 → 查 → 关闭」每次要 3~4 ms，
# 实测这才是列表接口的主要开销（查询本身 0.15 ms）。按线程复用连接后，
# 音频列表 21 ms → 3 ms，总览也顺带从 26 ms 掉到个位数。
# ThreadingHTTPServer 每条连接一个线程，连接寿命跟着请求走，不会无限增长。
_conn_guard = threading.Lock()
_conn_threads: set = set()      # 持有连接缓存的线程（只用于限流）
CONN_THREADS_MAX = 24


def _db_path(db_module):
    """各库的 sqlite 路径（读取当时的 config，测试换临时目录也能跟上）"""
    path = getattr(db_module, "DB_PATH", "")
    if path:
        return str(path)
    name = getattr(db_module, "__name__", "")
    if name.startswith("resource_manager"):
        return config.DB_PATH
    if name.startswith("video_manager"):
        return config.VIDEO_DB_PATH
    return ""


def _conn(db_module):
    """取本线程该库的连接；超出上限就返回 None，调用方退回短连接"""
    path = _db_path(db_module)
    if not path:
        return None
    store = getattr(_local, "conns", None)
    if store is None:
        with _conn_guard:
            # 线程退出后 ident 还留在集合里，顺手按当前存活线程清一遍
            _conn_threads.intersection_update(
                {thread.ident for thread in threading.enumerate()}
            )
            if len(_conn_threads) >= CONN_THREADS_MAX:
                return None
            _conn_threads.add(threading.get_ident())
        store = _local.conns = {}
    conn = store.get(path)
    if conn is not None:
        return conn
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        # 桌面端同时在扫描/写进度时，读到写事务中间态不该直接报 database is locked，
        # 等一会儿就过去了（WAL 下读不阻塞写，这里保险起见）
        conn.execute("PRAGMA busy_timeout = 3000")
    except sqlite3.Error:
        return None
    store[path] = conn
    return conn


def _rows(db_module, sql, params=()):
    conn = _conn(db_module)
    if conn is None:
        with db_module.get_conn() as short:
            return [dict(row) for row in short.execute(sql, params).fetchall()]
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _row(db_module, sql, params=()):
    conn = _conn(db_module)
    if conn is None:
        with db_module.get_conn() as short:
            row = short.execute(sql, params).fetchone()
            return dict(row) if row else None
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _count(db_module, table):
    row = _row(db_module, f"SELECT COUNT(*) AS c FROM {table}")
    return int(row["c"]) if row else 0


def _scalar(db_module, sql, params=()):
    """单值查询（COUNT 之类），查不到按 0 返回"""
    row = _row(db_module, sql, params)
    if not row:
        return 0
    value = next(iter(row.values()), 0)
    return int(value or 0)


def _basename(path):
    return os.path.basename(str(path or "").replace("\\", "/"))


# ==================== 分页 ====================

# 网页端列表默认每页条数 / 上限（page_size 参数超出会被钳回来）
PAGE_DEFAULT = 60
PAGE_MAX = 200

# 作品内图片页：卡片更密，一页给两倍，滚到底续页时少发几次请求
IMAGE_PAGE_DEFAULT = 120


def _page_args(page, page_size):
    """把请求参数钳成 (page, page_size)；非法值落回默认"""
    try:
        page = max(int(page or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(page_size or PAGE_DEFAULT)
    except (TypeError, ValueError):
        page_size = PAGE_DEFAULT
    return page, min(max(page_size, 1), PAGE_MAX)


def _page_result(items, total, page, page_size):
    """拼出前端统一的分页响应（内存分页与 SQL 分页共用）"""
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        # ceil：4 页 × 60 = 240 条也能在第 4 页装下 241 条
        "pages": max(1, -(-total // page_size)),
    }


def _paged(items, page, page_size):
    """内存分页：返回 items / total / page / pages，page_size<=0 时按默认值"""
    total = len(items)
    page, page_size = _page_args(page, page_size)
    start = (page - 1) * page_size
    return _page_result(items[start:start + page_size], total, page, page_size)


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
    # 关键词下推到 SQL（作品名 + 作者名），不再把整库读进内存后 Python 过滤
    works = photo_db.list_works(author_id, keyword)
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
    """分页版写真作品列表（网页端用）：切到写真页只拉一页，不再一次全量

    总数与当页都在 SQL 里算（LIMIT/OFFSET），命中多少条由关键词决定，
    不再「读全表 → Python 过滤 → 内存切片」。
    """
    page, page_size = _page_args(page, page_size)
    total, rows = photo_db.list_works_page(author_id, keyword, page, page_size)
    items = [
        {
            "id": w["id"],
            "name": w["name"],
            "author": w["author"],
            "image_count": int(w.get("image_count") or 0),
            "cover": f"/media/photo-cover/{w['id']}",
        }
        for w in rows
    ]
    return _page_result(items, total, page, page_size)


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


def photo_work_images(work_id, page=1, page_size=0):
    """作品内图片（分页）：一册写真可能两千多张，一次全吐会把手机端建满 DOM

    实测最大的一册 2025 张 → 300KB JSON、约 1.2 万个节点，老平板上要卡好几秒。
    改为按页拉，滚到底再续。
    """
    work = photo_work(work_id)
    if not work:
        return None
    order = _image_order(work_id)
    total = len(order)
    page, page_size = _page_args(page, page_size or IMAGE_PAGE_DEFAULT)
    start = (page - 1) * page_size
    return {
        "work": {
            "id": work["id"],
            "name": work["name"],
            "cover": f"/media/photo-cover/{work['id']}",
        },
        "images": [
            {
                "id": img_id,
                "thumb": f"/media/photo/{img_id}?w=640",
                # 看图用的主图：1600 宽在手机/平板上已经超过屏幕像素密度所需，
                # 体积却只有原图（常见 10~40MB）的几十分之一
                "full": f"/media/photo/{img_id}?w=1600",
                # 真要 100% 放大看细节时才点「原图」，按需加载
                "orig": f"/media/photo/{img_id}",
            }
            for img_id in order[start:start + page_size]
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, -(-total // page_size)),
    }


# 作品内图片顺序缓存：{work_id: (签名, [图片 id, ...])}
# 自然排序（1,2,10 而不是 1,10,2）只能在 Python 里做，一册两千多张要排序 15 ms；
# 分页后同一个作品会被连续请求十几次，这里按「行数 + 最大 mtime」判失效。
_IMAGE_ORDER_CACHE: dict[int, tuple] = {}
_IMAGE_ORDER_MAX = 64


def _image_order(work_id):
    """作品内图片 id 列表（自然排序），带缓存"""
    signature = _row(
        photo_db,
        "SELECT COUNT(*) AS c, MAX(mtime) AS m FROM images WHERE work_id = ?",
        (work_id,),
    ) or {}
    key = (int(signature.get("c") or 0), signature.get("m"))
    cached = _IMAGE_ORDER_CACHE.get(work_id)
    if cached and cached[0] == key:
        return cached[1]

    rows = _rows(
        photo_db, "SELECT id, path FROM images WHERE work_id = ?", (work_id,)
    )
    # 与桌面一致的自然排序：1.jpg, 2.jpg, ..., 10.jpg
    rows.sort(key=lambda row: photo_natural_key(row["path"]))
    order = [int(row["id"]) for row in rows]
    if len(_IMAGE_ORDER_CACHE) >= _IMAGE_ORDER_MAX:
        _IMAGE_ORDER_CACHE.clear()
    _IMAGE_ORDER_CACHE[work_id] = (key, order)
    return order


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

# 歌单封面最多向上找几层（与桌面 catalog 的继承规则一致）
_COVER_DEPTH = 6

_PLAYLIST_COLS = "id, name, path, parent_path, cover, tags, track_count"


def _in_list(values):
    """拼 SQL 的占位符；空列表由调用方提前返回"""
    return ",".join("?" * len(values))


def _inherited_covers(playlists):
    """{歌单 id: 封面路径}，自己没有就沿 parent_path 向上继承最近一层

    按「层」批量查：一页 60 个歌单往往同属一个父目录，逐条走父链要几百次查询，
    这里每层只发一条 SQL，最多 6 层——而封面继承正是音频页最热的路径。
    """
    resolved = {}
    pending = {}
    for item in playlists:
        cover = item.get("cover") or ""
        if _exists(cover):
            resolved[item["id"]] = cover
        elif item.get("parent_path"):
            pending[item["id"]] = item["parent_path"]

    # 已经查过的父路径缓存起来：同一层里多个歌单共用一个父目录时只查一次，
    # 但**不能**因为「查过」就跳过——上一层的父可能正是下一层要继续往上走的起点
    cache = {}
    for _ in range(_COVER_DEPTH):
        if not pending:
            break
        missing = [p for p in set(pending.values()) if p and p not in cache]
        if missing:
            rows = _rows(
                audio_db,
                f"SELECT id, path, cover, parent_path FROM playlists "
                f"WHERE path IN ({_in_list(missing)})",
                tuple(missing),
            )
            for row in rows:
                cache[row["path"]] = row

        next_pending = {}
        for playlist_id, parent_path in pending.items():
            parent = cache.get(parent_path)
            if not parent:
                continue
            cover = parent.get("cover") or ""
            if _exists(cover):
                resolved[playlist_id] = cover
            elif parent.get("parent_path"):
                next_pending[playlist_id] = parent["parent_path"]
        pending = next_pending

    return resolved


def _track_counts(playlist_ids):
    """{歌单 id: 曲目数}（只统计给定的这些歌单，不再全表 GROUP BY）"""
    if not playlist_ids:
        return {}
    rows = _rows(
        audio_db,
        f"SELECT playlist_id, COUNT(*) AS c FROM tracks "
        f"WHERE playlist_id IN ({_in_list(playlist_ids)}) GROUP BY playlist_id",
        tuple(playlist_ids),
    )
    return {int(row["playlist_id"]): int(row["c"] or 0) for row in rows}


def _child_counts(paths):
    """{歌单路径: 子歌单数}"""
    if not paths:
        return {}
    rows = _rows(
        audio_db,
        f"SELECT parent_path, COUNT(*) AS c FROM playlists "
        f"WHERE parent_path IN ({_in_list(paths)}) GROUP BY parent_path",
        tuple(paths),
    )
    return {row["parent_path"]: int(row["c"] or 0) for row in rows}


def _playlist_briefs(playlists):
    """把歌单行补成前端要的字段（曲目数/子集数/封面继承一次批量算完）"""
    if not playlists:
        return []
    # 以 tracks 表的实际行数为准：老库的 track_count 可能是 0（扫描器没回填），
    # 直接用会显示成「空歌单」，而点进去明明有歌。
    track_counts = _track_counts([item["id"] for item in playlists])
    child_counts = _child_counts([item["path"] for item in playlists])
    covers = _inherited_covers(playlists)
    return [
        {
            "id": item["id"],
            "name": item["name"],
            "track_count": int(track_counts.get(item["id"], item.get("track_count") or 0)),
            "child_count": int(child_counts.get(item["path"], 0)),
            "tags": item.get("tags") or "",
            "cover": f"/media/audio-cover/{item['id']}",
            "has_cover": bool(covers.get(item["id"])),
            "parent_path": item.get("parent_path") or "",
        }
        for item in playlists
    ]


def audio_playlists(parent=""):
    """指定层级下的全部歌单（parent="" 即顶层）"""
    rows = _rows(
        audio_db,
        f"SELECT {_PLAYLIST_COLS} FROM playlists WHERE parent_path = ? ORDER BY name",
        (parent or "",),
    )
    return _playlist_briefs(rows)


def audio_playlists_page(parent="", page=1, page_size=0):
    """分页版歌单列表（网页端用）：SQL 里就切好页，不再把整库读进内存"""
    parent = parent or ""
    page, page_size = _page_args(page, page_size)
    total = _scalar(
        audio_db, "SELECT COUNT(*) AS c FROM playlists WHERE parent_path = ?", (parent,)
    )
    rows = _rows(
        audio_db,
        f"SELECT {_PLAYLIST_COLS} FROM playlists WHERE parent_path = ? "
        f"ORDER BY name LIMIT ? OFFSET ?",
        (parent, page_size, (page - 1) * page_size),
    )
    return _page_result(_playlist_briefs(rows), total, page, page_size)


def _playlist_row(playlist_id):
    """单个歌单行（走本线程连接，封面列表页一次要请求几十个）"""
    return _row(
        audio_db,
        f"SELECT {_PLAYLIST_COLS} FROM playlists WHERE id = ?",
        (playlist_id,),
    )


def audio_playlist(playlist_id):
    playlist = _playlist_row(playlist_id)
    if not playlist:
        return None

    children = _playlist_briefs(_rows(
        audio_db,
        f"SELECT {_PLAYLIST_COLS} FROM playlists WHERE parent_path = ? ORDER BY name",
        (playlist["path"],),
    ))

    # 返回值里带上父歌单 id：网页端详情页的「返回上级」靠它，否则只能按系统返回键
    parent_path = playlist.get("parent_path") or ""
    parent = _row(
        audio_db, "SELECT id FROM playlists WHERE path = ?", (parent_path,)
    ) if parent_path else None

    covers = _inherited_covers([playlist])
    tracks = _rows(
        audio_db,
        "SELECT id, playlist_id, title, path, duration, track_number "
        "FROM tracks WHERE playlist_id = ? ORDER BY track_number, title",
        (playlist_id,),
    )
    return {
        "playlist": {
            "id": playlist["id"],
            "name": playlist["name"],
            "tags": playlist.get("tags") or "",
            "cover": f"/media/audio-cover/{playlist['id']}",
            "has_cover": bool(covers.get(playlist["id"])),
            "parent_path": parent_path,
            "parent_id": int(parent["id"]) if parent else 0,
            "track_count": len(tracks),
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
    playlist = _playlist_row(playlist_id)
    if not playlist:
        return ""
    return _inherited_covers([playlist]).get(playlist["id"], "")


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


def video_resume(keyword="", page=1, page_size=0):
    """「继续观看」：与桌面侧栏同一份数据（video_db.list_resume）

    孤儿进度（视频被删）在 SQL 的 INNER JOIN 里已经滤掉，
    这里只做字段改名和封面地址拼装——封面仍按 id 走 /media/video-cover/，
    前端拿不到任何路径。
    """
    keyword = (keyword or "").strip().lower()
    items = []
    for row in video_db.list_resume():
        work_id = int(row["work_id"])
        item = {
            "work_id": work_id,
            "video_id": int(row["video_id"]),
            "work_name": row["work_name"],
            "video_name": row["video_name"],
            "author": row["author"],
            "position_ms": int(row["position_ms"] or 0),
            "duration_ms": int(row["duration_ms"] or 0),
            "updated_at": int(row["updated_at"] or 0),
            "cover": f"/media/video-cover/{work_id}",
        }
        if keyword and not (
            keyword in (item["work_name"] or "").lower()
            or keyword in (item["author"] or "").lower()
            or keyword in (item["video_name"] or "").lower()
        ):
            continue
        items.append(item)
    return _paged(items, page, page_size)


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
        codec = video.get("codec") or ""
        mode = http_util.video_mode(ext, codec)
        items.append(
            {
                "id": video["id"],
                "name": video["name"],
                "index": index,
                "duration_ms": int(video.get("duration_ms") or 0),
                "size": int(video.get("size") or 0),
                "width": int(video.get("width") or 0),
                "height": int(video.get("height") or 0),
                "codec": codec,
                "ext": ext,
                "mode": mode,
                "playable": mode == http_util.MODE_DIRECT,
                "url": f"/media/video/{video['id']}",
                "stream_url": f"/media/video-stream/{video['id']}",
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


def video_play_info(video_id, caps=None):
    """播放前信息：真实路径只留在服务端，前端只拿到续播点与播放方式

    `caps` 是客户端自报的解码能力（见 `http_util.parse_client_caps`）：
    设备能硬解 HEVC 时，mkv 里的 HEVC 只需要换封装，不必重编码。
    """
    video = video_by_id(video_id)
    if not video:
        return None
    position, duration = video_db.get_progress(video["path"])
    ext = os.path.splitext(video["path"])[1].lower()
    codec = video.get("codec") or ""
    mode = http_util.video_mode(ext, codec, caps)
    # 已经转好并落盘的：服务端会当普通 mp4 发（Range 可用），前端可以按直连处理，
    # 不用再走「?start= 重连」那套，进度条也就不需要凭数据库时长偏算
    converted = transcode.cache_path(video)
    cached = bool(converted and os.path.isfile(converted))
    duration_ms = int(video.get("duration_ms") or duration or 0)
    return {
        "id": video["id"],
        "name": video["name"],
        "work_id": int(video.get("work_id") or 0),
        "duration_ms": duration_ms,
        "position_ms": int(position),
        "codec": codec,
        "ext": ext,
        "mode": mode,
        "cached": cached,
        "playable": mode == http_util.MODE_DIRECT,
        "url": f"/media/video/{video['id']}",
        "stream_url": f"/media/video-stream/{video['id']}",
        "remux_url": f"/media/video-remux/{video['id']}",
        # 片头/片尾跳过：和桌面播放器读同一份数据，谁标的一次都全剧通用
        "skip": video_skip_state(video, duration_ms),
    }


# ==================== 片头 / 片尾跳过 ====================
#
# 规则复用 `video_manager.skip`（纯函数），逐集微调复用 `video_manager.align`
# （音频包络对齐）。网页端只做三件事：算出「这一集该跳哪几段」给前端、
# 把手机上按下的标记写回同一张表、在第一次打开某集时跑一次后台对齐。
#
# 关键点：数据按**作品**记（`skip_segments`），逐集结果按**文件**记
# （`skip_overrides`）。所以手机上标一次，电脑端打开同一部动画也会自动跳。

def skip_flags():
    """两个开关（桌面端设置面板里改，这里只读）→ (跳片头, 跳片尾)"""
    return (
        bool(getattr(config, "VIDEO_SKIP_OP", True)),
        bool(getattr(config, "VIDEO_SKIP_ED", True)),
    )


def video_skip_state(video, duration_ms=None):
    """当前这一集真正要跳的区间

    前端拿到的是**毫秒**，直接跟 `currentTime * 1000` 比，不用再算一遍规则。
    `plan` 为空是常态：作品还没标记过 / 两段都被对齐判定为「这集没有」。
    """
    if not video:
        return {"work_id": 0, "segments": [], "plan": [], "enabled": {"op": True, "ed": True}}
    work_id = int(video.get("work_id") or 0)
    if duration_ms is None:
        duration_ms = video.get("duration_ms") or 0
    duration_ms = int(duration_ms or 0)
    segments = video_db.get_skip_segments(work_id) if work_id else {}
    overrides = video_db.get_skip_overrides(video.get("path") or "")
    skip_op, skip_ed = skip_flags()
    plan = skip_logic.build_plan(
        segments, overrides, duration_ms, skip_op=skip_op, skip_ed=skip_ed
    )
    return {
        "work_id": work_id,
        "duration_ms": duration_ms,
        "enabled": {"op": skip_op, "ed": skip_ed},
        "segments": [
            {
                "kind": kind,
                "start_ms": int(seg.get("start_ms") or 0),
                "end_ms": int(seg.get("end_ms") or 0),
            }
            for kind, seg in sorted(segments.items())
        ],
        "plan": [
            {"kind": kind, "start_ms": int(start), "end_ms": int(end)}
            for kind, start, end in plan
        ],
    }


def video_skip_state_by_id(video_id, duration_ms=None):
    video = video_by_id(video_id)
    if not video:
        return None
    return video_skip_state(video, duration_ms)


def save_video_skip(video_id, kind, start_ms, end_ms, duration_ms=None):
    """写作品级模板（手机上的「标片头 / 标片尾」）

    返回 (状态或 None, 错误文案)。写库失败与参数不合法都只回错误，不抛异常——
    跳片头是锦上添花，不能因为它把播放页搞崩。
    """
    video = video_by_id(video_id)
    if not video:
        return None, "视频不存在"
    if kind not in skip_logic.KINDS:
        return None, "只能标记片头或片尾"
    work_id = int(video.get("work_id") or 0)
    if not work_id:
        return None, "这个视频没有归属作品，无法全剧通用"

    try:
        start = max(0, int(start_ms or 0))
        end = max(0, int(end_ms or 0))
    except (TypeError, ValueError):
        return None, "时间点不是数字"

    # 片尾允许 end=0（「一直到文件结尾」）；片头必须有终点
    if end and end - start < skip_logic.MIN_SEGMENT_MS:
        return None, "这段太短了，多半是标偏了"
    if not end and kind == skip_logic.OP:
        return None, "片头需要起点和终点两个时间点"

    if not video_db.set_skip_segment(work_id, kind, start, end, video["path"]):
        return None, "写入失败，数据库可能被占用"
    return video_skip_state(video, duration_ms), ""


def clear_video_skip(video_id, kind=None):
    """清除本作品的标记（kind 为空时两种都清）"""
    video = video_by_id(video_id)
    if not video:
        return None
    work_id = int(video.get("work_id") or 0)
    if not work_id:
        return None
    video_db.clear_skip_segment(work_id, kind if kind in skip_logic.KINDS else None)
    return video_skip_state(video)


def skip_needs_align(video_id):
    """这一集还缺逐集对齐吗（缺则值得起一次后台对齐）"""
    video = video_by_id(video_id)
    if not video:
        return False
    work_id = int(video.get("work_id") or 0)
    if not work_id:
        return False
    op = video_db.get_skip_segments(work_id).get(skip_logic.OP)
    if not op:
        return False
    if video_db.get_skip_overrides(video.get("path") or "").get(skip_logic.OP):
        return False
    ref = op.get("source_path") or ""
    if ref and _same_file(ref, video.get("path") or ""):
        return False  # 当前就是基准那一集，模板本身最准
    return skip_align.available()


def align_video_skip(video_id):
    """把作品模板对齐到这一集（**阻塞**，约 1 秒；调用方放后台线程里跑）

    结果写进 `skip_overrides`，之后这集不再重复解码。返回 status
    （ok / absent / unknown / cached / skip），只用于日志。
    """
    video = video_by_id(video_id)
    if not video:
        return "skip"
    work_id = int(video.get("work_id") or 0)
    op = video_db.get_skip_segments(work_id).get(skip_logic.OP) if work_id else None
    if not op:
        return "skip"
    path = video.get("path") or ""
    if video_db.get_skip_overrides(path).get(skip_logic.OP):
        return "cached"

    ref = op.get("source_path") or ""
    start = int(op.get("start_ms") or 0)
    end = int(op.get("end_ms") or 0)
    if end - start < skip_logic.MIN_SEGMENT_MS:
        return "skip"

    result = skip_align.align_op(path, ref, start, end)
    status = str(result.get("status") or "unknown")
    if status in ("ok", "absent"):
        video_db.set_skip_override(
            path, skip_logic.OP,
            int(result.get("start_ms") or 0), int(result.get("end_ms") or 0), status,
        )
    return status


def _same_file(left, right):
    if not left or not right:
        return False
    try:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )
    except OSError:
        return False


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
