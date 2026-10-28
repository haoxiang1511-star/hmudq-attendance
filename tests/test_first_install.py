# -*- coding: utf-8 -*-
"""新装（空数据库）不预置名单；老数据库升级不丢学生。"""
import os, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import main as app


class FirstInstallTest(unittest.TestCase):
    def _fresh(self, d):
        app.DB_PATH = os.path.join(d, "t.db"); app._db_conn = None

    def test_empty_roster_on_first_install(self):
        with tempfile.TemporaryDirectory() as d:
            self._fresh(d)
            app.init_db(); app.load_students_order()
            self.assertEqual(app.STUDENTS_ORDER, [])
            self.assertNotEqual(app.db_get_config("class_name"), "24示例3班")
            app._db_conn.close(); app._db_conn = None

    def test_existing_data_kept_after_reinit(self):
        with tempfile.TemporaryDirectory() as d:
            self._fresh(d)
            app.init_db()
            app.update_db_roster(["张三", "李四", "王五"])
            app._db_conn.close(); app._db_conn = None
            app.init_db(); app.load_students_order()
            self.assertEqual(app.STUDENTS_ORDER, ["张三", "李四", "王五"])
            app._db_conn.close(); app._db_conn = None

    def test_export_filename_neutral_class(self):
        with tempfile.TemporaryDirectory() as d:
            self._fresh(d)
            app.init_db(); app.update_db_roster(["张三"])
            r = app.export_db_to_excel("2026-10")
            path = r[0] if isinstance(r, tuple) else r
            self.assertNotIn("三班", os.path.basename(str(path)))
            app._db_conn.close(); app._db_conn = None


if __name__ == "__main__":
    unittest.main()
