"""跨模块搜索（search_index）与启动器搜索的守卫

分两层：
- 数据层：三个库各自的 search() 与聚合结果，跑临时库（不碰用户数据）；
- 静态守卫：启动器必须走防抖、四种结果都要能跳对模块，作者列表重建
  必须静默恢复选中——这条是「点了搜索结果又被弹回列表」的根因。
"""
import ast
import os
import re
import wave

import pytest

import search_index
from audio_manager import database as audio_db
from resource_manager import config
from resource_manager import database as photo_db
from video_manager import database as video_db

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ==================== 夹具 ====================

@pytest.fixture
def env(tmp_path, monkeypatch):
    photo_root = tmp_path / "写真"
    photo_root.mkdir()
    video_root = tmp_path / "视频"
    video_root.mkdir()

    monkeypatch.setattr(config, "PHOTO_ROOT", str(photo_root))
    monkeypatch.setattr(config, "PHOTO_ROOTS", [str(photo_root)])
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "photo.db"))
    monkeypatch.setattr(config, "THUMBNAIL_DIR", str(tmp_path / "thumbs"))
    monkeypatch.setattr(config, "VIDEO_ROOT", str(video_root))
    monkeypatch.setattr(config, "VIDEO_ROOTS", [str(video_root)])
    monkeypatch.setattr(config, "VIDEO_DB_PATH", str(tmp_path / "video.db"))
    monkeypatch.setattr(config, "VIDEO_THUMB_DIR", str(tmp_path / "video_thumbs"))
    monkeypatch.setattr(audio_db, "DB_PATH", str(tmp_path / "audio.db"))

    photo_db.init_db()
    audio_db.init_db()
    video_db.init_db()

    # 写真：1 个作者 2 个作品
    works = []
    for name in ("温泉写真", "无关作品"):
        work_dir = photo_root / "作者甲" / name
        work_dir.mkdir(parents=True)
        image = work_dir / "01.jpg"
        image.write_bytes(b"\xff\xd8fake")
        works.append((name, str(work_dir), "", [(str(image), 0)]))
    photo_db.add_author_with_works("作者甲", works)

    # 音频：歌单「温泉歌单」含 1 首「温泉的曲子」，另有一个无关歌单
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    wav = audio_dir / "01.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x01" * 8000)
    hot = audio_db.add_playlist("温泉歌单", str(audio_dir))
    audio_db.add_tracks(hot, [("温泉的曲子", str(wav), 2.0, 1)])
    audio_db.update_playlist(hot, track_count=1)
    audio_db.add_playlist("别的东西", str(tmp_path / "other"))

    # 视频：1 个作品 1 集
    work_dir = video_root / "作者乙" / "温泉视频"
    work_dir.mkdir(parents=True)
    video = work_dir / "第01集.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200)
    video_db.add_author_with_works(
        "作者乙", [("温泉视频", str(work_dir), "", [(str(video), "第01集.mp4", 0, 220)])]
    )

    yield {"root": tmp_path, "playlist_id": hot}

    audio_db.end_persistent()
    photo_db.end_persistent()
    video_db.end_persistent()


# ==================== 数据层 ====================

def test_search_groups_all_three_modules(env):
    result = search_index.search("温泉")
    modules = [group["module"] for group in result["groups"]]
    assert modules == ["photo", "audio", "video"]
    assert result["query"] == "温泉"
    # 写真 1 个作品；音频 1 歌单 + 1 曲；视频 1 作品
    assert result["total"] == 4


def test_search_items_carry_kind_and_payload(env):
    result = search_index.search("温泉")
    kinds = {}
    for group in result["groups"]:
        for item in group["items"]:
            kinds.setdefault(item["kind"], []).append(item)

    assert set(kinds) == {"photo_work", "audio_playlist", "audio_track", "video_work"}
    photo_item = kinds["photo_work"][0]
    assert photo_item["payload"]["id"] and photo_item["payload"]["path"]
    assert photo_item["subtitle"] == "作者甲 · 1 张"
    assert kinds["audio_playlist"][0]["payload"]["id"] == env["playlist_id"]
    assert kinds["audio_track"][0]["subtitle"] == "歌曲 · 温泉歌单"
    assert kinds["video_work"][0]["payload"]["author"] == "作者乙"


