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


def _bump_dir_mtime(path, delta=10):
    """显式 bump 目录 mtime，避免文件系统秒级精度导致测试不稳定

    增删子条目时 OS 会自动更新父目录 mtime，但同一秒内操作可能因 mtime
    精度问题未变化。测试中显式设置确保确定性。
    """
    st = os.stat(path)
    os.utime(path, (st.st_mtime + delta, st.st_mtime + delta))


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


# ==================== 目录级指纹（level="dir"） ====================

def test_compute_dir_fingerprint_stable(temp_root):
    """目录无变化时目录级指纹应稳定"""
    _write_image(temp_root, "author1/work1/01.jpg")
    _write_image(temp_root, "author2/work2/01.jpg")
    fp1 = fp.compute_dir_fingerprint(temp_root)
    fp2 = fp.compute_dir_fingerprint(temp_root)
    assert fp1 == fp2
    assert len(fp1) == 32  # MD5 十六进制长度


def test_dir_fingerprint_changes_on_subdir_add(temp_root):
    """新增子目录 → 父目录 mtime 变 → 指纹变"""
    _write_image(temp_root, "author1/work1/01.jpg")
    fp1 = fp.compute_dir_fingerprint(temp_root)

    os.makedirs(os.path.join(temp_root, "author1", "work2"))
    _bump_dir_mtime(os.path.join(temp_root, "author1"))
    fp2 = fp.compute_dir_fingerprint(temp_root)
    assert fp1 != fp2


def test_dir_fingerprint_changes_on_subdir_delete(temp_root):
    """删除子目录 → 父目录 mtime 变 → 指纹变"""
    os.makedirs(os.path.join(temp_root, "author1", "work1"))
    os.makedirs(os.path.join(temp_root, "author1", "work2"))
    fp1 = fp.compute_dir_fingerprint(temp_root)

    os.rmdir(os.path.join(temp_root, "author1", "work2"))
    _bump_dir_mtime(os.path.join(temp_root, "author1"))
    fp2 = fp.compute_dir_fingerprint(temp_root)
    assert fp1 != fp2


def test_dir_fingerprint_changes_on_file_add(temp_root):
    """目录内新增文件 → 父目录 mtime 变 → 指纹变（用户核心场景）"""
    _write_image(temp_root, "author1/work1/01.jpg")
    fp1 = fp.compute_dir_fingerprint(temp_root)

    _write_image(temp_root, "author1/work1/02.jpg")
    _bump_dir_mtime(os.path.join(temp_root, "author1", "work1"))
    fp2 = fp.compute_dir_fingerprint(temp_root)
    assert fp1 != fp2


def test_dir_fingerprint_changes_on_file_delete(temp_root):
    """目录内删除文件 → 父目录 mtime 变 → 指纹变（用户核心场景）"""
    _write_image(temp_root, "author1/work1/01.jpg")
    _write_image(temp_root, "author1/work1/02.jpg")
    fp1 = fp.compute_dir_fingerprint(temp_root)

    os.remove(os.path.join(temp_root, "author1", "work1", "02.jpg"))
    _bump_dir_mtime(os.path.join(temp_root, "author1", "work1"))
    fp2 = fp.compute_dir_fingerprint(temp_root)
    assert fp1 != fp2


def test_dir_fingerprint_ignores_file_content_change(temp_root):
    """文件内容修改（size 变）但目录 mtime 不变 → 指纹不变（预期漏检）

    dir level 的设计前提：用户只增删不替换内容。此测试明确记录该漏检为预期行为。
    """
    _write_image(temp_root, "author1/work1/01.jpg", content=b"original")
    fp1 = fp.compute_dir_fingerprint(temp_root)

    # 修改文件内容（size 变），但不动目录 mtime
    _write_image(temp_root, "author1/work1/01.jpg", content=b"changed-content")
    fp2 = fp.compute_dir_fingerprint(temp_root)
    assert fp1 == fp2  # 预期漏检


def test_dir_fingerprint_ignores_file_mtime_change(temp_root):
    """仅改文件 mtime（目录 mtime 不变）→ 指纹不变（预期漏检）"""
    path = _write_image(temp_root, "author1/work1/01.jpg")
    fp1 = fp.compute_dir_fingerprint(temp_root)

    st = os.stat(path)
    os.utime(path, (st.st_mtime + 10, st.st_mtime + 10))
    fp2 = fp.compute_dir_fingerprint(temp_root)
    assert fp1 == fp2  # 预期漏检


