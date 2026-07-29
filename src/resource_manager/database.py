import os
import re
import sqlite3
from contextlib import contextmanager

import resource_manager.config as config


def _natural_key(s: str):
    """自然排序 key：将字符串中的数字部分按整数比较
    例：'10.jpg' -> ['', 10, '.jpg']
    使 '1.jpg, 2.jpg, ..., 10.jpg' 按数字顺序而非字符串顺序排列
    """
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
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
        _persistent_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
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

            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id INTEGER NOT NULL,
                path TEXT UNIQUE NOT NULL,
                FOREIGN KEY (work_id) REFERENCES works(id)
            );

            CREATE INDEX IF NOT EXISTS idx_works_author ON works(author_id);
            CREATE INDEX IF NOT EXISTS idx_images_work ON images(work_id);
            """
        )
        # migration: 给 images 表加 mtime 字段（用于增量扫描）
        cols = conn.execute("PRAGMA table_info(images)").fetchall()
        col_names = [c["name"] for c in cols]
        if "mtime" not in col_names:
            conn.execute("ALTER TABLE images ADD COLUMN mtime INTEGER DEFAULT 0")


def add_author(name):
    with get_conn() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO authors (name) VALUES (?)", (name,))
        conn.commit()
        return cur.lastrowid or get_author_id(name)


def get_author_id(name):
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM authors WHERE name = ?", (name,)).fetchone()
        return row["id"] if row else None


def list_authors():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.name, COUNT(w.id) AS work_count
            FROM authors a
            LEFT JOIN works w ON w.author_id = a.id
            GROUP BY a.id
            ORDER BY a.name
            """
        ).fetchall()
        return [dict(row) for row in rows]


def add_work(author_id, name, path, thumbnail=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO works (author_id, name, path, thumbnail) VALUES (?, ?, ?, ?)",
            (author_id, name, path, thumbnail),
        )
        conn.commit()
        return cur.lastrowid or get_work_id_by_path(path)


def get_work_id_by_path(path):
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM works WHERE path = ?", (path,)).fetchone()
        return row["id"] if row else None


def list_works(author_id=None):
    sql = """
        SELECT w.id, w.name, w.path, w.thumbnail, a.name AS author,
               (SELECT COUNT(*) FROM images WHERE work_id = w.id) AS image_count
        FROM works w
        JOIN authors a ON a.id = w.author_id
    """
    params = ()
    if author_id is not None:
        sql += " WHERE w.author_id = ?"
        params = (author_id,)
    sql += " ORDER BY a.name, w.name"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def add_image(work_id, path, mtime=0):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO images (work_id, path, mtime) VALUES (?, ?, ?)",
            (work_id, path, mtime),
        )


def add_author_with_works(author_name, works, progress_callback=None):
    """
    按作者批量插入作品和图片，整个作者在一个事务中完成。
    works: [(work_name, work_path, thumbnail, [(image_path, mtime), ...]), ...]
    progress_callback: 可选，接收 (current_work_name, total_images) 用于进度显示
    """
    with get_conn() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO authors (name) VALUES (?)", (author_name,))
        author_id = cur.lastrowid
        if not author_id:
            row = conn.execute("SELECT id FROM authors WHERE name = ?", (author_name,)).fetchone()
            author_id = row["id"]

        for work_name, work_path, thumbnail, images in works:
            cur = conn.execute(
                "INSERT OR IGNORE INTO works (author_id, name, path, thumbnail) VALUES (?, ?, ?, ?)",
                (author_id, work_name, work_path, thumbnail),
            )
            work_id = cur.lastrowid
            if not work_id:
                row = conn.execute("SELECT id FROM works WHERE path = ?", (work_path,)).fetchone()
                work_id = row["id"]

            if images:
                # images: [(path, mtime), ...]
                conn.executemany(
                    "INSERT OR IGNORE INTO images (work_id, path, mtime) VALUES (?, ?, ?)",
                    [(work_id, p, m) for p, m in images],
                )

            if progress_callback:
                progress_callback(work_name, len(images))


def get_images_by_work(work_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM images WHERE work_id = ?", (work_id,)
        ).fetchall()
        images = [dict(row) for row in rows]
    # 自然排序：1.jpg, 2.jpg, ..., 10.jpg（而非 1, 10, 11, ..., 2）
    images.sort(key=lambda x: _natural_key(x["path"]))
    return images


def clear_all():
    with get_conn() as conn:
        conn.executescript(
            "DELETE FROM images; DELETE FROM works; DELETE FROM authors;"
        )


# ==================== 增量扫描辅助函数 ====================

def list_all_authors():
    """返回 [(id, name), ...]"""
    with _active_conn() as conn:
        rows = conn.execute("SELECT id, name FROM authors").fetchall()
        return [(row["id"], row["name"]) for row in rows]


def list_works_by_author(author_id):
    """返回 {path: (id, name, thumbnail)}"""
    with _active_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, path, thumbnail FROM works WHERE author_id = ?",
            (author_id,),
        ).fetchall()
        return {row["path"]: (row["id"], row["name"], row["thumbnail"]) for row in rows}


def list_all_thumbnail_paths():
    """返回数据库中所有作品缩略图路径（用于孤儿清理）"""
    with _active_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT thumbnail FROM works WHERE thumbnail IS NOT NULL"
        ).fetchall()
        return [row["thumbnail"] for row in rows if row["thumbnail"]]


def get_work_image_mtimes(work_id):
    """返回 {path: mtime}"""
    with _active_conn() as conn:
        rows = conn.execute(
            "SELECT path, mtime FROM images WHERE work_id = ?", (work_id,)
        ).fetchall()
        return {row["path"]: row["mtime"] for row in rows}


def upsert_images(work_id, image_mtimes):
    """批量插入新图片 [(path, mtime), ...]"""
    if not image_mtimes:
        return
    with _active_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO images (work_id, path, mtime) VALUES (?, ?, ?)",
            [(work_id, p, m) for p, m in image_mtimes],
        )


def update_image_mtimes(updates):
    """批量更新图片 mtime [(new_mtime, path), ...]"""
    if not updates:
        return
    with _active_conn() as conn:
        conn.executemany(
            "UPDATE images SET mtime = ? WHERE path = ?",
            updates,
        )


def delete_images_by_paths(paths):
    """按路径批量删除图片"""
    if not paths:
        return
    with _active_conn() as conn:
        conn.executemany(
            "DELETE FROM images WHERE path = ?",
            [(p,) for p in paths],
        )


def delete_work(work_id):
    """删除作品及其所有图片"""
    with _active_conn() as conn:
        conn.execute("DELETE FROM images WHERE work_id = ?", (work_id,))
        conn.execute("DELETE FROM works WHERE id = ?", (work_id,))


def delete_author(author_id):
    """删除作者及其所有作品和图片"""
    with _active_conn() as conn:
        work_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM works WHERE author_id = ?", (author_id,)
        ).fetchall()]
        if work_ids:
            placeholders = ",".join("?" * len(work_ids))
            conn.execute(
                f"DELETE FROM images WHERE work_id IN ({placeholders})",
                work_ids,
            )
        conn.execute("DELETE FROM works WHERE author_id = ?", (author_id,))
        conn.execute("DELETE FROM authors WHERE id = ?", (author_id,))
