"""
写真作品墙分页：数据库分页行为 + 「不再一次性建全量卡片」的静态守卫

分页的意义不只是省内存——旧实现把全部作品卡一次性建出来（371 张实测 336ms，
FlowLayout 触发 39 次全量重排）。库再大一点，打开模块就是几秒的白屏。
这里既锁数据库层的边界，也锁住 UI 层不许偷偷改回全量查询。
"""
import ast
import os

import pytest

from resource_manager import config
from resource_manager import database as db

PAGE = 10
AUTHOR_A = "作者甲"
AUTHOR_B = "作者乙"
WORKS_PER_AUTHOR = 12


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """两个作者、各 12 个作品、每作品 3 张图（共 24 个作品）"""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.end_persistent()
    db.init_db()

    for author in (AUTHOR_A, AUTHOR_B):
        works = []
        for i in range(1, WORKS_PER_AUTHOR + 1):
            work_dir = f"X:/{author}/作品{i:02d}"
            works.append((
                f"作品{i:02d}",
                work_dir,
                None,
                [(f"{work_dir}/{k}.jpg", 1) for k in range(3)],
            ))
        db.add_author_with_works(author, works)

    yield db
    db.end_persistent()


# ==================== 分页边界 ====================

def test_first_page_fills_up(seeded):
    total, rows = seeded.list_works_page(None, "", 1, PAGE)
    assert total == 24
    assert len(rows) == PAGE


def test_last_page_is_partial(seeded):
    total, rows = seeded.list_works_page(None, "", 3, PAGE)
    assert total == 24
    assert len(rows) == 4


def test_page_beyond_end_returns_empty(seeded):
    total, rows = seeded.list_works_page(None, "", 99, PAGE)
    assert total == 24
    assert rows == []


def test_pages_do_not_overlap_or_skip(seeded):
    seen = []
    for page in (1, 2, 3):
        seen.extend(r["id"] for r in seeded.list_works_page(None, "", page, PAGE)[1])
    assert len(seen) == 24
    assert len(set(seen)) == 24


def test_invalid_page_and_size_fall_back(seeded):
    total, rows = seeded.list_works_page(None, "", 0, 0)
    assert total == 24
    assert len(rows) == 24  # page_size 非法时用默认 60，装得下全部


# ==================== 关键词下推 ====================

def test_keyword_matches_work_name(seeded):
    total, rows = seeded.list_works_page(None, "作品03", 1, PAGE)
    assert total == 2  # 两个作者各有一个「作品03」
    assert all("作品03" in r["name"] for r in rows)


def test_keyword_matches_author_name(seeded):
    total, rows = seeded.list_works_page(None, AUTHOR_B, 1, PAGE)
    assert total == WORKS_PER_AUTHOR
    assert {r["author"] for r in rows} == {AUTHOR_B}


def test_author_and_keyword_combine(seeded):
    author_id = seeded.get_author_id(AUTHOR_A)
    total, _ = seeded.list_works_page(author_id, "作品0", 1, PAGE)
    assert total == 9  # 作品01~作品09


def test_blank_keyword_returns_everything(seeded):
    assert seeded.list_works_page(None, "   ", 1, PAGE)[0] == 24


def test_like_wildcards_are_literal(seeded):
    """用户输入 % / _ 应当按字面匹配，不能变成「匹配一切」"""
    assert seeded.list_works_page(None, "%", 1, PAGE)[0] == 0
    assert seeded.list_works_page(None, "_", 1, PAGE)[0] == 0


# ==================== 向后兼容 ====================

def test_legacy_list_works_still_returns_all(seeded):
    assert len(seeded.list_works()) == 24


def test_legacy_list_works_accepts_keyword(seeded):
    assert len(seeded.list_works(None, "作品01")) == 2


# ==================== 静态守卫 ====================

def _main_window_path():
    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(src_dir, "ui", "main_window.py")


def _module_tree():
    with open(_main_window_path(), encoding="utf-8") as f:
        return ast.parse(f.read())


def test_load_works_uses_paged_query():
    """load_works 必须走分页查询；改回 list_works 就会一次性建全量卡片

    用 AST 而不是「子串存在」——旧写法 `db.list_works` 是 `db.list_works_page`
    的前缀，子串断言根本分不出来谁在调用。
    """
    func = next(
        node for node in ast.walk(_module_tree())
        if isinstance(node, ast.FunctionDef) and node.name == "load_works"
    )
    called = {ast.unparse(call.func) for call in ast.walk(func) if isinstance(call, ast.Call)}
    assert "db.list_works_page" in called
    assert "db.list_works" not in called


def test_page_size_is_reasonable():
    """页大小要落在「一屏看得完、又不是逐个建」的区间"""
    value = None
    for node in _module_tree().body:
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", None) == "WORKS_PAGE_SIZE" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
    assert value is not None, "main_window 缺少 WORKS_PAGE_SIZE"
    assert 10 <= value <= 100
