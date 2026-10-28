# -*- coding: utf-8 -*-
"""「从文件恢复」：只接受本软件生成的完整备份；不合格的文件一律拒绝，当前数据一条不动。"""
import os, sys, shutil, sqlite3, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests")); sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))
from _base import DBCase, app


class RestoreFromFile(DBCase):
    def setUp(self):
        super().setUp()
        app.update_db_roster(["张三", "李四"])
        app.save_record("2026-10-20", 0, "张三", "出勤"); app.save_record("2026-10-20", 0, "李四", "旷课")
        self.out = os.path.join(self.d, "外部 文件#1")       # 模拟微信下载目录、路径里有空格和 #
        os.makedirs(self.out)

    def snapshot(self):
        return (sorted(self.c.execute("SELECT id, class_id, name, active FROM students").fetchall()),
                sorted(self.c.execute("SELECT student_id, date, period, status FROM records").fetchall()),
                sorted(self.c.execute("SELECT id, name FROM classes").fetchall()))

    def exported_backup(self, newname="点名备份.db.1"):
        """立即备份 → 「分享」出去的文件（扩展名被微信改掉）。"""
        p = app.make_backup("manual", reason="手动备份", force=True)
        dst = os.path.join(self.out, newname); shutil.copyfile(p, dst)
        return dst

    def assert_rejected(self, path, keyword):
        before = self.snapshot(); n_backups = len(app.list_backups())
        with self.assertRaises(app.BackupError) as cm:
            app.prepare_restore_file(path)
        self.assertIn(keyword, str(cm.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(app.list_backups()), n_backups)       # 被拒绝时不做「恢复前」备份
        self.assertFalse(os.path.exists(os.path.join(app.get_backup_dir(), app._IMPORT_STAGING)))

    def test_backup_has_app_marker(self):
        p = app.make_backup("manual", force=True)
        c = sqlite3.connect(p)
        self.assertEqual(c.execute("SELECT value FROM app_config WHERE key='backup_app'").fetchone()[0], "com.flet.app_build")
        c.close()

    def test_restore_from_renamed_file(self):
        src = self.exported_backup()
        app.create_class("二班"); app.save_record("2026-10-21", 1, "张三", "病假"); app.update_db_roster(["王五"])
        staging, info = app.prepare_restore_file(src)
        self.assertEqual((info["classes"], info["students"], info["records"]), (1, 2, 2))
        self.assertNotIn(staging, [x["path"] for x in app.list_backups()])   # 临时副本不进备份列表
        pre, _ = app.restore_backup(staging); app.discard_restore_file(staging)
        self.assertTrue(os.path.exists(pre))
        self.assertEqual([c[1] for c in app.list_classes()], ["我的班级"])
        self.assertEqual(app.STUDENTS_ORDER, ["张三", "李四"])
        self.assertEqual(app.get_records("2026-10-20", 0), {"张三": "出勤", "李四": "旷课"})
        self.assertIsNone(app.db_get_config("backup_app"))                  # 标识只在备份文件里
        self.assertFalse(os.path.exists(staging))
        self.assertTrue(os.path.exists(src))                                # 用户的文件不动

    def test_restore_old_v131_file(self):
        """v1.3.1 老结构的库也接受：恢复后自动升级，记录一条不少。"""
        import make_v131_db
        old = os.path.join(self.out, "attendance.db")
        _, info = make_v131_db.make(old)
        staging, summ = app.prepare_restore_file(old)
        self.assertEqual(summ["records"], info["records"])
        app.restore_backup(staging)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM records").fetchone()[0], info["records"])
        self.assertEqual(self.c.execute("PRAGMA user_version").fetchone()[0], app.SCHEMA_VERSION)

    def test_reject_not_sqlite(self):
        p = os.path.join(self.out, "a.db"); open(p, "wb").write("不是数据库".encode() * 300)
        self.assert_rejected(p, "不是本软件生成的备份文件")

    def test_reject_empty_and_missing(self):
        p = os.path.join(self.out, "empty.db"); open(p, "wb").close()
        self.assert_rejected(p, "不是本软件")
        self.assert_rejected(os.path.join(self.out, "nope.db"), "找不到")

    def test_reject_other_sqlite(self):
        p = os.path.join(self.out, "other.db")
        c = sqlite3.connect(p); c.execute("CREATE TABLE foo(x)"); c.execute("INSERT INTO foo VALUES(1)"); c.commit(); c.close()
        self.assert_rejected(p, "缺少名单表或考勤表")

    def test_reject_wrong_columns(self):
        p = os.path.join(self.out, "cols.db")
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE students(id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, sid INTEGER, day TEXT)")
        c.execute("INSERT INTO students VALUES(1,'a')"); c.commit(); c.close()
        self.assert_rejected(p, "表结构不对")

    def test_reject_multi_without_classes(self):
        src = self.exported_backup("x.db")
        c = sqlite3.connect(src); c.execute("PRAGMA foreign_keys=OFF"); c.execute("DROP TABLE classes"); c.commit(); c.close()
        self.assert_rejected(src, "缺少班级表")

    def test_reject_foreign_status(self):
        src = self.exported_backup("x.db")
        c = sqlite3.connect(src); c.execute("UPDATE records SET status='到了'"); c.commit(); c.close()
        self.assert_rejected(src, "不认识的状态")

    def test_reject_bad_date(self):
        src = self.exported_backup("x.db")
        c = sqlite3.connect(src); c.execute("UPDATE records SET date='10/20'"); c.commit(); c.close()
        self.assert_rejected(src, "日期或节次不对")

    def test_reject_other_app_marker(self):
        src = self.exported_backup("x.db")
        c = sqlite3.connect(src); c.execute("UPDATE app_config SET value='com.other' WHERE key='backup_app'"); c.commit(); c.close()
        self.assert_rejected(src, "来自其他软件")

    def test_reject_newer_schema(self):
        src = self.exported_backup("x.db")
        c = sqlite3.connect(src); c.execute("PRAGMA user_version=99"); c.commit(); c.close()
        self.assert_rejected(src, "更新版本")

    def test_reject_corrupt(self):
        src = self.exported_backup("x.db")
        data = bytearray(open(src, "rb").read())
        for i in range(1024 + 100, min(len(data), 4096 * 2), 7):     # 破坏页内容，保留文件头
            data[i] = 0xFF
        open(src, "wb").write(bytes(data))
        before = self.snapshot()
        with self.assertRaises(app.BackupError):
            app.prepare_restore_file(src)
        self.assertEqual(self.snapshot(), before)

    def test_reject_no_data(self):
        app.update_db_roster([])
        self.c.execute("DELETE FROM records"); self.c.commit()
        p = app.make_backup("manual", force=True)
        dst = os.path.join(self.out, "blank.db"); shutil.copyfile(p, dst)
        self.assert_rejected(dst, "没有任何名单和考勤")

    def test_failed_init_after_restore_rolls_back(self):
        """恢复进来的库升级失败 → 自动退回恢复前的数据。"""
        import make_v131_db
        old = os.path.join(self.out, "attendance.db"); make_v131_db.make(old)
        staging, _ = app.prepare_restore_file(old)
        before = self.snapshot()
        app._MIGRATION_FAULT = "before_verify"
        try:
            with self.assertRaises(app.BackupError) as cm:
                app.restore_backup(staging)
        finally:
            app._MIGRATION_FAULT = None
        self.assertIn("已退回恢复前的数据", str(cm.exception))
        self.assertEqual(self.snapshot(), before)

    # ---- 电脑上脚本生成/修改的数据库 ----
    def live_copy_unmarked(self, name="attendance.db"):
        """模拟助手在电脑上直接拿到/改过的 attendance.db：本App结构完整，但没有备份标识。"""
        dst = os.path.join(self.out, name)
        b = sqlite3.connect(dst); self.c.backup(b); b.close()
        b = sqlite3.connect(dst)
        b.execute("DELETE FROM app_config WHERE key LIKE 'backup_%'")
        b.execute("INSERT INTO students(class_id, name, active, sort_order) SELECT class_id, '脚本加的', 1, 99 FROM students LIMIT 1")
        b.commit(); b.close()
        return dst

    def test_unmarked_app_db_accepted(self):
        src = self.live_copy_unmarked()
        staging, info = app.prepare_restore_file(src)
        self.assertEqual(info["students"], 3)
        app.restore_backup(staging); app.discard_restore_file(staging)
        self.assertIn("脚本加的", app.STUDENTS_ORDER)

    def test_make_restore_file_tool(self):
        src = self.live_copy_unmarked(); dst = os.path.join(self.out, "点名恢复.db")
        before = open(src, "rb").read()
        info = app.make_restore_file(src, dst)
        self.assertEqual(info["students"], 3)
        self.assertEqual(open(src, "rb").read(), before)                   # 源文件不动
        c = sqlite3.connect(dst)
        cfg = dict(c.execute("SELECT key, value FROM app_config WHERE key LIKE 'backup_%'").fetchall())
        c.close()
        self.assertEqual(cfg["backup_app"], app.BACKUP_APP_ID)
        self.assertEqual(cfg["backup_app_version"], f"{app.APP_VERSION}+{app.APP_BUILD}")
        self.assertEqual(app.validate_backup(dst), (True, None))
        staging, _ = app.prepare_restore_file(dst)
        app.restore_backup(staging)
        self.assertIn("脚本加的", app.STUDENTS_ORDER)
        self.assertIsNone(app.db_get_config("backup_app"))

    def test_make_restore_file_from_v131(self):
        import make_v131_db
        old = os.path.join(self.out, "attendance.db"); _, meta = make_v131_db.make(old)
        dst = os.path.join(self.out, "old_restore.db")
        info = app.make_restore_file(old, dst)
        self.assertEqual(info["records"], meta["records"])
        staging, _ = app.prepare_restore_file(dst)
        app.restore_backup(staging)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM records").fetchone()[0], meta["records"])
        self.assertEqual(self.c.execute("PRAGMA user_version").fetchone()[0], app.SCHEMA_VERSION)

    def test_make_restore_file_rejects_bad(self):
        src = self.live_copy_unmarked()
        c = sqlite3.connect(src); c.execute("UPDATE records SET status='到了'"); c.commit(); c.close()
        dst = os.path.join(self.out, "bad_out.db")
        with self.assertRaises(app.BackupError) as cm:
            app.make_restore_file(src, dst)
        self.assertIn("不认识的状态", str(cm.exception))
        self.assertFalse(os.path.exists(dst)); self.assertFalse(os.path.exists(dst + ".tmp"))
        p = os.path.join(self.out, "other.db")
        c = sqlite3.connect(p); c.execute("CREATE TABLE foo(x)"); c.commit(); c.close()
        with self.assertRaises(app.BackupError):
            app.make_restore_file(p, dst)
        self.assertFalse(os.path.exists(dst))

    def test_make_restore_file_cli(self):
        import subprocess
        src = self.live_copy_unmarked(); dst = os.path.join(self.out, "cli.db")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "make_restore_file.py"), src, dst],
                           capture_output=True, text=True, cwd=self.d)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("已生成", r.stdout)
        self.assertEqual(app.validate_backup(dst), (True, None))
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "make_restore_file.py"),
                            os.path.join(self.out, "none.db"), dst + "2"], capture_output=True, text=True, cwd=self.d)
        self.assertEqual(r.returncode, 1); self.assertFalse(os.path.exists(dst + "2"))

    def test_ui_wiring(self):
        src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
        self.assertIn('ft.TextButton("从文件恢复", on_click=on_restore_from_file)', src)
        self.assertIn("file_type=ft.FilePickerFileType.ANY", src)
        self.assertIn("prepare_restore_file(f.path)", src)


    def test_stamped_flag_and_origin_label(self):
        """有本软件戳 / 仅结构像：摘要 stamped 与确认文案区分。"""
        stamped_src = self.exported_backup("stamped.db")
        staging, info = app.prepare_restore_file(stamped_src)
        self.assertTrue(info.get("stamped"))
        self.assertIn("本软件生成的备份", app.restore_origin_label(info))
        self.assertEqual(app.restore_origin_warning(info), "")
        app.discard_restore_file(staging)

        unmarked = self.live_copy_unmarked("unmarked.db")
        staging2, info2 = app.prepare_restore_file(unmarked)
        self.assertFalse(info2.get("stamped"))
        self.assertIn("不是本软件打的备份戳", app.restore_origin_label(info2))
        self.assertIn("来源可信", app.restore_origin_warning(info2))
        app.discard_restore_file(staging2)

        src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
        self.assertIn("restore_origin_label(info)", src)
        self.assertIn("不是本软件打的备份戳", src)
        # 确认框不再一律写死「是本软件生成的备份」整句拼在 f-string 里
        self.assertNotIn("校验通过，是本软件生成的备份。\n", src)


if __name__ == "__main__":
    unittest.main()
