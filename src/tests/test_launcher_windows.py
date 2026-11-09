"""启动器的窗口切换守卫

三个模块窗口都是整屏大窗，同时开着会并排铺满桌面，想回启动器还得挨个关。
这里只做源码守卫（在 pytest 里反复建销 QWidget 会撞原生崩溃，见 tests/README 的约定）。
"""
import ast
import os
import re

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read():
    with open(os.path.join(SRC, "ui", "launcher.py"), encoding="utf-8") as handle:
        return handle.read()


def _function_source(name):
    source = _read()
    match = re.search(
        rf"    def {name}\(self.*?(?=\n    def )", source, re.DOTALL
    )
    assert match, f"launcher.py 里没有 {name}"
    return match.group(0)


def test_opening_a_module_hides_the_others():
    tree = ast.parse(_read())
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_open_module":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    calls.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    calls.add(sub.attr)
            break
    assert "_hide_other_modules" in calls, "打开模块时要把其余模块窗口收起来"

    body = _function_source("_hide_other_modules")
    assert "win.hide()" in body
    # 只 hide 不 close：窗口状态、播放队列都留着，再进来还是原样
    assert "close()" not in body


def test_reopening_a_hidden_module_calls_show():
    """被 hide 过的窗口单靠 raise_() 不会重新出现（点了卡片像没反应）"""
    body = _function_source("_open_module")
    assert "win.show()" in body
    assert "showNormal()" in body, "最小化的窗口要 showNormal 才能还原"
