"""
音频时长读取（纯 Python，无第三方依赖）

为曲目列表提供时长显示：根据文件扩展名调用对应解析器，
从文件头 / 元数据中读取时长（秒）。任一环节出错均返回 0，
不影响播放等主流程。解析器与数据库 scanner 解耦，按需调用。
"""
import os
import struct

# MPEG1 / MPEG2(.5) 的比特率表（kbps），下标为帧头 bitrate index
_MPEG1_BITRATES = {1:32, 2:40, 3:48, 4:56, 5:64, 6:80, 7:96,
                   8:112, 9:128, 10:160, 11:192, 12:224, 13:256, 14:320}
_MPEG2_BITRATES = {1:8, 2:16, 3:24, 4:32, 5:40, 6:48, 7:56,
                   8:64, 9:80, 10:96, 11:112, 12:128, 13:144, 14:160}
# 采样率表（Hz），下标为帧头 sample_rate index
_MPEG1_RATES = {0:44100, 1:48000, 2:32000}
_MPEG2_RATES = {0:22050, 1:24000, 2:16000}
_MPEG25_RATES = {0:11025, 1:12000, 2:8000}

# ASF（WMA）File Properties Object 的 GUID
_ASF_FILE_PROPERTIES_GUID = bytes.fromhex("A1DCAB8C47A9CF118EE400C00C205365")


def get_duration(path):
    """返回音频文件时长（秒）。不支持或解析失败时返回 0。"""
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".wav":
            return _wav_duration(path)
        if ext == ".flac":
            return _flac_duration(path)
        if ext in (".m4a", ".mp4", ".aac"):
            if ext in (".m4a", ".mp4"):
                return _m4a_duration(path)
            return _adts_duration(path)
        if ext == ".ogg":
            return _ogg_duration(path)
        if ext == ".mp3":
            return _mp3_duration(path)
        if ext == ".wma":
            return _asf_duration(path)
    except Exception:
        return 0
    return 0


def _round_sec(sec):
    if sec <= 0:
        return 0
    return int(round(sec))


def _read_head(path, size):
    """读取文件头部 size 字节（不足则返回实际读到内容）"""
    with open(path, "rb") as f:
        return f.read(size)


# ── WAV (RIFF) ──
def _wav_duration(path):
    data = _read_head(path, 4096)
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return 0
    pos = 12
    byte_rate = 0
    data_size = 0
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        (csize,) = struct.unpack_from("<I", data, pos + 4)
        if cid == b"fmt " and pos + 24 <= len(data):
            # fmt 块：audio_format(2) channels(2) sample_rate(4) byte_rate(4)
            byte_rate = struct.unpack_from("<I", data, pos + 16)[0]
        elif cid == b"data":
            data_size = csize
            break
        pos += 8 + csize + (csize & 1)
    if byte_rate <= 0:
        return 0
    return _round_sec(data_size / byte_rate)


# ── FLAC ──
def _read_bits(data, start_bit, num_bits):
    val = 0
    for i in range(num_bits):
        bit = start_bit + i
        byte = data[bit >> 3]
        val = (val << 1) | ((byte >> (7 - (bit & 7))) & 1)
    return val


def _flac_duration(path):
    data = _read_head(path, 64)
    if data[:4] != b"fLaC":
        return 0
    # 第一个元数据块应是 STREAMINFO（type=0）
    if data[4] & 0x7F != 0:
        return 0
    (blen,) = struct.unpack_from(">I", data, 4 + 1)  # 3 字节长度
    payload = data[8:8 + blen]
    # STREAMINFO 位偏移：min/max block(16x2)+min/max frame(24x2) 共 80 bit，其后为 sample_rate(20)
    if len(payload) * 8 < 144:
        return 0
    sample_rate = _read_bits(payload, 80, 20)
    total_samples = _read_bits(payload, 108, 36)
    if sample_rate <= 0:
        return 0
    return _round_sec(total_samples / sample_rate)


