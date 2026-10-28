# -*- coding: utf-8 -*-
"""名单导入：从各种不规范的 Excel/CSV 中尽可能识别学生姓名。纯 Python，不依赖 Flet，便于单元测试。"""
import csv
import os
import re

NAME_HEADER_KEYS = ["学生姓名", "姓名", "名字", "学生", "成员", "组员", "name"]
# 常见的非姓名词（单元格完全等于或以这些开头时排除）
STOP_WORDS = {
    "序号", "编号", "组号", "组别", "分组", "小组", "组长", "组员", "班级", "班长", "姓名", "名字", "学生", "学生姓名",
    "合计", "总计", "备注", "男", "女", "性别", "是", "否", "签字", "签名", "确认", "学院", "专业", "日期", "时间",
    "学号", "电话", "手机", "手机号", "宿舍", "寝室", "联系方式", "年级", "民族", "籍贯", "身份证", "已确认", "未确认",
    "回家", "留校", "离校", "返校", "去向", "原因", "人数", "共计", "请假", "出勤", "旷课", "病假", "事假", "空课",
    "汉族", "团员", "党员", "群众", "本人签字", "家长签字", "说明", "活动", "报名", "名单", "统计", "无", "有",
    "信管", "临床", "护理", "药学", "第一组", "第二组", "第三组", "第四组", "第五组", "第六组", "第七组", "第八组",
}
# v1.4.0：不再把单个「班/组/表」当作排除字（会误伤「班婕」「组明」等真名），改为只排除以它们结尾的「xx班/xx组/xx表」
STOP_SUBSTR = ["学院", "专业", "合计", "总计", "备注", "名单", "统计", "签字", "确认", "日期", "宿舍", "寝室",
               "年级", "大学", "校区", "老师", "辅导员", "班级", "小组", "名册"]
# 5 个字以上的长串（少数民族长名可达 5-8 字）额外排除像句子/标题的内容
LONG_STOP_SUBSTR = ["通知", "说明", "请", "参加", "活动", "报名", "签到", "考勤", "出勤", "信息", "汇总", "情况", "安排",
                    "课程", "学期", "要求", "注意", "以下", "如下", "分组", "表格"]
_NUM_CN = "一二三四五六七八九十百零〇两"
# 英文名中的常见表头/非姓名词（小写比较）
EN_STOP = {"name", "names", "student", "students", "no", "id", "total", "male", "female", "yes", "sex", "gender",
           "class", "group", "remark", "remarks", "note", "notes", "date", "phone", "tel", "email", "sum", "ok",
           "na", "nan", "none", "null", "leader", "member", "members", "number", "list", "sign", "signature", "time"}
ROLE_WORDS = "组长|副组长|组员|班长|副班长|团支书|学委|学习委员|生活委员|体委|文艺委员|宣传委员|心理委员|队长|负责人|联系人"

_HAN = r"\u4e00-\u9fff\u3400-\u4dbf"
# 汉字 2-8 个（含维吾尔族等不带间隔号的长名，如「阿依古丽买买提」），或带间隔号的少数民族/外文译名
NAME_RE = re.compile(rf"^[{_HAN}]{{2,8}}$|^[{_HAN}]{{1,8}}([·•][{_HAN}]{{1,10}}){{1,3}}$")
# 英文名：1-4 个单词，允许 - ' . （如 Tom、Mary-Jane O'Neil、J. Smith）
EN_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z'\-.]*( [A-Za-z][A-Za-z'\-.]*){0,3}$")


def normalize_name(val):
    """手动输入/导入共用的姓名规范化：中文去掉所有空白；英文把连续空白合并成一个空格。"""
    if val is None:
        return ""
    s = str(val).replace("\u3000", " ").replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()
    s = re.sub(r" +", " ", s)
    if re.search(rf"[{_HAN}]", s):
        s = s.replace(" ", "")
    return s


def _is_english_name(s):
    if not EN_NAME_RE.match(s):
        return False
    letters = re.sub(r"[^A-Za-z]", "", s)
    if len(letters) < 2:
        return False
    if s.lower() in EN_STOP or any(w.lower().strip(".") in EN_STOP for w in s.split(" ") if len(s.split(" ")) == 1):
        return False
    return True


def clean_cell(val):
    """把单元格清洗成候选姓名字符串；返回 '' 表示不是姓名。"""
    if val is None:
        return ""
    if isinstance(val, (int, float)):
        return ""
    raw = normalize_name(val)
    if not raw:
        return ""
    # 英文名（保留单词间的一个空格）
    en = re.sub(r"^\d+[.、．)）]\s*", "", raw)
    en = re.sub(r"\s*[（(][^（）()]*[)）]$", "", en)
    en = re.sub(r"[*★☆✓√✔]+$", "", en).strip()
    if en and not re.search(rf"[{_HAN}]", en):
        return en if _is_english_name(en) else ""
    s = raw.replace(" ", "")
    s = s.replace("•", "·").replace("．", "·")
    # 去掉“组长：张三”“1. 张三”之类前缀
    s = re.sub(rf"^({ROLE_WORDS})[:：]", "", s)
    s = re.sub(r"^\d+[.、．)）]", "", s)
    # 去掉“张三（组长）”“张三(男)”“张三*”之类后缀
    s = re.sub(r"[（(][^（）()]*[)）]$", "", s)
    s = re.sub(r"[*★☆✓√✔]+$", "", s)
    if s in STOP_WORDS:
        return ""
    if not NAME_RE.match(s):
        return ""
    if any(w in s for w in STOP_SUBSTR):
        return ""
    # 「三班」「第一组」「临床班」「签到表」这类：以 班/组/表 结尾，且前面是数字或长度≥3
    if s[-1] in "班组表" and (len(s) >= 3 or s[0] in _NUM_CN):
        return ""
    if len(s.replace("·", "")) >= 5 and "·" not in s and any(w in s for w in LONG_STOP_SUBSTR):
        return ""
    return s


