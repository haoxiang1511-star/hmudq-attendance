# -*- coding: utf-8 -*-
"""
24示例3班点名助手 (Flet App)
作者：Antigravity
"""
import os
import sys
import re
import sqlite3
import datetime
import openpyxl
from openpyxl.styles import Font
import flet as ft
import collections
from name_import import parse_names, merge_names

try:
    from pypinyin import lazy_pinyin, Style as _PyStyle
except Exception:  # 打包缺依赖时降级：只支持汉字搜索
    lazy_pinyin = None

_pinyin_cache = {}

def get_pinyin_initials(name):
    """返回姓名拼音首字母（如 赵甲 -> zky），任何班级通用；pypinyin 不可用时返回空串。"""
    if name in _pinyin_cache:
        return _pinyin_cache[name]
    code = ""
    if lazy_pinyin is not None:
        try:
            code = "".join(lazy_pinyin(name, style=_PyStyle.FIRST_LETTER, errors="ignore")).lower()
        except Exception:
            code = ""
    _pinyin_cache[name] = code
    return code

# 双向日志记录器，保存打印与错误日志用于 UI 导出
class DualLogger:
    def __init__(self, original_stream):
        self.terminal = original_stream if (original_stream and hasattr(original_stream, "write")) else None
        self.logs = collections.deque(maxlen=1000)
        
    def write(self, message):
        try:
            if message:
                if self.terminal:
                    try:
                        self.terminal.write(message)
                    except Exception:
                        pass
                
                # 安全进行类型转换与日志存储，避免异常导致递归崩溃
                now_str = datetime.datetime.now().strftime("%m-%d %H:%M:%S")
                if isinstance(message, bytes):
                    msg_str = message.decode("utf-8", errors="ignore")
                else:
                    msg_str = str(message)
                
                lines = msg_str.splitlines()
                for line in lines:
                    if line.strip():
                        self.logs.append(f"[{now_str}] {line}")
        except Exception:
            # 绝对不要再次抛出异常，防止 sys.stderr 递归崩溃
            pass
                    
    def flush(self):
        if self.terminal and hasattr(self.terminal, "flush"):
            try:
                self.terminal.flush()
            except Exception:
                pass

# 初始化全局 DualLogger
stdout_logger = DualLogger(sys.stdout)
stderr_logger = DualLogger(sys.stderr)
stderr_logger.logs = stdout_logger.logs  # 共享同一个日志列表

sys.stdout = stdout_logger
sys.stderr = stderr_logger

def get_app_logs():
    return "\n".join(stdout_logger.logs)

# 动态计算数据库存储路径
def get_db_path():
    # 检测是否在 Android 手机上运行
    if os.environ.get("ANDROID_DATA") or os.path.exists("/system/bin/app_process"):
        # 写入手机端 App 私有持久化文件空间 (e.g. /data/user/0/package/files)
        # 该目录是可读写的，且在应用升级/重新提取代码时不会被清理
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__)) if __file__ else "."
            # 往上跳两级：/files/flet/app -> /files
            target_dir = os.path.abspath(os.path.join(base_dir, "..", ".."))
            test_path = os.path.join(target_dir, "test_write.tmp")
            with open(test_path, "w") as f:
                f.write("test")
            os.remove(test_path)
            return os.path.join(target_dir, "attendance.db")
        except Exception:
            # 如果上一级或上两级目录不可写，退而求其次使用代码同级目录
            base_dir = os.path.dirname(os.path.abspath(__file__)) if __file__ else "."
            return os.path.join(base_dir, "attendance.db")
    else:
        # PC 端运行调试，写入代码同级目录下，方便可视化观察
        base_dir = os.path.dirname(os.path.abspath(__file__)) if __file__ else "."
        return os.path.join(base_dir, "attendance.db")


DB_PATH = get_db_path()

# 45名在读学生默认固定顺序（与自动化程序对齐，用于首次运行初始化 Seed）
DEFAULT_STUDENTS_ORDER = [
    "赵甲", "赵乙", "赵丙", "赵丁", "赵戊", "钱甲", "钱乙", "钱丙", "钱丁", "钱戊",
    "孙甲", "孙乙", "孙丙", "孙丁", "孙戊", "李甲", "李乙", "李丙", "李丁", "李戊",
    "周甲", "周乙", "周丙", "周丁", "周戊", "吴甲", "吴乙", "吴丙", "吴丁", "吴戊",
    "郑甲", "郑乙", "郑丙", "郑丁", "郑戊", "王甲", "王乙", "王丙", "王丁", "王戊",
    "冯甲", "冯乙", "冯丙", "冯丁", "冯戊"
]

# 课节名称
PERIOD_LABELS = ["1-2节", "3-4节", "5-6节", "7-8节", "9-10节"]

# 状态样式定义 (键: (颜色, 背景色))
STATUS_STYLES = {
    "出勤": ("#10b981", "#1a10b981"),
    "旷课": ("#ef4444", "#1aef4444"),
    "病假": ("#ea580c", "#1aea580c"),
    "事假": ("#d97706", "#1ad97706"),
    "空课": ("#6b7280", "#1a6b7280")
}

# ----------------- 1. 数据库逻辑 -----------------

_db_conn = None

def get_db_connection():
    global _db_conn
    if _db_conn is None:
        # SQLite 连接复用，并开启 WAL 模式以实现极速亚毫秒级读写
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db_conn.execute("PRAGMA journal_mode=WAL;")
        _db_conn.execute("PRAGMA synchronous=NORMAL;")
        # 开启外键约束，删除学生时级联删除其考勤记录（与删除确认弹窗的提示一致）
        _db_conn.execute("PRAGMA foreign_keys=ON;")
    return _db_conn

DEFAULT_CLASS_NAME = "我的班级"

# 动态加载数据库名册排序
STUDENTS_ORDER = []

def load_students_order():
    global STUDENTS_ORDER
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM students ORDER BY sort_order ASC")
    STUDENTS_ORDER = [row[0] for row in cursor.fetchall()]

# 更新数据库名册的事务函数
def update_db_roster(new_names):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. 获取数据库中当前的名单
    cursor.execute("SELECT name FROM students")
    db_names = {row[0] for row in cursor.fetchall()}
    
    new_names_set = set(new_names)
    
    # 2. 删除不存在于新名单中的学生
    deleted_names = db_names - new_names_set
    for name in deleted_names:
        cursor.execute("DELETE FROM students WHERE name = ?", (name,))
        
    # 3. 插入新学生
    added_names = new_names_set - db_names
    for name in added_names:
        # 记录入班日期：导出时入班前的日期留空，不会被伪造成“出勤/空课”
        cursor.execute("INSERT INTO students (name, sort_order, joined_date) VALUES (?, 0, ?)",
                       (name, datetime.date.today().isoformat()))
        
    # 4. 更新所有人的排序 sort_order
    for idx, name in enumerate(new_names):
        cursor.execute("UPDATE students SET sort_order = ? WHERE name = ?", (idx, name))
        
    conn.commit()

# 兼容度极高的一键导入名册逻辑（规则见 name_import.py：多工作表、合并单元格、表头识别、姓名特征打分、CSV）
def parse_names_from_excel(filepath):
    names, info, err = parse_names(filepath)
    if err:
        return None, err
    return names, None

def db_set_config(key, value):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()

