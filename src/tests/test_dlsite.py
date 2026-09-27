"""DLsite 集成测试：RJ 码提取、parse 解析（离线 fixture）、独立数据库读写"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from audio_manager import dlsite, dlsite_db  # noqa: E402
from resource_manager import config  # noqa: E402

# ── 离线 HTML fixture：模拟 DLsite 中文详情页关键结构 ──
FIXTURE_HTML = """
<html><head><script type="application/ld+json">
{"@type": "BreadcrumbList", "itemListElement": [
  {"position": 1, "name": "同人"},
  {"position": 2, "name": "音声作品"},
  {"position": 3, "name": "○○Sound"}
]}
</script></head>
<body>
<h1 id="work_name">【中文音声】被支配中毒的隔壁姐姐</h1>
<table id="work_outline">
  <tr><th>发售日</th><td>2024/05/31</td></tr>
  <tr><th>声优</th><td>柚木つばめ</td></tr>
  <tr><th>年龄指定</th><td>R-18</td></tr>
  <tr><th>作品形式</th><td>音声作品</td></tr>
  <tr><th>文件形式</th><td>WAV / MP3</td></tr>
  <tr><th>分类</th><td><a>ASMR</a><a>耳舐め</a><a>中文</a></td></tr>
</table>
<script>
var contents = {"detail": [{"image_main": "//img.dlsite.jp/modpub/images2/work/doujin/RJ01200000/RJ01196889_img_main.jpg", "price": "1100", "brand": "RG00001"}]};
</script>
<div itemprop="description">
  <div class="work_parts">
    <div class="work_parts_heading">介绍</div>
    <div class="work_parts_multitype_item type_text">深夜两点，隔壁传来轻轻的敲门声……</div>
  </div>
</div>
</body></html>
"""


class TestExtractRjCode(unittest.TestCase):
    def test_plain_code(self):
        self.assertEqual(dlsite.extract_rj_code("RJ01196889"), "RJ01196889")

    def test_code_with_title(self):
        self.assertEqual(dlsite.extract_rj_code("RJ01196889 被支配中毒"), "RJ01196889")

    def test_lowercase(self):
        self.assertEqual(dlsite.extract_rj_code("rj283610 xxx"), "RJ283610")

    def test_old_short_code(self):
        self.assertEqual(dlsite.extract_rj_code("[RJ283610] 作品"), "RJ283610")

    def test_no_code(self):
        self.assertIsNone(dlsite.extract_rj_code("普通歌单名"))
        self.assertIsNone(dlsite.extract_rj_code(""))
        self.assertIsNone(dlsite.extract_rj_code(None))

    def test_not_rj_prefix(self):
        self.assertIsNone(dlsite.extract_rj_code("VJ01196889"))


class TestParse(unittest.TestCase):
    def setUp(self):
        self.info = dlsite.parse(FIXTURE_HTML, "RJ01196889")

    def test_title(self):
        self.assertEqual(self.info["title"], "【中文音声】被支配中毒的隔壁姐姐")

    def test_circle_from_breadcrumb(self):
        self.assertEqual(self.info["circle"], "○○Sound")

    def test_cv_and_dates(self):
        self.assertEqual(self.info["cv"], "柚木つばめ")
        self.assertEqual(self.info["release_date"], "2024/05/31")
        self.assertEqual(self.info["age_rating"], "R-18")
        self.assertEqual(self.info["work_type"], "音声作品")

    def test_genres_list(self):
        self.assertEqual(self.info["genres"], ["ASMR", "耳舐め", "中文"])

    def test_cover_from_ga(self):
        self.assertEqual(
            self.info["cover"],
            "https://img.dlsite.jp/modpub/images2/work/doujin/RJ01200000/RJ01196889_img_main.jpg",
        )

    def test_description_parts(self):
        parts = self.info["description"]
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["heading"], "介绍")
        self.assertIn("敲门声", parts[0]["text"])

    def test_empty_html(self):
        info = dlsite.parse("<html></html>", "RJ00000000")
        self.assertEqual(info["rj_code"], "RJ00000000")
        self.assertIsNone(info.get("title"))


class TestDlsiteDb(unittest.TestCase):
    def setUp(self):
        # 临时目录数据库，避免污染真实 data/dlsite.db
        self._tmp = tempfile.mkdtemp()
        self._orig_db_path = config.DLSITE_DB_PATH
        dlsite_db.config.DLSITE_DB_PATH = os.path.join(self._tmp, "dlsite.db")
        dlsite_db.init_db()

    def tearDown(self):
        dlsite_db.config.DLSITE_DB_PATH = self._orig_db_path
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_upsert_and_get(self):
        info = {
            "rj_code": "RJ01196889", "title": "测试作品", "circle": "○○Sound",
            "cv": "某人", "genres": ["ASMR", "耳舐め"],
            "description": [{"heading": "介绍", "text": "内容"}],
        }
        dlsite_db.upsert_work(info)
        got = dlsite_db.get_work("RJ01196889")
        self.assertEqual(got["title"], "测试作品")
        # 列表字段应反序列化回来
        self.assertEqual(got["genres"], ["ASMR", "耳舐め"])
        self.assertEqual(got["description"][0]["heading"], "介绍")

    def test_upsert_overwrite(self):
        dlsite_db.upsert_work({"rj_code": "RJ00000001", "title": "v1"})
        dlsite_db.upsert_work({"rj_code": "RJ00000001", "title": "v2"})
        self.assertEqual(dlsite_db.get_work("RJ00000001")["title"], "v2")

    def test_get_missing(self):
        self.assertIsNone(dlsite_db.get_work("RJ99999999"))

    def test_has_work(self):
        dlsite_db.upsert_work({"rj_code": "RJ00000002", "title": "有标题"})
        dlsite_db.set_error("RJ00000003", "抓取失败")
        self.assertTrue(dlsite_db.has_work("RJ00000002"))
        self.assertFalse(dlsite_db.has_work("RJ00000003"))  # 只有 error 不算成功
        self.assertFalse(dlsite_db.has_work("RJ99999999"))

    def test_set_error_only(self):
        dlsite_db.set_error("RJ00000004", "网络超时")
        got = dlsite_db.get_work("RJ00000004")
        self.assertEqual(got["error"], "网络超时")


if __name__ == "__main__":
    unittest.main()
