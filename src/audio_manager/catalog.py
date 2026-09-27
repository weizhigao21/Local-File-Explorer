"""把扫描库中的目录树投影成用户可见的音频歌单。"""
import os
from collections import Counter

from audio_manager.dlsite import extract_rj_code


def _path_key(path):
    return (path or "").replace("\\", "/").rstrip("/").casefold()


def _relative_parts(path, root):
    """路径属于 root 时返回相对目录段，否则返回 None。"""
    normalized = path.replace("\\", "/").rstrip("/")
    root_normalized = root.replace("\\", "/").rstrip("/")
    if normalized.casefold() == root_normalized.casefold():
        return []
    prefix = root_normalized + "/"
    if normalized.casefold().startswith(prefix.casefold()):
        return normalized[len(prefix):].split("/")
    return None


def _root_name(root):
    return root.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or root


def build_audio_catalog(playlists, direct_counts, roots):
    """每个含直接音频的目录只生成一张卡片，空目录只参与路径和元数据继承。"""
    by_path = {_path_key(pl["path"]): pl for pl in playlists}
    roots = [r for r in roots if r]
    result = []
    cover_exists = {}

    for pl in playlists:
        own_count = direct_counts.get(pl["id"], 0)
        if own_count <= 0:
            continue

        # 嵌套音频根目录选最深的匹配项，避免名称把外层根目录也算进去。
        matched = [(root, parts) for root in roots
                   if (parts := _relative_parts(pl["path"], root)) is not None]
        root, parts = max(matched, key=lambda item: len(_path_key(item[0])), default=(None, None))
        if parts is None:
            display_name = pl["name"]
        elif parts:
            display_name = " > ".join(parts)
            if len(roots) > 1:
                display_name = f"{_root_name(root)} > {display_name}"
        else:
            display_name = _root_name(root)

        # 子目录常没有自己的封面/RJ 标签：沿文件夹路径向上找最近的可用值。
        cover = None
        tags = ""
        rj_code = None
        cursor = _path_key(pl["path"])
        root_key = _path_key(root) if root else ""
        while cursor:
            ancestor = by_path.get(cursor)
            if ancestor:
                candidate = ancestor.get("cover")
                if cover is None and candidate:
                    if candidate not in cover_exists:
                        cover_exists[candidate] = os.path.isfile(candidate)
                    if cover_exists[candidate]:
                        cover = candidate
                if not tags:
                    tags = ancestor.get("tags") or ""
                if not rj_code:
                    rj_code = extract_rj_code(ancestor.get("name", ""))
            if cursor == root_key:
                break
            parent = cursor.rpartition("/")[0]
            if not parent or parent == cursor:
                break
            cursor = parent

        row = dict(pl)
        row.update(name=display_name, folder_name=pl["name"], cover=cover,
                   tags=tags, rj_code=rj_code, track_count=own_count)
        result.append(row)

    # 多个根目录可能产生相同显示名；只在重名时附路径区分。
    name_counts = Counter(row["name"].casefold() for row in result)
    for row in result:
        if name_counts[row["name"].casefold()] > 1:
            row["name"] = f"{row['name']}（{row['path']}）"
    return result