def db_get_config(key, default=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT value FROM app_config WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row:
            val = row[0]
            if val == "True":
                return True
            elif val == "False":
                return False
            return val
    except sqlite3.OperationalError:
        pass
    return default

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 配置信息表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS app_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    conn.commit()
    
    # 学生信息表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        sort_order INTEGER DEFAULT 0,
        joined_date TEXT
    )
    """)
    
    # 尝试平滑添加 sort_order 字段，以防已有数据库升级报错
    try:
        cursor.execute("ALTER TABLE students ADD COLUMN sort_order INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # 迁移：入班日期字段（v1.3.0 新增；旧学生为 NULL，视为一直在班）
    try:
        cursor.execute("ALTER TABLE students ADD COLUMN joined_date TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # 迁移：移除不再需要的 dorm 和 is_leader 字段（旧版数据库残留）
    for col in ['dorm', 'is_leader']:
        try:
            cursor.execute(f"ALTER TABLE students DROP COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # 列不存在或 SQLite 版本过旧，忽略

    # 如果数据库中的最大 sort_order 为 0（即为旧版升级或初始状态），按默认出勤表顺序初始化 sort_order
    cursor.execute("SELECT MAX(sort_order) FROM students")
    max_so = cursor.fetchone()[0]
    if max_so is None or max_so == 0:
        for idx, student_name in enumerate(DEFAULT_STUDENTS_ORDER):
            cursor.execute("UPDATE students SET sort_order = ? WHERE name = ?", (idx, student_name))
        conn.commit()
        
    # 考勤记录表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        date TEXT NOT NULL,         -- 格式: YYYY-MM-DD
        period INTEGER NOT NULL,    -- 0-4
        status TEXT NOT NULL,       -- 出勤, 旷课, 病假, 事假, 空课
        FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
        UNIQUE(student_id, date, period)
    )
    """)
    
    # 建立组合索引，提升大数额下亚毫秒级读取性能
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_records_date_period ON records(date, period)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_records_student_date ON records(student_id, date)")
    
    # v1.3.0：新装不再预置任何班级名单（推广到其他班级），名单为空时界面提示先导入。
    # 已有数据的老用户不受影响（不删除、不改动现有学生）。

# 亚毫秒级获取单节课考勤数据
def get_records(date_str, period):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT s.name, r.status 
        FROM records r 
        JOIN students s ON r.student_id = s.id 
        WHERE r.date = ? AND r.period = ?
    """, (date_str, period))
    res = cursor.fetchall()
    return {name: status for name, status in res}

# 保存单条考勤数据到本地数据库
def save_record(date_str, period, name, status):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM students WHERE name = ?", (name,))
    row = cursor.fetchone()
    if row is None:  # 名单里已不存在该学生（例如刚被删除），跳过而不是闪退
        print(f"[save_record] 名单中找不到 {name}，已跳过")
        return
    student_id = row[0]
    cursor.execute("""
        INSERT INTO records (student_id, date, period, status) 
        VALUES (?, ?, ?, ?)
        ON CONFLICT(student_id, date, period) DO UPDATE SET status=excluded.status
    """, (student_id, date_str, period, status))
    conn.commit()

# 批量更新当前页状态
def bulk_save_records(date_str, period, name_status_dict):
    conn = get_db_connection()
    cursor = conn.cursor()
    for name, status in name_status_dict.items():
        cursor.execute("SELECT id FROM students WHERE name = ?", (name,))
        row = cursor.fetchone()
        if row is None:
            print(f"[bulk_save_records] 名单中找不到 {name}，已跳过")
            continue
        student_id = row[0]
        cursor.execute("""
            INSERT INTO records (student_id, date, period, status) 
            VALUES (?, ?, ?, ?)
            ON CONFLICT(student_id, date, period) DO UPDATE SET status=excluded.status
        """, (student_id, date_str, period, status))
    conn.commit()

# 获取所有包含有效考勤记录的月份 (非空课)
def get_available_months():
    conn = get_db_connection()
    cursor = conn.cursor()
    # 查找所有存在非“空课”记录的月份 ( substr(date, 1, 7) 提取 YYYY-MM )
    cursor.execute("""
        SELECT DISTINCT substr(r.date, 1, 7)
        FROM records r
        WHERE r.status != '空课'
    """)
    rows = cursor.fetchall()
    return sorted([row[0] for row in rows])

# 导出指定月份 SQLite 数据为 xlsx 的算法
def export_db_to_excel(target_ym, class_name=None):
    class_name = class_name or DEFAULT_CLASS_NAME
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT r.date, s.name, r.period, r.status
        FROM records r
        JOIN students s ON r.student_id = s.id
        WHERE substr(r.date, 1, 7) = ?
    """, (target_ym,))
    raw_records = cursor.fetchall()
    
    if not raw_records:
        return None, f"本地 SQLite 数据库中暂无 {target_ym} 的考勤记录"

    # 分类整理数据：date -> period -> name -> status
    from collections import defaultdict
    grouped = defaultdict(lambda: defaultdict(dict))
    for date_str, name, period, status in raw_records:
        grouped[date_str][period][name] = status

    # 过滤掉“全天均为 空课”的日期
    filtered_grouped = {}
    for date_str, period_data in grouped.items():
        has_active_attendance = False
        for period, student_data in period_data.items():
            for student_name, status in student_data.items():
                if status != "空课":
                    has_active_attendance = True
                    break
            if has_active_attendance:
                break
        if has_active_attendance:
            filtered_grouped[date_str] = period_data

    if not filtered_grouped:
        return None, f"该月份 {target_ym} 内暂无任何有效的非‘空课’考勤记录"

    sorted_dates = sorted(filtered_grouped.keys())
    # 名单直接从数据库读取（不依赖全局缓存），并带上入班日期
    cursor.execute("SELECT name, joined_date FROM students ORDER BY sort_order ASC, id ASC")
    roster = cursor.fetchall()
    try:
        m = int(target_ym.split('-')[1])
        month_label = f"{m}月"
    except Exception:
        month_label = target_ym
    
    # 动态匹配班级号（例如从“24示例3班”提取出“3”；“三班”提取出“三”；默认缺省“三”）
    import re
    match = re.search(r"([0-9一二三四五六七八九十]+)\s*班", class_name)
    if match:
        filename = f"{month_label}{match.group(1)}班上课出勤.xlsx"
    else:
        safe = re.sub(r'[\\/:*?"<>|\s]+', "", class_name) or "班级"
        filename = f"{month_label}{safe}上课出勤.xlsx"

    # 生成 Excel 表格，采用符合 quant_helper 规则的排版布局
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"

    # 计算大行块数（3天为一大横组，共占用21列A-U列，每组高度为 名单人数+1 行）
    for d_idx, date_str in enumerate(sorted_dates):
        # 将 '2026-06-05' 转换为 '06月05日' 等解析可配格式
        try:
            dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            formatted_date = f"{dt.month:02d}月{dt.day:02d}日"
        except Exception:
            formatted_date = date_str

        block_v = d_idx // 3
        block_h = d_idx % 3
        start_row = block_v * (len(roster) + 1) + 1  # 块高随名单人数伸缩（原写死46行=45人，超过45人会被下一块覆盖）
        start_col = block_h * 7 + 1

        # 填充表头行
        ws.cell(row=start_row, column=start_col, value="姓名")
        ws.cell(row=start_row, column=start_col + 1, value=formatted_date)
        for p in range(5):
            ws.cell(row=start_row, column=start_col + 2 + p, value=PERIOD_LABELS[p])

        # 填充规则（与点名页一致）：
        #   1) 数据库有记录 -> 原样写出；
        #   2) 该日期早于学生入班日期且无记录 -> 留空（不伪造状态）；
        #   3) 该节课有人被记录过（说明点过名）但此人没记录 -> 出勤（点名页对未记录者默认显示“出勤”）；
        #   4) 该节课完全没有记录 -> 空课。
        day_data = filtered_grouped[date_str]
        for s_idx, (student_name, joined) in enumerate(roster):
            row = start_row + 1 + s_idx
            ws.cell(row=row, column=start_col, value=student_name)
            ws.cell(row=row, column=start_col + 1, value=None)

            for p in range(5):
                period_data = day_data.get(p, {})
                if student_name in period_data:
                    status = period_data[student_name]
                elif joined and date_str < joined:
                    status = None
                elif period_data:
                    status = "出勤"
                else:
                    status = "空课"
                cell = ws.cell(row=row, column=start_col + 2 + p, value=status)
                if status in ["旷课", "病假", "事假"]:
                    cell.font = Font(color="FF0000")

    # 写到数据库所在的可写目录（手机上为 App 私有目录），不再依赖当前工作目录
    out_path = os.path.join(os.path.dirname(DB_PATH), filename)
    wb.save(out_path)
    return out_path, None

