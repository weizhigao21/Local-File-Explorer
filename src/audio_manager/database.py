"""
音频模块数据库操作
管理歌单和曲目，支持层级目录结构
"""
import os
import re
import sqlite3
from contextlib import contextmanager


from resource_manager.config import get_project_root
_DATA_DIR = os.path.join(get_project_root(), "data")
os.makedirs(_DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(_DATA_DIR, "audio_manager.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# 全局长连接：用于扫描等大批量操作场景，避免频繁 connect/close
_persistent_conn = None


def begin_persistent():
    """开启一个长连接供批量操作复用，调用方需调用 end_persistent() 释放"""
    global _persistent_conn
    if _persistent_conn is None:
        _persistent_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _persistent_conn.row_factory = sqlite3.Row
        _persistent_conn.execute("PRAGMA journal_mode = WAL")
        _persistent_conn.execute("PRAGMA synchronous = NORMAL")
        _persistent_conn.execute("PRAGMA cache_size = -65536")  # 64MB cache
    return _persistent_conn


def end_persistent():
    """释放长连接"""
    global _persistent_conn
    if _persistent_conn is not None:
        _persistent_conn.commit()
        _persistent_conn.close()
        _persistent_conn = None


@contextmanager
def _active_conn():
    """获取活动连接：长连接模式下复用，否则新建短连接"""
    if _persistent_conn is not None:
        yield _persistent_conn
    else:
        with get_conn() as conn:
            yield conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT UNIQUE NOT NULL,
                cover TEXT,
                tags TEXT DEFAULT '',
                track_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL,
                title TEXT,
                path TEXT UNIQUE NOT NULL,
                duration REAL DEFAULT 0,
                track_number INTEGER DEFAULT 0,
                FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_tracks_playlist ON tracks(playlist_id);
        """)
        # 为旧数据库添加 parent_path 列 + 索引（兼容已有数据库）
        for stmt in [
            "ALTER TABLE playlists ADD COLUMN parent_path TEXT DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS idx_playlists_parent ON playlists(parent_path)",
            "ALTER TABLE playlists ADD COLUMN mtime REAL DEFAULT 0",
        ]:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass


# ── 歌单层级查询 ──

def list_playlists():
    """返回所有歌单"""
    with _active_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, path, parent_path, cover, tags, track_count, mtime "
            "FROM playlists ORDER BY name"
        ).fetchall()
        return [dict(row) for row in rows]


def list_playlists_by_parent(parent_path=""):
    """返回指定父路径下的歌单"""
    with _active_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, path, parent_path, cover, tags, track_count, mtime "
            "FROM playlists WHERE parent_path = ? ORDER BY name",
            (parent_path,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_child_count(path):
    """返回指定路径下有多少个子歌单"""
    with _active_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM playlists WHERE parent_path = ?",
            (path,),
        ).fetchone()
        return row["cnt"] if row else 0


def get_child_counts_batch(paths):
    """批量获取多个路径的子歌单数 {path: count}（一次查询替代 N 次 get_child_count）"""
    if not paths:
        return {}
    placeholders = ",".join("?" * len(paths))
    with _active_conn() as conn:
        rows = conn.execute(
            f"SELECT parent_path, COUNT(*) AS cnt FROM playlists "
            f"WHERE parent_path IN ({placeholders}) GROUP BY parent_path",
            list(paths),
        ).fetchall()
        return {row["parent_path"]: row["cnt"] for row in rows}


def get_playlist(playlist_id):
    """获取单个歌单"""
    with _active_conn() as conn:
        row = conn.execute(
            "SELECT id, name, path, parent_path, cover, tags, track_count, mtime "
            "FROM playlists WHERE id = ?",
            (playlist_id,),
        ).fetchone()
        return dict(row) if row else None


def get_playlist_by_path(path):
    """按路径获取歌单"""
    with _active_conn() as conn:
        row = conn.execute(
            "SELECT id, name, path, parent_path, cover, tags, track_count, mtime "
            "FROM playlists WHERE path = ?",
            (path,),
        ).fetchone()
        return dict(row) if row else None


def playlist_has_children(playlist_id):
    """判断歌单是否有子歌单"""
    pl = get_playlist(playlist_id)
    if not pl:
        return False
    return get_child_count(pl["path"]) > 0


def get_descendant_playlists(parent_path):
    """获取指定路径下所有后代歌单（递归CTE，一次SQL查询替代N+1递归）"""
    if not parent_path:
        return []
    with _active_conn() as conn:
        rows = conn.execute("""
            WITH RECURSIVE descendants AS (
                SELECT id, name, path, parent_path, cover, tags, track_count, mtime
                FROM playlists WHERE parent_path = ?
                UNION ALL
                SELECT p.id, p.name, p.path, p.parent_path, p.cover, p.tags, p.track_count, p.mtime
                FROM playlists p
                JOIN descendants d ON p.parent_path = d.path
            )
            SELECT * FROM descendants
        """, (parent_path,)).fetchall()
        return [dict(row) for row in rows]


# ── 歌单 CRUD ──

def add_playlist(name, path, cover=None, tags="", parent_path="", mtime=0):
    """添加歌单，返回 id"""
    with _active_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO playlists (name, path, parent_path, cover, tags, mtime) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, path, parent_path, cover, tags, mtime),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM playlists WHERE path = ?", (path,)
        ).fetchone()
        return row["id"] if row else None


def update_playlist(playlist_id, cover=None, tags=None, track_count=None,
                    name=None, parent_path=None, mtime=None):
    """更新歌单信息"""
    sets = []
    params = []
    if cover is not None:
        sets.append("cover = ?")
        params.append(cover)
    if tags is not None:
        sets.append("tags = ?")
        params.append(tags)
    if track_count is not None:
        sets.append("track_count = ?")
        params.append(track_count)
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if parent_path is not None:
        sets.append("parent_path = ?")
        params.append(parent_path)
    if mtime is not None:
        sets.append("mtime = ?")
        params.append(mtime)
    if not sets:
        return
    params.append(playlist_id)
    with _active_conn() as conn:
        conn.execute(
            f"UPDATE playlists SET {', '.join(sets)} WHERE id = ?", params
        )


def delete_playlist(playlist_id):
    """删除歌单及所有子歌单、曲目（递归CTE批量删除，替代逐层递归N+1）"""
    pl = get_playlist(playlist_id)
    if not pl:
        return
    with _active_conn() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            DELETE FROM playlists WHERE id IN (
                WITH RECURSIVE descendants(id) AS (
                    SELECT id FROM playlists WHERE id = ?
                    UNION ALL
                    SELECT p.id FROM playlists p
                    JOIN descendants d ON p.parent_path = (
                        SELECT path FROM playlists WHERE id = d.id
                    )
                )
                SELECT id FROM descendants
            )
        """, (playlist_id,))


# ── 曲目 CRUD ──

def list_tracks(playlist_id):
    """返回歌单内所有曲目"""
    with _active_conn() as conn:
        rows = conn.execute(
            "SELECT id, playlist_id, title, path, duration, track_number "
            "FROM tracks WHERE playlist_id = ? ORDER BY track_number, title",
            (playlist_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def list_tracks_by_playlist_ids(playlist_ids):
    """批量获取多个歌单的曲目（临时表方案，突破SQLite 999参数上限）"""
    if not playlist_ids:
        return []
    with _active_conn() as conn:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _batch_pids (id INTEGER)")
        conn.execute("DELETE FROM _batch_pids")
        conn.executemany("INSERT INTO _batch_pids VALUES (?)",
                         [(pid,) for pid in playlist_ids])
        rows = conn.execute(
            "SELECT id, playlist_id, title, path, duration, track_number "
            "FROM tracks WHERE playlist_id IN (SELECT id FROM _batch_pids) "
            "ORDER BY playlist_id, track_number, title"
        ).fetchall()
        return [dict(row) for row in rows]


def add_tracks(playlist_id, tracks):
    """批量添加曲目
    tracks: [(title, path, duration, track_number), ...]
    """
    if not tracks:
        return
    with _active_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO tracks (playlist_id, title, path, duration, track_number) "
            "VALUES (?, ?, ?, ?, ?)",
            [(playlist_id, t, p, d, n) for t, p, d, n in tracks],
        )


def delete_tracks_not_in(playlist_id, valid_paths):
    """删除歌单中不在 valid_paths 中的曲目（临时表方案，突破SQLite 999参数上限）"""
    if not valid_paths:
        return
    with _active_conn() as conn:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _valid_paths (path TEXT)")
        conn.execute("DELETE FROM _valid_paths")
        conn.executemany("INSERT INTO _valid_paths VALUES (?)",
                         [(p,) for p in valid_paths])
        conn.execute(
            "DELETE FROM tracks WHERE playlist_id = ? "
            "AND path NOT IN (SELECT path FROM _valid_paths)",
            (playlist_id,),
        )


def get_playlist_paths():
    """返回 {path: id}"""
    with _active_conn() as conn:
        rows = conn.execute("SELECT id, path FROM playlists").fetchall()
        return {row["path"]: row["id"] for row in rows}


def clear_all():
    """清空所有数据"""
    with get_conn() as conn:
        conn.executescript("DELETE FROM tracks; DELETE FROM playlists;")


def get_all_tags():
    """获取所有不重复的标签（从所有歌单的 tags 字段解析）"""
    with _active_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT tags FROM playlists WHERE tags != ''"
        ).fetchall()
        tag_set = set()
        for row in rows:
            if row["tags"]:
                for t in re.split(r"[,，\n\r\s]+", row["tags"]):
                    t = t.strip()
                    if t:
                        tag_set.add(t)
        return sorted(tag_set, key=lambda x: x.lower())
