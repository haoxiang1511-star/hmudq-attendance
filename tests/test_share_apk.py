# -*- coding: utf-8 -*-
"""分享应用：本机 APK 优先；读不到则弹 Gitee/GitHub/蓝奏云下载对话框（不静默安装）。"""
import os
import tempfile
import unittest
from _base import app


class ShareApkHelpersTest(unittest.TestCase):
    def test_resolve_download_info_defaults(self):
        info = app.resolve_download_info_for_share()
        self.assertTrue(info["gitee_apk_url"].startswith("https://gitee.com/"))
        self.assertTrue(info["github_apk_url"].startswith("https://"))
        self.assertIn("lanzou", info["lanzou_url"])
        self.assertTrue(info["lanzou_code"])

    def test_lanzou_share_text_helper(self):
        text = app.build_lanzou_share_text()
        self.assertIn("蓝奏云", text)
        self.assertIn(app.UPDATE_CONFIG["default_lanzou_url"], text)

    def test_find_installed_apk_none_on_desktop(self):
        self.assertIsNone(app.find_installed_apk_path())

    def test_copy_apk_to_share_cache(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "base.apk")
            with open(src, "wb") as f:
                f.write(b"a" * 2048)
            dest = os.path.join(td, "cache", "out.apk")
            out = app.copy_apk_to_share_cache(src, dest=dest)
            self.assertEqual(out, dest)
            self.assertEqual(os.path.getsize(dest), 2048)

    def test_copy_apk_rejects_tiny(self):
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "tiny.apk")
            with open(src, "wb") as f:
                f.write(b"xx")
            with self.assertRaises(app.BackupError):
                app.copy_apk_to_share_cache(src, dest=os.path.join(td, "o.apk"))


class ShareApkSourceGuardTest(unittest.TestCase):
    def test_share_ui_uses_installed_then_dialog(self):
        src = open(app.__file__, encoding="utf-8").read()
        self.assertIn("find_installed_apk_path", src)
        self.assertIn("copy_apk_to_share_cache", src)
        self.assertIn("show_share_download_dialog", src)
        self.assertIn("resolve_download_info_for_share", src)
        self.assertNotIn("download_apk_for_share", src)
        # 退路对话框含 Gitee + GitHub + 蓝奏云，与更新页同款控件思路
        self.assertIn("国内直链（Gitee）", src)
        self.assertIn("GitHub 下载", src)
        self.assertIn("_lanzou_help_text_control", src)
        # 切页 / 侧栏手势
        self.assertIn("ft.PageView", src)
        self.assertIn("on_left_edge_drag_end", src)
        self.assertIn("absorb_h", src)


if __name__ == "__main__":
    unittest.main()
