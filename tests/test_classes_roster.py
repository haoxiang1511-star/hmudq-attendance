# -*- coding: utf-8 -*-
"""多班级、改名保记录、重名规则、软删除/恢复、移班、串课。"""
from _base import DBCase, app
import unittest


class Classes(DBCase):
    def test_create_switch_isolated(self):
        a = app.get_current_class_id(); app.rename_class(a, "临床3班")
        app.update_db_roster(["张三", "李四"]); app.save_record("2026-10-20", 0, "张三", "旷课")
        b = app.create_class("临床4班"); app.set_current_class(b); app.load_students_order()
        self.assertEqual(app.STUDENTS_ORDER, [])
        app.update_db_roster(["张三", "王五"])          # 不同班可以同名
        self.assertEqual(app.get_records("2026-10-20", 0), {})
        self.assertFalse(app.slot_taken("2026-10-20", 0))
        app.set_current_class(a); app.load_students_order()
        self.assertEqual(app.get_records("2026-10-20", 0), {"张三": "旷课"})
        self.assertEqual([c[1] for c in app.list_classes()], ["临床3班", "临床4班"])

    def test_class_name_rules(self):
        app.rename_class(app.get_current_class_id(), "临床3班")
        for bad, msg in [("", "不能为空"), ("   ", "不能为空"), ("临 床3班", "已有同名"), ("一" * 21, "最长 20")]:
            with self.assertRaises(ValueError) as cm:
                app.create_class(bad)
            self.assertIn(msg, str(cm.exception))
        self.assertEqual(app.rename_class(app.get_current_class_id(), "临床3班"), "临床3班")   # 改成自己不算重名

    def test_delete_class_keeps_one_and_backs_up(self):
        a = app.get_current_class_id()
        with self.assertRaises(ValueError):
            app.delete_class(a)
        app.update_db_roster(["张三"]); app.save_record("2026-10-20", 0, "张三", "旷课")
        b = app.create_class("新班")
        backup = app.delete_class(a)
        self.assertTrue(backup)
        self.assertEqual(app.get_current_class_id(), b)
        self.assertEqual(self.nrec(), 0)


