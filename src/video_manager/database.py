"""视频库数据库（authors / works / videos / progress 四表）

结构与 resource_manager.database 同构，便于两个模块共享同一套增量扫描心智：
根目录/作者/作品/视频文件，works.path 唯一，videos.path 唯一。

另加一张 progress 表按**文件路径**记续播点：视频被重新扫描（删除重插）时
videos.id 会变，用路径当键才不会丢「上次看到哪」。
"""
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from resource_manager import config
from resource_manager.utils import natural_key


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.VIDEO_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# 长连接按线程隔离（原因见 resource_manager/database.py：扫描线程超时 detach 后
# 仍在跑，若复用同一连接，先结束者会把对方正在用的连接 close 掉）
_local = threading.local()


def begin_persistent():
    """当前线程开启长连接，调用方需在同一线程 end_persistent()"""
    conn = getattr(_local, "persistent_conn", None)
    if conn is None:
        conn = sqlite3.connect(config.VIDEO_DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -16384")
        _local.persistent_conn = conn
    return conn


def end_persistent():
    conn = getattr(_local, "persistent_conn", None)
    if conn is not None:
        conn.commit()
        conn.close()
        _local.persistent_conn = None


@contextmanager
def _active_conn():
    conn = getattr(_local, "persistent_conn", None)
    if conn is not None:
        yield conn
    else:
        with get_conn() as short_conn:
            yield short_conn


def init_db():
    os.makedirs(os.path.dirname(config.VIDEO_DB_PATH) or ".", exist_ok=True)
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS authors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );

            CREATE TABLE IF NOT EXISTS works (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                path TEXT UNIQUE NOT NULL,
                thumbnail TEXT,
                FOREIGN KEY (author_id) REFERENCES authors(id),
                UNIQUE(author_id, name)
            );

            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id INTEGER NOT NULL,
                path TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                mtime INTEGER DEFAULT 0,
                size INTEGER DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                codec TEXT DEFAULT '',
                FOREIGN KEY (work_id) REFERENCES works(id)
            );

            CREATE TABLE IF NOT EXISTS progress (
                path TEXT PRIMARY KEY,
                position_ms INTEGER DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                updated_at INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_works_author ON works(author_id);
            CREATE INDEX IF NOT EXISTS idx_videos_work ON videos(work_id);
            """
        )


def _insert_or_ignore_id(conn, insert_sql, insert_params, id_sql, id_params):
    """INSERT OR IGNORE 后取主键：rowcount 为 0 时不能用 lastrowid

    原因同 resource_manager/database.py：lastrowid 是**连接级**上一次成功插入的
    行 id，被唯一约束忽略时它不更新，会把别的表上一次插入的 id 当成新 id 返回，
    造成悬空外键。
    """
    cur = conn.execute(insert_sql, insert_params)
    if cur.rowcount:
        return cur.lastrowid
    row = conn.execute(id_sql, id_params).fetchone()
    return row["id"] if row else None


# ==================== 作者 / 作品 ====================

def add_author(name):
    """作者幂等插入，返回 author_id（扫描期间必须走长连接，否则写锁冲突）"""
    with _active_conn() as conn:
        return _insert_or_ignore_id(
            conn,
            "INSERT OR IGNORE INTO authors (name) VALUES (?)",
            (name,),
            "SELECT id FROM authors WHERE name = ?",
            (name,),
        )


def get_author_id(name):
    with _active_conn() as conn:
        row = conn.execute("SELECT id FROM authors WHERE name = ?", (name,)).fetchone()
        return row["id"] if row else None


def list_authors():
    """侧栏作者列表：带作品数与视频数"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.name,
                   COUNT(DISTINCT w.id) AS work_count,
                   COUNT(v.id) AS video_count
            FROM authors a
            LEFT JOIN works w ON w.author_id = a.id
            LEFT JOIN videos v ON v.work_id = w.id
            GROUP BY a.id
            ORDER BY a.name
            """
        ).fetchall()
        return [dict(row) for row in rows]


def list_all_authors():
    """返回 [(id, name), ...]（增量扫描用）"""
    with _active_conn() as conn:
        rows = conn.execute("SELECT id, name FROM authors").fetchall()
        return [(row["id"], row["name"]) for row in rows]


def list_works(author_id=None):
    """作品卡片数据：作者名、视频数、总时长"""
    sql = """
        SELECT w.id, w.name, w.path, w.thumbnail, a.name AS author,
               COUNT(v.id) AS video_count,
               COALESCE(SUM(v.duration_ms), 0) AS total_duration_ms
        FROM works w
        JOIN authors a ON a.id = w.author_id
        LEFT JOIN videos v ON v.work_id = w.id
    """
    params = ()
    if author_id is not None:
        sql += " WHERE w.author_id = ?"
        params = (author_id,)
    sql += " GROUP BY w.id ORDER BY a.name, w.name"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def get_work(work_id):
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT w.id, w.name, w.path, w.thumbnail, a.name AS author
            FROM works w JOIN authors a ON a.id = w.author_id
            WHERE w.id = ?
            """,
            (work_id,),
        ).fetchone()
        return dict(row) if row else None


