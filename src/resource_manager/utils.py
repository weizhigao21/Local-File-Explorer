"""跨模块共享的工具函数"""
import os
import re


def natural_key(s: str):
    """自然排序 key：将字符串中的数字部分按整数比较
    例：'10.jpg' -> ['', 10, '.jpg']
    使 '1.jpg, 2.jpg, ..., 10.jpg' 按数字顺序而非字符串顺序排列
    """
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


def root_label(root):
    """根目录的短名：优先取末级目录名，盘符根退化成盘符

    "F:\\写真2" → "写真2"；"F:\\" → "F"。
    """
    name = os.path.basename(os.path.normpath(root)).strip()
    if name:
        return name
    drive = os.path.splitdrive(os.path.abspath(root))[0].rstrip(":\\/")
    return drive or "根目录"


def root_labels(roots):
    """给一组根目录算短标签，同名目录带上盘符区分

    两棵库分别叫 `E:\\写真` 和 `F:\\写真` 时，只写目录名会得到两个「写真」，
    后者的标签变成「写真@F」，重名作者才分得开。
    """
    counts = {}
    for root in roots:
        label = root_label(root)
        counts[label] = counts.get(label, 0) + 1
    labels = {}
    for root in roots:
        label = root_label(root)
        if counts[label] > 1:
            drive = os.path.splitdrive(os.path.abspath(root))[0].rstrip(":\\/")
            label = f"{label}@{drive}" if drive else label
        labels[root] = label
    return labels


def unique_author_name(name, root, claimed, label=None):
    """跨根目录的同名作者加根目录后缀区分

    authors.name 在库里是 UNIQUE，两棵库都有「某画师」时不能共用一条记录：
    works 表还有 UNIQUE(author_id, name)，同名作品会互相抢位置。
    规则：先到先得，后来的用「名字（根目录标签）」，仍冲突再加序号。

    claimed: {已占用的作者名: 归属根目录}，跨整轮扫描复用。
    """
    owner = claimed.get(name)
    if owner is None or owner == root:
        claimed.setdefault(name, root)
        return name
    tag = label or root_label(root)
    base = f"{name}（{tag}）"
    candidate = base
    index = 2
    while candidate in claimed and claimed[candidate] != root:
        candidate = f"{base}-{index}"
        index += 1
    claimed[candidate] = root
    return candidate