class Roster(DBCase):
    def test_rename_keeps_records(self):
        app.update_db_roster(["张三", "李四"])
        app.save_record("2026-10-20", 0, "张三", "病假"); app.save_record("2026-10-21", 1, "张三", "旷课")
        e = self.entries(); e[0]["name"] = "张叁"
        r = app.apply_roster_changes(app.get_current_class_id(), e)
        self.assertEqual(r["renamed"][0][1:], ("张三", "张叁")); self.assertTrue(r["backup"])
        self.assertEqual(app.get_records("2026-10-20", 0), {"张叁": "病假"})
        self.assertEqual(app.get_records("2026-10-21", 1), {"张叁": "旷课"})
        self.assertEqual(self.nrec(), 2)

    def test_swap_names(self):
        app.update_db_roster(["张三", "李四"]); app.save_record("2026-10-20", 0, "张三", "病假")
        e = self.entries(); e[0]["name"], e[1]["name"] = "李四", "张三"
        app.apply_roster_changes(app.get_current_class_id(), e)
        self.assertEqual(app.get_records("2026-10-20", 0), {"李四": "病假"})

    def test_duplicate_in_class_rejected_suffix_ok(self):
        app.update_db_roster(["张伟"])
        e = self.entries() + [{"id": None, "name": "张伟"}]
        with self.assertRaises(ValueError) as cm:
            app.plan_roster_changes(app.get_current_class_id(), e)
        self.assertIn("张伟(2)", str(cm.exception))
        self.assertEqual(app.disambiguate("张伟", ["张伟"]), "张伟(2)")
        self.assertEqual(app.disambiguate("张伟", ["张伟", "张伟(2)"]), "张伟(3)")
        e[1]["name"] = app.disambiguate("张伟", ["张伟"])
        app.apply_roster_changes(app.get_current_class_id(), e)
        self.assertEqual(app.STUDENTS_ORDER, ["张伟", "张伟(2)"])

    def test_rename_into_removed_name_blocked(self):
        app.update_db_roster(["张三", "李四"])
        app.update_db_roster(["李四"])                         # 张三 移出（软删除）
        e = self.entries(); e[0]["name"] = "张三"
        with self.assertRaises(ValueError) as cm:
            app.plan_roster_changes(app.get_current_class_id(), e)
        self.assertIn("已移出名单", str(cm.exception))

    def test_soft_delete_and_readd_restores(self):
        app.update_db_roster(["张三", "李四"]); app.save_record("2026-10-20", 0, "张三", "旷课")
        app.update_db_roster(["李四"])
        self.assertEqual(self.nrec(), 1); self.assertEqual(app.STUDENTS_ORDER, ["李四"])
        app.update_db_roster(["李四", "张三"])
        self.assertEqual(app.get_records("2026-10-20", 0), {"张三": "旷课"})

    def test_new_student_joined_date(self):
        app.update_db_roster(["张三"])
        self.assertIsNone(app.get_class_students()[0]["joined_date"])   # 新班第一次导入：一直在班
        app.save_record("2026-10-01", 0, "张三", "出勤")
        app.update_db_roster(["张三", "插班生"])
        self.assertEqual(app.get_class_students()[1]["joined_date"], app._today())

    def test_dirty_detection(self):
        app.update_db_roster(["张三", "李四"])
        saved = [(s["id"], s["name"]) for s in app.get_class_students()]
        e = self.entries()
        self.assertFalse(app.roster_is_dirty(e, saved))
        e[0]["name"] = "张叁"; self.assertTrue(app.roster_is_dirty(e, saved))
        e = self.entries()[::-1]; self.assertTrue(app.roster_is_dirty(e, saved))

    def test_move_between_classes_with_records(self):
        a = app.get_current_class_id(); app.update_db_roster(["张三", "李四"])
        app.save_record("2026-10-20", 0, "张三", "旷课")
        b = app.create_class("B班")
        sid = app.get_class_students(a)[0]["id"]
        app.move_students([sid], b, from_cid=a)
        self.assertEqual([s["name"] for s in app.get_class_students(b)], ["张三"])
        self.assertEqual(app.get_records("2026-10-20", 0, b), {"张三": "旷课"})
        # 目标班有同名 → 拦下
        app.set_current_class(b); app.update_db_roster(["张三", "李四"])
        lisi_a = app.get_class_students(a)[0]["id"]
        with self.assertRaises(ValueError):
            app.move_students([lisi_a], b, from_cid=a)

    def test_cross_class_lookup_and_move_via_save(self):
        a = app.get_current_class_id(); app.update_db_roster(["张三"]); app.save_record("2026-10-20", 0, "张三", "病假")
        b = app.create_class("B班"); app.set_current_class(b)
        found = app.find_name_in_other_classes("张三")
        self.assertEqual(found[0][2], app.get_class_name(a)); self.assertEqual(found[0][3], 1)
        r = app.apply_roster_changes(b, [{"id": found[0][0], "name": "张三"}])
        self.assertEqual(r["moved_from"], [a])
        self.assertEqual(app.get_records("2026-10-20", 0, b), {"张三": "病假"})

    def test_shared_slots_resolve(self):
        a = app.get_current_class_id(); app.update_db_roster(["甲", "乙"])
        b = app.create_class("B班"); app.set_current_class(b); app.update_db_roster(["丙"])
        app.set_current_class(a)
        app.bulk_save_records("2026-10-20", 0, {"甲": "出勤", "乙": "出勤"})
        app.bulk_save_records("2026-10-20", 0, {"丙": "出勤"}, cid=b)
        slots = app.shared_slots(a, b); self.assertEqual(len(slots), 1)
        n, backup = app.resolve_shared_slots([("2026-10-20", 0, a, b)])
        self.assertEqual(n, 1); self.assertTrue(backup)
        self.assertEqual(app.shared_slots(a, b), [])
        self.assertEqual(len(app.get_records("2026-10-20", 0, a)), 2)


    def test_undo_remove_blocked_after_class_switch(self):
        """软删后切班：撤销守卫拦截，人仍在原班（不移到当前班）。"""
        a = app.get_current_class_id()
        app.update_db_roster(["张三", "李四"])
        app.save_record("2026-10-20", 0, "张三", "旷课")
        zhang = next(s for s in app.get_class_students(a) if s["name"] == "张三")
        # 软删张三
        keep = [{"id": s["id"], "name": s["name"]} for s in app.get_class_students(a) if s["name"] != "张三"]
        app.apply_roster_changes(a, keep)
        self.assertEqual(app.STUDENTS_ORDER, ["李四"])
        b = app.create_class("B班")
        app.set_current_class(b)
        self.assertFalse(app.undo_remove_allowed(a, app.get_current_class_id()))
        self.assertTrue(app.undo_remove_allowed(a, a))
        # 模拟「正确」行为：切班后不执行跨班 undo → 张三仍在原班 inactive
        inactive = [s for s in app.get_class_students(a, include_inactive=True) if s["id"] == zhang["id"]]
        self.assertEqual(len(inactive), 1)
        self.assertFalse(inactive[0]["active"])
        # get_class_students(a, …) 已按 class_id 过滤，无需再断言 class_id 字段
        self.assertEqual(app.get_class_students(b), [])
        # UI 源码：undo 绑 class_id，切班 clear_undo_state
        src = open(app.__file__, encoding="utf-8").read()
        self.assertIn('undo_state["class_id"]', src)
        self.assertIn("clear_undo_state()", src)
        self.assertIn("已切换班级，无法撤销", src)


if __name__ == "__main__":
    unittest.main()
