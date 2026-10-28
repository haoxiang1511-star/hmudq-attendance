# -*- coding: utf-8 -*-
"""把电脑上（脚本生成/修改过的）attendance.db 打成 App「从文件恢复」能识别的标准恢复文件。

用法（在 build_fix 目录下，用装了 flet 的 venv）：
    python tools/make_restore_file.py 源.db [输出文件]
默认输出：源文件同目录的 点名恢复_时间.db

做的事情和 App 的「立即备份」一样：一致性复制 → 写入本软件标识（app_config.backup_app 等）→
integrity_check → 用 App 恢复时同一个 validate_backup 校验（结构、状态、日期、节次）。
不合格直接报错退出，不生成文件。旧版（v1.2/v1.3）结构的库也可以，App 恢复后会自动升级。
不会修改源文件。
"""
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import main as app  # noqa: E402  与 App 用同一套校验规则


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    src = os.path.abspath(argv[1])
    if len(argv) > 2:
        dst = os.path.abspath(argv[2])
    else:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(os.path.dirname(src), f"点名恢复_{ts}.db")
    if os.path.abspath(dst) == src:
        print("输出文件不能和源文件相同")
        return 2
    try:
        info = app.make_restore_file(src, dst)
    except app.BackupError as ex:
        print(f"失败：{ex}")
        return 1
    print(f"已生成：{dst}")
    print(f"内容：{info.get('classes', 0)} 个班、{info.get('students', 0)} 名学生、{info.get('records', 0)} 条考勤")
    print("把这个文件发到手机（微信改了扩展名也没关系），在 App「关于软件 → 数据备份与恢复 → 从文件恢复」里选它。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
