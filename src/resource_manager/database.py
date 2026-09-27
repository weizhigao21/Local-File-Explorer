import sqlite3
from contextlib import contextmanager

import resource_manager.config as config
from resource_manager.utils import natural_key


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


def _insert_or_ignore_id(conn, insert_sql, insert_params, id_sql, id_params):
    """执行 INSERT OR IGNORE，返回对应行的主键 id（不存在则返回 None）。

    为什么不能直接用 `cur.lastrowid`：
        sqlite3 的 `lastrowid` 取自连接级的 `sqlite3_last_insert_rowid()`，
        含义是「**本连接上**上一次成功插入的行 id」。当 INSERT OR IGNORE 因唯一约束
        被忽略时，该值**不会更新**，于是会把上一次插入——可能是**另一张表**——的
        行 id 当成新行 id 返回。典型后果：长连接扫描中先插入 images（rowid=2），
        再对已存在作者执行 `INSERT OR IGNORE INTO authors`，作者 id 被错认成 2，
        作品就挂到了不存在的 author_id 上（悬空外键）。
    因此仅当 rowcount==1（确实插入）时采用 lastrowid，否则按唯一键回查。
    """
    cur = conn.execute(insert_sql, insert_params)
    if cur.rowcount:
        return cur.lastrowid
    row = conn.execute(id_sql, id_params).fetchone()
    return row["id"] if row else None


def add_author(name):
    """插入作者（幂等），返回 author_id。

    必须走 _active_conn()：扫描期间 scan() 持有长连接写事务，若此处另开短连接写入，
    SQLite 会因无法取得写锁而抛 `database is locked`，且该异常被扫描器的
    per-author try/except 吞掉 → 整批作品静默丢失。
    """
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


def add_author_with_works(author_name, works, progress_callback=None):
    """
    按作者批量插入作品和图片，整个作者在一个事务中完成。
    works: [(work_name, work_path, thumbnail, [(image_path, mtime), ...]), ...]
    progress_callback: 可选，接收 (current_work_name, total_images) 用于进度显示

    必须走 _active_conn()：与 add_author 同理，扫描长连接持写锁时另开短连接会
    `database is locked`，导致新作品整批丢失（而统计仍显示成功）。
    """
    with _active_conn() as conn:
        author_id = _insert_or_ignore_id(
            conn,
            "INSERT OR IGNORE INTO authors (name) VALUES (?)",
            (author_name,),
            "SELECT id FROM authors WHERE name = ?",
            (author_name,),
        )

        for work_name, work_path, thumbnail, images in works:
            work_id = _insert_or_ignore_id(
                conn,
                "INSERT OR IGNORE INTO works (author_id, name, path, thumbnail) VALUES (?, ?, ?, ?)",
                (author_id, work_name, work_path, thumbnail),
                "SELECT id FROM works WHERE path = ?",
                (work_path,),
            )
            if work_id is None:
                # path 未命中：目录被重命名时 name 冲突而 path 变化，按 (author_id, name) 兜底
                row = conn.execute(
                    "SELECT id FROM works WHERE author_id = ? AND name = ?",
                    (author_id, work_name),
                ).fetchone()
                work_id = row["id"] if row else None
            if work_id is None:
                # 兜底仍失败：宁可不写，也不落下悬空外键
                print(f"[跳过作品] 无法解析 work_id: {work_path}")
                continue

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
    images.sort(key=lambda x: natural_key(x["path"]))
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
