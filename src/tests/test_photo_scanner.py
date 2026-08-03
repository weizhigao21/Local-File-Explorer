"""
写真模块扫描器单元测试
重点覆盖目录级指纹（level="dir"）的集成行为：
- 首次扫描建立 dir_fingerprint 后，二次扫描无变化时返回 unchanged 跳过
- 新增作者 / 新增作品 / 删除作品均能被 dir level 检测到
- 文件内容修改（同文件名覆盖）dir level 漏检 —— 这是设计取舍，验证预期行为

使用临时 PHOTO_ROOT、临时 DB、临时指纹缓存，避免污染真实数据。
"""
import os
import shutil
import time

import pytest
from PIL import Image

from resource_manager import config
from resource_manager import database as db
from resource_manager import fingerprint_cache as fp
from resource_manager import scanner


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
