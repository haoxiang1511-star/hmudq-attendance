# -*- coding: utf-8 -*-
"""回归 v1.3.1：show_snackbar 也会入对话框栈，同一处理函数里不能先 show_snackbar 再 pop_dialog，
否则 pop 掉的是 SnackBar，对话框关不上（切换班级“确认”不关闭的 Bug）。"""
import ast, os, unittest
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


def _walk_own(node):
    """遍历但不进入嵌套函数 / lambda（它们是之后才触发的回调）。"""
    for ch in ast.iter_child_nodes(node):
        if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield ch
        yield from _walk_own(ch)


def _calls(fn):
    out = []
    for node in _walk_own(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in ("show_snackbar", "pop_dialog", "save_roster_action"):
                out.append((node.lineno, node.col_offset, name))
    return sorted(out)


class DialogOrderTest(unittest.TestCase):
    def test_no_pop_after_snackbar(self):
        tree = ast.parse(open(SRC, encoding="utf-8").read())
        bad = []
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and fn.name not in ("main",) and fn.name != "show_snackbar":
                # 只看函数自身（不进入嵌套函数）
                seen_snack = False
                for _, _, name in _calls(fn):
                    if name in ("show_snackbar", "save_roster_action"):
                        seen_snack = True
                    elif name == "pop_dialog" and seen_snack:
                        bad.append(fn.name)
        self.assertEqual(bad, [], f"这些函数先弹 SnackBar 再 pop_dialog：{bad}")


if __name__ == "__main__":
    unittest.main()
