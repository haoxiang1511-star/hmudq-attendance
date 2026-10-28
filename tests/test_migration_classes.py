# -*- coding: utf-8 -*-
"""v1.4.1：旧名单带班级标记（students.class_name）/ 重名时的升级。
旧库的结构（v1.3.x 某些版本）：students(id, name, sort_order, class_name DEFAULT '24示例3班', joined_date)，
UNIQUE(name, class_name)，user_version=0，app_config.class_name 为当前班。"""
import os, sys, shutil, sqlite3, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import main as app



def make_old(path, students, records=(), cfg_class="24示例3班", with_class_col=True, unique=True):
    """students: [(id, name, sort_order, class_name)]；records: [(id, student_id, date, period, status)]"""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE app_config (key TEXT PRIMARY KEY, value TEXT)")
    if with_class_col:
        c.execute("CREATE TABLE students (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                  "sort_order INTEGER DEFAULT 0, class_name TEXT DEFAULT '24示例3班', joined_date TEXT)")
        if unique:
            c.execute("CREATE UNIQUE INDEX idx_students_name_class ON students(name, class_name)")
        c.executemany("INSERT INTO students(id, name, sort_order, class_name) VALUES(?,?,?,?)", students)
    else:
        c.execute("CREATE TABLE students (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                  "sort_order INTEGER DEFAULT 0, joined_date TEXT)")
        c.executemany("INSERT INTO students(id, name, sort_order) VALUES(?,?,?)", [s[:3] for s in students])
    c.execute("CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL, "
              "date TEXT NOT NULL, period INTEGER NOT NULL, status TEXT NOT NULL, "
              "FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE, UNIQUE(student_id, date, period))")
    c.executemany("INSERT INTO records(id, student_id, date, period, status) VALUES(?,?,?,?,?)", records)
    if cfg_class is not None:
        c.execute("INSERT INTO app_config VALUES('class_name', ?)", (cfg_class,))
    c.commit(); c.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        if app._db_conn:
            app.close_db_connection()
        app._db_conn = None
        app.DB_PATH = os.path.join(self.d, "attendance.db")
        app._MIGRATION_FAULT = None

    def tearDown(self):
        app._MIGRATION_FAULT = None
        if app._db_conn:
            app.close_db_connection()
        app._db_conn = None
        shutil.rmtree(self.d, ignore_errors=True)

    def raw(self):
        c = sqlite3.connect(app.DB_PATH)
        s = c.execute("SELECT id, name, sort_order, joined_date FROM students ORDER BY id").fetchall()
        r = c.execute("SELECT id, student_id, date, period, status FROM records ORDER BY id").fetchall()
        c.close(); return s, r

    def upgrade(self):
        info = app.init_db(); app.load_students_order()
        self.c = app.get_db_connection()
        return info

    def classes(self):
        return self.c.execute("SELECT name FROM classes ORDER BY sort_order, id").fetchall()

    def who(self):
        """{id: (name, class_name)}"""
        return {i: (n, c) for i, n, c in self.c.execute(
            "SELECT s.id, s.name, c.name FROM students s JOIN classes c ON c.id=s.class_id")}


