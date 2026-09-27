"""
QSS（Qt 样式表）合法性回归测试

缺陷背景：QSS 不允许把「裸声明」与「选择器块」混写。
    color: #777; font-size: 12px;
    QLabel:hover { color: red; }
Qt 一旦在顶层遇到裸声明，就判定**整张表**非法并整表丢弃，只打印一条
`Could not parse stylesheet of object ...`，所有样式（含 hover）静默失效。
v1.1.9 里播放条的「hover 变色」与 ⏮/⏭ 的 #BBB 灰即因此从未生效。

两道守卫：
1. 运行时：用 qInstallMessageHandler 捕获 Qt 真实输出的解析告警（最贴近事实）。
2. 静态：扫描所有源码里的 QSS 字面量，找出非法结构 —— 覆盖"只在特定条件下才
   被 setStyleSheet"的样式（运行时守卫未必能触发到）。
"""
import ast
import os
import re

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 必须在导入 Qt 之前设置

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import qInstallMessageHandler  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ─────────────────────── 静态扫描 ───────────────────────

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def _qss_errors(qss: str):
    """返回 QSS 中的非法结构片段。

    规则（Qt 的实际解析要求）：
      - 顶层只允许 `注释` 与 `选择器 { 声明块 }`；
      - 选择器里不可能出现 `;`（出现即说明这是被混进来的裸声明）；
      - 不允许出现无法配对的大括号与顶层残留文本。
    """
    text = _COMMENT_RE.sub("", qss)
    errors = []
    pos = 0
    while True:
        ob = text.find("{", pos)
        if ob == -1:
            tail = text[pos:].strip()
            if tail:
                errors.append(f"顶层残留裸声明: {tail[:60]!r}")
            break
        selector = text[pos:ob].strip()
        if not selector:
            errors.append(f"缺少选择器的声明块: {text[ob:ob + 40]!r}")
        elif ";" in selector:
            errors.append(f"选择器位置出现裸声明: {selector[:60]!r}")
        depth = 0
        j = ob
        while j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= len(text):
            errors.append("大括号未配对")
            break
        pos = j + 1
    return errors


def _flatten_static(node):
    """把由 字符串 / f-string / `+` 拼接 构成的表达式压成静态骨架。

    非静态部分（变量、函数调用）用空串占位 —— 它们只是值，不影响结构合法性判断。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten_static(node.left) + _flatten_static(node.right)
    return ""


def _is_static_str_expr(node) -> bool:
    """该表达式是否含字符串字面量（用于识别 字符串 + ACCENT + 字符串 这类拼接）"""
    if isinstance(node, (ast.JoinedStr,)):
        return True
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_static_str_expr(node.left) or _is_static_str_expr(node.right)
    return False


class _StringCollector(ast.NodeVisitor):
    """收集「完整」的字符串表达式，而不是它的碎片。

    关键：f-string 与 `+` 拼接都只取静态骨架，且**不下钻**内部 —— 否则
    ast.walk 会把每个片段（如 'QPushButton {'、'\\n background: '）当成独立字符串，
    单独看都是括号不配对的，全是误报。
    """

    def __init__(self):
        self.items = []

    def visit_JoinedStr(self, node):
        self.items.append((node.lineno, _flatten_static(node)))
        # 刻意不 generic_visit：插值表达式里的字符串不是 QSS

    def visit_BinOp(self, node):
        if isinstance(node.op, ast.Add) and _is_static_str_expr(node):
            self.items.append((node.lineno, _flatten_static(node)))
            return  # 整段拼接当作一个样式表，不下钻
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            self.items.append((node.lineno, node.value))


def _iter_string_literals(src):
    """产出 (行号, 字符串)"""
    collector = _StringCollector()
    collector.visit(ast.parse(src))
    return collector.items


def _looks_like_qss(s: str) -> bool:
    """启发式：像样式表的字符串（避免把 JSON / 格式化模板当样式表）"""
    if "{" not in s or "}" not in s or ";" not in s:
        return False
    return any(k in s for k in ("color", "background", "border", "font", "padding", "QWidget", "QPushButton", "QLabel"))


def test_no_illegal_qss_in_source():
    """源码中所有 QSS 字面量都必须是合法的 `选择器 { ... }` 结构"""
    offenders = []
    for name in sorted(os.listdir(UI_DIR)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(UI_DIR, name)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for lineno, s in _iter_string_literals(src):
            if not _looks_like_qss(s):
                continue
            for err in _qss_errors(s):
                offenders.append(f"{name}:{lineno}  {err}")

    assert offenders == [], "发现非法 QSS（Qt 会整表丢弃）:\n" + "\n".join(offenders)


# ─────────────────────── 运行时验证 ───────────────────────

def _capture_qt_parse_warnings(qapp, build):
    """构建 widget 并捕获 Qt 输出的样式表解析告警"""
    captured = []
    old = qInstallMessageHandler(lambda mode, ctx, msg: captured.append(msg))
    try:
        widget = build()
        widget.resize(900, 64)
        widget.show()
        qapp.processEvents()  # 触发 polish，样式表在此阶段真正解析
        widget.close()
    finally:
        qInstallMessageHandler(old)
    return [m for m in captured if "stylesheet" in m.lower()]


def test_audio_player_bar_stylesheet_parses(qapp):
    """播放条（曾经踩坑处）不应产生任何样式表解析告警"""
    from ui.audio_player import AudioPlayerBar

    bad = _capture_qt_parse_warnings(qapp, AudioPlayerBar)
    assert bad == [], f"Qt 报告样式表解析失败: {bad}"


def test_audio_player_bar_states_still_parse(qapp):
    """切换播放/暂停与曲目名后，样式表仍可解析（覆盖状态切换时的重新设样式）"""
    from ui.audio_player import AudioPlayerBar

    def build():
        bar = AudioPlayerBar()
        bar.set_now_playing("某歌单 / 某曲目")
        bar.set_playing(True)
        bar.set_playing(False)
        bar.set_now_playing("未播放")
        return bar

    bad = _capture_qt_parse_warnings(qapp, build)
    assert bad == [], f"状态切换后样式表解析失败: {bad}"
