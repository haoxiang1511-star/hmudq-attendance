# -*- coding: utf-8 -*-
"""版本号一致（关于页 / CHANGELOG / pyproject）、包名不变、打包排除项完整、check_apk 通过、顶部不显示班名。"""
import os, re, sys, shutil, tempfile, unittest, ast, tomllib
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tools"))
import main as app
import check_apk

SRC = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
CFG = tomllib.load(open(os.path.join(ROOT, "pyproject.toml"), "rb"))
CHANGELOG = open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read()


class Version(unittest.TestCase):
    def test_versions_agree(self):
        self.assertEqual(app.APP_VERSION, "1.5.1"); self.assertEqual(app.APP_BUILD, 109)
        self.assertEqual(CFG["project"]["version"], app.APP_VERSION)
        self.assertEqual(CFG["tool"]["flet"]["build_number"], app.APP_BUILD)
        first = re.search(r"^## v(\S+)（[^，]+，build-number (\d+)）", CHANGELOG, re.M)
        self.assertEqual((first.group(1), int(first.group(2))), (app.APP_VERSION, app.APP_BUILD))
        self.assertGreater(app.APP_BUILD, 102)               # 只增不减

    def test_about_page_uses_constants(self):
        self.assertIn('f"v{APP_VERSION} (build {APP_BUILD})"', SRC)
        self.assertIn('f"当前版本 v{APP_VERSION}"', SRC)
        # 界面文字里不能残留写死的旧版本号（注释、文档字符串、print 日志不算）
        tree = ast.parse(SRC); skip = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print":
                skip.update(id(x) for x in ast.walk(n))
            if isinstance(n, (ast.FunctionDef, ast.Module)) and n.body and isinstance(n.body[0], ast.Expr):
                skip.add(id(n.body[0].value))
        bad = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and id(n) not in skip and re.search(r"v?1\.[0-3]\.\d", n.value)]
        self.assertEqual(bad, [])

    def test_package_name_unchanged(self):
        self.assertEqual(CFG["tool"]["flet"]["bundle_id"], "com.flet.app_build")
        self.assertEqual(CFG["tool"]["flet"]["org"], "com.flet")

    def test_no_class_name_on_roll_call_header(self):
        tree = ast.parse(SRC)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "roll_call_view" for t in node.targets):
                names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
                self.assertNotIn("class_name_text", names)
                self.assertNotIn("class_setting_row", names)
                return
        self.fail("找不到 roll_call_view")


class Packaging(unittest.TestCase):
    def test_exclude_list_complete(self):
        ex = set(CFG["tool"]["flet"]["app"]["exclude"])
        for need in [".git", "tests", "tools", "attendance.db", "attendance.db-wal", "attendance.db-shm", "backups"]:
            self.assertIn(need, ex)

    def test_check_apk_passes_on_source(self):
        files = check_apk.simulate_package()
        bad, missing = check_apk.problems(files)
        self.assertEqual((bad, missing), ([], []), files)
        self.assertIn("main.py", files)

    def test_check_apk_catches_db_and_tests(self):
        d = tempfile.mkdtemp()
        try:
            for f in ["main.py", "name_import.py", "attendance.db", "attendance.db-wal", "backups/a.db",
                      "tests/t.py", ".git/HEAD", "10月临床3班上课出勤.xlsx"]:
                p = os.path.join(d, f); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").close()
            # 用真实的排除表：数据库/备份/tests/.git 被排除，但导出的 xlsx 会被抓出来
            files = check_apk.simulate_package(d, check_apk.load_excludes())
            bad, _ = check_apk.problems(files)
            self.assertEqual([b[0] for b in bad], ["10月临床3班上课出勤.xlsx"])
            # 不排除时全部抓出来
            bad, _ = check_apk.problems(check_apk.simulate_package(d, []))
            self.assertEqual(len(bad), 6)
        finally:
            shutil.rmtree(d)

    def test_no_seed_roster(self):
        """新装不预置名单：DEFAULT_STUDENTS_ORDER 只能出现在旧库补排序里，不能被 INSERT。"""
        uses = [m.start() for m in re.finditer(r"DEFAULT_STUDENTS_ORDER", SRC)]
        for u in uses:
            line = SRC[SRC.rfind("\n", 0, u) + 1: SRC.find("\n", u)]
            self.assertNotIn("INSERT", line)


if __name__ == "__main__":
    unittest.main()