def test_is_unchanged_dir_level_first_time(temp_root):
    """dir level 首次无缓存，应返回有变化"""
    _write_image(temp_root, "author1/work1/01.jpg")
    unchanged, current = fp.is_unchanged(temp_root, level="dir")
    assert not unchanged
    assert len(current) == 32


def test_update_and_is_unchanged_dir_level(temp_root):
    """dir level: update 后 is_unchanged 返回 True"""
    _write_image(temp_root, "author1/work1/01.jpg")
    current = fp.compute_dir_fingerprint(temp_root)
    fp.update(temp_root, level="dir")

    unchanged, cached = fp.is_unchanged(temp_root, level="dir")
    assert unchanged
    assert cached == current


def test_is_unchanged_dir_level_after_subdir_added(temp_root):
    """dir level: 新增子目录后应检测到变化"""
    _write_image(temp_root, "author1/work1/01.jpg")
    fp.update(temp_root, level="dir")

    os.makedirs(os.path.join(temp_root, "author1", "work2"))
    _bump_dir_mtime(os.path.join(temp_root, "author1"))
    unchanged, _ = fp.is_unchanged(temp_root, level="dir")
    assert not unchanged


def test_clear_removes_dir_fingerprint(temp_root):
    """clear 后 dir level 也应认为有变化"""
    _write_image(temp_root, "author1/work1/01.jpg")
    fp.update(temp_root, level="dir")
    assert fp.is_unchanged(temp_root, level="dir")[0]

    fp.clear(temp_root)
    unchanged, _ = fp.is_unchanged(temp_root, level="dir")
    assert not unchanged


def test_dir_and_file_level_independent(temp_root):
    """dir 和 file level 指纹互不干扰：update(dir) 不影响 file level 判断"""
    _write_image(temp_root, "author1/work1/01.jpg")
    fp.update(temp_root, level="dir")
    # file level 未 update，应返回有变化
    unchanged_file, _ = fp.is_unchanged(temp_root, level="file")
    assert not unchanged_file
    # dir level 已 update，应返回无变化
    unchanged_dir, _ = fp.is_unchanged(temp_root, level="dir")
    assert unchanged_dir


def test_dir_level_missing_field_returns_changed(temp_root):
    """缓存中无 dir_fingerprint 字段时，dir level 返回有变化（触发首次扫描）"""
    _write_image(temp_root, "author1/work1/01.jpg")
    fp.update(temp_root, level="file")  # 只写 file_fingerprint
    unchanged, _ = fp.is_unchanged(temp_root, level="dir")
    assert not unchanged


def test_empty_root_dir_fingerprint_nonempty(temp_root):
    """空目录（无子目录）的目录级指纹非空（含 root 自身 mtime）"""
    fp_val = fp.compute_dir_fingerprint(temp_root)
    assert fp_val != ""
    assert len(fp_val) == 32


def test_nonexistent_root_returns_empty_dir_fingerprint(temp_root):
    """不存在的目录返回空指纹，is_unchanged 返回有变化"""
    fp_val = fp.compute_dir_fingerprint(os.path.join(temp_root, "nonexistent"))
    assert fp_val == ""
    unchanged, current = fp.is_unchanged(
        os.path.join(temp_root, "nonexistent"), level="dir"
    )
    assert not unchanged
    assert current == ""


# ==================== v1 → v2 缓存格式迁移 ====================

def test_legacy_cache_migration(temp_root):
    """v1 缓存（只有 fingerprint 字段，无 version）加载时迁移为 file_fingerprint"""
    import json
    legacy_fp = "abc123def456"
    cache = {
        temp_root: {
            "fingerprint": legacy_fp,
            "updated_at": 1000,
        }
    }
    with open(fp._FINGERPRINT_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f)

    loaded = fp._load_cache()
    record = loaded[temp_root]
    assert record["file_fingerprint"] == legacy_fp
    assert "fingerprint" not in record  # 旧字段已迁移
    assert loaded.get("version") == 2


def test_v2_cache_no_migration(temp_root):
    """已是 v2 格式时不重复迁移"""
    import json
    cache = {
        "version": 2,
        temp_root: {
            "file_fingerprint": "aaa",
            "dir_fingerprint": "bbb",
            "level": "dir",
            "updated_at": 1000,
        }
    }
    with open(fp._FINGERPRINT_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f)

    loaded = fp._load_cache()
    assert loaded[temp_root]["file_fingerprint"] == "aaa"
    assert loaded[temp_root]["dir_fingerprint"] == "bbb"
    assert loaded["version"] == 2
