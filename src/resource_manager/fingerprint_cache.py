"""目录指纹缓存

支持两种指纹级别：
- level="file"（默认）：文件级指纹，遍历目录树并对每个匹配扩展名的文件
  stat 取 mtime+size。准确度高但 IO 量大，写真模块使用。
- level="dir"：目录级指纹，只 stat 目录（含 root 自身）取 mtime，不 stat 文件。
  快一个数量级，适用于"用户只增删文件夹/文件、不替换文件内容"的场景。

  dir level 的漏检前提：文件内容修改（同文件名覆盖）不更新父目录 mtime，
  故 dir level 会漏检"封面图/标签文件内容被替换"。音频模块假设用户日常
  仅增删文件夹与音频文件，不替换封面/标签内容，故此漏检可接受。若未来
  需求变化（如支持编辑标签），音频模块应回退到 file level 或叠加文件级校验。
"""
import hashlib
import json
import os
import time

from resource_manager import config
from resource_manager.utils import natural_key


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
    """计算文件级指纹（MD5 of mtime+size summary）

    Args:
        root: 根目录路径
        extensions: 文件扩展名集合，为 None 时使用 IMAGE_EXTENSIONS
    """
    raw = _collect_fingerprint(root, extensions=extensions)
    if not raw:
        return ""
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _collect_dir_fingerprint(root):
    """收集目录结构 + 每个目录的 mtime 生成指纹字符串（目录级指纹）

    只 stat 目录，不 stat 文件，比文件级指纹快一个数量级。
    捕获：目录增删、目录内直接子条目（文件/子目录）增删/重命名
          （这些操作会更新父目录 mtime）。
    漏检：文件内容修改（同文件名覆盖，不改父目录 mtime）——见模块顶部说明。

    返回多行文本，每行 "{relpath}\t{mtime_int}"，relpath 归一化为正斜杠，
    root 自身用 "."。行按字典序排序保证跨平台/跨文件系统顺序稳定。
    """
    if not os.path.isdir(root):
        return ""
    lines = []
    # root 自身
    try:
        root_mtime = int(os.stat(root).st_mtime)
    except OSError:
        return ""
    lines.append(f".\t{root_mtime}")
    # 栈式遍历所有子目录（避免递归深度问题）
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        # 子目录按 natural_key 排序后压栈，保证遍历顺序稳定
        subdirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
        subdirs.sort(key=lambda e: natural_key(e.name))
        for entry in subdirs:
            try:
                mtime = int(entry.stat().st_mtime)
            except OSError:
                continue
            rel = os.path.relpath(entry.path, root).replace(os.sep, "/")
            lines.append(f"{rel}\t{mtime}")
            stack.append(entry.path)
    # 按 relpath 字典序排序，确保指纹稳定（root 自身 "." 排在最前）
    lines.sort()
    return "\n".join(lines)


def compute_dir_fingerprint(root):
    """计算目录级指纹（MD5 of 目录结构 + 目录 mtime）

    比 compute_fingerprint（文件级）快得多，适用于"用户只增删不替换内容"的场景。
    """
    raw = _collect_dir_fingerprint(root)
    if not raw:
        return ""
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _load_cache():
    """加载指纹缓存

    返回 {root: {"file_fingerprint":..., "dir_fingerprint":..., "level":..., "updated_at":...}}

    兼容 v1 格式（只有 "fingerprint" 字段，无 version）：懒迁移到 "file_fingerprint"，
    顶层加 version:2。纯内存迁移，不写回磁盘（写回发生在下次 update）。
    """
    if not os.path.exists(_FINGERPRINT_PATH):
        return {}
    try:
        with open(_FINGERPRINT_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    # v1 → v2 懒迁移：顶层无 version 视为 v1
    if cache.get("version") != 2:
        for root, record in cache.items():
            if not isinstance(record, dict):
                continue  # 跳过 "version" 等非记录字段
            if "fingerprint" in record and "file_fingerprint" not in record:
                record["file_fingerprint"] = record.pop("fingerprint")
        cache["version"] = 2
    return cache


def _save_cache(cache):
    """保存指纹缓存"""
    try:
        with open(_FINGERPRINT_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"[指纹缓存] 保存失败: {e}")


def is_unchanged(root=None, extensions=None, level="file"):
    """判断目录相对上次扫描是否无变化
    返回 (unchanged: bool, current_fingerprint: str)

    Args:
        level: "file" 用文件级指纹（默认，写真模块用）；
               "dir" 用目录级指纹（音频模块用，只 stat 目录，快一个数量级）。
        extensions: 仅 level="file" 时生效，为 None 时使用 IMAGE_EXTENSIONS。
    """
    root = root or config.PHOTO_ROOT
    if level == "dir":
        current = compute_dir_fingerprint(root)
        field = "dir_fingerprint"
    else:
        current = compute_fingerprint(root, extensions=extensions)
        field = "file_fingerprint"
    if not current:
        # 空目录视为有变化（需要扫描清理数据库）
        return False, current
    cache = _load_cache()
    cached = cache.get(root, {}).get(field)
    return current == cached, current


def update(root=None, extensions=None, level="file"):
    """更新目录指纹缓存

    Args:
        level: "file" 更新文件级指纹；"dir" 更新目录级指纹。
        extensions: 仅 level="file" 时生效。
    """
    root = root or config.PHOTO_ROOT
    if level == "dir":
        current = compute_dir_fingerprint(root)
        field = "dir_fingerprint"
    else:
        current = compute_fingerprint(root, extensions=extensions)
        field = "file_fingerprint"
    cache = _load_cache()
    record = cache.get(root, {})
    if not isinstance(record, dict):
        record = {}
    record[field] = current
    record["level"] = level
    record["updated_at"] = int(time.time())
    cache[root] = record
    cache["version"] = 2
    _save_cache(cache)


def clear(root=None):
    """清除指定 root 的指纹缓存，下次扫描强制重新同步（两个 level 一起清）"""
    root = root or config.PHOTO_ROOT
    cache = _load_cache()
    if root in cache:
        del cache[root]
        _save_cache(cache)
