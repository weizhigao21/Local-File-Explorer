"""
日志落盘：print / stderr / 时间戳 / 幂等 / 半行处理

打包成 exe 后是 windowed 程序，`sys.stdout` 是 `None`，项目里所有
`print("[网页封面] 生成失败 ...")` 都会蒸发。这里锁住「接管后仍然能落盘」，
否则排障入口又变回「什么都看不到」。

踩过的坑：pytest 的 capture 在 fixture 之后、测试体之前还会再 start 一次，
把 `sys.stdout` 重新绑回自己的捕获流 —— 所以 **setup() 必须写在测试体里**，
放 fixture 里会被无声地覆盖，表现是「print 完全没进文件」。
"""
import io
import os
import re
import sys

import pytest

import applog


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """准备干净的日志环境；用 monkeypatch 记下 stdout/stderr 以便自动恢复"""
    applog._reset_for_tests()
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    yield tmp_path
    applog._reset_for_tests()


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_creates_log_file(log_dir):
    path = applog.setup(str(log_dir))
    assert os.path.isfile(path)
    assert os.path.basename(path) == "app.log"


def test_setup_writes_session_header(log_dir):
    path = applog.setup(str(log_dir))
    text = _read(path)
    assert "会话开始" in text
    assert str(os.getpid()) in text


def test_print_lands_in_file(log_dir):
    path = applog.setup(str(log_dir))
    print("[网页封面] 生成失败 C:/x.jpg: 模拟错误")
    sys.stdout.flush()
    assert "模拟错误" in _read(path)


def test_stderr_is_recorded_as_warning(log_dir):
    path = applog.setup(str(log_dir))
    sys.stderr.write("崩溃前的一行\n")
    sys.stderr.flush()
    text = _read(path)
    line = next(row for row in text.splitlines() if "崩溃前的一行" in row)
    assert "WARNING" in line


def test_lines_carry_timestamp_and_level(log_dir):
    path = applog.setup(str(log_dir))
    applog.get_logger().info("带时间戳")
    line = next(row for row in _read(path).splitlines() if "带时间戳" in row)
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO", line)


def test_partial_line_is_flushed(log_dir):
    """`print(..., end="")` 的半截输出也要落盘，不能卡在缓冲里丢掉"""
    path = applog.setup(str(log_dir))
    sys.stdout.write("没有换行的半行")
    sys.stdout.flush()
    assert "没有换行的半行" in _read(path)


def test_original_stream_still_receives_output(log_dir):
    """接管之后，终端/父流仍然要能看到输出（不能变成只写文件）

    这里不拿 `sys.stdout` 下手：pytest 的捕获流是 EncodedFile，读不出内容。
    直接给 _Tee 一个 StringIO 当"终端"，验证两条链路都写到了。
    """
    path = applog.setup(str(log_dir))
    sink = io.StringIO()
    tee = applog._Tee(sink)
    tee.write("两边都要有\n")
    tee.flush()
    assert "两边都要有" in sink.getvalue()
    assert "两边都要有" in _read(path)


def test_setup_is_idempotent(log_dir):
    """重复 setup 不能叠加文件 handler（否则同一行日志写两遍）"""
    from logging.handlers import RotatingFileHandler

    first = applog.setup(str(log_dir))
    again = applog.setup(str(log_dir))
    assert again == first
    # pytest 的 logging 插件也会往这个 logger 塞 handler，只数我们自己的
    ours = [h for h in applog.logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(ours) == 1


def test_log_path_matches_setup_result(log_dir):
    path = applog.setup(str(log_dir))
    assert applog.log_path() == path