class SplitByClassName(Base):
    def test_two_classes_split(self):
        studs = [(1, "张三", 0, "24示例3班"), (2, "赵乙", 1, "24示例3班"), (3, "李四", 2, "24示例3班"),
                 (10, "王五", 3, "24示例2班"), (11, "赵乙", 4, "24示例2班")]
        recs = [(1, 1, "2026-06-03", 0, "出勤"), (2, 2, "2026-06-03", 0, "旷课"), (3, 11, "2026-06-04", 1, "病假")]
        make_old(app.DB_PATH, studs, recs)
        s0, r0 = self.raw()
        info = self.upgrade()
        self.assertTrue(info["migrated"])
        self.assertEqual(info["classes"], ["24示例3班", "24示例2班"])
        self.assertEqual(info["renamed"], {})
        self.assertEqual(self.classes(), [("24示例3班",), ("24示例2班",)])
        self.assertEqual(app.get_class_name(app.get_current_class_id()), "24示例3班")
        w = self.who()
        self.assertEqual(w[2], ("赵乙", "24示例3班")); self.assertEqual(w[11], ("赵乙", "24示例2班"))
        self.assertEqual({k: v[1] for k, v in w.items()},
                         {1: "24示例3班", 2: "24示例3班", 3: "24示例3班", 10: "24示例2班", 11: "24示例2班"})
        s1, r1 = self.raw()
        self.assertEqual((s1, r1), (s0, r0))           # id/姓名/排序/加入日期、考勤逐条一致
        self.assertEqual(self.c.execute("PRAGMA user_version").fetchone()[0], 2)
        # 当前班名单只有本班的人；二班有自己的名单和考勤
        self.assertEqual(app.STUDENTS_ORDER, ["张三", "赵乙", "李四"])
        cid2 = self.c.execute("SELECT id FROM classes WHERE name='24示例2班'").fetchone()[0]
        self.assertEqual([s["name"] for s in app.get_class_students(cid2)], ["王五", "赵乙"])
        # 第二次启动不再升级
        self.assertFalse(app.init_db()["migrated"])

    def test_current_class_from_config_and_others_by_name(self):
        studs = [(1, "甲", 0, "C班"), (2, "乙", 1, "A班"), (3, "丙", 2, "B班")]
        make_old(app.DB_PATH, studs, cfg_class="B班")
        self.upgrade()
        self.assertEqual(self.classes(), [("B班",), ("A班",), ("C班",)])
        self.assertEqual(app.get_class_name(app.get_current_class_id()), "B班")

    def test_class_name_normalized_and_spaces_merged(self):
        long = "临床医学" * 6                               # 24 字，超过 CLASS_NAME_MAX
        studs = [(1, "甲", 0, " 24示例3班 "), (2, "乙", 1, "24示例 3班"), (3, "丙", 2, long), (4, "丁", 3, "24示例3班")]
        make_old(app.DB_PATH, studs, unique=False)
        self.upgrade()
        names = [n for (n,) in self.classes()]
        self.assertEqual(names[0], "24示例3班")
        self.assertEqual(len(names), 2)
        self.assertEqual(names[1], long[:app.CLASS_NAME_MAX])
        self.assertTrue(all(len(n) <= app.CLASS_NAME_MAX for n in names))
        w = self.who()
        self.assertEqual({w[i][1] for i in (1, 2, 4)}, {"24示例3班"})

    def test_empty_class_name_goes_to_config_class(self):
        studs = [(1, "甲", 0, None), (2, "乙", 1, ""), (3, "丙", 2, "  "), (4, "丁", 3, "二班")]
        make_old(app.DB_PATH, studs, cfg_class="一班", unique=False)
        info = self.upgrade()
        self.assertEqual(info["classes"], ["一班", "二班"])
        w = self.who()
        self.assertEqual([w[i][1] for i in (1, 2, 3, 4)], ["一班", "一班", "一班", "二班"])
        self.assertEqual(app.get_class_name(app.get_current_class_id()), "一班")

    def test_all_empty_class_name_one_class(self):
        make_old(app.DB_PATH, [(1, "甲", 0, None), (2, "乙", 1, None)], cfg_class=None, unique=False)
        info = self.upgrade()
        self.assertEqual(info["classes"], [])
        self.assertEqual(self.classes(), [(app.DEFAULT_CLASS_NAME,)])

    def test_config_class_without_students_picks_class_with_most_records(self):
        studs = [(1, "甲", 0, "二班"), (2, "乙", 1, "三班"), (3, "丙", 2, "三班")]
        make_old(app.DB_PATH, studs, [(1, 2, "2026-06-01", 0, "出勤")], cfg_class="临床2班")
        self.upgrade()
        self.assertEqual(self.classes(), [("三班",), ("二班",)])
        self.assertEqual(app.get_class_name(app.get_current_class_id()), "三班")


class DuplicateNames(Base):
    def test_same_class_duplicates_get_suffix(self):
        studs = [(1, "张伟", 0, "一班"), (2, "李雷", 1, "一班"), (3, "张伟", 2, "一班"), (4, "张伟", 3, "一班"),
                 (5, "张伟(3)", 4, "一班"), (6, "张伟", 5, "二班")]
        recs = [(1, 3, "2026-06-01", 0, "旷课"), (2, 4, "2026-06-01", 0, "事假"), (3, 1, "2026-06-01", 0, "出勤")]
        make_old(app.DB_PATH, studs, recs, cfg_class="一班", unique=False)
        _, r0 = self.raw()
        info = self.upgrade()
        # 第 1 个保持原名；第 2 个 (2)；第 3 个本应 (3) 但已有人叫「张伟(3)」→ (4)；二班的张伟不受影响
        self.assertEqual(info["renamed"], {3: "张伟(2)", 4: "张伟(4)"})
        w = self.who()
        self.assertEqual([w[i] for i in range(1, 7)],
                         [("张伟", "一班"), ("李雷", "一班"), ("张伟(2)", "一班"), ("张伟(4)", "一班"),
                          ("张伟(3)", "一班"), ("张伟", "二班")])
        _, r1 = self.raw()
        self.assertEqual(r1, r0)                         # 考勤跟着 id 走，一条不少
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM students").fetchone()[0], 6)

    def test_duplicates_without_class_column(self):
        make_old(app.DB_PATH, [(1, "王芳", 0, None), (2, "王芳", 1, None)],
                 [(1, 2, "2026-06-01", 0, "病假")], cfg_class="临床2班", with_class_col=False)
        info = self.upgrade()
        self.assertEqual(info["renamed"], {2: "王芳(2)"})
        self.assertEqual(info["classes"], [])
        self.assertEqual(self.classes(), [("临床2班",)])
        self.assertEqual(app.STUDENTS_ORDER, ["王芳", "王芳(2)"])


