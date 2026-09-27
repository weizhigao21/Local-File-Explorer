"""
写真模块扫描器单元测试
重点覆盖目录级指纹（level="dir"）的集成行为：
- 首次扫描建立 dir_fingerprint 后，二次扫描无变化时返回 unchanged 跳过
- 新增作者 / 新增作品 / 删除作品均能被 dir level 检测到
- 文件内容修改（同文件名覆盖）dir level 漏检 —— 这是设计取舍，验证预期行为

使用临时 PHOTO_ROOT、临时 DB、临时指纹缓存，避免污染真实数据。
"""
import shutil
import sqlite3
import time

import pytest
from PIL import Image

from resource_manager import config, scanner
from resource_manager import database as db
from resource_manager import fingerprint_cache as fp


@pytest.fixture
def temp_photo_env(tmp_path, monkeypatch):
    """搭建临时写真扫描环境：临时 PHOTO_ROOT、DB、指纹缓存、缩略图目录"""
    photo_root = tmp_path / "photo_root"
    photo_root.mkdir()

    monkeypatch.setattr(config, "PHOTO_ROOT", str(photo_root))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "THUMBNAIL_DIR", str(tmp_path / "thumbnails"))
    # 指纹缓存路径是模块级变量，需单独 monkeypatch 避免污染真实缓存
    monkeypatch.setattr(fp, "_FINGERPRINT_PATH", str(tmp_path / ".scan_fingerprint.json"))

    db.end_persistent()
    db.init_db()
    yield photo_root
    db.end_persistent()


def _make_image(path, color=(255, 0, 0)):
    """生成一张简单的 JPEG 测试图片"""
    img = Image.new("RGB", (10, 10), color)
    img.save(path, "JPEG")


def _make_work(photo_root, author, work, img_count=1):
    """在 photo_root 下创建 作者/作品/图片 三级结构"""
    work_dir = photo_root / author / work
    work_dir.mkdir(parents=True)
    for i in range(img_count):
        _make_image(work_dir / f"{i:03d}.jpg")
    # 留出 mtime 精度窗口，确保后续目录 mtime 变化可被检测
    time.sleep(0.02)
    return work_dir


# ==================== unchanged 跳过 ====================

def test_first_scan_builds_dir_fingerprint(temp_photo_env):
    """首次扫描应真实执行（缓存为空），并在完成后写入 dir_fingerprint"""
    photo_root = temp_photo_env
    _make_work(photo_root, "作者A", "作品1")

    stats = scanner.scan(check_fingerprint=True)
    # 真实扫描不应返回 unchanged
    assert "unchanged" not in stats
    assert stats.get("skipped_all") is None
    # 扫描完成后应写入 dir_fingerprint
    unchanged, _ = fp.is_unchanged(str(photo_root), level="dir")
    assert unchanged is True


def test_second_scan_unchanged_returns_skipped(temp_photo_env):
    """首次扫描建立指纹后，第二次无变化扫描应返回 unchanged=True 跳过整个流程"""
    photo_root = temp_photo_env
    _make_work(photo_root, "作者A", "作品1")

    scanner.scan(check_fingerprint=True)

    stats2 = scanner.scan(check_fingerprint=True)
    assert stats2.get("unchanged") is True
    assert stats2.get("skipped_all") is True


# ==================== 目录变化检测 ====================

def test_scan_detects_new_author(temp_photo_env):
    """新增作者文件夹后，dir 指纹变化，scan 应重新同步"""
    photo_root = temp_photo_env
    _make_work(photo_root, "作者A", "作品1")
    scanner.scan(check_fingerprint=True)

    _make_work(photo_root, "作者B", "作品1")

    unchanged, _ = fp.is_unchanged(str(photo_root), level="dir")
    assert unchanged is False

    stats = scanner.scan(check_fingerprint=True)
    assert "unchanged" not in stats
    authors = [name for _, name in db.list_all_authors()]
    assert "作者A" in authors
    assert "作者B" in authors


def test_scan_detects_new_work_under_existing_author(temp_photo_env):
    """在已有作者下新增作品文件夹后，父作者目录 mtime 变化，dir 指纹应变化"""
    photo_root = temp_photo_env
    _make_work(photo_root, "作者A", "作品1")
    scanner.scan(check_fingerprint=True)

    _make_work(photo_root, "作者A", "作品2")

    unchanged, _ = fp.is_unchanged(str(photo_root), level="dir")
    assert unchanged is False


def test_scan_detects_deleted_work(temp_photo_env):
    """删除作品文件夹后，父作者目录 mtime 变化，dir 指纹应变化"""
    photo_root = temp_photo_env
    _make_work(photo_root, "作者A", "作品1")
    _make_work(photo_root, "作者A", "作品2")
    scanner.scan(check_fingerprint=True)

    shutil.rmtree(photo_root / "作者A" / "作品2")
    time.sleep(0.02)

    unchanged, _ = fp.is_unchanged(str(photo_root), level="dir")
    assert unchanged is False