def test_search_matches_author_name(env):
    """作者名也要能搜到（作品名的关键词不该是唯一入口）"""
    result = search_index.search("作者乙")
    video_group = next(g for g in result["groups"] if g["module"] == "video")
    assert [i["title"] for i in video_group["items"]] == ["温泉视频"]


def test_search_wildcards_are_literal(env):
    for keyword in ("%", "_", "%%"):
        assert search_index.search(keyword)["total"] == 0


def test_search_ignores_blank_keyword(env):
    for keyword in ("", "   ", None):
        assert search_index.search(keyword) == {
            "query": "",
            "total": 0,
            "groups": [],
        }


def test_search_reports_count_beyond_visible_items(env):
    """分组里的 count 是命中总数，items 只是前几条：UI 才能写「显示前 N」"""
    for index in range(9):
        audio_db.add_playlist(f"温泉合集{index}", str(env["root"] / f"p{index}"))
    result = search_index.search("温泉合集", limit=3)
    audio_group = next(g for g in result["groups"] if g["module"] == "audio")
    assert audio_group["count"] == 9
    assert len(audio_group["items"]) == 3


def test_search_survives_a_broken_module(env, monkeypatch):
    """某个库坏了只少一组，不能让整个搜索框报错"""

    def boom(*_args, **_kwargs):
        raise RuntimeError("库被锁了")

    monkeypatch.setattr(video_db, "search", boom)
    result = search_index.search("温泉")
    assert [g["module"] for g in result["groups"]] == ["photo", "audio"]


# ==================== 静态守卫 ====================

def _read(relative):
    with open(os.path.join(SRC, relative), encoding="utf-8") as handle:
        return handle.read()


def _calls_in(relative, func_name):
    """收集某个函数体里出现的方法/函数名（Name.id 与 Attribute.attr 都算）"""
    tree = ast.parse(_read(relative))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            found = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    found.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    found.add(sub.attr)
            return found
    raise AssertionError(f"{relative} 里没有 {func_name}")


def test_launcher_has_debounced_global_search():
    source = _read("ui/launcher.py")
    assert "import search_index" in source
    assert "setSingleShot(True)" in source
    # 每敲一个字就查三个库会连发十几条 SQL
    assert "setInterval(" in source
    assert "Ctrl+K" in source


def test_launcher_routes_every_result_kind():
    calls = _calls_in("ui/launcher.py", "_on_search_result")
    source = _read("ui/launcher.py")
    for kind in ("photo_work", "video_work", "audio_playlist", "audio_track", "video_file"):
        assert f'"{kind}"' in source
    assert "open_work" in calls
    assert "open_playlist" in calls


def test_launcher_search_keyboard_shortcuts():
    """Ctrl+K 聚焦、回车开第一条、Esc 清空——键盘党不用摸鼠标"""
    source = _read("ui/launcher.py")
    assert "returnPressed" in source
    assert 'QKeySequence("Escape")' in source
    panel = _read("ui/search_panel.py")
    assert "def first_result" in panel


def test_search_panel_clears_rows_immediately():
    """takeAt 不脱离父级时旧行还占着位置，新结果会和旧结果叠在一起"""
    calls = _calls_in("ui/search_panel.py", "_clear")
    assert "setParent" in calls
    assert "deleteLater" in calls


@pytest.mark.parametrize("relative", ["ui/main_window.py", "ui/video_view.py"])
def test_author_list_rebuild_is_silent(relative):
    """重建作者列表必须静默恢复选中，否则视图自动选第 0 行会把用户踢回列表"""
    calls = _calls_in(relative, "load_authors")
    assert "blockSignals" in calls
    assert "setCurrentRow" in calls, "必须显式恢复选中，否则视图会自己选第 0 行"
    source = _read(relative)
    # blockSignals 必须成对出现（True/False），只开不关会让整块列表失去交互
    block = re.search(
        r"def load_authors\(self\).*?(?=\n    def )", source, re.DOTALL
    ).group(0)
    assert "blockSignals(True)" in block
    assert "blockSignals(False)" in block
