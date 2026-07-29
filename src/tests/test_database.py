"""
数据库增删查改单元测试
使用临时数据库，避免污染真实数据
"""
import sqlite3

import pytest

from resource_manager import database as db
from resource_manager import config


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """创建临时数据库，避免污染真实数据库"""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    # 清除任何已存在的长连接
    db.end_persistent()
    db.init_db()
    yield db
    # 清理长连接
    db.end_persistent()


def _get_columns(table):
    """获取表的所有列名"""
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    cols = [c["name"] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    conn.close()
    return cols


# ==================== init_db / schema ====================

def test_init_db_creates_tables(temp_db):
    """init_db 创建所有表"""
    conn = sqlite3.connect(config.DB_PATH)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    conn.close()
    assert 'authors' in tables
    assert 'works' in tables
    assert 'images' in tables


def test_images_table_has_mtime(temp_db):
    """images 表应该有 mtime 字段（用于增量扫描）"""
    cols = _get_columns("images")
    assert "mtime" in cols


def test_init_db_idempotent(temp_db):
    """init_db 可重复调用，不会破坏 schema"""
    db.init_db()
    db.init_db()
    cols = _get_columns("images")
    assert "mtime" in cols
    # mtime 字段不会被重复添加
    assert cols.count("mtime") == 1


# ==================== 作者 ====================

def test_add_and_get_author(temp_db):
    """添加作者、查询作者 ID"""
    aid = db.add_author("作者A")
    assert aid is not None
    aid2 = db.get_author_id("作者A")
    assert aid2 == aid
    # 不存在的作者
    assert db.get_author_id("不存在的作者") is None


def test_list_all_authors(temp_db):
    """列出所有作者"""
    db.add_author("作者A")
    db.add_author("作者B")
    authors = db.list_all_authors()
    assert len(authors) == 2
    names = [a[1] for a in authors]
    assert "作者A" in names
    assert "作者B" in names


# ==================== 作品 + 图片 ====================

def test_add_author_with_works(temp_db):
    """批量添加作者和作品（含图片 mtime）"""
    works = [
        ("作品1", "/path/work1", "/path/thumb1.jpg",
         [("/path/img1.jpg", 1000), ("/path/img2.jpg", 2000)]),
        ("作品2", "/path/work2", "/path/thumb2.jpg",
         [("/path/img3.jpg", 3000)]),
    ]
    db.add_author_with_works("作者A", works)
    # 查询验证
    aid = db.get_author_id("作者A")
    work_map = db.list_works_by_author(aid)
    assert len(work_map) == 2
    assert "/path/work1" in work_map
    # 验证图片 mtime
    work_id = work_map["/path/work1"][0]
    mtimes = db.get_work_image_mtimes(work_id)
    assert len(mtimes) == 2
    assert mtimes["/path/img1.jpg"] == 1000
    assert mtimes["/path/img2.jpg"] == 2000


def test_get_work_image_mtimes_empty(temp_db):
    """空作品的图片 mtime 查询返回空 dict"""
    works = [("作品1", "/path/work1", "/thumb.jpg", [])]
    db.add_author_with_works("作者A", works)
    aid = db.get_author_id("作者A")
    work_map = db.list_works_by_author(aid)
    work_id = work_map["/path/work1"][0]
    mtimes = db.get_work_image_mtimes(work_id)
    assert mtimes == {}


# ==================== 增量同步 ====================

def test_upsert_images(temp_db):
    """新增图片"""
    works = [("作品1", "/path/work1", "/thumb.jpg",
              [("/path/img1.jpg", 1000)])]
    db.add_author_with_works("作者A", works)
    aid = db.get_author_id("作者A")
    work_id = db.list_works_by_author(aid)["/path/work1"][0]
    # 新增 2 张
    db.upsert_images(work_id, [("/path/img2.jpg", 2000), ("/path/img3.jpg", 3000)])
    mtimes = db.get_work_image_mtimes(work_id)
    assert len(mtimes) == 3
    assert mtimes["/path/img2.jpg"] == 2000


def test_update_image_mtimes(temp_db):
    """更新图片 mtime"""
    works = [("作品1", "/path/work1", "/thumb.jpg",
              [("/path/img1.jpg", 1000), ("/path/img2.jpg", 2000)])]
    db.add_author_with_works("作者A", works)
    # 更新 mtime
    db.update_image_mtimes([(5000, "/path/img1.jpg")])
    aid = db.get_author_id("作者A")
    work_id = db.list_works_by_author(aid)["/path/work1"][0]
    mtimes = db.get_work_image_mtimes(work_id)
    assert mtimes["/path/img1.jpg"] == 5000
    assert mtimes["/path/img2.jpg"] == 2000  # 未变


def test_delete_images_by_paths(temp_db):
    """按路径删除图片"""
    works = [("作品1", "/path/work1", "/thumb.jpg",
              [("/path/img1.jpg", 1000), ("/path/img2.jpg", 2000)])]
    db.add_author_with_works("作者A", works)
    db.delete_images_by_paths(["/path/img1.jpg"])
    aid = db.get_author_id("作者A")
    work_id = db.list_works_by_author(aid)["/path/work1"][0]
    mtimes = db.get_work_image_mtimes(work_id)
    assert len(mtimes) == 1
    assert "/path/img1.jpg" not in mtimes
    assert "/path/img2.jpg" in mtimes


def test_delete_images_by_paths_empty(temp_db):
    """空列表不报错"""
    db.delete_images_by_paths([])
    db.delete_images_by_paths(None)


def test_full_incremental_sync(temp_db):
    """完整增量同步流程：增、删、改"""
    # 初始：1 作品，2 张图
    works = [("作品1", "/path/work1", "/thumb.jpg",
              [("/path/img1.jpg", 1000), ("/path/img2.jpg", 2000)])]
    db.add_author_with_works("作者A", works)
    aid = db.get_author_id("作者A")
    work_id = db.list_works_by_author(aid)["/path/work1"][0]

    # 模拟：图1 mtime 变化，新增图3，删除图2
    db.upsert_images(work_id, [("/path/img3.jpg", 3000)])
    db.update_image_mtimes([(5000, "/path/img1.jpg")])
    db.delete_images_by_paths(["/path/img2.jpg"])

    mtimes = db.get_work_image_mtimes(work_id)
    assert len(mtimes) == 2
    assert mtimes["/path/img1.jpg"] == 5000  # 更新
    assert mtimes["/path/img3.jpg"] == 3000  # 新增
    assert "/path/img2.jpg" not in mtimes    # 删除


# ==================== 级联删除 ====================

def test_delete_work(temp_db):
    """删除作品时同时删除其所有图片"""
    works = [("作品1", "/path/work1", "/thumb.jpg",
              [("/path/img1.jpg", 1000), ("/path/img2.jpg", 2000)])]
    db.add_author_with_works("作者A", works)
    aid = db.get_author_id("作者A")
    work_id = db.list_works_by_author(aid)["/path/work1"][0]
    db.delete_work(work_id)
    # 作品被删
    work_map = db.list_works_by_author(aid)
    assert "/path/work1" not in work_map
    # 图片也被删（直接查数据库）
    conn = sqlite3.connect(config.DB_PATH)
    count = conn.execute(
        "SELECT COUNT(*) FROM images WHERE work_id = ?", (work_id,)
    ).fetchone()[0]
    conn.close()
    assert count == 0


def test_delete_author_cascade(temp_db):
    """删除作者时级联删除作品和图片"""
    works = [
        ("作品1", "/path/work1", "/thumb1.jpg", [("/path/img1.jpg", 1000)]),
        ("作品2", "/path/work2", "/thumb2.jpg", [("/path/img2.jpg", 2000)]),
    ]
    db.add_author_with_works("作者A", works)
    aid = db.get_author_id("作者A")
    assert aid is not None
    db.delete_author(aid)
    # 作者被删
    assert db.get_author_id("作者A") is None
    # 作品被删
    assert db.list_works_by_author(aid) == {}
    # 图片被删
    conn = sqlite3.connect(config.DB_PATH)
    count = conn.execute(
        "SELECT COUNT(*) FROM images"
    ).fetchone()[0]
    conn.close()
    assert count == 0


# ==================== 缩略图路径查询 ====================

def test_list_all_thumbnail_paths(temp_db):
    """查询所有作品缩略图路径（用于 GC）"""
    works = [
        ("作品1", "/path/work1", "/thumb/a.jpg", []),
        ("作品2", "/path/work2", "/thumb/b.jpg", []),
        ("作品3", "/path/work3", None, []),  # 无缩略图
    ]
    db.add_author_with_works("作者A", works)
    thumbs = db.list_all_thumbnail_paths()
    assert "/thumb/a.jpg" in thumbs
    assert "/thumb/b.jpg" in thumbs
    assert len(thumbs) == 2  # None 不应出现


# ==================== 自然排序图片查询 ====================

def test_get_images_by_work_natural_sort(temp_db):
    """get_images_by_work 应按自然顺序返回"""
    works = [("作品1", "/path/work1", "/thumb.jpg", [
        ("img10.jpg", 1000),
        ("img2.jpg", 2000),
        ("img1.jpg", 3000),
        ("img11.jpg", 4000),
        ("img100.jpg", 5000),
    ])]
    db.add_author_with_works("作者A", works)
    aid = db.get_author_id("作者A")
    work_id = db.list_works_by_author(aid)["/path/work1"][0]
    images = db.get_images_by_work(work_id)
    paths = [img["path"] for img in images]
    assert paths == ["img1.jpg", "img2.jpg", "img10.jpg", "img11.jpg", "img100.jpg"]


# ==================== 长连接 ====================

def test_persistent_connection(temp_db):
    """长连接模式下复用同一个 connection"""
    db.begin_persistent()
    db.add_author("作者A")
    db.add_author("作者B")
    authors = db.list_all_authors()
    assert len(authors) == 2
    db.end_persistent()
    # 结束后还能正常使用短连接
    db.add_author("作者C")
    assert len(db.list_all_authors()) == 3
