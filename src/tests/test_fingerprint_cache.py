"""
目录指纹缓存单元测试
验证基于 mtime + size 的指纹生成、对比、更新、清除逻辑
"""
import os
import time

import pytest

from resource_manager import fingerprint_cache as fp


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """创建临时资源根目录，并隔离指纹缓存文件"""
    root = tmp_path / "photos"
    root.mkdir()
    cache_path = tmp_path / ".scan_fingerprint.json"
    monkeypatch.setattr(fp, "_FINGERPRINT_PATH", str(cache_path))
    yield str(root)


def _write_image(root, relpath, content=b"dummy"):
    """在临时根目录下创建一张假图片"""
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


def test_compute_fingerprint_changes_with_content(temp_root):
    """文件内容变化后指纹应改变"""
    _write_image(temp_root, "author1/work1/01.jpg")
    fp1 = fp.compute_fingerprint(temp_root)

    # 修改文件内容（size 变化）
    _write_image(temp_root, "author1/work1/01.jpg", content=b"changed")
    fp2 = fp.compute_fingerprint(temp_root)
    assert fp1 != fp2


def test_compute_fingerprint_changes_with_mtime(temp_root):
    """仅修改 mtime（size 不变）指纹也应改变"""
    path = _write_image(temp_root, "author1/work1/01.jpg")
    fp1 = fp.compute_fingerprint(temp_root)

    # 只改 mtime：显式设置为比之前晚 2 秒，避免 FAT/NTFS 精度问题
    old_stat = os.stat(path)
    new_mtime = old_stat.st_mtime + 2
    os.utime(path, (new_mtime, new_mtime))
    fp2 = fp.compute_fingerprint(temp_root)
    assert fp1 != fp2


def test_compute_fingerprint_stable_for_unchanged_dir(temp_root):
    """目录无变化时指纹应稳定"""
    _write_image(temp_root, "author1/work1/01.jpg")
    fp1 = fp.compute_fingerprint(temp_root)
    fp2 = fp.compute_fingerprint(temp_root)
    assert fp1 == fp2
    assert len(fp1) == 32  # MD5 十六进制长度


def test_is_unchanged_first_time(temp_root):
    """首次扫描没有缓存，应返回有变化"""
    _write_image(temp_root, "author1/work1/01.jpg")
    unchanged, current = fp.is_unchanged(temp_root)
    assert not unchanged
    assert len(current) == 32


def test_update_and_is_unchanged(temp_root):
    """更新缓存后，is_unchanged 应返回 True"""
    _write_image(temp_root, "author1/work1/01.jpg")
    current = fp.compute_fingerprint(temp_root)
    fp.update(temp_root)

    unchanged, cached = fp.is_unchanged(temp_root)
    assert unchanged
    assert cached == current


def test_is_unchanged_after_file_added(temp_root):
    """新增文件后应检测到变化"""
    _write_image(temp_root, "author1/work1/01.jpg")
    fp.update(temp_root)

    _write_image(temp_root, "author1/work1/02.jpg")
    unchanged, _ = fp.is_unchanged(temp_root)
    assert not unchanged


def test_is_unchanged_after_file_deleted(temp_root):
    """删除文件后应检测到变化"""
    path = _write_image(temp_root, "author1/work1/01.jpg")
    _write_image(temp_root, "author1/work1/02.jpg")
    fp.update(temp_root)

    os.remove(path)
    unchanged, _ = fp.is_unchanged(temp_root)
    assert not unchanged


def test_clear_removes_fingerprint(temp_root):
    """清除缓存后下次扫描应认为有变化"""
    _write_image(temp_root, "author1/work1/01.jpg")
    fp.update(temp_root)
    assert fp.is_unchanged(temp_root)[0]

    fp.clear(temp_root)
    unchanged, _ = fp.is_unchanged(temp_root)
    assert not unchanged


def test_empty_root_returns_empty_fingerprint(temp_root):
    """空目录指纹为空，且 is_unchanged 返回 False"""
    assert fp.compute_fingerprint(temp_root) == ""
    unchanged, current = fp.is_unchanged(temp_root)
    assert not unchanged
    assert current == ""


def test_non_image_files_ignored(temp_root):
    """非图片文件不参与指纹计算"""
    _write_image(temp_root, "author1/work1/01.jpg")
    fp1 = fp.compute_fingerprint(temp_root)

    # 添加一个非图片文件
    txt_path = os.path.join(temp_root, "author1", "readme.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("hello")
    fp2 = fp.compute_fingerprint(temp_root)
    assert fp1 == fp2