# ── MP3 ──
def _parse_mp3_frame_header(h):
    """解析 4 字节 MPEG 帧头，返回 (version, layer, bitrate_kbps, sample_rate_hz, channel_mode, vbr)"""
    if h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
        return None
    version_id = (h[1] >> 3) & 0x3   # 0=2.5,1=reserved,2=2,3=1
    layer_id = (h[1] >> 1) & 0x3      # 1=Layer III
    if version_id == 1 or layer_id != 1:
        return None
    bitrate_idx = (h[2] >> 4) & 0xF
    rate_idx = (h[2] >> 2) & 0x3
    channel_mode = (h[3] >> 6) & 0x3
    if bitrate_idx == 0 or bitrate_idx == 15 or rate_idx == 3:
        return None
    if version_id == 3:
        bitrate = _MPEG1_BITRATES.get(bitrate_idx)
        rate = _MPEG1_RATES.get(rate_idx)
        frame_len_denom = 144
    elif version_id == 2:
        bitrate = _MPEG2_BITRATES.get(bitrate_idx)
        rate = _MPEG2_RATES.get(rate_idx)
        frame_len_denom = 144
    else:  # version 2.5
        bitrate = _MPEG2_BITRATES.get(bitrate_idx)
        rate = _MPEG25_RATES.get(rate_idx)
        frame_len_denom = 72
    return version_id, bitrate, rate, channel_mode, frame_len_denom


