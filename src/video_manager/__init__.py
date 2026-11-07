"""视频模块核心层（纯 Python，禁止导入 Qt）

分层约定与 audio_manager / resource_manager 一致：扫描、数据库、探针、续播规则
都放在这里，`ui/` 只负责绘制与交互，`cli` 可单独复用。`tests/test_ui_theme.py::
test_core_layers_do_not_import_qt` 会扫描本目录兜住分层。
"""