# ==================== 漏检预期行为（设计取舍） ====================

def test_dir_level_ignores_file_content_change(temp_photo_env):
    """仅修改图片文件内容（不增删文件、不改文件夹结构）时，dir level 应漏检

    这是目录级指纹的设计取舍：用速度换"文件内容替换"检测能力。
    用户场景（仅增删文件夹/文件）下此漏检可接受，已在 fingerprint_cache.py
    顶部注释记录。同时用 file level 对比验证 file level 能检测到。
    """
    photo_root = temp_photo_env
    _make_work(photo_root, "作者A", "作品1")
    scanner.scan(check_fingerprint=True)

    # 直接覆盖文件字节内容（open wb 截断写入，不删除/新建文件，目录条目不变）
    img_path = photo_root / "作者A" / "作品1" / "000.jpg"
    with open(img_path, "wb") as f:
        f.write(b"different content bytes")

    # dir level 不 stat 文件，应仍判定为 unchanged
    unchanged, _ = fp.is_unchanged(str(photo_root), level="dir")
    assert unchanged is True

    # file level 应能检测到（mtime 或 size 变化）
    unchanged_file, _ = fp.is_unchanged(str(photo_root), level="file")
    assert unchanged_file is False


# ==================== 长连接写锁（回归守卫） ====================
# 背景：scan() 用 begin_persistent() 开长连接，写事务一直持有到扫描结束才提交；
# 而 database.py 里的 add_author / get_author_id / add_author_with_works 曾走
# get_conn() 短连接，与长连接争抢 SQLite 写锁 → 短连接抛 `database is locked`。
# 该异常被 scan() 外层 per-author try/except 吞掉（只打印），于是：
#   1. 受影响作者的作品整批丢失；
#   2. stats["added"] 仍照常累加（假成功）；
#   3. 扫描结尾照样 fp.update() 写指纹 → 下次扫描判定"无变化"直接跳过，
#      新作品长期不出现且用户看不到任何报错。
# 因此断言必须落在「数据库真实内容」上，不能依赖异常传播。

def _scan_once_no_unchanged():
    stats = scanner.scan(check_fingerprint=True)
    assert "unchanged" not in stats, "本轮扫描应真实执行而非被指纹跳过"
    return stats


def _count_orphan_works():
    """统计 works.author_id 指向不存在作者的「悬空外键」数量"""
    conn = sqlite3.connect(config.DB_PATH)
    n = conn.execute(
        "SELECT COUNT(*) FROM works w LEFT JOIN authors a ON a.id = w.author_id "
        "WHERE a.id IS NULL"
    ).fetchone()[0]
    conn.close()
    return n


def test_scan_new_work_alongside_image_add_not_lost(temp_photo_env):
    """同一轮扫描里既有「已有作品追加图片」(长连接写) 又有「新增作品」时，两者都要落库"""
    photo_root = temp_photo_env
    work1 = _make_work(photo_root, "作者A", "作品1", img_count=1)
    _scan_once_no_unchanged()

    # 同一轮扫描内：作品1 追加一张图（触发长连接写事务）+ 新增作品2（短连接写）
    _make_image(work1 / "001.jpg")
    time.sleep(0.02)
    _make_work(photo_root, "作者A", "作品2", img_count=1)

    stats = _scan_once_no_unchanged()

    works = db.list_works_by_author(db.get_author_id("作者A"))
    assert str(work1) in works, "已有作品1 被误删"
    assert str(photo_root / "作者A" / "作品2") in works, (
        "作品2 未入库 —— 长连接持写锁时短连接写入失败且异常被吞掉"
    )
    # 作品1 追加的图片也必须落库
    assert len(db.get_work_image_mtimes(works[str(work1)][0])) == 2
    # stats 与实际落库一致（旧实现下 stats 会虚报）
    assert stats["added"] == 2
    assert _count_orphan_works() == 0, "作品的 author_id 指向了不存在的作者"


def test_scan_new_author_alongside_image_add_not_lost(temp_photo_env):
    """作者A 持写锁时新增「作者B」——add_author 走短连接会让整个新作者丢失"""
    photo_root = temp_photo_env
    work_a = _make_work(photo_root, "作者A", "作品1", img_count=1)
    _scan_once_no_unchanged()

    # 同一轮扫描内：给作者A 的作品追加图片 + 新增作者B
    _make_image(work_a / "001.jpg")
    time.sleep(0.02)
    _make_work(photo_root, "作者B", "作品1", img_count=1)

    _scan_once_no_unchanged()

    authors = [name for _, name in db.list_all_authors()]
    assert "作者A" in authors
    assert "作者B" in authors, (
        "作者B 整个丢失 —— add_author 短连接写入被长连接写锁挡住且异常被吞掉"
    )
    works_b = db.list_works_by_author(db.get_author_id("作者B"))
    assert str(photo_root / "作者B" / "作品1") in works_b
