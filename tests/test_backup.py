# -*- coding: utf-8 -*-
"""自动备份、手动备份、恢复（整库）、坏备份、保留数量、旧课节补记。"""
import os, sqlite3, shutil, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))
from _base import DBCase, app
import unittest


class Backup(DBCase):
    def setUp(self):
        super().setUp(); app.update_db_roster(["张三", "李四"])
        app.bulk_save_records("2026-10-20", 0, {"张三": "出勤", "李四": "旷课"})

    def test_startup_no_daily_backup(self):
        """v1.4.2：启动不再每日备份；v1.5.0 改为每周一（有考勤）备份。"""
        import ast, inspect
        tree = ast.parse(inspect.getsource(app.main))
        names = {getattr(n, "id", getattr(n, "attr", None)) for n in ast.walk(tree)}
        self.assertNotIn("daily_backup_if_needed", names)
        self.assertIn("weekly_backup_if_needed", names)
        bdir = app.get_backup_dir()
        daily = [f for f in (os.listdir(bdir) if os.path.isdir(bdir) else []) if f.startswith("attendance_daily_")]
        self.assertEqual(daily, [])
        self.assertNotIn("每天第一次打开", open(app.__file__, encoding="utf-8").read())
        self.assertEqual(app.BACKUP_RETENTION.get("weekly"), 8)
        self.assertEqual(app.BACKUP_KIND_LABELS.get("weekly"), "每周")

    def test_old_daily_backup_still_listed(self):
        """手机上已有的 attendance_daily_* 旧备份仍能列出、校验和恢复。"""
        p = app.daily_backup_if_needed()          # 模拟旧版生成的每日备份文件
        self.assertTrue(p and os.path.basename(p).startswith("attendance_daily_"))
        items = [i for i in app.list_backups() if i["path"] == p]
        self.assertEqual(len(items), 1); self.assertTrue(items[0]["ok"])
        self.assertIn("daily", app.BACKUP_KIND_LABELS)
        app.update_db_roster([])
        app.restore_backup(p)
        self.assertEqual(app.STUDENTS_ORDER, ["张三", "李四"])

    def test_manual_backup_listed(self):
        p = app.make_backup("manual", reason="手动备份", force=True)
        items = app.list_backups()
        self.assertEqual(items[0]["path"], p)
        self.assertEqual((items[0]["classes"], items[0]["students"], items[0]["records"]), (1, 2, 2))
        self.assertTrue(items[0]["ok"]); self.assertEqual(items[0]["reason"], "手动备份")
        ok, err = app.validate_backup(p); self.assertTrue(ok, err)

    def test_restore_whole_db(self):
        p = app.make_backup("manual", force=True)
        b = app.create_class("B班"); app.save_record("2026-10-21", 0, "张三", "病假")
        app.update_db_roster([])
        pre, _ = app.restore_backup(p)
        self.assertTrue(pre and os.path.exists(pre))           # 恢复前先把现状备份
        self.assertEqual([c[1] for c in app.list_classes()], ["我的班级"])
        self.assertEqual(app.STUDENTS_ORDER, ["张三", "李四"])
        self.assertEqual(app.get_records("2026-10-20", 0), {"张三": "出勤", "李四": "旷课"})
        self.assertEqual(self.nrec(), 2)
        # 再恢复回去（恢复错了还能撤回）
        app.restore_backup(pre)
        self.assertEqual(len(app.list_classes()), 2)

    def test_corrupt_backup_rejected(self):
        bad = os.path.join(app.get_backup_dir(), "attendance_manual_20261001-120000.db")
        os.makedirs(os.path.dirname(bad), exist_ok=True)
        open(bad, "wb").write(b"not a database" * 100)
        ok, err = app.validate_backup(bad); self.assertFalse(ok)
        with self.assertRaises(Exception):
            app.restore_backup(bad)
        self.assertEqual(self.nrec(), 2)                       # 现有数据没动
        self.assertFalse([x for x in app.list_backups() if x["path"] == bad][0]["ok"])

    def test_retention(self):
        for _ in range(13):
            app.make_backup("manual", force=True)
        self.assertEqual(len([x for x in app.list_backups() if x["kind"] == "manual"]), app.BACKUP_RETENTION["manual"])

    def test_op_backup_before_remove(self):
        n = len(app.list_backups())
        app.update_db_roster(["李四"])
        self.assertEqual(len(app.list_backups()), n + 1)

    def test_partial_slot_stays_empty(self):
        """只标了部分同学的课节：没标记的人一律是空课，不补记、不询问（v1.4.0 build 104 起去掉补记）。"""
        app.save_record("2026-10-21", 1, "张三", "旷课")       # 只标了一人
        self.assertEqual(len(app.partial_slots(until="2026-10-27")), 1)
        self.assertFalse(hasattr(app, "fill_partial_slots"))
        self.assertEqual(app.get_records("2026-10-21", 1), {"张三": "旷课"})
        roster = [(n, app.STUDENT_JOINED.get(n)) for n in app.STUDENTS_ORDER]
        eff = app.effective_statuses(roster, app.get_records("2026-10-21", 1), "2026-10-21")
        self.assertEqual(eff["李四"], "空课")


    def test_restore_old_structure_backup(self):
        """恢复一个 v1.3.1 老结构的备份：恢复后自动升级，记录一条不少。"""
        import make_v131_db
        old = os.path.join(self.d, "old_v131.db")
        _, info = make_v131_db.make(old)
        ok, err = app.validate_backup(old); self.assertTrue(ok, err)
        _, res = app.restore_backup(old)
        self.assertTrue(res["migrated"])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM records").fetchone()[0], info["records"])
        self.assertEqual(len(app.STUDENTS_ORDER), info["students"])

    def test_weekly_backup_rules(self):
        """无考勤不备；非周一不备；周一有考勤备一次；同周第二次不备。"""
        import datetime
        monday = datetime.date(2026, 10, 26)  # 确认为周一
        self.assertEqual(monday.weekday(), 0)
        tuesday = monday + datetime.timedelta(days=1)
        # 清空考勤
        self.c.execute("DELETE FROM records"); self.c.commit()
        self.assertIsNone(app.weekly_backup_if_needed(today=monday))
        # 有考勤但非周一
        app.bulk_save_records("2026-10-20", 0, {"张三": "出勤", "李四": "旷课"})
        self.assertIsNone(app.weekly_backup_if_needed(today=tuesday))
        # 周一有考勤 → 备
        p = app.weekly_backup_if_needed(today=monday)
        self.assertTrue(p and os.path.basename(p).startswith("attendance_weekly_"))
        items = [i for i in app.list_backups() if i["kind"] == "weekly"]
        self.assertTrue(any(i["path"] == p for i in items))
        self.assertEqual(items[0]["label"], "每周")
        # 同周再调不备
        self.assertIsNone(app.weekly_backup_if_needed(today=monday))
        # 伪造「下周一」应再备（需改文件名日期或清掉本周文件）；这里直接换更晚的周一并清 weekly
        bdir = app.get_backup_dir()
        for fn in os.listdir(bdir):
            if fn.startswith("attendance_weekly_"):
                os.remove(os.path.join(bdir, fn))
        next_mon = monday + datetime.timedelta(days=7)
        p2 = app.weekly_backup_if_needed(today=next_mon)
        # 同秒内文件名可能与已删的 p 相同，只要求再次成功写出 weekly
        self.assertTrue(p2 and os.path.basename(p2).startswith("attendance_weekly_"))
        self.assertTrue(os.path.isfile(p2))


if __name__ == "__main__":
    unittest.main()
