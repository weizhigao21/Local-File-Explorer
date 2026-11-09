"""文件操作层：路径定位、默认程序打开、回收站删除

只测纯逻辑与参数处理，**不真的往回收站丢东西**——SHFileOperationW 会把
测试文件留在用户的回收站里，跑一次留一堆垃圾比不测更烦。真正的删除动作
在交付前手工验过一次即可，这里守住的是「不该删的别删」「不许绕开回收站」。
"""
import ast
import os

import file_ops

SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _calls_in(func_name, filename):
    """收集某个函数里出现过的 `模块.函数` 调用名（AST，防子串误判）"""
    path = os.path.join(SRC_DIR, filename)
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    func = call.func
                    if isinstance(func, ast.Attribute):
                        owner = func.value
                        if isinstance(owner, ast.Name):
                            found.add(f"{owner.id}.{func.attr}")
                        elif isinstance(owner, ast.Attribute):
                            found.add(func.attr)
    return found


def test_reveal_rejects_empty_and_missing(tmp_path):
    assert file_ops.reveal("") is False
    assert file_ops.reveal(None) is False
    assert file_ops.reveal(str(tmp_path / "nope.jpg")) is False


def test_open_with_default_rejects_missing(tmp_path):
    assert file_ops.open_with_default(str(tmp_path / "nope.jpg")) is False


def test_recycle_bin_ignores_missing_paths(tmp_path):
    assert file_ops.move_to_recycle_bin([]) == (0, [])
    # 单传字符串也要能用（菜单里经常只有一个路径）
    assert file_ops.move_to_recycle_bin(str(tmp_path / "gone.jpg")) == (0, [])


def test_recycle_bin_on_non_windows_deletes_nothing(tmp_path, monkeypatch):
    target = tmp_path / "a.jpg"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(file_ops, "IS_WINDOWS", False)
    ok, failed = file_ops.move_to_recycle_bin([str(target)])
    assert ok == 0
    assert failed == [str(target)]
    assert target.exists(), "非 Windows 分支不许真的删文件"


def test_delete_never_bypasses_recycle_bin():
    """删除只允许走 SHFileOperationW + FOF_ALLOWUNDO

    这条守卫是防「优化」的：有人觉得调 Win32 太绕，换成 os.remove 一行搞定，
    行为上"能用"，但用户误点一次原图就没了。源码里连出现都不许出现。
    """
    with open(os.path.join(SRC_DIR, "file_ops.py"), encoding="utf-8") as fh:
        source = fh.read()
    assert "FOF_ALLOWUNDO" in source

    banned = {"os.remove", "os.unlink", "shutil.rmtree", "os.rmdir"}
    for name in banned:
        assert name not in source, f"file_ops 里出现了 {name}：删除必须走回收站"


def test_image_delete_syncs_disk_db_and_index():
    """图片墙的删除必须三处同步

    少任何一步都会留下不一致：不删库 → 下次扫描当成缺图重新加回来；
    不重建列表 → ROLE_INDEX 是下标，后面每张图的编号全部错位。
    """
    calls = _calls_in("_delete_image", "ui/work_images_view.py")
    assert "file_ops.move_to_recycle_bin" in calls
    assert "db.delete_images_by_paths" in calls
    assert "self._rebuild_items" in calls


def test_rebuild_reassigns_index_from_zero():
    """重建列表时下标必须从 0 连续编号（删除后下标整体前移）"""
    calls = _calls_in("_rebuild_items", "ui/work_images_view.py")
    assert "clear" in calls
    assert "addItem" in calls
