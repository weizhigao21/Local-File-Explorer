"""
筛选选择器（标签/CV/社团三页签）可勾选列表的点击回归测试（离屏）

覆盖缺陷场景：
    在「添加筛选条件」对话框的标签页，用搜索框过滤标签后点击列表项，
    触发 _CheckListWidget.mouseReleaseEvent → _on_check_indicator →
    QListView.initStyleOption。PyQt6 未暴露该方法（唯一可见重载是祖先类
    QFrame 的，参数类型不匹配），每次调用必抛 TypeError；该异常从 C++ 事件
    循环直接调用的 mouseReleaseEvent 里逃出会直接终止进程——表现为
    「点击标签即闪退」（无 traceback、无错误框、进程静默退出）。

正确行为：点击列表行任意位置（含指示器方块）恰好翻转一次勾选状态，
对话框正常返回所选标签。
"""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 必须在导入 Qt 之前设置

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QStyle,
    QStyleOptionViewItem,
)

import ui.tag_widgets as tag_widgets  # noqa: E402
from ui.tag_widgets import FilterSelectorDialog  # noqa: E402

ALL_TAGS = ["催眠", "asmr", "耳舐め", "癒やし"]


@pytest.fixture(scope="session")
def qapp():
    """离屏 QApplication（整个测试会话共用一个实例）"""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def dialog(qapp):
    dlg = FilterSelectorDialog(
        all_tags=ALL_TAGS,
        circle_counts={},
        cv_counts={},
        parent=None,
    )
    dlg.resize(380, 480)
    dlg.show()
    qapp.processEvents()
    yield dlg
    dlg.close()
    qapp.processEvents()


def _indicator_center(list_widget, item):
    """勾选指示器方块中心（手工构造 option 经 style 计算，与实际渲染一致）"""
    opt = QStyleOptionViewItem()
    opt.initFrom(list_widget)
    opt.rect = list_widget.visualItemRect(item)
    opt.features |= QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
    opt.checkState = item.checkState()
    rect = list_widget.style().subElementRect(
        QStyle.SubElement.SE_ItemViewItemCheckIndicator, opt, list_widget
    )
    return rect.center()


def _visible_items(list_widget):
    return [list_widget.item(i) for i in range(list_widget.count())
            if not list_widget.item(i).isHidden()]


def _click_row_center(list_widget, item):
    """点击整行中心（行中部，位于指示器右侧）"""
    QTest.mouseClick(
        list_widget.viewport(), Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier, list_widget.visualItemRect(item).center(),
    )


def _click_indicator(list_widget, item):
    """点击指示器方块中心"""
    QTest.mouseClick(
        list_widget.viewport(), Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier, _indicator_center(list_widget, item),
    )


def test_click_row_center_toggles_exactly_once(dialog, qapp):
    """点击行中部：恰好翻转一次勾选状态（不得崩溃，也不得双翻抵消）"""
    tab = dialog.tag_tab
    item = tab.list_widget.item(0)
    assert item.checkState() == Qt.CheckState.Unchecked

    _click_row_center(tab.list_widget, item)
    assert item.checkState() == Qt.CheckState.Checked

    _click_row_center(tab.list_widget, item)
    assert item.checkState() == Qt.CheckState.Unchecked


def test_click_indicator_toggles_exactly_once(dialog, qapp):
    """点击指示器方块：恰好翻转一次（Qt 自行处理或由我们补翻，只能翻一次）"""
    tab = dialog.tag_tab
    item = tab.list_widget.item(0)

    _click_indicator(tab.list_widget, item)
    assert item.checkState() == Qt.CheckState.Checked

    _click_indicator(tab.list_widget, item)
    assert item.checkState() == Qt.CheckState.Unchecked


def test_search_then_click_tag_no_crash(dialog, qapp):
    """用户报告场景：标签页搜索框输入 → 防抖过滤 → 点击可见标签

    旧实现在这条路径上必崩（initStyleOption TypeError 从 mouseReleaseEvent
    逃出 → 进程静默终止）。修复后：不闪退，勾选恰好翻一次，
    且确定后能取到所选标签。
    """
    tab = dialog.tag_tab
    tab.search_box.setText("asmr")  # textChanged 会启动 150ms 防抖过滤

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and len(_visible_items(tab.list_widget)) != 1:
        qapp.processEvents()
        time.sleep(0.02)

    visible = _visible_items(tab.list_widget)
    assert [it.text() for it in visible] == ["asmr"]

    _click_row_center(tab.list_widget, visible[0])
    assert visible[0].checkState() == Qt.CheckState.Checked

    dialog.accept()
    tags, circles, cvs = dialog.get_selected()
    assert tags == ["asmr"]
    assert circles == [] and cvs == []


def test_no_unavailable_listview_init_style_option():
    """静态守卫：PyQt6 未暴露 QListView.initStyleOption，本文件不得再调用它

    一旦回归到「计算指示器矩形」的旧实现，行为测试会以进程级崩溃的形式
    失败；本守卫则在源码层面直接挡住该 API 的重新引入。
    只扫描真实调用（ast.Call），docstring 中的历史说明不受影响。
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(tag_widgets))
    attr_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "initStyleOption" not in attr_calls