def list_works_by_author(author_id):
    """返回 {path: (id, name, thumbnail)}"""
    with _active_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, path, thumbnail FROM works WHERE author_id = ?",
            (author_id,),
        ).fetchall()
        return {row["path"]: (row["id"], row["name"], row["thumbnail"]) for row in rows}


def add_author_with_works(author_name, works):
    """按作者批量插入作品与视频（一个事务）

    works: [(work_name, work_path, thumbnail, [(video_path, video_name, mtime, size), ...]), ...]
    """
    with _active_conn() as conn:
        author_id = _insert_or_ignore_id(
            conn,
            "INSERT OR IGNORE INTO authors (name) VALUES (?)",
            (author_name,),
            "SELECT id FROM authors WHERE name = ?",
            (author_name,),
        )

        for work_name, work_path, thumbnail, videos in works:
            work_id = _insert_or_ignore_id(
                conn,
                "INSERT OR IGNORE INTO works (author_id, name, path, thumbnail) VALUES (?, ?, ?, ?)",
                (author_id, work_name, work_path, thumbnail),
                "SELECT id FROM works WHERE path = ?",
                (work_path,),
            )
            if work_id is None:
                # 目录被改名：path 变了但 (author_id, name) 还能兜住
                row = conn.execute(
                    "SELECT id FROM works WHERE author_id = ? AND name = ?",
                    (author_id, work_name),
                ).fetchone()
                work_id = row["id"] if row else None
            if work_id is None:
                print(f"[跳过作品] 无法解析 work_id: {work_path}")
                continue

            if videos:
                conn.executemany(
                    "INSERT INTO videos (work_id, path, name, mtime, size) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET work_id = excluded.work_id, "
                    "name = excluded.name, mtime = excluded.mtime, size = excluded.size",
                    [(work_id, p, n, m, s) for p, n, m, s in videos],
                )


def delete_work(work_id):
    with _active_conn() as conn:
        conn.execute("DELETE FROM videos WHERE work_id = ?", (work_id,))
        conn.execute("DELETE FROM works WHERE id = ?", (work_id,))


def delete_author(author_id):
    with _active_conn() as conn:
        work_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM works WHERE author_id = ?", (author_id,)
        ).fetchall()]
        if work_ids:
            placeholders = ",".join("?" * len(work_ids))
            conn.execute(f"DELETE FROM videos WHERE work_id IN ({placeholders})", work_ids)
        conn.execute("DELETE FROM works WHERE author_id = ?", (author_id,))
        conn.execute("DELETE FROM authors WHERE id = ?", (author_id,))


# ==================== 视频文件 ====================

