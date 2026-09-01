# -*- coding: utf-8 -*-
"""
DLsite 作品信息独立数据库（data/dlsite.db）。

与主库 resource_manager.db 完全隔离，短连接模式（与 resource_manager.database 一致），
可被后台抓取线程与 UI 线程并发访问。
"""
import json
import os
import sqlite3
import time

from resource_manager import config

# 连接级配置：WAL + 忙等待，多线程短连接友好
_CONN_KWARGS = dict(timeout=10)


def get_conn() -> sqlite3.Connection:
    """打开一个短连接（调用方负责 close），行工厂返回 dict"""
    os.makedirs(os.path.dirname(config.DLSITE_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DLSITE_DB_PATH, **_CONN_KWARGS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """建表（幂等）"""
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS works (
                rj_code      TEXT PRIMARY KEY,
                title        TEXT,
                circle       TEXT,
                cv           TEXT,
                release_date TEXT,
                age_rating   TEXT,
                work_type    TEXT,
                file_type    TEXT,
                file_size    TEXT,
                genres       TEXT,
                description  TEXT,
                price        TEXT,
                cover_url    TEXT,
                cover_path   TEXT,
                fetched_at   REAL,
                error        TEXT
            );
            """
        )


def upsert_work(info: dict):
    """写入/更新一条作品记录（info 至少含 rj_code）"""
    rj = info.get("rj_code")
    if not rj:
        return
    # description / genres 是列表 → JSON 序列化
    desc = info.get("description")
    if isinstance(desc, list):
        desc = json.dumps(desc, ensure_ascii=False)
    genres = info.get("genres")
    if isinstance(genres, list):
        genres = json.dumps(genres, ensure_ascii=False)

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO works (rj_code, title, circle, cv, release_date,
                               age_rating, work_type, file_type, file_size,
                               genres, description, price,
                               cover_url, cover_path, fetched_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rj_code) DO UPDATE SET
                title=excluded.title, circle=excluded.circle, cv=excluded.cv,
                release_date=excluded.release_date, age_rating=excluded.age_rating,
                work_type=excluded.work_type, file_type=excluded.file_type,
                file_size=excluded.file_size, genres=excluded.genres,
                description=excluded.description, price=excluded.price,
                cover_url=excluded.cover_url, cover_path=excluded.cover_path,
                fetched_at=excluded.fetched_at, error=excluded.error
            """,
            (rj, info.get("title"), info.get("circle"), info.get("cv"),
             info.get("release_date"), info.get("age_rating"),
             info.get("work_type"), info.get("file_type"), info.get("file_size"),
             genres, desc, info.get("price"),
             info.get("cover_url"), info.get("cover_path"),
             info.get("fetched_at", time.time()), info.get("error")),
        )


def set_error(rj: str, error: str):
    """记录抓取失败原因（便于后续重试）"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO works (rj_code, error, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(rj_code) DO UPDATE SET error=excluded.error, fetched_at=excluded.fetched_at",
            (rj, error, time.time()),
        )


def get_work(rj: str) -> dict | None:
    """查询单条作品记录（无则 None）；genres/description 反序列化为列表"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM works WHERE rj_code = ?", (rj,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    for key in ("genres", "description"):
        v = d.get(key)
        if isinstance(v, str) and v:
            try:
                d[key] = json.loads(v)
            except json.JSONDecodeError:
                pass
    return d


def has_work(rj: str) -> bool:
    """是否已成功抓取过（有标题且无 error 视为成功）"""
    w = get_work(rj)
    return bool(w and w.get("title") and not w.get("error"))


def get_cached_rjs() -> set:
    """已成功获取信息的 RJ 码集合（启动批量校验用，一次全量查询代替逐条 SELECT）"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT rj_code FROM works WHERE title IS NOT NULL AND title != '' AND error IS NULL"
        ).fetchall()
    return {r["rj_code"] for r in rows}


def get_cover_map() -> dict:
    """返回 {rj_code: cover_path}，仅含本地封面文件确实存在的记录"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT rj_code, cover_path FROM works WHERE cover_path IS NOT NULL"
        ).fetchall()
    return {
        r["rj_code"]: r["cover_path"]
        for r in rows
        if r["cover_path"] and os.path.exists(r["cover_path"])
    }


def get_genre_map() -> dict:
    """返回 {rj_code: [分类, ...]}，仅含抓取成功的记录（genres 为 JSON 数组）"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT rj_code, genres FROM works WHERE title IS NOT NULL AND error IS NULL "
            "AND genres IS NOT NULL"
        ).fetchall()
    out = {}
    for r in rows:
        try:
            gs = json.loads(r["genres"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(gs, list) and gs:
            out[r["rj_code"]] = gs
    return out


def search_by_genres(required) -> set:
    """返回 genres 同时包含全部 required 项（不区分大小写）的 RJ 码集合。

    required 为要匹配的分类标签列表；为空时返回空集合。
    供浏览器按 DLsite 分类筛选歌单使用，一次查询预处理全部作品。
    """
    req = {g.strip().lower() for g in required if g and str(g).strip()}
    if not req:
        return set()
    with get_conn() as conn:
        rows = conn.execute("SELECT rj_code, genres FROM works").fetchall()
    hits = set()
    for rj, genres_json in rows:
        if not genres_json:
            continue
        try:
            gs = {g.strip().lower() for g in json.loads(genres_json)}
        except (json.JSONDecodeError, TypeError):
            continue
        if req <= gs:
            hits.add(rj)
    return hits
