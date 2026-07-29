import hashlib
import json
import os

from resource_manager import config


# 指纹缓存文件路径（项目根目录 data/ 下）
_FINGERPRINT_PATH = os.path.join(config.get_project_root(), "data", ".scan_fingerprint.json")


def _collect_fingerprint(root, extensions=None):
    """收集目录结构 + 文件元数据生成指纹字符串
    包含：每个目录的相对路径 + 每个媒体文件的 mtime/size
    确保新增/删除/重命名目录也能触发重新扫描
    
    Args:
        root: 根目录路径
        extensions: 文件扩展名集合，为 None 时使用 IMAGE_EXTENSIONS
    """
    if extensions is None:
        from resource_manager.config import IMAGE_EXTENSIONS
        extensions = IMAGE_EXTENSIONS
    lines = []
    if not os.path.isdir(root):
        return ""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        # 纳入目录条目，确保目录结构变更（新建/删除/重命名文件夹）触发再扫描
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != ".":
            lines.append(f"[dir]\t{rel_dir}")
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in extensions:
                continue
            full_path = os.path.join(dirpath, fname)
            try:
                st = os.stat(full_path)
                rel = os.path.relpath(full_path, root)
                lines.append(f"{rel}\t{int(st.st_mtime)}\t{int(st.st_size)}")
            except OSError:
                continue
    return "\n".join(lines)


def compute_fingerprint(root, extensions=None):
    """计算目录指纹（MD5 of mtime+size summary）
    
    Args:
        root: 根目录路径
        extensions: 文件扩展名集合，为 None 时使用 IMAGE_EXTENSIONS
    """
    raw = _collect_fingerprint(root, extensions=extensions)
    if not raw:
        return ""
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _load_cache():
    """加载指纹缓存，返回 {photo_root: {"fingerprint": ..., "updated_at": ...}}"""
    if not os.path.exists(_FINGERPRINT_PATH):
        return {}
    try:
        with open(_FINGERPRINT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache):
    """保存指纹缓存"""
    try:
        with open(_FINGERPRINT_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"[指纹缓存] 保存失败: {e}")


def is_unchanged(root=None, extensions=None):
    """判断目录相对上次扫描是否无变化
    返回 (unchanged: bool, current_fingerprint: str)
    """
    root = root or config.PHOTO_ROOT
    current = compute_fingerprint(root, extensions=extensions)
    if not current:
        # 空目录视为有变化（需要扫描清理数据库）
        return False, current
    cache = _load_cache()
    cached = cache.get(root, {}).get("fingerprint")
    return current == cached, current


def update(root=None, extensions=None):
    """更新目录指纹缓存"""
    root = root or config.PHOTO_ROOT
    current = compute_fingerprint(root, extensions=extensions)
    cache = _load_cache()
    cache[root] = {
        "fingerprint": current,
        "updated_at": int(__import__("time").time()),
    }
    _save_cache(cache)


def clear(root=None):
    """清除指定 root 的指纹缓存，下次扫描强制重新同步"""
    root = root or config.PHOTO_ROOT
    cache = _load_cache()
    if root in cache:
        del cache[root]
        _save_cache(cache)
