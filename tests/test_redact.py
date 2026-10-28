# -*- coding: utf-8 -*-
"""失败页 / 日志脱敏纯函数。"""
import os, unittest
from _base import app


class Redact(unittest.TestCase):
    def test_path_to_basename(self):
        s = app.redact_sensitive_text("数据文件：/data/user/0/com.flet.app_build/files/attendance.db")
        self.assertNotIn("/data/user", s)
        self.assertIn("attendance.db", s)

    def test_name_redaction(self):
        s = app.redact_sensitive_text("已删除『张三』", names=["张三", "李四"])
        self.assertNotIn("张三", s)
        self.assertIn("同学", s)

    def test_friendly_data_path(self):
        self.assertIn("应用数据目录内的备份/数据文件", app.friendly_data_path("/a/b/c.db"))
        self.assertIn("c.db", app.friendly_data_path("/a/b/c.db"))
        self.assertIn("无", app.friendly_data_path(None))

    def test_startup_error_ui_no_raw_db_path(self):
        import inspect
        src = inspect.getsource(app.show_startup_error)
        self.assertIn("friendly_data_path", src)
        self.assertIn("redact_sensitive_text", src)
        self.assertIn("_safe_logs", src)
        # 界面拼接不再直接 f"数据文件：{DB_PATH}"
        self.assertNotIn('f"数据文件：{DB_PATH}"', src)


if __name__ == "__main__":
    unittest.main()