def _mp3_duration(path):
    size = os.path.getsize(path)
    data = _read_head(path, 1024 * 1024)
    # 跳过 ID3 标签（synchsafe 编码，每字节仅低 7 位有效）
    if data[:3] == b"ID3":
        tag_bytes = ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14
                     | (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
        start = 10 + tag_bytes
    else:
        start = 0
    # 查找第一个有效 MPEG 帧头
    hdr_off = -1
    for i in range(start, min(len(data) - 4, size)):
        parsed = _parse_mp3_frame_header(data[i:i + 4])
        if parsed:
            hdr_off = i
            break
    if hdr_off < 0:
        return 0
    version_id, bitrate, sample_rate, channel_mode, denom = _parse_mp3_frame_header(data[hdr_off:hdr_off + 4])

    # Xing / Info 标签（VBR）：位于首帧帧头之后、side info 之前
    mono = (channel_mode == 3)
    side_info = 17 if mono else 32
    if version_id != 3:
        side_info = 9 if mono else 17
    tag_pos = hdr_off + 4 + side_info
    if tag_pos + 16 <= len(data) and data[tag_pos:tag_pos + 4] in (b"Xing", b"Info"):
        num_frames = struct.unpack_from(">I", data, tag_pos + 8)[0]
        if num_frames > 0:
            # MPEG1 每帧 1152 采样，MPEG2/2.5 每帧 576 采样
            samples_per_frame = 1152 if version_id == 3 else 576
            return _round_sec(num_frames * samples_per_frame / sample_rate)

    # 无 VBR 标签时按常码率估算
    if bitrate and sample_rate:
        media_bytes = size - hdr_off
        return _round_sec(media_bytes * 8 / (bitrate * 1000))
    return 0


# ── M4A / MP4 ──
def _walk_boxes(data, offset, length, target):
    """在 [offset, offset+length) 范围内按 box 结构查找 target 类型的 box 内容"""
    end = min(offset + length, len(data))
    pos = offset
    while pos + 8 <= end:
        (b_size,) = struct.unpack_from(">I", data, pos)
        b_type = data[pos + 4:pos + 8]
        header = 8
        if b_size == 1 and pos + 16 <= end:
            (b_size,) = struct.unpack_from(">Q", data, pos + 8)
            header = 16
        elif b_size == 0:
            b_size = end - pos
        b_pos = pos + header
        if b_type == target:
            return b_pos, b_size - header
        if b_size < header:
            return None
        if b_type == b"moov":
            found = _walk_boxes(data, b_pos, b_size - header, target)
            if found:
                return found
        pos += b_size
    return None


def _m4a_duration(path):
    with open(path, "rb") as f:
        data = f.read()
    hit = _walk_boxes(data, 0, len(data), b"mvhd")
    if not hit:
        return 0
    pos, length = hit
    if pos + 4 > len(data):
        return 0
    (version,) = data[pos]
    if version == 1:
        if pos + 8 + 16 + 8 + 4 + 4 <= len(data):
            timescale = struct.unpack_from(">I", data, pos + 8 + 16 + 8)[0]
            duration = struct.unpack_from(">Q", data, pos + 8 + 16 + 12)[0]
        else:
            return 0
    else:
        if pos + 8 + 4 + 4 + 4 + 4 <= len(data):
            timescale = struct.unpack_from(">I", data, pos + 8 + 4 + 4)[0]
            duration = struct.unpack_from(">I", data, pos + 8 + 4 + 8)[0]
        else:
            return 0
    if timescale <= 0:
        return 0
    return _round_sec(duration / timescale)


# ── ADTS AAC ──
def _adts_duration(path):
    with open(path, "rb") as f:
        data = f.read()
    i = 0
    frames = 0
    sample_rate = 0
    n = len(data)
    while i + 7 <= n:
        if data[i] == 0xFF and (data[i + 1] & 0xF6) == 0xF0:
            rate_idx = (data[i + 2] >> 2) & 0xF
            sr = {0:96000, 1:88200, 2:64000, 3:48000, 4:44100, 5:32000,
                  6:24000, 7:22050, 8:16000, 9:12000, 10:11025,
                  11:8000, 12:7350}.get(rate_idx, 0)
            if not sample_rate:
                sample_rate = sr
            frame_len = ((data[i + 3] & 0x3) << 11) | (data[i + 4] << 3) \
                | ((data[i + 5] >> 5) & 0x7)
            if frame_len < 7:
                break
            frames += 1
            i += frame_len
        else:
            i += 1
    if frames == 0 or sample_rate <= 0:
        return 0
    return _round_sec(frames * 1024 / sample_rate)


# ── OGG (Vorbis) ──
def _ogg_duration(path):
    size = os.path.getsize(path)
    head = _read_head(path, 64 * 1024)
    if head[:4] != b"OggS":
        return 0
    # 首个 logical bitstream 的 Vorbis identification header 里含采样率
    sample_rate = 0
    # identification header packet: ogg 页头(27 字节) + 段表后，包开头为 "\x01vorbis"
    # Vorbis identification header：0x01 + "vorbis"(6) + version(4) + channels(1) + sample_rate(4 LE)
    nseg = head[26] if len(head) > 26 else 0
    if nseg > 0:
        seg_start = 27 + nseg
        if (head[seg_start:seg_start + 7] == b"\x01vorbis"
                and seg_start + 7 + 4 + 1 + 4 <= len(head)):
            sample_rate = struct.unpack_from("<I", head, seg_start + 13)[0]
    # 读取文件末尾，找到最后一个 Ogg 页的 granule position（未设置残留位）
    tail = _read_head_gen(path, size)
    end = len(tail)
    last_granule = 0
    i = 0
    while i + 27 <= end:
        if tail[i:i + 4] == b"OggS":
            granule = struct.unpack_from("<Q", tail, i + 6)[0]
            if granule > 0:
                last_granule = granule
            i += 27 + tail[i + 26] + 1
        else:
            i += 1
    if sample_rate > 0 and last_granule > 0:
        return _round_sec(last_granule / sample_rate)
    return 0


def _read_head_gen(path, size):
    """读取文件末尾最多 64KB（用于定位最后一页）"""
    with open(path, "rb") as f:
        if size > 64 * 1024:
            f.seek(size - 64 * 1024)
        return f.read()


# ── WMA (ASF) ──
def _asf_duration(path):
    data = _read_head(path, 1024 * 1024)
    if data[:16] != bytes.fromhex("3026B2758E66CF11A6D900AA0062CE6C"):
        return 0
    (num_objects,) = struct.unpack_from("<I", data, 24)
    pos = 30
    for _ in range(num_objects):
        if pos + 24 > len(data):
            break
        guid = data[pos:pos + 16]
        (obj_size,) = struct.unpack_from("<Q", data, pos + 16)
        if guid == _ASF_FILE_PROPERTIES_GUID:
            # 载荷结构：file_id(16) file_size(8) creation(8) packets(8) 之后为 play_duration(8)
            pay = pos + 24
            if pay + 40 + 8 <= len(data):
                play_duration = struct.unpack_from("<Q", data, pay + 40)[0]
                return _round_sec(play_duration / 10000000)
            return 0
        pos += obj_size
    return 0