# ----------------- 1.5 辅助布局函数 -----------------
def chunk_controls(controls, chunk_size=2):
    rows = []
    for i in range(0, len(controls), chunk_size):
        chunk = controls[i:i+chunk_size]
        for c in chunk:
            c.expand = True
        if len(chunk) < chunk_size:
            # 添加空白 Container 填充，保持两列宽度对齐
            chunk.append(ft.Container(expand=True))
        rows.append(ft.Row(controls=chunk, spacing=8))
    return rows


# ----------------- 2. Flet 主应用 -----------------

def main(page: ft.Page):
    page.title = "点名助手"
    page.theme = ft.Theme(font_family="Microsoft YaHei, PingFang SC, Segoe UI, sans-serif")
    page.theme_mode = ft.ThemeMode.LIGHT
    page.window_width = 460
    page.window_height = 820
    page.window_resizable = True
    page.padding = ft.Padding(top=45, left=15, right=15, bottom=15)

    # 初始化本地数据库
    init_db()
    load_students_order()

    # 注册系统原生分享服务
    share_service = ft.Share()
    page.services.append(share_service)

    # 状态变量
    current_date = datetime.date.today().strftime("%Y-%m-%d")
    current_period = 0 # 0-4
    show_anomaly_only = False
    search_query = ""
    
    # 考勤状态字典缓存 {name: status}
    attendance_cache = {}
    
    # 控件引用映射 {name: {"container": Container, "status_text": Text, "status_badge": Container}}
    student_card_map = {}

    # 自动推断当前课节
    now_hour = datetime.datetime.now().hour
    if 10 <= now_hour < 12:
        current_period = 1
    elif 12 <= now_hour < 15:
        current_period = 2
    elif 15 <= now_hour < 18:
        current_period = 3
    elif now_hour >= 18:
        current_period = 4



    # 侧边栏/对话框 - 导出的文件成功 Snackbar 提示
    def show_snackbar(text, is_error=False):
        page.show_dialog(
            ft.SnackBar(
                content=ft.Text(text, color=ft.Colors.WHITE),
                bgcolor=ft.Colors.RED_500 if is_error else ft.Colors.GREEN_600
            )
        )

    try:
        dt = datetime.datetime.strptime(current_date, "%Y-%m-%d")
        date_display_init = f"{dt.month:02d}-{dt.day:02d}"
    except Exception:
        date_display_init = f"{current_date}"

    date_text = ft.Text(date_display_init, color=ft.Colors.BLUE_800, weight=ft.FontWeight.BOLD, size=15)

    # 日期选择器逻辑
    def on_date_change(e):
        nonlocal current_date
        val = date_picker.value
        if val:
            if val.tzinfo is None:
                # Flet DatePicker naive datetime values should be localized to UTC first
                val = val.replace(tzinfo=datetime.timezone.utc)
            current_date = val.astimezone().strftime("%Y-%m-%d")
        try:
            dt = datetime.datetime.strptime(current_date, "%Y-%m-%d")
            date_display = f"{dt.month:02d}-{dt.day:02d}"
        except Exception:
            date_display = f"{current_date}"
        date_text.value = date_display
        date_btn.update()
        load_attendance_from_db()
        rebuild_roster_ui()
        page.update()

    date_picker = ft.DatePicker(
        on_change=on_date_change,
        first_date=datetime.datetime(2025, 1, 1),
        last_date=datetime.datetime(2030, 12, 31)
    )
    page.overlay.append(date_picker)

    date_btn = ft.Container(
        content=ft.Row([
            ft.Icon(ft.Icons.CALENDAR_MONTH, color=ft.Colors.BLUE_800, size=18),
            date_text
        ], alignment=ft.MainAxisAlignment.CENTER, spacing=5),
        on_click=lambda _: page.show_dialog(date_picker),
        bgcolor=ft.Colors.GREY_100,
        border=ft.Border.all(1, ft.Colors.GREY_300),
        border_radius=8,
        height=45,
        padding=ft.Padding.symmetric(horizontal=12),
        alignment=ft.Alignment.CENTER
    )

    # 课节选择器下拉框
    def on_period_change(e):
        nonlocal current_period
        current_period = int(period_dropdown.value)
        load_attendance_from_db()
        rebuild_roster_ui()
        page.update()

    period_dropdown = ft.Dropdown(
        value=str(current_period),
        options=[ft.dropdown.Option(str(i), label) for i, label in enumerate(PERIOD_LABELS)],
        on_select=on_period_change,
        width=110,
        height=45,
        dense=True,
        border=ft.InputBorder.NONE,
        color=ft.Colors.BLUE_800,
        text_style=ft.TextStyle(weight=ft.FontWeight.BOLD, size=15),
        # 上下内边距对称，使文字与左侧日期按钮处于同一水平中线
        content_padding=ft.Padding(10, 0, 0, 0)
    )

    dropdown_container = ft.Container(
        content=period_dropdown,
        bgcolor=ft.Colors.GREY_100,
        border=ft.Border.all(1, ft.Colors.GREY_300),
        border_radius=8,
        height=45,
        width=110,
        alignment=ft.Alignment.CENTER
    )

    # 搜索框与状态过滤
    def on_search_change(e):
        nonlocal search_query
        search_query = search_input.value.strip().lower()
        rebuild_roster_ui()
        page.update()

    search_input = ft.TextField(
        hint_text="搜索姓名/拼音首字母...",
        on_change=on_search_change,
        prefix_icon=ft.Icons.SEARCH,
        height=40,
        content_padding=10,
        expand=True,
        bgcolor=ft.Colors.GREY_100,
        border_radius=8,
        border_color=ft.Colors.GREY_300,
        color=ft.Colors.BLACK
    )

    # 异常过滤器状态
    def on_filter_toggle(e):
        nonlocal show_anomaly_only
        show_anomaly_only = filter_switch.value
        rebuild_roster_ui()
        page.update()

    filter_switch = ft.Switch(
        label="只看异常",
        value=show_anomaly_only,
        on_change=on_filter_toggle,
        active_color=ft.Colors.AMBER_600
    )

    # 统计数据文本已删除

    # 考勤列表滚动容器
    roster_column = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True, spacing=8)

    # ----------------- 4. 业务逻辑与重绘 -----------------

    # 载入数据并入缓存
    def load_attendance_from_db():
        nonlocal attendance_cache
        db_records = get_records(current_date, current_period)
        
        default_status = "空课"
        
        attendance_cache = {}
        for name in STUDENTS_ORDER:
            attendance_cache[name] = db_records.get(name, default_status)

    # 刷新状态统计
    def refresh_stats_ui():
        pass

    # 在不重绘整个页面（不导致滚动条回弹）的情况下更新单个学生卡片状态
    def update_student_card_state(name, new_status):
        if name not in student_card_map:
            return
        card_info = student_card_map[name]
        card_container = card_info["container"]
        status_text = card_info["status_text"]
        status_badge = card_info["status_badge"]

        # 更新状态文本值
        status_text.value = new_status

        # 获取样式颜色与背景微光
        color_hex, bg_color = STATUS_STYLES.get(new_status, ("#6b7280", "rgba(107, 114, 128, 0.1)"))

        # 更新徽章样式
        status_badge.border = ft.Border.all(1, color_hex)
        status_badge.bgcolor = bg_color
        status_text.color = color_hex

        # 根据状态更新卡片的边框、阴影和背景色
        if new_status == "出勤":
            card_border = ft.Border.all(2, ft.Colors.GREY_300)
            card_shadow = ft.BoxShadow(spread_radius=1, blur_radius=4, color=ft.Colors.BLACK12)
            card_bg = ft.Colors.WHITE
        elif new_status == "空课":
            card_border = ft.Border.all(2, ft.Colors.GREY_200)
            card_shadow = None
            card_bg = "#f9fafb"
        elif new_status == "旷课":
            card_border = ft.Border.all(2, color_hex)
            card_shadow = ft.BoxShadow(spread_radius=1, blur_radius=8, color="#1fef4444")
            card_bg = "#fef2f2"
        elif new_status == "病假":
            card_border = ft.Border.all(2, color_hex)
            card_shadow = ft.BoxShadow(spread_radius=1, blur_radius=8, color="#1fea580c")
            card_bg = "#fff7ed"
        elif new_status == "事假":
            card_border = ft.Border.all(2, color_hex)
            card_shadow = ft.BoxShadow(spread_radius=1, blur_radius=8, color="#1fd97706")
            card_bg = "#fefce8"

        card_container.border = card_border
        card_container.shadow = card_shadow
        card_container.bgcolor = card_bg

        # 增量式局部刷新控制树
        card_container.update()

    # 快速切换 出勤 / 旷课
    def toggle_attendance_fast(name):
        curr = attendance_cache.get(name, "出勤")
        next_status = "旷课" if curr == "出勤" else "出勤"
        
        save_record(current_date, current_period, name, next_status)
        attendance_cache[name] = next_status
        
        refresh_stats_ui()
        
        if show_anomaly_only:
            rebuild_roster_ui()
            page.update()
        else:
            update_student_card_state(name, next_status)

    # 弹出状态选择对话框
    def show_status_selector(name):
        curr_status = attendance_cache.get(name, "出勤")
        
        def select_status(status):
            save_record(current_date, current_period, name, status)
            attendance_cache[name] = status
            refresh_stats_ui()
            page.pop_dialog()  # 关闭选择器弹窗
            if show_anomaly_only:
                rebuild_roster_ui()
                page.update()
            else:
                update_student_card_state(name, status)

        # 5个考勤状态的选项按钮，配合白底色采用高对比度彩色文字与微光背景
        status_buttons = []
        for st, (color_hex, bg_color) in STATUS_STYLES.items():
            is_selected = (st == curr_status)
            status_buttons.append(
                ft.Container(
                    content=ft.Row([
                        ft.Icon(
                            icon=ft.Icons.CHECK if is_selected else ft.Icons.RADIO_BUTTON_UNCHECKED,
                            color=color_hex,
                            size=20
                        ),
                        ft.Text(st, size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK)
                    ], spacing=10),
                    bgcolor=bg_color if is_selected else ft.Colors.TRANSPARENT,
                    border=ft.Border.all(2, color_hex) if is_selected else ft.Border.all(1, ft.Colors.GREY_300),
                    border_radius=8,
                    padding=ft.Padding.symmetric(horizontal=15, vertical=10),
                    on_click=lambda _, s=st: select_status(s),
                    alignment=ft.Alignment.CENTER_LEFT
                )
            )

        dlg = ft.AlertDialog(
            title=ft.Text(f"选择考勤状态：{name}", size=18, weight=ft.FontWeight.BOLD),
            content=ft.Column(
                status_buttons,
                spacing=8,
                tight=True,
                width=280
            ),
            actions=[
                ft.TextButton(content="取消", on_click=lambda _: page.pop_dialog())
            ],
            actions_alignment=ft.MainAxisAlignment.END
        )
        page.show_dialog(dlg)

    # 重新构建考勤网格
    def rebuild_roster_ui():
        roster_column.controls.clear()
        student_card_map.clear()
        
        # 读取干部和名单结构
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM students")
        all_students_db = cursor.fetchall()

        student_names = {row[0] for row in all_students_db}

        # 名单为空（新装）：显示导入提示
        if not all_students_db:
            def go_import(e):
                page.navigation_bar.selected_index = 1
                main_body.content = roster_manage_view
                page.update()
            roster_column.controls.append(
                ft.Container(
                    content=ft.Column([
                        ft.Icon(ft.Icons.GROUP_ADD_ROUNDED, size=56, color=ft.Colors.BLUE_800),
                        ft.Text("还没有班级名单", size=18, weight=ft.FontWeight.BOLD),
                        ft.Text("请先到「名单管理」导入本班名单（支持 Excel / 粘贴姓名），导入后即可开始点名。",
                                size=13, color=ft.Colors.GREY_700, text_align=ft.TextAlign.CENTER),
                        ft.FilledButton("去导入名单", icon=ft.Icons.UPLOAD_FILE_ROUNDED, on_click=go_import),
                    ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=12),
                    alignment=ft.Alignment.CENTER,
                    padding=40,
                )
            )
            page.update()
            return

        # Helper to create a card container for a student name
        def build_card(name):
            status = attendance_cache.get(name, "出勤")
            color_hex, bg_color = STATUS_STYLES.get(status, ("#6b7280", "rgba(107, 114, 128, 0.1)"))
            
            status_text = ft.Text(status, size=11, color=color_hex, weight=ft.FontWeight.BOLD)
            status_badge = ft.Container(
                content=status_text,
                border=ft.Border.all(1, color_hex),
                border_radius=6,
                padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                bgcolor=bg_color
            )
            
            card_row = ft.Row(
                [
                    ft.Text(name, size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK),
                    ft.GestureDetector(
                        content=status_badge,
                        on_tap=lambda _, n=name: show_status_selector(n)
                    )
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN
            )

            if status == "出勤":
                card_border = ft.Border.all(2, ft.Colors.GREY_300)
                card_shadow = ft.BoxShadow(spread_radius=1, blur_radius=4, color=ft.Colors.BLACK12)
                card_bg = ft.Colors.WHITE
            elif status == "空课":
                card_border = ft.Border.all(2, ft.Colors.GREY_200)
                card_shadow = None
                card_bg = "#f9fafb"
            elif status == "旷课":
                card_border = ft.Border.all(2, color_hex)
                card_shadow = ft.BoxShadow(spread_radius=1, blur_radius=8, color="#1fef4444")
                card_bg = "#fef2f2"
            elif status == "病假":
                card_border = ft.Border.all(2, color_hex)
                card_shadow = ft.BoxShadow(spread_radius=1, blur_radius=8, color="#1fea580c")
                card_bg = "#fff7ed"
            elif status == "事假":
                card_border = ft.Border.all(2, color_hex)
                card_shadow = ft.BoxShadow(spread_radius=1, blur_radius=8, color="#1fd97706")
                card_bg = "#fefce8"

            card_container = ft.Container(
                content=card_row,
                padding=ft.Padding.all(12),
                border=card_border,
                border_radius=10,
                bgcolor=card_bg,
                on_click=lambda _, n=name: toggle_attendance_fast(n),
                on_long_press=lambda _, n=name: show_status_selector(n),
                shadow=card_shadow
            )
            
            student_card_map[name] = {
                "container": card_container,
                "status_text": status_text,
                "status_badge": status_badge
            }
            return card_container

        # 筛选符合条件的学生姓名
        matched_names = []
        for name in STUDENTS_ORDER:
            if name not in student_names:
                continue
            
            status = attendance_cache.get(name, "出勤")
            
            # 1. 异常考勤过滤
            if show_anomaly_only and status == "出勤":
                continue
                
            # 2. 搜索检索过滤
            if search_query and (search_query not in name.lower()):
                from_pinyin = search_query in get_pinyin_initials(name)
                if not from_pinyin:
                    continue
                    
            matched_names.append(name)

        if show_anomaly_only:
            # 区分：旷课(未到) 与 其他异常 (病假、事假、空课)
            kuangke_names = [n for n in matched_names if attendance_cache.get(n, "出勤") == "旷课"]
            other_names = [n for n in matched_names if attendance_cache.get(n, "出勤") != "旷课"]
            
            if not kuangke_names and not other_names:
                roster_column.controls.append(
                    ft.Container(
                        content=ft.Text("没有找到匹配的异常记录", color=ft.Colors.GREY_500, size=13),
                        alignment=ft.Alignment.CENTER,
                        padding=50
                    )
                )
            else:
                if kuangke_names:
                    # 未到学生标题
                    roster_column.controls.append(
                        ft.Container(
                            content=ft.Row([
                                ft.Icon(ft.Icons.ERROR_OUTLINE_ROUNDED, color=ft.Colors.RED_600, size=18),
                                ft.Text("未到学生 (旷课)", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.RED_600)
                            ], spacing=5),
                            padding=ft.Padding.only(top=5, bottom=5)
                        )
                    )
                    kuangke_controls = [build_card(name) for name in kuangke_names]
                    roster_column.controls.extend(chunk_controls(kuangke_controls))
                    
                if kuangke_names and other_names:
                    # 分割隔离线，增加视觉间距
                    roster_column.controls.append(
                        ft.Container(
                            height=25,
                            content=ft.Divider(color=ft.Colors.GREY_300, height=1, thickness=1),
                            alignment=ft.Alignment.CENTER,
                            padding=ft.Padding.symmetric(vertical=5)
                        )
                    )
                    
                if other_names:
                    # 其他异常标题
                    roster_column.controls.append(
                        ft.Container(
                            content=ft.Row([
                                ft.Icon(ft.Icons.INFO_OUTLINE_ROUNDED, color=ft.Colors.AMBER_700, size=18),
                                ft.Text("其他异常 (请假/空课)", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.AMBER_700)
                            ], spacing=5),
                            padding=ft.Padding.only(top=5, bottom=5)
                        )
                    )
                    other_controls = [build_card(name) for name in other_names]
                    roster_column.controls.extend(chunk_controls(other_controls))
        else:
            if matched_names:
                grid_controls = [build_card(name) for name in matched_names]
                roster_column.controls.extend(chunk_controls(grid_controls))
            else:
                roster_column.controls.append(
                    ft.Container(
                        content=ft.Text("没有找到匹配的学生记录", color=ft.Colors.GREY_500, size=13),
                        alignment=ft.Alignment.CENTER,
                        padding=50
                    )
                )
        
        refresh_stats_ui()

    # ----------------- 5. 全局操作事件 -----------------

    # 一键全员出勤
    def on_all_present(e):
        bulk = {name: "出勤" for name in STUDENTS_ORDER}
        bulk_save_records(current_date, current_period, bulk)
        load_attendance_from_db()
        rebuild_roster_ui()
        page.update()
        show_snackbar("已一键标记全员为：出勤")

    # 一键全员空课
    def on_all_noclass(e):
        bulk = {name: "空课" for name in STUDENTS_ORDER}
        bulk_save_records(current_date, current_period, bulk)
        load_attendance_from_db()
        rebuild_roster_ui()
        page.update()
        show_snackbar("已一键标记全员为：空课")

    def on_copy_summary(e):
        # 1. 格式化第一行，例如：6.3 24示例3班 3-4
        try:
            dt = datetime.datetime.strptime(current_date, "%Y-%m-%d")
            date_formatted = f"{dt.month}.{dt.day}"
        except Exception:
            date_formatted = current_date
            
        class_name = db_get_config("class_name") or DEFAULT_CLASS_NAME
        period_formatted = PERIOD_LABELS[current_period].replace("节", "")
        header_line = f"{date_formatted} {class_name} {period_formatted}"
        
        # 2. 统计名单
        kuangke_list = [name for name, status in attendance_cache.items() if status == "旷课"]
        bingjia_list = [name for name, status in attendance_cache.items() if status == "病假"]
        shijia_list = [name for name, status in attendance_cache.items() if status == "事假"]
        
        lines = [header_line]
        
        if not kuangke_list and not bingjia_list and not shijia_list:
            lines.append("全")
        else:
            if kuangke_list:
                lines.append(f"未到：{' '.join(kuangke_list)}")
            if bingjia_list:
                lines.append(f"病假：{' '.join(bingjia_list)}")
            if shijia_list:
                lines.append(f"事假：{' '.join(shijia_list)}")
            
        summary_text = "\n".join(lines)
        
        # 写入系统剪贴板
        async def set_clip():
            await ft.Clipboard().set(summary_text)
        page.run_task(set_clip)
        show_snackbar("已按微信格式复制考勤汇总到剪贴板！")

    def on_export_excel(e):
        # 获取所有包含有效考勤记录的月份
        available_months = get_available_months()
        if not available_months:
            show_snackbar("本地 SQLite 数据库中暂无任何有效的考勤记录", is_error=True)
            return

        class_name = db_get_config("class_name") or DEFAULT_CLASS_NAME

        def execute_export(ym):
            filename, err = export_db_to_excel(ym, class_name)
            if err:
                show_snackbar(f"导出失败：{err}", is_error=True)
            else:
                # 兼容字符串与 PagePlatform Enum 类型的移动端平台判定
                is_mobile = page.platform in ["android", "ios", ft.PagePlatform.ANDROID, ft.PagePlatform.IOS]
                if is_mobile:
                    async def do_share():
                        try:
                            abs_path = os.path.abspath(filename)
                            # 使用 Flet 原生 Share 服务唤起系统分享面板
                            await share_service.share_files(
                                [ft.ShareFile.from_path(abs_path)],
                                title=f"{ym}考勤表"
                            )
                            show_snackbar("已成功唤起系统分享面板！")
                        except Exception as ex:
                            show_snackbar(f"唤起分享失败，文件已保存到本地：{ex}", is_error=True)
                    page.run_task(do_share)
                else:
                    show_snackbar(f"📊 已导出考勤 Excel：{filename}")

        if len(available_months) == 1:
            # 如果只有一个月的数据，无需选择，直接导出该月
            execute_export(available_months[0])
        else:
            # 弹出对话框供用户选择要导出的月份
            month_buttons = []
            for ym in available_months:
                try:
                    m = int(ym.split('-')[1])
                    month_label = f"{m}月"
                except Exception:
                    month_label = ym

                # 定义点击事件的处理器，显式传参绑定
                def create_click_handler(target_ym):
                    return lambda _: (page.pop_dialog(), execute_export(target_ym))

                month_buttons.append(
                    ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.CALENDAR_MONTH_ROUNDED, color=ft.Colors.BLUE_600, size=20),
                            ft.Text(f"导出 {month_label} 考勤表", size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK)
                        ], spacing=10),
                        border=ft.Border.all(1, ft.Colors.GREY_300),
                        border_radius=8,
                        padding=ft.Padding.symmetric(horizontal=15, vertical=12),
                        on_click=create_click_handler(ym),
                        alignment=ft.Alignment.CENTER_LEFT
                    )
                )

            dlg = ft.AlertDialog(
                title=ft.Text("选择要导出的月份", size=18, weight=ft.FontWeight.BOLD),
                content=ft.Column(
                    month_buttons,
                    spacing=8,
                    tight=True,
                    width=280
                ),
                actions=[
                    ft.TextButton(content="取消", on_click=lambda _: page.pop_dialog())
                ],
                actions_alignment=ft.MainAxisAlignment.END
            )
            page.show_dialog(dlg)

    # 导出 SQLite 数据为 xlsx 的算法已移至模块级别定义

    # ----------------- 6. 构建界面骨架 -----------------

    # 首行：日期与课节
    header_row = ft.Row(
        [
            date_btn,
            dropdown_container,
            ft.Row(
                [
                    ft.IconButton(
                        icon=ft.Icons.COPY_ALL,
                        tooltip="复制微信考勤汇总",
                        on_click=on_copy_summary,
                        icon_color=ft.Colors.BLUE_800,
                        bgcolor=ft.Colors.GREY_100,
                        width=45,
                        height=45
                    ),
                    ft.IconButton(
                        icon=ft.Icons.FILE_DOWNLOAD,
                        tooltip="导出考勤 Excel",
                        on_click=on_export_excel,
                        icon_color=ft.Colors.BLUE_800,
                        bgcolor=ft.Colors.GREY_100,
                        width=45,
                        height=45
                    )
                ],
                spacing=8
            )
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN
    )

    # 搜索和过滤开关
    search_row = ft.Row(
        [
            search_input,
            filter_switch
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        spacing=10
    )

    # 全局标记按钮
    global_buttons = ft.Row(
        [
            ft.TextButton(content="✓ 全员出勤", on_click=on_all_present, style=ft.ButtonStyle(color=ft.Colors.GREEN_600)),
            ft.TextButton(content="💤 全员空课", on_click=on_all_noclass, style=ft.ButtonStyle(color=ft.Colors.GREY_600))
        ],
        alignment=ft.MainAxisAlignment.START,
        spacing=20
    )



    # 组装进 Page 主容器
    # ----------------- 6. 名单管理页面 UI 与组件 -----------------
    
    current_editing_names = []

    def apply_imported_names(imported_names, mode):
        nonlocal current_editing_names
        if mode == "replace":
            current_editing_names = list(imported_names)
            msg = f"已用导入的 {len(imported_names)} 人替换编辑中的名单，请检查后点「保存并应用更改」"
        else:
            before = len(current_editing_names)
            current_editing_names = merge_names(current_editing_names, imported_names)
            msg = f"已合并追加 {len(current_editing_names) - before} 名新同学，请检查后点「保存并应用更改」"
        rebuild_manage_list_ui()
        show_snackbar(msg)

    def handle_imported_file(filepath):
        imported_names, info, err = parse_names(filepath)
        if err:
            show_snackbar(f"导入解析失败: {err}", is_error=True)
            return
        if not imported_names:
            show_snackbar("表格中未找到任何像姓名的内容，请检查文件", is_error=True)
            return
        new_cnt = len([n for n in imported_names if n not in set(current_editing_names)])
        preview = "、".join(imported_names[:8]) + (f" 等" if len(imported_names) > 8 else "")

        def do(mode):
            page.pop_dialog()
            apply_imported_names(imported_names, mode)

        dlg = ft.AlertDialog(
            title=ft.Text(f"识别到 {len(imported_names)} 个姓名"),
            content=ft.Text(
                f"来源：{info}\n预览：{preview}\n\n其中 {new_cnt} 人不在当前名单中。\n"
                f"「替换」会用导入结果覆盖编辑中的名单；「合并追加」只把新名字加到末尾。\n"
                f"两种方式都不会立即生效，需要再点「保存并应用更改」。"
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda _: page.pop_dialog()),
                ft.TextButton("合并追加", on_click=lambda _: do("merge")),
                ft.TextButton("替换", on_click=lambda _: do("replace"), style=ft.ButtonStyle(color=ft.Colors.RED_600)),
            ],
            actions_alignment=ft.MainAxisAlignment.END
        )
        page.show_dialog(dlg)

    file_picker = ft.FilePicker()
    page.services.append(file_picker)
    
    # 快速录入模块
    add_name_input = ft.TextField(
        hint_text="输入新成员姓名...",
        height=45,
        content_padding=10,
        expand=True,
        bgcolor=ft.Colors.GREY_100,
        border_radius=8,
        border_color=ft.Colors.GREY_300,
        color=ft.Colors.BLACK
    )
    
    def on_add_member(e):
        nonlocal current_editing_names
        name = add_name_input.value.strip().replace(" ", "")
        if not name:
            return
        if name in current_editing_names:
            show_snackbar(f"名单中已存在该姓名: {name}", is_error=True)
            return
        current_editing_names.append(name)
        add_name_input.value = ""
        rebuild_manage_list_ui()

    add_name_btn = ft.Button(
        content="添加",
        on_click=on_add_member,
        style=ft.ButtonStyle(
            color=ft.Colors.WHITE,
            bgcolor=ft.Colors.BLUE_600,
            shape=ft.RoundedRectangleBorder(radius=8)
        )
    )
    
    def save_roster_action():
        update_db_roster(current_editing_names)
        load_students_order()
        load_attendance_from_db()
        page.navigation_bar.selected_index = 0
        main_body.content = roll_call_view
        page.update()  # 先挂载点名页，再重建卡片，避免 Flet 重复渲染
        rebuild_roster_ui()
        show_snackbar("班级名单保存并应用成功！")
        page.update()

    def on_save_roster(e):
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 找出当前数据库的所有人
        cursor.execute("SELECT name FROM students")
        db_names = {row[0] for row in cursor.fetchall()}
        
        deleted_names = db_names - set(current_editing_names)
        
        # 统计其中有哪些学生存在考勤记录（status != '出勤' 或有记录）
        affected_count = 0
        affected_names = []
        for d_name in deleted_names:
            cursor.execute("SELECT id FROM students WHERE name = ?", (d_name,))
            res = cursor.fetchone()
            if res:
                s_id = res[0]
                cursor.execute("SELECT COUNT(*) FROM records WHERE student_id = ? AND status != '出勤'", (s_id,))
                cnt = cursor.fetchone()[0]
                if cnt > 0:
                    affected_count += cnt
                    affected_names.append(d_name)
                    
        # 如果存在有考勤异常记录的人被删除，则弹出二次确认预警 Dialog
        if affected_names:
            names_str = "、".join(affected_names[:3])
            if len(affected_names) > 3:
                names_str += f" 等 {len(affected_names)} 人"
            
            def confirm_save(e2):
                page.pop_dialog()  # 先关对话框，再保存（保存会弹 SnackBar）
                save_roster_action()
                
            dlg = ft.AlertDialog(
                title=ft.Text("⚠️ 级联删除警告"),
                content=ft.Text(f"学生 {names_str} 在数据库中有累计 {affected_count} 条考勤异常记录。\n\n删除这些学生将永久清除他们的所有考勤记录。是否确认保存？"),
                actions=[
                    ft.TextButton("取消", on_click=lambda _: page.pop_dialog()),
                    ft.TextButton("确认保存", on_click=confirm_save, style=ft.ButtonStyle(color=ft.Colors.RED_600))
                ],
                actions_alignment=ft.MainAxisAlignment.END
            )
            page.show_dialog(dlg)
        else:
            save_roster_action()

    def init_roster_manage_ui():
        nonlocal current_editing_names
        current_editing_names = list(STUDENTS_ORDER)
        rebuild_manage_list_ui()

    manage_list_column = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True, spacing=6)

    def rebuild_manage_list_ui():
        nonlocal current_editing_names
        manage_list_column.controls.clear()
        
        for idx, name in enumerate(current_editing_names):
            
            def make_up_click(i=idx):
                nonlocal current_editing_names
                if i > 0:
                    current_editing_names[i], current_editing_names[i-1] = current_editing_names[i-1], current_editing_names[i]
                    rebuild_manage_list_ui()
                    
            def make_down_click(i=idx):
                nonlocal current_editing_names
                if i < len(current_editing_names) - 1:
                    current_editing_names[i], current_editing_names[i+1] = current_editing_names[i+1], current_editing_names[i]
                    rebuild_manage_list_ui()
                    
            def make_delete_click(i=idx):
                nonlocal current_editing_names
                current_editing_names.pop(i)
                rebuild_manage_list_ui()

            row_item = ft.Container(
                content=ft.Row(
                    [
                        ft.Row([
                            ft.Text(f"{idx+1}.", size=12, color=ft.Colors.GREY_500, width=24),
                            ft.Text(name, size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK),
                        ], spacing=5),
                        ft.Row(
                            [
                                ft.IconButton(icon=ft.Icons.ARROW_UPWARD, icon_size=18, icon_color=ft.Colors.BLUE_600, on_click=lambda _, i=idx: make_up_click(i)),
                                ft.IconButton(icon=ft.Icons.ARROW_DOWNWARD, icon_size=18, icon_color=ft.Colors.BLUE_600, on_click=lambda _, i=idx: make_down_click(i)),
                                ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, icon_size=18, icon_color=ft.Colors.RED_600, on_click=lambda _, i=idx: make_delete_click(i)),
                            ],
                            spacing=0
                        )
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN
                ),
                padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                border=ft.Border.all(1, ft.Colors.GREY_300),
                border_radius=8,
                bgcolor=ft.Colors.WHITE
            )
            manage_list_column.controls.append(row_item)
            
        page.update()

    # 底部保存和重置按钮
    save_roster_btn = ft.Button(
        content="💾 保存并应用更改",
        on_click=on_save_roster,
        style=ft.ButtonStyle(
            color=ft.Colors.WHITE,
            bgcolor=ft.Colors.GREEN_600,
            padding=12,
            shape=ft.RoundedRectangleBorder(radius=10)
        ),
        expand=True
    )
    
    reset_roster_btn = ft.Button(
        content="🔄 重置",
        on_click=lambda _: init_roster_manage_ui(),
        style=ft.ButtonStyle(
            color=ft.Colors.BLACK,
            bgcolor=ft.Colors.GREY_300,
            padding=12,
            shape=ft.RoundedRectangleBorder(radius=10)
        )
    )

    async def on_import_click(e):
        # Flet 0.80+ 的 pick_files 直接返回所选文件（旧版 on_result 回调已失效）
        files = await file_picker.pick_files(allowed_extensions=["xlsx", "csv", "xls"])
        if files and files[0].path:
            handle_imported_file(files[0].path)
        elif files:
            show_snackbar("无法读取所选文件的路径", is_error=True)

    # 导入按钮
    import_excel_btn = ft.Container(
        content=ft.Button(
            content="📥 从 Excel / CSV 导入名册",
            on_click=on_import_click,
            style=ft.ButtonStyle(
                color=ft.Colors.BLUE_800,
                bgcolor=ft.Colors.BLUE_100,
                padding=12,
                shape=ft.RoundedRectangleBorder(radius=10)
            ),
            expand=True
        ),
        alignment=ft.Alignment.CENTER,
        padding=ft.Padding.only(bottom=5)
    )

    # 加载班级名
    init_class_name = db_get_config("class_name") or DEFAULT_CLASS_NAME
    class_name_text = ft.Text(init_class_name, size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_800)

    def show_change_class_dialog(e):
        current_name = db_get_config("class_name") or DEFAULT_CLASS_NAME
        name_input = ft.TextField(
            value=current_name,
            hint_text="请输入新的班级名称...",
            height=45,
            content_padding=10,
            bgcolor=ft.Colors.GREY_50,
            border_radius=8,
            border_color=ft.Colors.GREY_300,
            color=ft.Colors.BLACK
        )

        def save_class_name(e2):
            new_name = name_input.value.strip()
            if not new_name:
                # 用输入框内的错误提示，避免 SnackBar 压到对话框栈顶
                name_input.error_text = "班级名称不能为空"
                name_input.update()
                return
            db_set_config("class_name", new_name)
            # 先关闭对话框：show_snackbar 也走 page.show_dialog 入栈，
            # 若先弹 SnackBar，pop_dialog 关掉的是 SnackBar 而不是本对话框
            page.pop_dialog()
            class_name_text.value = new_name
            page.update()
            show_snackbar(f"成功将考勤班级切换为：{new_name}")

        dlg = ft.AlertDialog(
            title=ft.Text("切换班级", size=18, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Text("请输入您的班级名称。更改后微信考勤及导出的 Excel 文件名将自动同步该班级。", size=12, color=ft.Colors.GREY_600),
                        ft.Container(height=5),
                        ft.Text("标准格式：“xx专业x班” (例如：24示例3班)", size=12, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_800),
                        name_input
                    ],
                    tight=True
                ),
                width=280
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda _: page.pop_dialog()),
                ft.TextButton("确认", on_click=save_class_name, style=ft.ButtonStyle(color=ft.Colors.BLUE_800))
            ],
            actions_alignment=ft.MainAxisAlignment.END
        )
        page.show_dialog(dlg)

    # 班级设置横幅组件
    class_setting_row = ft.Container(
        content=ft.Row(
            [
                ft.Row(
                    [
                        ft.Icon(ft.Icons.SCHOOL_ROUNDED, color=ft.Colors.BLUE_800, size=18),
                        ft.Text("当前班级：", size=14, color=ft.Colors.GREY_700),
                        class_name_text
                    ],
                    spacing=3
                ),
                ft.TextButton(
                    content=ft.Text("切换班级", size=13, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_800),
                    on_click=show_change_class_dialog
                )
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN
        ),
        padding=ft.Padding.symmetric(horizontal=10, vertical=8),
        bgcolor=ft.Colors.BLUE_50,
        border_radius=8
    )

    # 组装名单管理页面视图
    roster_manage_view = ft.Column(
        [
            ft.Row(
                [
                    ft.Column(
                        [
                            ft.Text("名单管理", size=18, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_800),
                            ft.Text("从出勤表导入、手动增删成员、上下移动排序", size=11, color=ft.Colors.GREY_600)
                        ],
                        spacing=2
                    ),
                    ft.Icon(icon=ft.Icons.PEOPLE_ROUNDED, color=ft.Colors.BLUE_800, size=24)
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN
            ),
            ft.Divider(color=ft.Colors.GREY_300, height=5),
            class_setting_row,
            import_excel_btn,
            ft.Row([add_name_input, add_name_btn], spacing=10),
            manage_list_column,
            ft.Row([reset_roster_btn, save_roster_btn], spacing=10)
        ],
        expand=True,
        spacing=10
    )

    # 点名页面视图
    roll_call_view = ft.Column(
        [
            header_row,
            ft.Divider(color=ft.Colors.GREY_300, height=5),
            search_row,
            global_buttons,
            roster_column
        ],
        expand=True,
        spacing=10
    )

    # ----------------- 6.5 关于软件页面 UI 与组件 -----------------
    
    # 软件 Logo 与名字区域 (中上部分留白效果)
    logo_avatar = ft.Container(
        content=ft.Icon(ft.Icons.CHECKLIST_ROUNDED, size=52, color=ft.Colors.BLUE_800),
        width=100,
        height=100,
        bgcolor=ft.Colors.BLUE_50,
        border_radius=50,
        alignment=ft.Alignment.CENTER,
        shadow=ft.BoxShadow(spread_radius=1, blur_radius=10, color=ft.Colors.BLACK12)
    )
    
    about_header = ft.Container(
        content=ft.Column(
            [
                logo_avatar,
                ft.Text("点名助手", size=24, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_800),
                ft.Text("学委量化管理提效神器", size=12, color=ft.Colors.GREY_600),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=10
        ),
        alignment=ft.Alignment.CENTER,
        padding=ft.Padding.only(top=60, bottom=60) # 预留中上部分的大面积留白
    )
    
    # 软件基本信息卡片
    info_card = ft.Container(
        content=ft.Column(
            [
                ft.Row([
                    ft.Text("版本号", size=14, color=ft.Colors.GREY_600),
                    ft.Text("v1.3.0 (Release)", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK)
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Divider(color=ft.Colors.GREY_200, height=1),
                ft.Row([
                    ft.Text("支持平台", size=14, color=ft.Colors.GREY_600),
                    ft.Text("Android / Windows", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK)
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Divider(color=ft.Colors.GREY_200, height=1),
                ft.Row([
                    ft.Text("开发团队", size=14, color=ft.Colors.GREY_600),
                    ft.Text("LEO", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK)
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Divider(color=ft.Colors.GREY_200, height=1),
                ft.Row([
                    ft.Text("版权所有", size=14, color=ft.Colors.GREY_600),
                    ft.Text("© 2026 HMUDQ-CS", size=12, color=ft.Colors.GREY_500)
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ],
            spacing=12
        ),
        padding=ft.Padding.all(15),
        border=ft.Border.all(1, ft.Colors.GREY_300),
        border_radius=12,
        bgcolor=ft.Colors.WHITE,
        shadow=ft.BoxShadow(spread_radius=1, blur_radius=4, color=ft.Colors.BLACK12)
    )
    
    # 列表型设置按钮
    def make_setting_tile(icon, title, subtitle="", on_click=None):
        return ft.Container(
            content=ft.Row(
                [
                    ft.Row(
                        [
                            ft.Container(
                                content=ft.Icon(icon, color=ft.Colors.BLUE_800, size=20),
                                bgcolor=ft.Colors.BLUE_50,
                                padding=8,
                                border_radius=8
                            ),
                            ft.Column(
                                [
                                    ft.Text(title, size=15, weight=ft.FontWeight.BOLD, color=ft.Colors.BLACK),
                                    ft.Text(subtitle, size=11, color=ft.Colors.GREY_500) if subtitle else ft.Container()
                                ],
                                spacing=2,
                                tight=True
                            )
                        ],
                        spacing=12
                    ),
                    ft.Icon(ft.Icons.ARROW_FORWARD_IOS_ROUNDED, color=ft.Colors.GREY_400, size=14)
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN
            ),
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            border=ft.Border.all(1, ft.Colors.GREY_300),
            border_radius=10,
            bgcolor=ft.Colors.WHITE,
            on_click=on_click if on_click else lambda _: show_snackbar("该功能已预留，正在开发中..."),
            shadow=ft.BoxShadow(spread_radius=1, blur_radius=4, color=ft.Colors.BLACK12)
        )

    def show_logs_dialog(e):
        log_text = get_app_logs()
        if not log_text:
            log_text = "暂无运行日志。"

        text_field = ft.TextField(
            value=log_text,
            multiline=True,
            read_only=True,
            min_lines=8,
            max_lines=15,
            text_size=12,
            bgcolor=ft.Colors.GREY_50,
            border_color=ft.Colors.GREY_300,
            color=ft.Colors.BLACK
        )

        def copy_logs(e2):
            async def set_clip():
                await ft.Clipboard().set(log_text)
            page.run_task(set_clip)
            show_snackbar("日志已成功复制到剪贴板！")

        dlg = ft.AlertDialog(
            title=ft.Text("系统运行日志", size=18, weight=ft.FontWeight.BOLD),
            content=ft.Container(
                content=text_field,
                width=350,
                height=300
            ),
            actions=[
                ft.TextButton("复制并关闭", on_click=lambda e: (copy_logs(e), page.pop_dialog())),
                ft.TextButton("关闭", on_click=lambda _: page.pop_dialog())
            ],
            actions_alignment=ft.MainAxisAlignment.END
        )
        page.show_dialog(dlg)

    # 预留按钮列表
    action_tiles = ft.Column(
        [
            make_setting_tile(ft.Icons.BUG_REPORT_ROUNDED, "导出运行日志", "查看并复制运行日志用于反馈 Bug", on_click=show_logs_dialog),
            make_setting_tile(ft.Icons.SYSTEM_UPDATE_ALT_ROUNDED, "检查更新", "当前已是最新版本 v1.3.0"),
            make_setting_tile(ft.Icons.FEEDBACK_ROUNDED, "问题反馈", "向开发团队反馈 Bug 或建议"),
            make_setting_tile(ft.Icons.SHARE_ROUNDED, "分享应用", "一键分享 APK 导出文件"),
            make_setting_tile(ft.Icons.CODE_ROUNDED, "访问项目源码地址", "查看 GitHub 开源仓库")
        ],
        spacing=8
    )

    about_view = ft.Column(
        [
            about_header,
            ft.Text("软件信息", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.GREY_700),
            info_card,
            ft.Container(height=10),
            ft.Text("系统操作", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.GREY_700),
            action_tiles,
            ft.Container(height=20)
        ],
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        spacing=10
    )

    # 页面切换容器
    main_body = ft.Container(content=roll_call_view, expand=True)

    # ----------------- 7. 部署底导与组装 Page -----------------

    def on_nav_change(e):
        idx = int(e.control.selected_index)
        if idx == 0:
            main_body.content = roll_call_view
        elif idx == 1:
            main_body.content = roster_manage_view
        else:
            main_body.content = about_view
        page.update()

    page.navigation_bar = ft.NavigationBar(
        destinations=[
            ft.NavigationBarDestination(icon=ft.Icons.CHECKLIST_ROUNDED, label="上课点名"),
            ft.NavigationBarDestination(icon=ft.Icons.PEOPLE_ROUNDED, label="名单管理"),
            ft.NavigationBarDestination(icon=ft.Icons.INFO_ROUNDED, label="关于软件"),
        ],
        selected_index=0,
        on_change=on_nav_change,
        bgcolor=ft.Colors.GREY_50,
        height=65
    )

    # 初始展示骨架结构，并用 Loading 动画占位
    roster_column.controls.append(
        ft.Container(
            content=ft.ProgressRing(color=ft.Colors.BLUE_800),
            alignment=ft.Alignment.CENTER,
            padding=100
        )
    )
    page.add(main_body)
    page.update()

    # 首次加载考勤缓存并绘制 (采用后台异步加载以消除冷启动白屏/白底延迟)
    async def initial_load(e=None):
        import asyncio
        await asyncio.sleep(0.01)
        load_attendance_from_db()
        rebuild_roster_ui()
        init_roster_manage_ui() # 提前渲染名单管理页面以避免切换卡顿
        page.update()

    # 在后台异步载入 SQLite 数据库与渲染人名卡片列表
    page.run_task(initial_load)

if __name__ == '__main__':
    # 也可以作为一个 Web 服务启动以支持多端同步 (如: ft.app(target=main, view=ft.AppView.WEB_BROWSER))
    # 默认直接以桌面程序运行，本地读写速度最快
    ft.run(main)
