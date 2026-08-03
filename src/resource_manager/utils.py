"""跨模块共享的工具函数"""
import re


def natural_key(s: str):
    """自然排序 key：将字符串中的数字部分按整数比较
    例：'10.jpg' -> ['', 10, '.jpg']
    使 '1.jpg, 2.jpg, ..., 10.jpg' 按数字顺序而非字符串顺序排列
    """
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]
