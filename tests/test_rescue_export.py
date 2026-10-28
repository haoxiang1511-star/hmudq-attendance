# -*- coding: utf-8 -*-
"""升级失败页「导出数据」：导出当前库的只读副本 + 升级前备份；原库不动；导出的文件能被「从文件恢复」认出来。"""
import os, sys, hashlib, inspect, shutil, sqlite3, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests")); sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))
import main as app
import make_v131_db
from test_migration import raw_snapshot


def md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def data_of(path):
    """名单 + 考勤（不含 app_config 里的备份标识）。"""
    c = sqlite3.connect(path)
    try:
        studs = sorted(c.execute("SELECT * FROM students"))
        recs = sorted(c.execute("SELECT * FROM records"))
    finally:
        c.close()
    return studs, recs


class RescueExport(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        if app._db_conn:
            app.close_db_connection()
        app._db_conn = None
        app.DB_PATH = os.path.join(self.d, "attendance.db")
        app._MIGRATION_FAULT = None
        make_v131_db.make(app.DB_PATH)
        self.before = raw_snapshot(app.DB_PATH)

    def tearDown(self):
        app._MIGRATION_FAULT = None
        if app._db_conn:
            app.close_db_connection()
        app._db_conn = None
        shutil.rmtree(self.d, ignore_errors=True)

    def fail_upgrade(self):
        app._MIGRATION_FAULT = "before_verify"
        with self.assertRaises(app.MigrationError) as cm:
            app.init_db()
        app._MIGRATION_FAULT = None
        return cm.exception

    def test_export_after_failed_upgrade_with_backup(self):
        ex = self.fail_upgrade()
        self.assertTrue(ex.backup_path and os.path.isfile(ex.backup_path))
        h0 = md5(app.DB_PATH)
        out_dir, files = app.export_rescue_files(ex.backup_path)
        self.assertEqual(out_dir, os.path.join(self.d, "rescue_export"))
        self.assertEqual(len(files), 2)
        cur, pre = files
        self.assertIn("当前数据", os.path.basename(cur)); self.assertIn("升级前备份", os.path.basename(pre))
        self.assertTrue(all(os.path.dirname(f) == out_dir for f in files))
        # 原库一点没动
        self.assertEqual(md5(app.DB_PATH), h0)
        self.assertEqual(raw_snapshot(app.DB_PATH), self.before)
        # 导出副本内容和原库一致（名单、考勤、结构版本）
        self.assertEqual(data_of(cur), data_of(app.DB_PATH))
        self.assertEqual(raw_snapshot(cur)[:3], self.before[:3])
        self.assertEqual(data_of(pre), data_of(app.DB_PATH))
        self.assertFalse(os.path.exists(cur + ".tmp"))
        # 两份都能通过「从文件恢复」的校验，并带本软件标识
        for f in files:
            ok, why = app.validate_backup(f)
            self.assertTrue(ok, why)
            c = sqlite3.connect(f)
            self.assertEqual(c.execute("SELECT value FROM app_config WHERE key='backup_app'").fetchone()[0],
                             app.BACKUP_APP_ID)
            c.close()

    def test_export_without_backup(self):
        self.fail_upgrade()
        out_dir, files = app.export_rescue_files(None)
        self.assertEqual(len(files), 1)
        self.assertIn("当前数据", os.path.basename(files[0]))
        out_dir2, files2 = app.export_rescue_files(os.path.join(self.d, "不存在的备份.db"))
        self.assertEqual(len(files2), 1)
        self.assertEqual(raw_snapshot(app.DB_PATH), self.before)

    def test_export_while_connection_open_and_closed(self):
        self.fail_upgrade()
        app.get_db_connection()                     # 失败页上连接可能还开着
        _, f1 = app.export_rescue_files(None)
        app.close_db_connection(); app._db_conn = None
        _, f2 = app.export_rescue_files(None)
        self.assertEqual(data_of(f1[0]), data_of(f2[0]))
        self.assertEqual(raw_snapshot(app.DB_PATH), self.before)

    def test_no_db_file(self):
        os.remove(app.DB_PATH)
        _, files = app.export_rescue_files(None)
        self.assertEqual(files, [])
        self.assertFalse(os.path.exists(app.DB_PATH))   # 不会新建空库

    def test_exported_copy_restores_on_fixed_version(self):
        """修好后的新版：用「从文件恢复」选导出的当前数据副本，能认出来、恢复后自动升级，记录一条不少。"""
        ex = self.fail_upgrade()
        _, files = app.export_rescue_files(ex.backup_path)
        keep = os.path.join(tempfile.mkdtemp(dir=self.d), "微信收到的文件.db.1")
        shutil.copyfile(files[0], keep)
        # 换一台新手机：空库
        app.close_db_connection(); app._db_conn = None
        app.DB_PATH = os.path.join(tempfile.mkdtemp(dir=self.d), "attendance.db")
        app.init_db(); app.load_students_order()
        staging, info = app.prepare_restore_file(keep)
        self.assertEqual(info["records"], len(self.before[1]))
        app.restore_backup(staging); app.discard_restore_file(staging)
        app.close_db_connection(); app._db_conn = None
        self.assertEqual(raw_snapshot(app.DB_PATH)[1], self.before[1])
        self.assertIsNone(app.db_get_config("backup_app"))

    def test_fail_page_registers_share_and_export(self):
        src = inspect.getsource(app.show_startup_error)
        self.assertIn("ft.Share()", src)
        self.assertIn("page.services.append(share_service)", src)
        self.assertIn("share_files", src)
        self.assertIn("export_rescue_files(backup)", src)
        self.assertIn("导出数据（名单+考勤）", src)
        self.assertIn("不要卸载 App，也不要安装旧版本", src)

    def test_rescue_dir_excluded_from_package(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import check_apk
        self.assertIn("rescue_export", check_apk.load_excludes())
        self.assertIn("rescue_export", check_apk.FORBIDDEN_DIRS)


    def test_fail_page_check_update_and_email(self):
        """失败页：检查更新按钮、三步引导、开发者邮箱一键复制；检查更新与关于页同源。"""
        src = inspect.getsource(app.show_startup_error)
        self.assertIn("检查更新", src)
        self.assertIn("on_startup_check_update", src)
        self.assertIn("asyncio.to_thread(check_for_update)", src)
        self.assertIn("show_update_dialog", src)
        self.assertIn("show_lanzou_fallback_dialog", src)
        self.assertIn("UPDATE_CHECKING_TEXT", src)
        self.assertEqual(app.UPDATE_CHECKING_TEXT, "正在检查更新…")
        for kw in ("导出数据", "检查更新", "开发者", "微信", "覆盖安装"):
            self.assertIn(kw, src, kw)
        self.assertIn("不要卸载 App，也不要安装旧版本", src)
        self.assertEqual(app.DEVELOPER_EMAIL, "haoxiang1511@gmail.com")
        self.assertIn("DEVELOPER_EMAIL", src)
        self.assertIn('notify("已复制")', src)
        self.assertIn("await ft.Clipboard().set(DEVELOPER_EMAIL)", src)
        about = open(app.__file__, encoding="utf-8").read()
        self.assertIn("def show_update_dialog(", about)
        self.assertIn("show_update_dialog(page, info, open_link, show_snackbar)", about)


if __name__ == "__main__":
    unittest.main()
