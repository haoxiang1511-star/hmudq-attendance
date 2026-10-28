# -*- coding: utf-8 -*-
"""名单导入确认框：列出全部姓名的固定高度小框，滚动条一直显示；重复提醒和说明在小框外。"""
import os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import flet as ft
import main as app

SRC = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()


class ImportPreview(unittest.TestCase):
    def build(self, n=120, dups=("张伟",)):
        names = [f"同学{i:03d}" for i in range(n)]
        dup_lines = [f"⚠ 表里『{d}』出现了 2 次" for d in dups]
        return names, app.build_import_preview(names, "Sheet1 / 姓名列", n - 5, dup_lines, set(names[:5]))

    def find_box(self, col):
        boxes = [c for c in col.controls if isinstance(c, ft.Container) and isinstance(c.content, ft.Column)]
        self.assertEqual(len(boxes), 1); return boxes[0]

    def test_all_names_listed_with_index(self):
        names, col = self.build(120)
        box = self.find_box(col)
        rows = box.content.controls
        self.assertEqual(len(rows), 120)                                     # 不再只预览前 8 个
        self.assertEqual(rows[0].controls[0].value, "1."); self.assertEqual(rows[-1].controls[1].value, names[-1])
        self.assertEqual(rows[0].controls[2].value, "")                      # 已在名单
        self.assertEqual(rows[10].controls[2].value, "新")

    def test_fixed_height_and_scrollbar_always_visible(self):
        _, col = self.build()
        box = self.find_box(col)
        self.assertTrue(180 <= box.height <= 220)
        sb = box.content.scroll
        self.assertIsInstance(sb, ft.Scrollbar)
        self.assertTrue(sb.thumb_visibility); self.assertTrue(sb.track_visibility)
        self.assertIsNone(col.scroll)                                        # 外层不滚动

    def test_dup_warning_outside_box(self):
        _, col = self.build(dups=("张伟", "李娜"))
        box = self.find_box(col)
        outer = [c.value for c in col.controls if isinstance(c, ft.Text)]
        inner = [r.controls[1].value for r in box.content.controls]
        self.assertTrue(any("张伟" in t for t in outer)); self.assertTrue(any("合并追加" in t for t in outer))
        self.assertFalse(any("⚠" in t for t in inner))
        self.assertLess(col.controls.index(box),
                        [i for i, c in enumerate(col.controls) if isinstance(c, ft.Text) and "⚠" in c.value][0])

    def test_handler_no_longer_truncates(self):
        body = SRC[SRC.index("    def handle_imported_file(filepath):"):SRC.index("    # ---- 保存 ----")]
        self.assertNotIn("[:8]", body); self.assertIn("build_import_preview(", body)


if __name__ == "__main__":
    unittest.main()