def get_work_videos(work_id):
    """作品内视频列表（自然排序：第 1 集、第 2 集、…、第 10 集）"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, work_id, path, name, mtime, size, duration_ms, width, height, codec "
            "FROM videos WHERE work_id = ?",
            (work_id,),
        ).fetchall()
        videos = [dict(row) for row in rows]
    videos.sort(key=lambda v: natural_key(v["name"]))
    return videos


def get_video_mtimes(work_id):
    """返回 {path: (mtime, size)}，增量扫描比对用"""
    with _active_conn() as conn:
        rows = conn.execute(
            "SELECT path, mtime, size FROM videos WHERE work_id = ?", (work_id,)
        ).fetchall()
        return {row["path"]: (row["mtime"], row["size"]) for row in rows}


def upsert_videos(work_id, rows):
    """批量插入/改归属 [(path, name, mtime, size), ...]"""
    if not rows:
        return
    with _active_conn() as conn:
        conn.executemany(
            "INSERT INTO videos (work_id, path, name, mtime, size) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET work_id = excluded.work_id, "
            "name = excluded.name, mtime = excluded.mtime, size = excluded.size",
            [(work_id, p, n, m, s) for p, n, m, s in rows],
        )


def update_video_file(path, mtime, size):
    with _active_conn() as conn:
        conn.execute(
            "UPDATE videos SET mtime = ?, size = ? WHERE path = ?", (mtime, size, path)
        )


def update_video_meta(path, duration_ms=0, width=0, height=0, codec=""):
    """回填探针结果（扫描时 ffprobe 缺失、或播放器拿到真实时长后补写）"""
    with _active_conn() as conn:
        conn.execute(
            "UPDATE videos SET duration_ms = ?, width = ?, height = ?, codec = ? WHERE path = ?",
            (int(duration_ms or 0), int(width or 0), int(height or 0), codec or "", path),
        )


def update_video_duration(path, duration_ms):
    """只补时长：播放器拿到真实 duration 后回填，不覆盖已探到的分辨率/编码"""
    if not duration_ms:
        return
    try:
        with _active_conn() as conn:
            conn.execute(
                "UPDATE videos SET duration_ms = ? WHERE path = ? AND duration_ms = 0",
                (int(duration_ms), path),
            )
    except sqlite3.Error as e:
        print(f"[视频元信息] 回填时长失败: {e}")


def delete_videos_by_paths(paths):
    if not paths:
        return
    with _active_conn() as conn:
        conn.executemany("DELETE FROM videos WHERE path = ?", [(p,) for p in paths])


def count_videos():
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM videos").fetchone()
        return row["c"] if row else 0


def list_all_thumbnail_paths():
    with _active_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT thumbnail FROM works WHERE thumbnail IS NOT NULL"
        ).fetchall()
        return [row["thumbnail"] for row in rows if row["thumbnail"]]


# ==================== 续播点 ====================

def get_progress(path):
    """返回 (position_ms, duration_ms)，没有记录返回 (0, 0)

    续播是「锦上添花」：库文件缺失/表未建/被锁死都不许把播放器带崩，
    所以这里吞掉 sqlite 异常，退化成「从头播」。
    """
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT position_ms, duration_ms FROM progress WHERE path = ?", (path,)
            ).fetchone()
            if row is None:
                return 0, 0
            return int(row["position_ms"] or 0), int(row["duration_ms"] or 0)
    except sqlite3.Error as e:
        print(f"[视频续播] 读取失败，按从头播处理: {e}")
        return 0, 0


def get_progress_map(paths):
    """批量取续播点 {path: position_ms}（详情页一次查完，避免每行一次查询）"""
    paths = list(paths)
    if not paths:
        return {}
    try:
        with get_conn() as conn:
            placeholders = ",".join("?" * len(paths))
            rows = conn.execute(
                f"SELECT path, position_ms FROM progress WHERE path IN ({placeholders})", paths
            ).fetchall()
            return {row["path"]: int(row["position_ms"] or 0) for row in rows}
    except sqlite3.Error as e:
        print(f"[视频续播] 批量读取失败: {e}")
        return {}


def set_progress(path, position_ms, duration_ms=0):
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO progress (path, position_ms, duration_ms, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET position_ms = excluded.position_ms, "
                "duration_ms = excluded.duration_ms, updated_at = excluded.updated_at",
                (path, int(position_ms or 0), int(duration_ms or 0), int(time.time())),
            )
    except sqlite3.Error as e:
        print(f"[视频续播] 写入失败: {e}")


def clear_progress(path):
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM progress WHERE path = ?", (path,))
    except sqlite3.Error as e:
        print(f"[视频续播] 清除失败: {e}")


def clear_all():
    """测试/重置用：清空全部表"""
    with get_conn() as conn:
        conn.executescript(
            "DELETE FROM progress; DELETE FROM videos; DELETE FROM works; DELETE FROM authors;"
        )
