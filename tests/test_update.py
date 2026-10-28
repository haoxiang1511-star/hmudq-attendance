# -*- coding: utf-8 -*-
"""检查更新：解析 version.json、比较版本、按顺序回退、全部失败；不联网（用假的 opener）。"""
import io, json, os, sys, unittest, ast, tomllib, urllib.error
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import main as app

SRC = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
SAMPLE = json.load(open(os.path.join(ROOT, "tools", "version.json"), encoding="utf-8"))


class FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def opener_for(mapping, calls=None):
    def op(req, timeout=None):
        if calls is not None:
            calls.append((req.full_url, timeout))
        v = mapping.get(req.full_url)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise urllib.error.URLError("no route")
        return FakeResp(v if isinstance(v, bytes) else json.dumps(v).encode())
    return op


def info(**kw):
    d = {"version": "1.4.1", "build": app.APP_BUILD + 1, "notes": "修复若干问题",
         "gitee_apk_url": "https://gitee.com/o/r/releases/download/v9.9.9/hmudq-attendance.apk",
         "github_apk_url": "https://github.com/o/r/releases/latest/download/hmudq-attendance.apk",
         "lanzou_url": "https://wwxx.lanzoux.com/abc"}
    d.update(kw); return d


class Update(unittest.TestCase):
    URLS = ["https://gitee.example/v.json", "https://github.example/v.json"]

    def test_config_order_and_placeholders(self):
        urls = app.UPDATE_CONFIG["version_urls"]
        self.assertTrue(urls[0].startswith("https://gitee.com/"))
        self.assertTrue(urls[1].startswith("https://raw.githubusercontent.com/"))
        self.assertEqual(app.UPDATE_CONFIG["timeout"], 10)   # 单个地址 10 秒（两边都超时最多约 20 秒）

    def test_sample_json_valid(self):
        i = app.parse_update_info(json.dumps(SAMPLE))
        self.assertIn("/releases/latest/download/", i["github_apk_url"])
        for k in ("version", "build", "notes", "gitee_apk_url", "github_apk_url", "lanzou_url", "lanzou_code"):
            self.assertIn(k, SAMPLE)
        self.assertTrue(SAMPLE["gitee_apk_url"].startswith("https://gitee.com/"))

    def test_parse_rejects_bad(self):
        for bad in [b"not json", b"[]", json.dumps(info(version="abc")), json.dumps(info(build="x")),
                    json.dumps(info(gitee_apk_url="http://insecure", github_apk_url="http://insecure", lanzou_url="")),
                    json.dumps(info(gitee_apk_url="", github_apk_url="", lanzou_url=""))]:
            with self.assertRaises(ValueError):
                app.parse_update_info(bad, defaults={})
        self.assertEqual(app.parse_update_info(json.dumps(info(notes=["a", "b"])))["notes"], "• a\n• b")
        self.assertEqual(app.parse_update_info("\ufeff" + json.dumps(info(version="v1.5.0")))["version"], "1.5.0")

    def test_builtin_download_defaults(self):
        """version.json 里有 https 链接就用它；没有/不是 https 就用代码里内置的。"""
        ge = app.UPDATE_CONFIG["default_gitee_apk_url"]
        g, l = app.UPDATE_CONFIG["default_github_apk_url"], app.UPDATE_CONFIG["default_lanzou_url"]
        self.assertTrue(ge.startswith("https://gitee.com/") and "/releases/download/" in ge)
        self.assertTrue(g.startswith("https://github.com/") and "/releases/latest/download/" in g)
        self.assertTrue(l.startswith("https://") and "lanzou" in l)
        i = app.parse_update_info(json.dumps({"version": "1.4.1", "build": 999}))
        self.assertEqual((i["gitee_apk_url"], i["github_apk_url"], i["lanzou_url"]), (ge, g, l))
        i = app.parse_update_info(json.dumps(info(lanzou_url="https://wwxx.lanzoux.com/new")))
        self.assertEqual(i["lanzou_url"], "https://wwxx.lanzoux.com/new")
        i = app.parse_update_info(json.dumps(info(gitee_apk_url="http://evil/x.apk", github_apk_url="http://evil/x.apk", lanzou_url="")))
        self.assertEqual((i["gitee_apk_url"], i["github_apk_url"], i["lanzou_url"]), (ge, g, l))
        i = app.parse_update_info(json.dumps(info(lanzou_url="")), defaults={"lanzou_url": "ftp://x",
            "gitee_apk_url": ge, "github_apk_url": g})
        self.assertEqual(i["lanzou_url"], "")                              # 内置地址也必须是 https

    def test_lanzou_folder_and_code(self):
        """蓝奏云：内置固定文件夹链接 + 提取码；version.json 的 lanzou_code 合法就优先用，否则用内置。"""
        self.assertEqual(app.UPDATE_CONFIG["default_lanzou_url"], "https://wwbwu.lanzoup.com/b01i5ycuyb")
        self.assertEqual(app.UPDATE_CONFIG["default_lanzou_code"], "dt9b")
        self.assertEqual(app.parse_update_info(json.dumps(info()))["lanzou_code"], "dt9b")
        self.assertEqual(app.parse_update_info(json.dumps(info(lanzou_code="ab12")))["lanzou_code"], "ab12")
        self.assertEqual(app.parse_update_info(json.dumps(info(lanzou_code=" ab12 ")))["lanzou_code"], "ab12")
        for bad in ("dt 9b", "<script>", "提取码", "x" * 13, 1.5, ["a"]):
            self.assertEqual(app.parse_update_info(json.dumps(info(lanzou_code=bad)))["lanzou_code"], "dt9b", bad)
        i = app.parse_update_info(json.dumps(info(lanzou_url="")), defaults={"github_apk_url": "https://g/x.apk", "gitee_apk_url": ""})
        self.assertEqual((i["lanzou_url"], i["lanzou_code"]), ("", ""))     # 没有蓝奏云按钮就不带提取码
        self.assertEqual(app.parse_update_info(json.dumps(SAMPLE))["lanzou_code"], SAMPLE["lanzou_code"])

    def test_lanzou_code_ui(self):
        self.assertIn('ft.Text("提取码："', SRC)
        self.assertIn('"提取码已复制"', SRC)
        self.assertIn("await ft.Clipboard().set(lanzou_code)", SRC)
        # 复制提取码不能关掉更新弹窗
        body = SRC[SRC.index("def copy_lanzou_code"):SRC.index("lanzou_block = lanzou_btn")]
        self.assertNotIn("pop_dialog", body)

    def test_default_timeout_is_10s(self):
        calls = []
        app.check_for_update(self.URLS, None, opener_for({}, calls))
        self.assertEqual([c[1] for c in calls], [10, 10])

    def test_checking_status_and_lanzou_help(self):
        self.assertEqual(app.UPDATE_CHECKING_TEXT, "正在检查更新…")
        # 检查期间「检查更新」一行的小字显示「正在检查更新…」，结束（成功/失败/异常）都恢复
        body = SRC[SRC.index("def on_check_update"):SRC.index("page.run_task(run)", SRC.index("def on_check_update"))]
        self.assertIn("set_update_tile_status(True)", body)
        self.assertIn("finally:", body)
        self.assertIn("set_update_tile_status(False)", body[body.index("finally:"):])
        self.assertEqual(app.LANZOU_HELP_TEXT,
                         "点『蓝奏云下载』，输入提取码，在文件夹里选最上面那个（版本号最大的）安装包下载，"
                         "下完点开直接安装，名单和考勤都会保留。")
        self.assertIn('ft.TextSpan("最上面", ft.TextStyle(weight=ft.FontWeight.BOLD))', SRC)

    def test_compare(self):
        b = app.APP_BUILD
        self.assertTrue(app.is_newer_version({"version": "9.9.9", "build": b + 1}))
        self.assertFalse(app.is_newer_version({"version": app.APP_VERSION, "build": b}))
        self.assertFalse(app.is_newer_version({"version": "1.9.9", "build": b - 1}))   # build 优先
        self.assertTrue(app.is_newer_version({"version": "9.9.9", "build": b}))

    def test_first_url_wins(self):
        calls = []
        r, d = app.check_for_update(self.URLS, 5, opener_for({self.URLS[0]: info()}, calls))
        self.assertEqual(r, "new"); self.assertEqual(len(calls), 1); self.assertEqual(calls[0][1], 5)

    def test_fallback_to_github(self):
        calls = []
        m = {self.URLS[0]: TimeoutError("timed out"), self.URLS[1]: info()}
        r, d = app.check_for_update(self.URLS, 5, opener_for(m, calls))
        self.assertEqual(r, "new"); self.assertEqual([c[0] for c in calls], self.URLS)
        # 第一个地址返回了坏内容也要换下一个
        m = {self.URLS[0]: b"<html>404</html>", self.URLS[1]: info()}
        self.assertEqual(app.check_for_update(self.URLS, 5, opener_for(m))[0], "new")

    def test_latest(self):
        m = {self.URLS[0]: info(version=app.APP_VERSION, build=app.APP_BUILD)}
        self.assertEqual(app.check_for_update(self.URLS, 5, opener_for(m))[0], "latest")

    def test_all_fail(self):
        r, errs = app.check_for_update(self.URLS, 5, opener_for({}))
        self.assertEqual(r, "fail"); self.assertEqual(len(errs), 2)

    def test_too_big(self):
        m = {self.URLS[0]: b" " * (app.UPDATE_CONFIG["max_bytes"] + 10)}
        self.assertEqual(app.check_for_update(self.URLS[:1], 5, opener_for(m))[0], "fail")

    def test_ui_wiring(self):
        self.assertIn('"检查更新", f"当前版本 v{APP_VERSION}",', SRC)
        self.assertIn("on_click=lambda e: on_check_update(e)", SRC)
        self.assertIn("badge=update_tile_dot", SRC)
        self.assertIn("menu_update_dot", SRC)
        self.assertIn("roster_menu_update_dot", SRC)
        self.assertIn('f"已是最新版本 v{APP_VERSION}"', SRC)
        self.assertIn('"检查失败，请检查网络后再试"', SRC)
        self.assertIn("asyncio.to_thread(check_for_update)", SRC)
        self.assertIn("mode=ft.LaunchMode.EXTERNAL_APPLICATION", SRC)
        self.assertIn('"以后再说"', SRC)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "assets", "github_mark_white.png")))
        self.assertIn('src="github_mark_white.png"', SRC)
        self.assertIn("国内直链（Gitee）", SRC)
        # 按钮顺序：Gitee → GitHub → 蓝奏云
        self.assertLess(SRC.index("国内直链（Gitee）"), SRC.index("GitHub 下载"))

    def test_silent_check_no_auto_popup(self):
        """启动可静默检查，但不得自动弹更新窗/SnackBar；弹窗仍只走手动检查。"""
        self.assertIn("silent_update_check", SRC)
        self.assertIn("should_auto_check_update", SRC)
        self.assertIn("set_update_badges", SRC)
        tree = ast.parse(SRC)
        silent = None
        initial = None
        for fn in ast.walk(tree):
            if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "silent_update_check":
                silent = fn
            if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "initial_load":
                initial = fn
        self.assertIsNotNone(silent, "找不到 silent_update_check")
        self.assertIsNotNone(initial, "找不到 initial_load")
        silent_names = {getattr(n, "id", getattr(n, "attr", None)) for n in ast.walk(silent)}
        self.assertIn("check_for_update", silent_names)
        self.assertNotIn("open_update_dialog", silent_names)
        self.assertNotIn("show_update_dialog", silent_names)
        self.assertNotIn("show_snackbar", silent_names)
        init_names = {getattr(n, "id", getattr(n, "attr", None)) for n in ast.walk(initial)}
        self.assertNotIn("on_check_update", init_names)
        self.assertNotIn("open_update_dialog", init_names)

    def test_never_executes_downloaded_code(self):
        for bad in ("exec(", "eval(", "urlretrieve", "subprocess", "os.system", "__import__("):
            self.assertNotIn(bad, SRC)

    def test_internet_permission_only(self):
        cfg = tomllib.load(open(os.path.join(ROOT, "pyproject.toml"), "rb"))
        perms = cfg["tool"]["flet"]["android"]["permission"]
        self.assertEqual(perms, {"android.permission.INTERNET": True})


if __name__ == "__main__":
    unittest.main()
