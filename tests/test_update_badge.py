# -*- coding: utf-8 -*-
"""更新小红点：缓存读写、同日不重复自动查、红点显隐、手动检查仍可写缓存。"""
import json, os, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import main as app


class UpdateBadgeCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "update_cache.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_missing_and_bad(self):
        self.assertEqual(app.load_update_cache(self.path), {})
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not-json")
        self.assertEqual(app.load_update_cache(self.path), {})
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([1, 2], f)
        self.assertEqual(app.load_update_cache(self.path), {})

    def test_save_and_load_roundtrip(self):
        data = {"checked_date": "2026-10-28", "available": True,
                "latest_version": "1.5.1", "latest_build": 108}
        app.save_update_cache(data, self.path)
        self.assertTrue(os.path.isfile(self.path))
        loaded = app.load_update_cache(self.path)
        self.assertEqual(loaded["checked_date"], "2026-10-28")
        self.assertTrue(loaded["available"])
        self.assertEqual(loaded["latest_build"], 108)

    def test_same_day_skips_auto_check(self):
        cache = {"checked_date": "2026-10-28"}
        self.assertTrue(app.update_cache_checked_today(cache, "2026-10-28"))
        self.assertFalse(app.should_auto_check_update(cache, "2026-10-28"))
        self.assertTrue(app.should_auto_check_update(cache, "2026-10-29"))
        self.assertTrue(app.should_auto_check_update({}, "2026-10-28"))

    def test_badge_on_when_newer_cached(self):
        cache = {"available": True, "latest_version": "9.9.9", "latest_build": app.APP_BUILD + 10}
        self.assertTrue(app.update_badge_should_show(cache))
        # 当前 App 已不低于缓存 → 灭
        cache2 = {"available": True, "latest_version": app.APP_VERSION, "latest_build": app.APP_BUILD}
        self.assertFalse(app.update_badge_should_show(cache2))
        cache3 = {"available": True, "latest_version": "0.0.1", "latest_build": 1}
        self.assertFalse(app.update_badge_should_show(cache3))

    def test_badge_off_when_unavailable_or_bad(self):
        self.assertFalse(app.update_badge_should_show({}))
        self.assertFalse(app.update_badge_should_show({"available": False, "latest_version": "9.9.9", "latest_build": 999}))
        self.assertFalse(app.update_badge_should_show({"available": True}))  # 缺版本
        self.assertFalse(app.update_badge_should_show({"available": True, "latest_version": "x", "latest_build": 1}))

    def test_apply_new_latest_fail(self):
        today = "2026-10-28"
        info = {"version": "1.6.0", "build": app.APP_BUILD + 1, "notes": "x",
                "github_apk_url": "https://example.com/a.apk", "lanzou_url": "https://lanzou.example/b",
                "lanzou_code": "ab12"}
        c = app.apply_update_check_to_cache({}, "new", info, today=today)
        self.assertEqual(c["checked_date"], today)
        self.assertTrue(c["available"])
        self.assertEqual(c["latest_version"], "1.6.0")
        self.assertEqual(c["latest_build"], app.APP_BUILD + 1)
        self.assertTrue(app.update_badge_should_show(c))

        c2 = app.apply_update_check_to_cache(c, "latest", {"version": app.APP_VERSION, "build": app.APP_BUILD}, today=today)
        self.assertFalse(c2["available"])
        self.assertFalse(app.update_badge_should_show(c2))

        # fail 保留原 available
        prior = {"available": True, "latest_version": "9.9.9", "latest_build": app.APP_BUILD + 5}
        c3 = app.apply_update_check_to_cache(prior, "fail", None, today=today)
        self.assertEqual(c3["checked_date"], today)
        self.assertTrue(c3["available"])
        self.assertEqual(c3["latest_build"], app.APP_BUILD + 5)
        self.assertFalse(app.should_auto_check_update(c3, today))

    def test_manual_check_not_blocked_by_same_day_helper(self):
        """一日一次只约束 should_auto_check_update；手动检查直接调用 check_for_update，不受此限制。"""
        cache = {"checked_date": "2026-10-28", "available": False}
        self.assertFalse(app.should_auto_check_update(cache, "2026-10-28"))
        # 手动路径仍可写入新结果
        info = {"version": "9.9.9", "build": app.APP_BUILD + 1, "notes": "",
                "github_apk_url": "https://example.com/a.apk", "lanzou_url": "", "lanzou_code": ""}
        c = app.apply_update_check_to_cache(cache, "new", info, today="2026-10-28")
        self.assertTrue(c["available"])
        app.save_update_cache(c, self.path)
        self.assertTrue(app.update_badge_should_show(app.load_update_cache(self.path)))

    def test_get_update_cache_path_beside_db(self):
        db = os.path.join(self.tmp.name, "attendance.db")
        p = app.get_update_cache_path(db)
        self.assertEqual(os.path.dirname(p), self.tmp.name)
        self.assertTrue(p.endswith("update_cache.json"))


if __name__ == "__main__":
    unittest.main()
