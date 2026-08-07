"""
自然排序单元测试
"""
from resource_manager.utils import natural_key


def test_pure_digit_sort():
    """纯数字编号的图片按数字顺序排序"""
    paths = ['10.jpg', '2.jpg', '1.jpg', '100.jpg', '11.jpg']
    sorted_paths = sorted(paths, key=natural_key)
    assert sorted_paths == ['1.jpg', '2.jpg', '10.jpg', '11.jpg', '100.jpg']


def test_mixed_text_and_digit():
    """混合文本和数字：数字部分按整数比较"""
    paths = ['img10.jpg', 'img2.jpg', 'img1.jpg', 'img100.jpg']
    sorted_paths = sorted(paths, key=natural_key)
    assert sorted_paths == ['img1.jpg', 'img2.jpg', 'img10.jpg', 'img100.jpg']


def test_chinese_path():
    """中文路径：数字部分按整数比较，中文按 Unicode 排序"""
    paths = ['作品10/1.jpg', '作品2/1.jpg', '作品1/1.jpg']
    sorted_paths = sorted(paths, key=natural_key)
    assert sorted_paths == ['作品1/1.jpg', '作品2/1.jpg', '作品10/1.jpg']


def test_multi_segment_digits():
    """多段数字：按顺序依次比较"""
    paths = ['img001_010.jpg', 'img001_002.jpg', 'img001_001.jpg']
    sorted_paths = sorted(paths, key=natural_key)
    assert sorted_paths == ['img001_001.jpg', 'img001_002.jpg', 'img001_010.jpg']


def test_case_insensitive():
    """大小写不敏感：英文字母部分转小写比较"""
    paths = ['IMG10.JPG', 'img2.jpg', 'Img1.jpg']
    sorted_paths = sorted(paths, key=natural_key)
    assert sorted_paths == ['Img1.jpg', 'img2.jpg', 'IMG10.JPG']


def test_no_digits():
    """没有数字时按字母顺序"""
    paths = ['banana.jpg', 'apple.jpg', 'cherry.jpg']
    sorted_paths = sorted(paths, key=natural_key)
    assert sorted_paths == ['apple.jpg', 'banana.jpg', 'cherry.jpg']


def test_empty_and_single():
    """空列表和单元素"""
    assert sorted([], key=natural_key) == []
    assert sorted(['a.jpg'], key=natural_key) == ['a.jpg']
