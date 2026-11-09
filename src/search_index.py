"""跨模块搜索（纯 Python，禁止 Qt）

启动器只有一个搜索框，用户想找「那个温泉的视频」时不该先猜它在哪个模块。
这里把三个库的搜索聚成一份**已排序、可直接渲染**的结果：

    {"query": "...", "total": 7, "groups": [{module, title, items: [...]}, ...]}

每条 item 都带 `kind`（work/playlist/track/video），UI 按 kind 决定点下去
是「打开作品」「进歌单」还是「定位到某一集」——搜索层不关心窗口怎么开，
只负责把 id 和展示文本给全。
"""
from __future__ import annotations

from audio_manager import database as audio_db
from resource_manager import database as photo_db
from video_manager import database as video_db

# 每个模块最多给几条：启动器是一屏的入口，不是结果页
PER_MODULE_LIMIT = 6

MODULE_TITLES = {
    "photo": "写真",
    "audio": "音频",
    "video": "视频",
}


def _photo_items(keyword, limit):
    data = photo_db.search_works(keyword, limit)
    items = [
        {
            "kind": "photo_work",
            "id": w["id"],
            "title": w["name"],
            "subtitle": f"{w['author']} · {int(w.get('image_count') or 0)} 张",
            "payload": w,
        }
        for w in data["items"]
    ]
    return items, data["total"]


def _audio_items(keyword, limit):
    data = audio_db.search(keyword, limit)
    items = [
        {
            "kind": "audio_playlist",
            "id": p["id"],
            "title": p["name"],
            "subtitle": f"歌单 · {int(p.get('track_count') or 0)} 首",
            "payload": p,
        }
        for p in data["playlists"]
    ]
    items.extend(
        {
            "kind": "audio_track",
            "id": t["id"],
            "title": t["title"] or "(未命名)",
            "subtitle": f"歌曲 · {t['playlist']}",
            "payload": t,
        }
        for t in data["tracks"]
    )
    return items, data["playlist_total"] + data["track_total"]


def _video_items(keyword, limit):
    data = video_db.search(keyword, limit)
    items = [
        {
            "kind": "video_work",
            "id": w["id"],
            "title": w["name"],
            "subtitle": f"{w['author']}",
            "payload": w,
        }
        for w in data["works"]
    ]
    items.extend(
        {
            "kind": "video_file",
            "id": v["id"],
            "title": v["name"],
            "subtitle": f"视频 · {v['author']} / {v['work_name']}",
            "payload": v,
        }
        for v in data["videos"]
    )
    return items, data["work_total"] + data["video_total"]


_BUILDERS = {
    "photo": _photo_items,
    "audio": _audio_items,
    "video": _video_items,
}


def search(keyword, limit=PER_MODULE_LIMIT):
    """按模块分组返回搜索结果；空关键词返回空结果（不查库）"""
    keyword = (keyword or "").strip()
    if not keyword:
        return {"query": "", "total": 0, "groups": []}

    groups = []
    total = 0
    for module in ("photo", "audio", "video"):
        try:
            items, count = _BUILDERS[module](keyword, limit)
        except Exception as e:  # noqa: BLE001 - 某个库坏了不能让整个搜索框报错
            print(f"[全局搜索] {module} 查询失败: {e}")
            items, count = [], 0
        if not items:
            continue
        groups.append(
            {
                "module": module,
                "title": MODULE_TITLES[module],
                "count": int(count),
                "items": items,
            }
        )
        total += int(count)
    return {"query": keyword, "total": total, "groups": groups}
