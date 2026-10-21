"""ICO 容器：目录结构、DIB 条目、PNG 条目、尺寸去重与越界保护"""
import struct

import pytest

from resource_manager import ico


def _dib(size=2):
    """造一份纯色的 RGBA 像素（不关心颜色，只要长度对）"""
    return bytes([0, 0, 0, 255] * (size * size))


def test_build_ico_writes_valid_directory():
    data = ico.build_ico([(16, _dib(2)), (32, _dib(2))])
    assert data[:2] == b"\x00\x00"  # reserved
    assert struct.unpack("<H", data[2:4])[0] == 1  # type = icon
    assert struct.unpack("<H", data[4:6])[0] == 2  # count
    assert ico.read_directory(data) == [(16, len(_dib(2)), 6 + 32),
                                        (32, len(_dib(2)), 6 + 32 + len(_dib(2)))]


def test_directory_entries_are_sorted_by_size():
    data = ico.build_ico([(256, b"x"), (16, b"y"), (48, b"z")])
    sizes = [entry[0] for entry in ico.read_directory(data)]
    assert sizes == [16, 48, 256], "小尺寸在前，256 用 0 表示但读回来是 256"


def test_256_uses_zero_width_byte():
    """ICO 规范里 256 要写成 0，否则 1 字节装不下"""
    data = ico.build_ico([(256, b"png-bytes")])
    assert data[6] == 0 and data[7] == 0


def test_dib_entry_header_matches_spec():
    size = 4
    _size, payload = ico.bmp_entry(size, bytes([10, 20, 30, 255] * (size * size)))
    header = struct.unpack("<IiiHHIIiiII", payload[:40])
    assert header[0] == 40  # biSize
    assert header[1] == size  # biWidth
    assert header[2] == size * 2  # biHeight：XOR + AND 两段
    assert header[3] == 1 and header[4] == 32
    assert header[5] == 0  # BI_RGB

    pixel_bytes = size * size * 4
    mask_stride = ((size + 31) // 32) * 4
    assert len(payload) == 40 + pixel_bytes + mask_stride * size


def test_dib_pixels_are_bottom_up_bgra():
    """Windows 要求自底向上、BGRA；写反了图片会上下颠倒、红蓝互换"""
    size = 2
    rgba = bytearray()
    for y in range(size):
        for _x in range(size):
            rgba += bytes([1, 2, 3, 4]) if y == 0 else bytes([5, 6, 7, 8])
    _size, payload = ico.bmp_entry(size, bytes(rgba))
    first_row = payload[40:48]  # 文件里第一行是"图像的最后一行"
    assert first_row[:4] == bytes([7, 6, 5, 8])  # B G R A


def test_rejects_wrong_pixel_length():
    with pytest.raises(ValueError, match="像素数据长度不符"):
        ico.bmp_entry(4, b"\x00" * 10)


def test_rejects_size_out_of_range():
    with pytest.raises(ValueError, match="超出 ICO 支持范围"):
        ico.bmp_entry(512, b"\x00" * (512 * 512 * 4))


def test_rejects_duplicate_sizes():
    with pytest.raises(ValueError, match="尺寸重复"):
        ico.build_ico([(16, b"a"), (16, b"b")])


def test_rejects_empty():
    with pytest.raises(ValueError, match="至少需要一个尺寸"):
        ico.build_ico([])


def test_png_entry_requires_png_signature():
    with pytest.raises(ValueError, match="PNG 数据缺少文件头"):
        ico.png_entry(256, b"not a png")


def test_write_ico_creates_parent_directory(tmp_path):
    target = tmp_path / "deep" / "nested" / "mp3.ico"
    path = ico.write_ico(str(target), [(16, _dib(2))])
    assert path == str(target)
    assert ico.read_directory(target.read_bytes())[0][0] == 16


def test_read_directory_rejects_non_ico():
    with pytest.raises(ValueError, match="文件过短"):
        ico.read_directory(b"\x00\x00")
    with pytest.raises(ValueError, match="不是 ICO"):
        ico.read_directory(struct.pack("<HHH", 0, 2, 0) + b"\x00" * 16)