def _is_header(val):
    if val is None or not isinstance(val, str):
        return False
    s = val.strip().replace(" ", "").replace("\n", "").lower()
    if len(s) > 8:
        return False
    return any(k in s for k in NAME_HEADER_KEYS) and "学号" not in s and "签" not in s


def _read_grids(filepath):
    """返回 [(sheet_name, rows)]，rows 为二维列表；合并单元格取左上角值填充。"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".csv":
        data = None
        for enc in ("utf-8-sig", "gb18030"):
            try:
                with open(filepath, newline="", encoding=enc) as f:
                    data = [row for row in csv.reader(f)]
                break
            except UnicodeDecodeError:
                continue
        if data is None:
            raise ValueError("CSV 文件编码无法识别，请另存为 UTF-8 或 xlsx")
        return [("csv", data)]
    if ext == ".xls":
        try:
            import xlrd  # noqa
        except ImportError:
            raise ValueError("暂不支持旧版 .xls 文件，请在 Excel/WPS 中“另存为 .xlsx”后再导入")
        book = xlrd.open_workbook(filepath)
        return [(sh.name, [sh.row_values(r) for r in range(sh.nrows)]) for sh in book.sheets()]
    import openpyxl
    wb = openpyxl.load_workbook(filepath, data_only=True)
    grids = []
    for ws in wb.worksheets:
        if getattr(ws, "sheet_state", "visible") != "visible":
            continue
        max_r = min(ws.max_row or 0, 2000)
        max_c = min(ws.max_column or 0, 60)
        rows = [[ws.cell(row=r, column=c).value for c in range(1, max_c + 1)] for r in range(1, max_r + 1)]
        for rng in ws.merged_cells.ranges:
            v = ws.cell(row=rng.min_row, column=rng.min_col).value
            for r in range(rng.min_row, min(rng.max_row, max_r) + 1):
                for c in range(rng.min_col, min(rng.max_col, max_c) + 1):
                    # 纵向合并的姓名只取一次，避免重复；横向合并的标题无所谓
                    if r == rng.min_row and c != rng.min_col:
                        rows[r - 1][c - 1] = None
        grids.append((ws.title, rows))
    return grids


def _column_candidates(rows):
    """对一个表格的每一列打分，返回 [(score, names, header_hit)]。"""
    if not rows:
        return []
    ncol = max(len(r) for r in rows)
    out = []
    for c in range(ncol):
        col = [r[c] if c < len(r) else None for r in rows]
        header_row = None
        for i, v in enumerate(col[:10]):
            if _is_header(v):
                header_row = i
                break
        body = col[header_row + 1:] if header_row is not None else col
        non_empty = [v for v in body if v not in (None, "")]
        names = [clean_cell(v) for v in non_empty]
        names = [n for n in names if n]
        if not names:
            continue
        frac = len(names) / max(1, len(non_empty))
        score = len(names) * (0.3 + frac) + (1000 if header_row is not None and len(names) >= 1 else 0)
        out.append((score, names, header_row is not None, frac))
    return out


def parse_names_detail(filepath):
    """返回 (names, info, err, dups)。dups = {姓名: 在表里出现的次数}（只列出现 ≥2 次的）。"""
    try:
        grids = _read_grids(filepath)
    except Exception as e:
        return None, "", str(e), {}
    best = None
    for sheet, rows in grids:
        for score, names, hdr, frac in _column_candidates(rows):
            # 不带表头且像姓名的比例太低的列（如混杂的备注列）不要
            if not hdr and (frac < 0.5 or len(names) < 2):
                continue
            if best is None or score > best[0]:
                best = (score, names, sheet, hdr)
    if not best:
        return [], "", None, {}
    names, seen, counts = [], set(), {}
    for n in best[1]:
        counts[n] = counts.get(n, 0) + 1
        if n not in seen:
            seen.add(n)
            names.append(n)
    dups = {n: c for n, c in counts.items() if c > 1}
    info = f"工作表「{best[2]}」" + ("（按“姓名”表头识别）" if best[3] else "（按姓名特征自动识别）")
    return names, info, None, dups


def parse_names(filepath):
    """返回 (names, info, err)。info 说明从哪个工作表识别。"""
    names, info, err, _ = parse_names_detail(filepath)
    return names, info, err


def disambiguate(name, existing):
    """同班重名时给出带区分标记的新名字：张伟 -> 张伟(2)、张伟(3)…"""
    existing = set(existing)
    i = 2
    while f"{name}({i})" in existing:
        i += 1
    return f"{name}({i})"


def merge_names(current, imported):
    """合并追加：保留现有顺序，新名字追加在末尾。"""
    seen = set(current)
    return list(current) + [n for n in imported if n not in seen and not seen.add(n)]