class OrphansAndOldDb(Base):
    def test_orphan_records_kept(self):
        studs = [(1, "甲", 0, "一班"), (2, "乙", 1, "二班")]
        recs = [(1, 1, "2026-06-01", 0, "出勤"), (2, 35, "2026-06-01", 0, "旷课"), (3, 35, "2026-06-02", 1, "出勤")]
        make_old(app.DB_PATH, studs, recs, cfg_class="一班")
        _, r0 = self.raw()
        info = self.upgrade()
        _, r1 = self.raw()
        self.assertEqual(r1, r0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM records WHERE student_id=35").fetchone()[0], 2)
        self.assertEqual(info["orphan_records"], 2)

    def test_no_class_column_keeps_old_behaviour(self):
        studs = [(1, "甲", 0, None), (2, "乙", 1, None), (3, "丙", 2, None)]
        make_old(app.DB_PATH, studs, [(1, 1, "2026-06-01", 0, "出勤")], cfg_class="临床2班", with_class_col=False)
        s0, r0 = self.raw()
        info = self.upgrade()
        self.assertEqual(info["classes"], []); self.assertEqual(info["renamed"], {})
        self.assertEqual(self.classes(), [("临床2班",)])
        self.assertEqual(self.raw(), (s0, r0))
        self.assertEqual(app.STUDENTS_ORDER, ["甲", "乙", "丙"])

    def test_failed_then_retry(self):
        """v1.4.0 失败回滚后的库（旧结构 + 可能残留的空 students_new）再升级：成功。"""
        studs = [(1, "赵乙", 0, "24示例3班"), (2, "赵乙", 1, "24示例2班")]
        make_old(app.DB_PATH, studs, [(1, 1, "2026-06-01", 0, "出勤")])
        c = sqlite3.connect(app.DB_PATH)
        c.execute("CREATE TABLE students_new (id INTEGER PRIMARY KEY, class_id INTEGER, name TEXT)")
        c.execute("INSERT INTO app_config VALUES('backup_reason', '升级失败时导出（当前数据）')")
        c.commit(); c.close()
        app._MIGRATION_FAULT = "before_verify"
        with self.assertRaises(app.MigrationError):
            app.init_db()
        app._MIGRATION_FAULT = None
        app.close_db_connection(); app._db_conn = None
        k = sqlite3.connect(app.DB_PATH)
        self.assertEqual(k.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertIn("class_name", [r[1] for r in k.execute("PRAGMA table_info(students)")])
        k.close()
        s0, r0 = self.raw()
        info = self.upgrade()
        self.assertTrue(info["migrated"])
        self.assertEqual(self.raw(), (s0, r0))
        self.assertEqual(sorted(self.who().values()), [("赵乙", "24示例2班"), ("赵乙", "24示例3班")])
        tabs = {r[0] for r in self.c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("students_new", tabs)

    def _fault(self, fault):
        """核对按 id 比班级、姓名：写入结果和分班/改名计划不一致就整体回滚，旧库原样。"""
        make_old(app.DB_PATH, [(1, "甲", 0, "一班"), (2, "乙", 1, "二班"), (3, "甲", 2, "一班")],
                 [(1, 2, "2026-06-01", 0, "出勤")], cfg_class="一班", unique=False)
        s0, r0 = self.raw()
        app._MIGRATION_FAULT = fault
        with self.assertRaises(app.MigrationError) as cm:
            app.init_db()
        self.assertIn("核对失败：学生", str(cm.exception))
        app._MIGRATION_FAULT = None
        app.close_db_connection(); app._db_conn = None
        self.assertEqual(self.raw(), (s0, r0))
        k = sqlite3.connect(app.DB_PATH)
        self.assertEqual(k.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertNotIn("classes", {r[0] for r in k.execute("SELECT name FROM sqlite_master")})
        k.close()

    def test_verify_catches_wrong_class(self): self._fault("wrong_class")
    def test_verify_catches_wrong_name(self):  self._fault("wrong_name")


if __name__ == "__main__":
    unittest.main()
