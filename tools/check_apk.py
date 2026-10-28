# -*- coding: utf-8 -*-
"""打包检查：安装包里不能有数据库、备份、名单/导出表格、.git、tests。

用法：
  python tools/check_apk.py              # 打包前：按 pyproject 的 exclude 模拟 flet build 会打进包的文件
  python tools/check_apk.py xxx.apk      # 打包后：直接检查 APK 里的 app.zip
退出码 0 = 通过，1 = 有问题。

注意：serious_python 的 exclude 是「相对路径完全相等」才排除（不支持 *.db 这类通配符），
所以这里按同样的规则模拟，不按通配符。
"""
import os, sys, io, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JUNK_DIRS = {"__pycache__"}                       # serious_python 自带的清理
FORBIDDEN_EXT = (".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".xlsx", ".xls", ".csv", ".tmp")
FORBIDDEN_DIRS = {".git", "tests", "backups", "rescue_export", "tools"}


def load_excludes():
    try:
        import tomllib
    except ImportError:                       # Python < 3.11
        import tomli as tomllib
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        cfg = tomllib.load(f)
    return ["build"] + list(cfg.get("tool", {}).get("flet", {}).get("app", {}).get("exclude", []))


def simulate_package(root=ROOT, excludes=None):
    excludes = set(excludes if excludes is not None else load_excludes())
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        keep = []
        for d in dirnames:
            rel = os.path.normpath(os.path.join(rel_dir, d))
            if rel in excludes or d in JUNK_DIRS:
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, fn))
            if rel in excludes:
                continue
            out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def apk_app_files(apk):
    with zipfile.ZipFile(apk) as z:
        inner = [n for n in z.namelist() if n.endswith("flutter_assets/app/app.zip")]
        if not inner:
            raise SystemExit(f"{apk} 里找不到 assets/flutter_assets/app/app.zip")
        with zipfile.ZipFile(io.BytesIO(z.read(inner[0]))) as a:
            return sorted(n for n in a.namelist() if not n.endswith("/"))


def problems(files):
    bad = []
    for f in files:
        parts = f.split("/")
        low = f.lower()
        if any(p in FORBIDDEN_DIRS for p in parts[:-1]) or parts[0] in FORBIDDEN_DIRS:
            bad.append((f, "不该打包的目录"))
        elif low.endswith(FORBIDDEN_EXT):
            bad.append((f, "数据库 / 表格 / 临时文件"))
        elif parts[-1] in (".gitignore",):
            bad.append((f, "开发文件"))
    must = {"main.py", "name_import.py"}
    have = {os.path.basename(f).replace(".pyc", ".py") for f in files}
    missing = [m for m in must if m not in have]
    return bad, missing


def main(argv):
    if len(argv) > 1:
        files = apk_app_files(argv[1]); src = argv[1]
    else:
        files = simulate_package(); src = "源码目录（模拟 flet build）"
    bad, missing = problems(files)
    print(f"检查：{src}，共 {len(files)} 个文件")
    for f in files:
        print("  ", f)
    for f, why in bad:
        print(f"✗ {f}：{why}")
    for m in missing:
        print(f"✗ 缺少 {m}")
    if bad or missing:
        print("不通过"); return 1
    print("通过：包里没有数据库、备份、名单表格、.git、tests"); return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
