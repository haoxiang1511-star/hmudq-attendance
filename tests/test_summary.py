# -*- coding: utf-8 -*-
"""复制汇总：未来 / 没点名 / 部分标记 / 全员；与点名页一致（没记录 = 空课）。"""
from _base import DBCase, app
import unittest

T = "2026-10-27"


class Summary(DBCase):
    def page(self, d, p):
        recs = app.get_records(d, p)
        st = app.effective_statuses([(n, app.STUDENT_JOINED.get(n)) for n in app.STUDENTS_ORDER], recs, d)
        taken = app.slot_taken(d, p)
        unmarked = sum(1 for n in app.STUDENTS_ORDER if taken and n not in recs and st[n] != app.NOT_JOINED)
        return app.build_summary(d, p, "临床3班", [(n, st[n]) for n in app.STUDENTS_ORDER], taken, unmarked, today=T), st

    def setUp(self):
        super().setUp(); app.update_db_roster(["张三", "李四", "王五"])

    def test_future_untaken(self):
        (text, hint, kind), st = self.page("2026-10-30", 0)
        self.assertIsNone(text); self.assertEqual(kind, "future")
        self.assertEqual(hint, "10月30日 1-2节还没到，暂无点名记录，未复制")
        self.assertEqual(set(st.values()), {"空课"})

    def test_past_untaken(self):
        (text, hint, kind), _ = self.page("2026-10-20", 1)
        self.assertIsNone(text); self.assertEqual(kind, "untaken")
        self.assertIn("暂无点名记录，未复制", hint); self.assertIn("全员出勤", hint)

    def test_full_day_all_present(self):
        app.bulk_save_records("2026-10-20", 0, {n: "出勤" for n in app.STUDENTS_ORDER})
        (text, hint, kind), _ = self.page("2026-10-20", 0)
        self.assertEqual(text, "10.20 临床3班 1-2\n全"); self.assertIsNone(hint)

    def test_full_with_absent(self):
        app.bulk_save_records("2026-10-20", 0, {n: "出勤" for n in app.STUDENTS_ORDER})
        app.save_record("2026-10-20", 0, "李四", "旷课"); app.save_record("2026-10-20", 0, "王五", "病假")
        (text, hint, kind), _ = self.page("2026-10-20", 0)
        self.assertEqual(text, "10.20 临床3班 1-2\n未到：李四\n病假：王五")

    def test_partial_marks_not_full(self):
        """只标了一个人（没点全员出勤）：其余人是空课，不能写「全」，并提示有几人没标记。"""
        app.save_record("2026-10-20", 2, "张三", "事假")
        (text, hint, kind), st = self.page("2026-10-20", 2)
        self.assertEqual(text, "10.20 临床3班 5-6\n事假：张三")
        self.assertIn("有 2 人这节没有标记", hint)
        self.assertEqual(st["李四"], "空课")
        app.save_record("2026-10-20", 3, "张三", "出勤")
        (text, hint, kind), _ = self.page("2026-10-20", 3)
        self.assertEqual(text, "10.20 临床3班 7-8\n全"); self.assertIn("有 2 人", hint)

    def test_all_noclass(self):
        app.bulk_save_records("2026-10-20", 4, {n: "空课" for n in app.STUDENTS_ORDER})
        (text, hint, kind), _ = self.page("2026-10-20", 4)
        self.assertIsNone(text); self.assertEqual(kind, "allnoclass")

    def test_not_joined_display(self):
        self.c.execute("UPDATE students SET joined_date='2026-10-25' WHERE name='王五'"); self.c.commit()
        app.load_students_order()
        app.bulk_save_records("2026-10-20", 0, {"张三": "出勤", "李四": "出勤"})
        (text, hint, kind), st = self.page("2026-10-20", 0)
        self.assertEqual(st["王五"], app.NOT_JOINED)
        self.assertEqual(text, "10.20 临床3班 1-2\n全"); self.assertIsNone(hint)

    def test_all_present_writes_explicit_records(self):
        """「全员出勤」= 每人一条明确的出勤记录（不是靠"没记录=出勤"）。"""
        app.bulk_save_records("2026-10-20", 0, {n: "出勤" for n in app.STUDENTS_ORDER})
        self.assertEqual(app.get_records("2026-10-20", 0), {n: "出勤" for n in app.STUDENTS_ORDER})
        self.assertEqual(self.nrec(), 3)

    def test_export_filename_full_class_name(self):
        self.assertEqual(app.export_filename("10月24", "临床3班"), "10月24临床3班上课出勤.xlsx")
        self.assertEqual(app.export_filename("10月", "临 床/3班"), "10月临床3班上课出勤.xlsx")

    def test_available_months_skip_future(self):
        app.save_record("2026-11-02", 0, "张三", "旷课")
        self.assertEqual(app.get_available_months(today=T), [])
        app.save_record("2026-10-02", 0, "张三", "旷课")
        self.assertEqual(app.get_available_months(today=T), ["2026-10"])


if __name__ == "__main__":
    unittest.main()
