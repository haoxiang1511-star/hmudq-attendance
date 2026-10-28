# -*- coding: utf-8 -*-
"""用真实的 v1.3.1 代码（tests/fixtures/main_v131.py = git 8f2fb92:main.py）生成一个「像真的」旧库：
- 班名「临床3班」，48 人（其中 3 人 10-13 插班，joined_date=2026-10-13），1 人 10-20 被移出（v1.3.1 会连记录一起删）
- 10 月 1-27 日工作日每天 2~4 节：大部分节先点「全员出勤」（bulk 写 48 条出勤）再改个别缺勤；
  少量节只标了缺勤的同学（没点全员出勤）——这些就是 v1.4.0 里会显示「空课」的节；少量节「全员空课」。
返回 (db_path, info)。info["partial"] = 只标了部分同学的课节数。"""
import os, sys, random, importlib.util, sqlite3, io, contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))   # name_import
FAM = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛"
GIV = "伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英华玉兰萍红"


def _names(n):
    out, i = [], 0
    while len(out) < n:
        s = FAM[i % len(FAM)] + GIV[(i * 3) % len(GIV)] + GIV[(i * 7 + 1) % len(GIV)]
        if s not in out:
            out.append(s)
        i += 1
    return out


def load_old():
    spec = importlib.util.spec_from_file_location("main_v131", os.path.join(HERE, "main_v131.py"))
    mod = importlib.util.module_from_spec(spec)
    saved = sys.stdout, sys.stderr
    spec.loader.exec_module(mod)
    sys.stdout, sys.stderr = saved          # 旧代码会接管 stdout，测试里还原
    return mod


def make(db_path, seed=131):
    old = load_old()
    old.DB_PATH = db_path
    old._db_conn = None
    old.init_db()
    old.db_set_config("class_name", "临床3班")
    rnd = random.Random(seed)
    names = _names(48)
    base, late, gone = names[:45], names[45:], names[10]
    old.update_db_roster(base)
    # v1.3.1 给新加的人写 joined_date=今天；这 45 人是开学前就在的老名单 → NULL（一直在班）
    old.get_db_connection().execute("UPDATE students SET joined_date=NULL"); old.get_db_connection().commit()
    info = {"partial": 0, "taken": 0, "noclass": 0}
    days = [d for d in range(1, 28) if __import__("datetime").date(2026, 10, d).weekday() < 5]
    for d in days:
        ds = f"2026-10-{d:02d}"
        if d == 13:
            old.update_db_roster(base + late)
            old.get_db_connection().execute(
                "UPDATE students SET joined_date='2026-10-13' WHERE name IN (?,?,?)", late)
            old.get_db_connection().commit()
        if d == 20:
            cur = [n for n in (base + late) if n != gone]
            old.update_db_roster(cur)
        old.load_students_order()
        roster = list(old.STUDENTS_ORDER)
        for p in rnd.sample(range(5), rnd.randint(2, 4)):
            r = rnd.random()
            if r < 0.08:          # 全员空课
                old.bulk_save_records(ds, p, {n: "空课" for n in roster}); info["noclass"] += 1
            elif r < 0.20:        # 只点了缺勤的同学（没点「全员出勤」）
                for n in rnd.sample(roster, rnd.randint(1, 4)):
                    old.save_record(ds, p, n, rnd.choice(["旷课", "病假", "事假"]))
                info["partial"] += 1
            else:                 # 正常流程：全员出勤 → 再改缺勤
                old.bulk_save_records(ds, p, {n: "出勤" for n in roster})
                for n in rnd.sample(roster, rnd.randint(0, 3)):
                    old.save_record(ds, p, n, rnd.choice(["旷课", "病假", "事假"]))
            info["taken"] += 1
    conn = old.get_db_connection()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    info["students"] = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    info["records"] = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    conn.close()
    old._db_conn = None
    return db_path, info


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else "/tmp/v131_realistic.db"
    if os.path.exists(p):
        os.remove(p)
    print(make(p))
