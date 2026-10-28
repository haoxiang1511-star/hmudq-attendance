# -*- coding: utf-8 -*-
"""
点名助手 (Flet App) v1.4.0
作者：Antigravity
"""
import os
import sys
import re
import sqlite3
import datetime
import shutil
import json
import urllib.request
import urllib.parse
import urllib.error
import openpyxl
from openpyxl.styles import Font
import flet as ft
import collections
import math
from name_import import parse_names, parse_names_detail, normalize_name, disambiguate
from telemetry import (
    TELEMETRY_ENABLED,
    TELEMETRY_ENDPOINT,
    TELEMETRY_PAYLOAD_KEYS,
    TELEMETRY_EVENTS,
    TELEMETRY_FEATURES,
    build_telemetry_payload,
    get_or_create_install_id,
    maybe_send_telemetry as _telemetry_maybe_send,
    session_seconds_capped,
    local_hour_weekday,
    normalize_platform,
    note_roll_mark,
    record_roll_call_mark,
    flush_roll_call_day_telemetry,
    reset_roll_call_day_counters,
)

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

# ----------------- 版本号（全工程只在这里写一次） -----------------
APP_VERSION = "1.5.1"
APP_BUILD = 109
SCHEMA_VERSION = 2          # 数据库结构版本（PRAGMA user_version）

# ----------------- 匿名使用统计（骨架，默认关闭） -----------------
# 开启方式：TELEMETRY_ENABLED=True，并填写 TELEMETRY_ENDPOINT 为你的接收 URL。
# 可上报：打开时段、会话时长、点名频次汇总、功能点击；不含名单、考勤明细、姓名、班级、路径。
# TELEMETRY_ENABLED / TELEMETRY_ENDPOINT 定义在 telemetry.py。

_telemetry_session_start = None  # datetime；进程内会话起点


def maybe_send_telemetry(event="app_open", **kwargs):
    """默认关闭或无 endpoint 时立即返回 False，失败静默。"""
    try:
        kwargs.setdefault("data_dir", os.path.dirname(os.path.abspath(DB_PATH)))
        kwargs.setdefault("app_version", APP_VERSION)
        kwargs.setdefault("app_build", APP_BUILD)
        return _telemetry_maybe_send(event, **kwargs)
    except Exception:
        return False


def telemetry_platform_from_page(page=None):
    """粗粒度平台；读不到则 unknown。"""
    try:
        if page is not None and getattr(page, "platform", None) is not None:
            return normalize_platform(page.platform)
    except Exception:
        pass
    return "unknown"


def telemetry_note_feature(feature, page=None):
    """功能点击埋点；关闭时 no-op。"""
    try:
        return maybe_send_telemetry(
            "feature", feature=feature,
            platform=telemetry_platform_from_page(page),
        )
    except Exception:
        return False


def telemetry_note_roll_mark(period):
    """点名改状态成功后累加；关闭时只做内存计数。"""
    try:
        return note_roll_mark(period)
    except Exception:
        return None


def telemetry_flush_roll_day(page=None):
    try:
        return flush_roll_call_day_telemetry(
            data_dir=os.path.dirname(os.path.abspath(DB_PATH)),
            app_version=APP_VERSION,
            app_build=APP_BUILD,
            platform=telemetry_platform_from_page(page),
        )
    except Exception:
        return False


def telemetry_end_session(page=None):
    """会话结束：先 flush 点名日汇总，再发 app_session（时长封顶 7200）。"""
    global _telemetry_session_start
    try:
        telemetry_flush_roll_day(page)
    except Exception:
        pass
    try:
        start = _telemetry_session_start
        if start is None:
            return False
        elapsed = (datetime.datetime.now() - start).total_seconds()
        _telemetry_session_start = None
        return maybe_send_telemetry(
            "app_session",
            session_seconds=session_seconds_capped(elapsed),
            platform=telemetry_platform_from_page(page),
        )
    except Exception:
        return False


# 旧版（v1.2）库第一次升级时用来补排序的默认顺序；只在旧库升级且 sort_order 全为 0 时使用，
# 不会向数据库插入任何学生（新装始终是空名单）。
DEFAULT_STUDENTS_ORDER = [
    "赵甲", "赵乙", "赵丙", "赵丁", "赵戊", "钱甲", "钱乙", "钱丙", "钱丁", "钱戊",
    "孙甲", "孙乙", "孙丙", "孙丁", "孙戊", "李甲", "李乙", "李丙", "李丁", "李戊",
    "周甲", "周乙", "周丙", "周丁", "周戊", "吴甲", "吴乙", "吴丙", "吴丁", "吴戊",
    "郑甲", "郑乙", "郑丙", "郑丁", "郑戊", "王甲", "王乙", "王丙", "王丁", "王戊",
    "冯甲", "冯乙", "冯丙", "冯丁", "冯戊"
]

# 课节名称
PERIOD_LABELS = ["1-2节", "3-4节", "5-6节", "7-8节", "9-10节"]

# 1.5.2 界面配色（对齐 render/ui15/mock.css）
UI_BG = "#f8f9ff"
UI_BLUE = "#1565c0"
UI_BLUE_SOFT = "#e3f2fd"
UI_BLUE_PILL = "#bbdefb"
UI_LINE = "#e0e0e0"
UI_INK = "#1a1a1a"
UI_MUTED = "#757575"
UI_DANGER = "#e53935"
UI_OK = "#2e7d32"
UI_TOP_INSET = 36   # 状态栏留白（原 page.padding.top 挪进各页）

# 可以写入数据库的考勤状态（状态选择器只列这 5 个）
RECORD_STATUSES = ["出勤", "旷课", "病假", "事假", "空课"]
NOT_JOINED = "未入班"      # 只用于显示：入班日期晚于所选日期且没有记录

# 状态样式定义 (键: (颜色, 背景色))
STATUS_STYLES = {
    "出勤": ("#10b981", "#1a10b981"),
    "旷课": ("#ef4444", "#1aef4444"),
    "病假": ("#ea580c", "#1aea580c"),
    "事假": ("#d97706", "#1ad97706"),
    "空课": ("#6b7280", "#1a6b7280"),
    NOT_JOINED: ("#9ca3af", "#0d9ca3af"),
}

# ----------------- 1. 数据库逻辑 -----------------

# 名单导入确认框：姓名小框固定高度，右侧滚动条一直显示（ScrollMode.ALWAYS 在手机上滚动条只有 4px，这里加粗并显示轨道）
IMPORT_PREVIEW_HEIGHT = 200
IMPORT_PREVIEW_SCROLLBAR = ft.Scrollbar(thumb_visibility=True, track_visibility=True,
                                        thickness=6, radius=3, interactive=True)

# ----------------- 检查更新（手动点「检查更新」立刻联网；启动后还可静默查一次/天，只用小红点提示、绝不自动弹窗；只读版本信息、打开下载链接，不下载/执行任何代码） -----------------
UPDATE_CONFIG = {
    # 版本信息文件（格式见 tools/version.json 和 tools/UPDATE_README.md），按顺序尝试，任一成功即用：先 Gitee（国内快）再 GitHub
    "version_urls": [
        "https://gitee.com/Hector1511/hmudq-attendance-release/raw/master/version.json",
        "https://raw.githubusercontent.com/haoxiang1511-star/hmudq-attendance-release/main/version.json",
    ],
    # 内置下载地址：version.json 里写了（且是 https）就用 version.json 的，否则用这里的
    # Gitee：国内直链优先；无 latest 永久链，每次发版写死该 tag 的 download URL（与 version.json 的 gitee_apk_url 同步）
    "default_gitee_apk_url": "https://gitee.com/Hector1511/hmudq-attendance-release/releases/download/v1.5.1/hmudq-attendance.apk",
    # GitHub 用 releases/latest/download/固定文件名，永远指向最新版
    "default_github_apk_url": "https://github.com/haoxiang1511-star/hmudq-attendance-release/releases/latest/download/hmudq-attendance.apk",
    # 蓝奏云：固定的文件夹分享链接（永不更换），每次发版把最新安装包放在文件夹最上面；文件夹有提取码
    "default_lanzou_url": "https://wwbwu.lanzoup.com/b01i5ycuyb",
    "default_lanzou_code": "dt9b",
    "timeout": 10,                # 每个地址的超时（秒）；先 Gitee 后 GitHub，两边都超时最多约 20 秒
    "max_bytes": 64 * 1024,       # 版本信息文件最大读取字节数
}


UPDATE_CHECKING_TEXT = "正在检查更新…"
LANZOU_HELP_TEXT = "点『蓝奏云下载』，输入提取码，在文件夹里选最上面那个（版本号最大的）安装包下载，下完点开直接安装，名单和考勤都会保留。"


DEVELOPER_EMAIL = "haoxiang1511@gmail.com"
SOURCE_REPO_URL = "https://github.com/haoxiang1511-star/hmudq-attendance"
APP_PACKAGE_NAME = "com.flet.app_build"  # 覆盖安装包名；分享本机 APK 时按此查找


def get_share_apk_cache_path(db_path=None):
    """可分享缓存路径（应用数据目录下 apk_cache）。"""
    base = os.path.dirname(os.path.abspath(db_path or DB_PATH))
    return os.path.join(base, "apk_cache", "hmudq-attendance.apk")


def find_installed_apk_path(package_name=None):
    """在 Android 上定位本 App 已安装的 APK（sourceDir / packageCodePath / base.apk）。
    非 Android 或读不到时返回 None。不联网、不安装。"""
    package_name = package_name or APP_PACKAGE_NAME
    candidates = []
    try:
        from jnius import autoclass
        # 优先：当前 Application Context
        try:
            ActivityThread = autoclass("android.app.ActivityThread")
            ctx = ActivityThread.currentApplication().getApplicationContext()
        except Exception:
            ctx = None
        if ctx is None:
            try:
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                ctx = PythonActivity.mActivity
            except Exception:
                ctx = None
        if ctx is not None:
            try:
                p = str(ctx.getPackageCodePath() or "")
                if p:
                    candidates.append(p)
            except Exception as ex:
                print(f"[share-apk] getPackageCodePath 失败：{ex}")
            try:
                pm = ctx.getPackageManager()
                ai = pm.getApplicationInfo(package_name, 0)
                for attr in ("sourceDir", "publicSourceDir"):
                    try:
                        p = str(getattr(ai, attr, None) or "")
                        if p:
                            candidates.append(p)
                    except Exception:
                        pass
            except Exception as ex:
                print(f"[share-apk] getApplicationInfo 失败：{ex}")
    except Exception as ex:
        print(f"[share-apk] jnius 不可用：{ex}")

    # 常见 base.apk 路径兜底（部分机型 Context API 受限时）
    for root in ("/data/app", "/data/app-private"):
        try:
            if not os.path.isdir(root):
                continue
            for name in os.listdir(root):
                if package_name.replace(".", "") in name.replace(".", "") or package_name in name:
                    base = os.path.join(root, name, "base.apk")
                    if os.path.isfile(base):
                        candidates.append(base)
        except OSError:
            pass

    seen = set()
    for p in candidates:
        if not p or p in seen:
            continue
        seen.add(p)
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 1024:
                return p
        except OSError:
            continue
    return None


def copy_apk_to_share_cache(src, dest=None):
    """把安装包复制到可分享缓存目录。成功返回 dest。"""
    if not src or not os.path.isfile(src):
        raise BackupError("找不到本机安装包")
    dest = dest or get_share_apk_cache_path()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass
    shutil.copyfile(src, tmp)
    size = os.path.getsize(tmp)
    if size < 1024:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise BackupError("安装包太小，可能不是有效 APK")
    os.replace(tmp, dest)
    return dest


def build_lanzou_share_text(info=None):
    """蓝奏云下载短文案（测试/备用文案；正式退路走 show_share_download_dialog）。"""
    info = info or {}
    url = (info.get("lanzou_url")
           or UPDATE_CONFIG.get("default_lanzou_url")
           or "").strip()
    code = (info.get("lanzou_code")
            or UPDATE_CONFIG.get("default_lanzou_code")
            or "").strip()
    lines = [
        f"点名助手 v{APP_VERSION}",
        f"蓝奏云下载：{url}" if url else "蓝奏云下载页暂不可用",
    ]
    if code:
        lines.append(f"提取码：{code}")
    lines.append("选文件夹里最上面（版本号最大）的安装包下载后覆盖安装，名单和考勤都会保留。")
    return "\n".join(lines)

def resolve_download_info_for_share(info=None):
    """分享退路用的 Gitee / GitHub / 蓝奏云地址。
    可传入检查更新得到的 info；否则用内置 default_*（不在此联网，避免分享卡顿）。"""
    base = {
        "gitee_apk_url": UPDATE_CONFIG.get("default_gitee_apk_url", ""),
        "github_apk_url": UPDATE_CONFIG.get("default_github_apk_url", ""),
        "lanzou_url": UPDATE_CONFIG.get("default_lanzou_url", ""),
        "lanzou_code": UPDATE_CONFIG.get("default_lanzou_code", ""),
    }
    if info:
        for k in ("gitee_apk_url", "github_apk_url", "lanzou_url", "lanzou_code"):
            v = (info.get(k) or "").strip()
            if v:
                base[k] = v
    return base



# 失败页 / 复制日志：脱敏用（绝对路径 → 文件名；已知姓名 → 「同学」）
_ABS_PATH_RE = re.compile(
    r'(?:(?:[A-Za-z]:)?(?:/|\\)[^\s"\'，。；、：:]+)+'
)


def friendly_data_path(path):
    """界面展示用：不暴露完整绝对路径。"""
    if not path:
        return "（无）"
    return f"应用数据目录内的备份/数据文件「{os.path.basename(path)}」"


def redact_sensitive_text(text, names=None):
    """把绝对路径缩成文件名；可选把学生姓名换成「同学」。纯函数，便于单测。"""
    if text is None:
        return ""
    out = str(text)

    def _path_repl(m):
        p = m.group(0)
        base = os.path.basename(p.rstrip("/\\"))
        return base if base else p

    out = _ABS_PATH_RE.sub(_path_repl, out)
    if names:
        for n in sorted({x for x in names if x and len(str(x)) >= 2}, key=lambda s: len(str(s)), reverse=True):
            out = out.replace(str(n), "同学")
    return out


def collect_student_names_for_redact(conn=None):
    """从当前库收集姓名，供日志脱敏；库不可用时返回空。"""
    try:
        conn = conn or get_db_connection()
        if not _table_exists(conn, "students"):
            return []
        return [r[0] for r in conn.execute("SELECT DISTINCT name FROM students") if r and r[0]]
    except Exception:
        return []


def undo_remove_allowed(undo_class_id, current_class_id):
    """删人撤销是否仍有效：必须仍在删除时所在班级。"""
    return undo_class_id is not None and current_class_id is not None and undo_class_id == current_class_id


class MigrationError(Exception):
    """数据升级失败：原有数据没有改动（已回滚）。backup_path 为升级前备份（可能为 None）。"""
    def __init__(self, message, backup_path=None):
        super().__init__(message)
        self.backup_path = backup_path


class BackupError(Exception):
    pass


_db_conn = None

def get_db_connection():
    global _db_conn
    if _db_conn is None:
        # SQLite 连接复用，并开启 WAL 模式以实现极速亚毫秒级读写
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db_conn.execute("PRAGMA journal_mode=WAL;")
        _db_conn.execute("PRAGMA synchronous=NORMAL;")
        _db_conn.execute("PRAGMA foreign_keys=ON;")
    return _db_conn


def close_db_connection():
    """关闭全局连接（测试切换数据库时用；App 运行中不调用）。"""
    global _db_conn
    if _db_conn is not None:
        try:
            _db_conn.close()
        except Exception:
            pass
    _db_conn = None


DEFAULT_CLASS_NAME = "我的班级"

# 当前班级的名单（只含在名单里的 active=1 学生），以及入班日期
STUDENTS_ORDER = []
STUDENT_JOINED = {}


def _table_exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _table_cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _count(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()[0]


def _today():
    return datetime.date.today().isoformat()


def db_set_config(key, value):
    conn = get_db_connection()
    conn.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()


def db_get_config(key, default=None):
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
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


# ----------------- 1.0 检查更新 -----------------

def _version_tuple(v):
    parts = re.findall(r"\d+", str(v or ""))
    return tuple(int(x) for x in parts[:3]) + (0,) * (3 - min(len(parts), 3))


def _is_lanzou_code(code):
    return bool(re.fullmatch(r"[A-Za-z0-9]{1,12}", str(code or "")))


def parse_update_info(raw, defaults=None):
    """解析并校验 version.json 的内容（bytes/str）。返回规范化 dict，不合格抛 ValueError。
    下载地址：version.json 里有且是 https 就用它，没有（或不是 https）就用内置的 UPDATE_CONFIG 默认地址。"""
    if defaults is None:
        defaults = {"gitee_apk_url": UPDATE_CONFIG.get("default_gitee_apk_url", ""),
                    "github_apk_url": UPDATE_CONFIG.get("default_github_apk_url", ""),
                    "lanzou_url": UPDATE_CONFIG.get("default_lanzou_url", ""),
                    "lanzou_code": UPDATE_CONFIG.get("default_lanzou_code", "")}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig")
    data = json.loads(str(raw).lstrip("\ufeff"))
    if not isinstance(data, dict):
        raise ValueError("版本信息格式不对")
    version = str(data.get("version", "")).strip().lstrip("vV")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"版本号格式不对：{data.get('version')!r}")
    try:
        build = int(data.get("build"))
    except (TypeError, ValueError):
        raise ValueError(f"build 不是数字：{data.get('build')!r}")
    notes = data.get("notes", "")
    if isinstance(notes, list):
        notes = "\n".join(f"• {x}" for x in notes)
    out = {"version": version, "build": build, "notes": str(notes).strip()[:2000]}
    for key in ("gitee_apk_url", "github_apk_url", "lanzou_url"):
        url = str(data.get(key) or "").strip()
        if url and not url.startswith("https://"):
            print(f"[update] {key} 不是 https 链接，改用内置地址：{url!r}")
            url = ""
        if not url:
            url = str(defaults.get(key) or "").strip()
            url = url if url.startswith("https://") else ""
        out[key] = url
    if not (out["gitee_apk_url"] or out["github_apk_url"] or out["lanzou_url"]):
        raise ValueError("没有下载地址")
    # 蓝奏云提取码：version.json 里有且是短字母数字串就用它，否则用内置的（与 lanzou_url 的兜底一致）
    code = str(data.get("lanzou_code") or "").strip()
    if code and not _is_lanzou_code(code):
        print(f"[update] lanzou_code 格式不对，改用内置提取码：{code!r}")
        code = ""
    if not code:
        code = str(defaults.get("lanzou_code") or "").strip()
        code = code if _is_lanzou_code(code) else ""
    out["lanzou_code"] = code if out["lanzou_url"] else ""
    return out


def is_newer_version(info, cur_version=None, cur_build=None):
    """先比 build（只增不减），build 相同再比版本号。"""
    cur_version = cur_version or APP_VERSION
    cur_build = APP_BUILD if cur_build is None else cur_build
    if info["build"] != cur_build:
        return info["build"] > cur_build
    return _version_tuple(info["version"]) > _version_tuple(cur_version)


def fetch_update_info(urls=None, timeout=None, opener=None):
    """按顺序请求版本信息文件，任一成功即返回 (info, None)；全部失败返回 (None, [错误...])。
    只用标准库 urllib，只读 JSON，不下载安装包、不执行任何内容。会阻塞，请在后台线程里调用。"""
    urls = UPDATE_CONFIG["version_urls"] if urls is None else urls
    timeout = UPDATE_CONFIG["timeout"] if timeout is None else timeout
    opener = opener or urllib.request.urlopen
    errors = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": f"HMUDQ-Attendance/{APP_VERSION} (build {APP_BUILD})",
                "Accept": "application/json, text/plain, */*", "Cache-Control": "no-cache"})
            with opener(req, timeout=timeout) as resp:
                raw = resp.read(UPDATE_CONFIG["max_bytes"] + 1)
            if len(raw) > UPDATE_CONFIG["max_bytes"]:
                raise ValueError("版本信息文件太大")
            return parse_update_info(raw), None
        except Exception as ex:
            errors.append(f"{url}：{ex}")
            print(f"[update] 检查失败 {url}：{ex}")
    return None, errors


def check_for_update(urls=None, timeout=None, opener=None):
    """返回 ("new", info) / ("latest", info) / ("fail", 错误列表)。"""
    info, errors = fetch_update_info(urls, timeout, opener)
    if info is None:
        return "fail", errors
    return ("new" if is_newer_version(info) else "latest"), info


# ----------------- 1.0b 更新缓存 / 小红点（静默检查，不强制） -----------------

UPDATE_CACHE_FILENAME = "update_cache.json"


def get_update_cache_path(db_path=None):
    """更新检查缓存路径（应用数据目录下 update_cache.json）。"""
    base = os.path.dirname(os.path.abspath(db_path or DB_PATH))
    return os.path.join(base, UPDATE_CACHE_FILENAME)


def load_update_cache(path=None):
    """读取更新缓存；坏文件/不存在返回空 dict。"""
    path = path or get_update_cache_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_update_cache(data, path=None):
    """原子写入更新缓存。"""
    path = path or get_update_cache_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data or {}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path


def update_cache_checked_today(cache, today=None):
    """缓存里的 checked_date 是否等于今天（ISO 日期）。"""
    today = today or datetime.date.today().isoformat()
    return str((cache or {}).get("checked_date") or "") == str(today)


def should_auto_check_update(cache, today=None):
    """静默自动检查：同一自然日最多一次。"""
    return not update_cache_checked_today(cache, today)


def cached_update_info(cache):
    """从缓存拼出可供 is_newer_version 比较的 info；缺字段返回 None。"""
    cache = cache or {}
    try:
        version = str(cache.get("latest_version") or "").strip().lstrip("vV")
        build = int(cache.get("latest_build"))
    except (TypeError, ValueError):
        return None
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return None
    return {
        "version": version,
        "build": build,
        "notes": str(cache.get("notes") or ""),
        "gitee_apk_url": str(cache.get("gitee_apk_url") or ""),
        "github_apk_url": str(cache.get("github_apk_url") or ""),
        "lanzou_url": str(cache.get("lanzou_url") or ""),
        "lanzou_code": str(cache.get("lanzou_code") or ""),
    }


def update_badge_should_show(cache, cur_version=None, cur_build=None):
    """有缓存的新版且仍高于当前 App 时才亮红点。"""
    cache = cache or {}
    if not cache.get("available"):
        return False
    info = cached_update_info(cache)
    if not info:
        return False
    return is_newer_version(info, cur_version, cur_build)


def apply_update_check_to_cache(cache, result, info=None, today=None, mark_checked=True):
    """把一次检查结果合并进缓存并返回新 dict。
    result: new / latest / fail。fail 只记检查日期，保留原 available。"""
    out = dict(cache or {})
    today = today or datetime.date.today().isoformat()
    if mark_checked:
        out["checked_date"] = str(today)
    if result == "new" and isinstance(info, dict):
        out["available"] = True
        out["latest_version"] = str(info.get("version") or "").strip().lstrip("vV")
        try:
            out["latest_build"] = int(info["build"])
        except (TypeError, ValueError, KeyError):
            out["latest_build"] = info.get("build")
        out["notes"] = str(info.get("notes") or "")[:2000]
        out["gitee_apk_url"] = str(info.get("gitee_apk_url") or "")
        out["github_apk_url"] = str(info.get("github_apk_url") or "")
        out["lanzou_url"] = str(info.get("lanzou_url") or "")
        out["lanzou_code"] = str(info.get("lanzou_code") or "")
    elif result == "latest":
        out["available"] = False
        if isinstance(info, dict):
            ver = str(info.get("version") or "").strip().lstrip("vV")
            if ver:
                out["latest_version"] = ver
            try:
                out["latest_build"] = int(info["build"])
            except (TypeError, ValueError, KeyError):
                pass
    # fail：不改 available / latest_*
    return out


# ----------------- 1.1 备份 -----------------

BACKUP_RETENTION = {"upgrade": 5, "daily": 7, "weekly": 8, "op": 15, "manual": 10, "restore": 5}
BACKUP_KIND_LABELS = {"upgrade": "升级前", "daily": "每日", "weekly": "每周", "op": "操作前", "manual": "手动", "restore": "恢复前"}
_BACKUP_RE = re.compile(r"^attendance_([a-z]+)_(\d{8}-\d{6})(?:_(\d+))?\.db$")


def get_backup_dir():
    return os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")


def db_has_data(conn=None):
    conn = conn or get_db_connection()
    total = 0
    for t in ("students", "records"):
        if _table_exists(conn, t):
            total += _count(conn, f"SELECT COUNT(*) FROM {t}")
    return total > 0


def stamp_backup_file(b, reason):
    """给备份/恢复文件写上标识（App 的备份和 tools/make_restore_file.py 生成的文件共用这一套）。"""
    b.execute("CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT)")
    b.executemany("INSERT OR REPLACE INTO app_config(key, value) VALUES(?, ?)",
                  [("backup_reason", reason), ("backup_app", BACKUP_APP_ID),
                   ("backup_app_version", f"{APP_VERSION}+{APP_BUILD}")])
    b.commit()


def make_restore_file(src, dst, reason="电脑上生成的恢复文件"):
    """把一个 attendance.db（新旧结构都行）打成 App「从文件恢复」能识别的标准恢复文件。
    校验规则与 App 恢复时完全一样（validate_backup）；不合格抛 BackupError，不留下半成品。返回内容摘要。"""
    if not os.path.isfile(src):
        raise BackupError("找不到源数据库")
    ok, why = validate_backup(src)
    if not ok:
        raise BackupError(f"源数据库不合格：{why}")
    tmp = dst + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    s = sqlite3.connect(_ro_uri(src), uri=True)
    b = sqlite3.connect(tmp)
    try:
        s.backup(b)
        b.execute("PRAGMA journal_mode=DELETE")
        stamp_backup_file(b, reason)
        b.execute("VACUUM")
    finally:
        b.close(); s.close()
    ok, why = validate_backup(tmp)
    info = _backup_summary(tmp) if ok else None
    if ok and not (info.get("students") or info.get("records")):
        ok, why = False, "里面没有任何名单和考勤"
    if not ok:
        os.remove(tmp)
        raise BackupError(f"生成的恢复文件不合格：{why}")
    os.replace(tmp, dst)
    return info


def make_backup(kind, reason="", force=False, conn=None):
    """把当前库完整复制到 backups/attendance_{kind}_{时间}.db 并校验。空库默认不备份（返回 None）。
    校验失败抛 BackupError —— 调用方据此停止后面的危险操作。"""
    conn = conn or get_db_connection()
    if not force and not db_has_data(conn):
        return None
    if conn.in_transaction:
        conn.commit()
    bdir = get_backup_dir()
    os.makedirs(bdir, exist_ok=True)
    try:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    except sqlite3.Error:
        pass
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"attendance_{kind}_{ts}"
    dst = os.path.join(bdir, base + ".db")
    i = 2
    while os.path.exists(dst):
        dst = os.path.join(bdir, f"{base}_{i}.db")
        i += 1
    tmp = dst + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    ok = None
    b = sqlite3.connect(tmp)
    try:
        conn.backup(b)
        b.execute("PRAGMA journal_mode=DELETE")
        stamp_backup_file(b, reason or BACKUP_KIND_LABELS.get(kind, kind))
        ok = b.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        b.close()
    if ok != "ok":
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise BackupError(f"备份文件校验失败：{ok}")
    os.replace(tmp, dst)
    print(f"[backup] 已备份（{BACKUP_KIND_LABELS.get(kind, kind)}）：{dst}")
    prune_backups(kind)
    return dst


def prune_backups(kind):
    bdir = get_backup_dir()
    keep = BACKUP_RETENTION.get(kind, 10)
    if not os.path.isdir(bdir):
        return
    files = []
    for fn in os.listdir(bdir):
        m = _BACKUP_RE.match(fn)
        if m and m.group(1) == kind:
            files.append((m.group(2), int(m.group(3) or 1), fn))
    files.sort(reverse=True)
    for _, _, fn in files[keep:]:
        try:
            os.remove(os.path.join(bdir, fn))
            print(f"[backup] 超出保留份数，已清理：{fn}")
        except OSError:
            pass


def _backup_summary(path):
    info = {"classes": 0, "students": 0, "records": 0, "reason": "", "stamped": False}
    c = sqlite3.connect(path)
    try:
        if _table_exists(c, "classes"):
            info["classes"] = _count(c, "SELECT COUNT(*) FROM classes")
        if _table_exists(c, "students"):
            cols = _table_cols(c, "students")
            info["students"] = _count(c, "SELECT COUNT(*) FROM students" + (" WHERE active=1" if "active" in cols else ""))
            if not info["classes"]:
                info["classes"] = 1
        if _table_exists(c, "records"):
            info["records"] = _count(c, "SELECT COUNT(*) FROM records")
        if _table_exists(c, "app_config"):
            r = c.execute("SELECT value FROM app_config WHERE key='backup_reason'").fetchone()
            info["reason"] = r[0] if r else ""
            row = c.execute("SELECT value FROM app_config WHERE key='backup_app'").fetchone()
            info["stamped"] = bool(row and row[0] == BACKUP_APP_ID)
    finally:
        c.close()
    return info


def restore_origin_label(info):
    """从文件恢复确认框：区分「本软件戳」与「仅结构像」。"""
    if info.get("stamped"):
        return "校验通过，是本软件生成的备份。"
    return "结构像点名助手的数据库，但不是本软件打的备份戳。"


def restore_origin_warning(info):
    """无本软件戳时附加的确认提醒。"""
    if info.get("stamped"):
        return ""
    return "将整库替换，请确认来源可信。"


def list_backups():
    """返回备份列表（新的在前）：dict(path, file, kind, label, time(datetime), classes, students, records, reason, ok)。"""
    bdir = get_backup_dir()
    out = []
    if not os.path.isdir(bdir):
        return out
    for fn in os.listdir(bdir):
        m = _BACKUP_RE.match(fn)
        if not m:
            continue
        path = os.path.join(bdir, fn)
        item = {"path": path, "file": fn, "kind": m.group(1),
                "label": BACKUP_KIND_LABELS.get(m.group(1), m.group(1)),
                "time": datetime.datetime.strptime(m.group(2), "%Y%m%d-%H%M%S"),
                "seq": int(m.group(3) or 1), "ok": True,
                "classes": 0, "students": 0, "records": 0, "reason": ""}
        try:
            item.update(_backup_summary(path))
            ok, err = validate_backup(path)
            if not ok:
                item["ok"] = False
                item["reason"] = err
        except Exception as ex:
            item["ok"] = False
            item["reason"] = f"无法读取：{ex}"
        out.append(item)
    out.sort(key=lambda x: (x["time"], x["seq"]), reverse=True)
    return out


def daily_backup_if_needed():
    """（v1.4.2 起不再调用）保留函数，仅供测试模拟旧版「每日」备份文件；启动不再调用。"""
    tag = datetime.date.today().strftime("%Y%m%d")
    bdir = get_backup_dir()
    if os.path.isdir(bdir) and any(fn.startswith(f"attendance_daily_{tag}") for fn in os.listdir(bdir)):
        return None
    return make_backup("daily")


def _week_bounds(d):
    """返回包含 d 的自然周（周一到周日）起止日期。"""
    start = d - datetime.timedelta(days=d.weekday())  # Monday=0
    return start, start + datetime.timedelta(days=6)


def has_attendance_records(conn=None):
    conn = conn or get_db_connection()
    if not _table_exists(conn, "records"):
        return False
    return _count(conn, "SELECT COUNT(*) FROM records") > 0


def weekly_backup_exists_for_week(today, bdir=None):
    """本周（周一至周日）是否已有 weekly 备份文件。"""
    bdir = bdir or get_backup_dir()
    if not os.path.isdir(bdir):
        return False
    week_start, week_end = _week_bounds(today)
    for fn in os.listdir(bdir):
        m = _BACKUP_RE.match(fn)
        if not m or m.group(1) != "weekly":
            continue
        try:
            dt = datetime.datetime.strptime(m.group(2), "%Y%m%d-%H%M%S").date()
        except ValueError:
            continue
        if week_start <= dt <= week_end:
            return True
    return False


def weekly_backup_if_needed(today=None, conn=None):
    """启动时：今天是周一、库里已有考勤、且本周还没有 weekly 备份 → 备一份。
    today 可注入假日期以便单测。寒暑假不开 App / 无考勤则不备。返回路径或 None。"""
    today = today or datetime.date.today()
    if today.weekday() != 0:  # 仅周一
        return None
    if not has_attendance_records(conn):
        return None
    if weekly_backup_exists_for_week(today):
        return None
    return make_backup("weekly", reason="每周自动备份", conn=conn)


BACKUP_APP_ID = "com.flet.app_build"          # 写进备份 app_config.backup_app
SQLITE_MAGIC = b"SQLite format 3\x00"
RESTORE_MAX_BYTES = 200 * 1024 * 1024
_IMPORT_STAGING = "_import_staging.db"         # 「从文件恢复」时的临时副本（不匹配 _BACKUP_RE，不会出现在备份列表）
_NOT_OURS = "这不是本软件生成的备份文件"


def _ro_uri(path):
    """只读打开用的 SQLite URI（路径里有 ? # % 空格 中文也安全）。"""
    return "file:" + urllib.parse.quote(os.path.abspath(path).replace(os.sep, "/")) + "?mode=ro"


def validate_backup(path):
    """严格校验一份备份 / 外部文件能不能用来恢复。返回 (ok, 错误信息)。
    接受：本软件 v1.4.0 的多班结构；也接受 v1.2 / v1.3 老结构（恢复后 init_db 会原地升级）。
    拒绝：不是 SQLite、integrity_check 不通过、缺本软件的表/列、考勤内容不像本软件写的、
    备份标识不是本软件、或比当前 App 新的结构。"""
    if not os.path.isfile(path):
        return False, "找不到这份文件"
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError as ex:
        return False, f"读取文件失败：{ex}"
    if size > RESTORE_MAX_BYTES:
        return False, f"{_NOT_OURS}（文件太大）"
    if head != SQLITE_MAGIC or size < 512:
        return False, f"{_NOT_OURS}（不是数据库文件）"
    try:
        c = sqlite3.connect(_ro_uri(path), uri=True)
        try:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                return False, "这份备份已损坏（完整性检查没通过）"
            ver = c.execute("PRAGMA user_version").fetchone()[0]
            if ver > SCHEMA_VERSION:
                return False, "这份备份来自更新版本的点名助手，请先把 App 升级到最新版再恢复"
            if not (_table_exists(c, "students") and _table_exists(c, "records")):
                return False, f"{_NOT_OURS}（缺少名单表或考勤表）"
            scols, rcols = set(_table_cols(c, "students")), set(_table_cols(c, "records"))
            if not {"id", "name"} <= scols or not {"student_id", "date", "period", "status"} <= rcols:
                return False, f"{_NOT_OURS}（表结构不对）"
            multi = ver >= 2 or _table_exists(c, "classes") or "class_id" in scols
            if multi:
                if not _table_exists(c, "classes") or not {"id", "name"} <= set(_table_cols(c, "classes")):
                    return False, f"{_NOT_OURS}（缺少班级表）"
                if not {"class_id", "active"} <= scols or not _table_exists(c, "app_config"):
                    return False, f"{_NOT_OURS}（表结构不完整）"
            if _table_exists(c, "app_config"):
                row = c.execute("SELECT value FROM app_config WHERE key='backup_app'").fetchone()
                if row and row[0] != BACKUP_APP_ID:
                    return False, f"{_NOT_OURS}（来自其他软件）"
            bad_status = [r[0] for r in c.execute("SELECT DISTINCT status FROM records")
                          if r[0] not in RECORD_STATUSES]
            if bad_status:
                return False, f"{_NOT_OURS}（考勤里有不认识的状态：{bad_status[0]}）"
            bad = _count(c, "SELECT COUNT(*) FROM records WHERE period NOT BETWEEN 0 AND 4 OR date NOT GLOB "
                            "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'")
            if bad:
                return False, f"{_NOT_OURS}（有 {bad} 条考勤的日期或节次不对）"
        finally:
            c.close()
    except sqlite3.Error as ex:
        return False, f"{_NOT_OURS}或已损坏（{ex}）"
    return True, None


def prepare_restore_file(src_path):
    """「从文件恢复」第一步：把用户选的文件复制到 backups/_import_staging.db，严格校验并统计内容。
    不碰当前数据。返回 (临时副本路径, 摘要 dict)；不合格抛 BackupError（临时副本已删）。"""
    bdir = get_backup_dir()
    os.makedirs(bdir, exist_ok=True)
    staging = os.path.join(bdir, _IMPORT_STAGING)
    discard_restore_file(staging)
    try:
        if not src_path or not os.path.isfile(src_path):
            raise BackupError("找不到选中的文件")
        if os.path.getsize(src_path) > RESTORE_MAX_BYTES:
            raise BackupError(f"{_NOT_OURS}（文件太大）")
        shutil.copyfile(src_path, staging)
        ok, err = validate_backup(staging)
        if not ok:
            raise BackupError(err)
        info = _backup_summary(staging)
        if info["students"] + info["records"] == 0:
            raise BackupError("这份备份里没有任何名单和考勤，不能用来恢复")
        return staging, info
    except BackupError:
        discard_restore_file(staging)
        raise
    except Exception as ex:
        discard_restore_file(staging)
        raise BackupError(f"读取文件失败：{ex}")


def discard_restore_file(staging):
    try:
        if staging and os.path.exists(staging):
            os.remove(staging)
    except OSError:
        pass


def restore_backup(path):
    """整库恢复：严格校验 → 先备份当前库（恢复前）→ 用 SQLite backup 写回同一个 attendance.db →
    重新初始化（老结构会自动升级）。如果恢复进来的库初始化/升级失败，自动退回恢复前的数据再抛 BackupError。
    返回 (恢复前备份路径, init_db 的结果)。"""
    ok, err = validate_backup(path)
    if not ok:
        raise BackupError(err)
    conn = get_db_connection()
    pre = make_backup("restore", reason=f"恢复前（恢复到 {os.path.basename(path)} 之前）", force=True)
    if conn.in_transaction:
        conn.commit()

    def copy_into_live(src_file):
        src = sqlite3.connect(_ro_uri(src_file), uri=True)
        try:
            src.backup(conn)
        finally:
            src.close()
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        if _table_exists(conn, "app_config"):
            conn.execute("DELETE FROM app_config WHERE key IN ('backup_reason', 'backup_app', 'backup_app_version')")
            conn.commit()

    copy_into_live(path)
    try:
        info = init_db()
    except Exception as ex:
        print(f"[restore] 恢复后初始化失败，退回恢复前的数据：{ex}")
        copy_into_live(pre)
        init_db()
        load_students_order()
        raise BackupError(f"这份备份恢复后无法使用（{ex}），已退回恢复前的数据")
    load_students_order()
    print(f"[restore] 已从 {path} 恢复")
    return pre, info


# ----------------- 1.2 建库与原地升级 -----------------

_STUDENTS_V2_SQL = """
CREATE TABLE {name} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_id INTEGER NOT NULL REFERENCES classes(id),
    name TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    joined_date TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    left_date TEXT,
    UNIQUE(class_id, name)
)"""

_RECORDS_SQL = """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    date TEXT NOT NULL,         -- 格式: YYYY-MM-DD
    period INTEGER NOT NULL,    -- 0-4
    status TEXT NOT NULL,       -- 出勤, 旷课, 病假, 事假, 空课
    FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
    UNIQUE(student_id, date, period)
)"""

_CLASSES_SQL = """
CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT
)"""

# 测试用：模拟升级写到一半失败（正常运行时恒为 None）
_MIGRATION_FAULT = None


def _ensure_indexes(conn):
    conn.execute("CREATE INDEX IF NOT EXISTS idx_records_date_period ON records(date, period)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_records_student_date ON records(student_id, date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_students_class ON students(class_id)")


def _ensure_default_class(conn):
    if _count(conn, "SELECT COUNT(*) FROM classes") == 0:
        conn.execute("INSERT INTO classes(name, sort_order, created_at) VALUES(?, 0, ?)",
                     (DEFAULT_CLASS_NAME, _today()))
    cur = db_get_config("current_class_id")
    ids = [r[0] for r in conn.execute("SELECT id FROM classes ORDER BY sort_order, id")]
    if cur is None or not str(cur).isdigit() or int(cur) not in ids:
        conn.execute("INSERT OR REPLACE INTO app_config(key, value) VALUES('current_class_id', ?)", (str(ids[0]),))
    conn.commit()


def partial_slots(conn=None, until=None):
    """统计「点过名但有在班同学没有记录」的课节（这些同学一律显示/导出为空课，不询问、不补记）。
    只用于升级时写日志，方便以后排查。返回 [(class_name, date, period, 没有记录的人数)]。只读，不改数据。"""
    conn = conn or get_db_connection()
    until = until or _today()
    out = []
    for cid, cname in conn.execute("SELECT id, name FROM classes ORDER BY sort_order, id").fetchall():
        studs = conn.execute("SELECT id, joined_date, active, left_date FROM students WHERE class_id=?", (cid,)).fetchall()
        slots = conn.execute(
            "SELECT r.date, r.period, COUNT(*) FROM records r JOIN students s ON s.id=r.student_id "
            "WHERE s.class_id=? AND r.date<=? GROUP BY r.date, r.period ORDER BY r.date, r.period",
            (cid, until)).fetchall()
        for d, p, n in slots:
            expected = sum(1 for _, j, a, l in studs
                           if (not j or j <= d) and (a == 1 or (l and d < l)))
            if n < expected:
                out.append((cname, d, p, expected - n))
    return out


def init_db():
    """启动时调用。返回 {"fresh", "migrated", "backup", "partial_slots"}。
    升级失败抛 MigrationError（已回滚）；任何情况下都不删除、不新建来替换已有的数据库文件。"""
    conn = get_db_connection()
    result = {"fresh": False, "migrated": False, "backup": None, "partial_slots": 0, "classes": [], "renamed": {}}
    conn.execute("CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()

    if not _table_exists(conn, "students"):
        if _table_exists(conn, "records") and _count(conn, "SELECT COUNT(*) FROM records") > 0:
            raise MigrationError("数据库里有考勤记录但找不到学生表，无法自动处理。原有数据没有改动。")
        # 新装：直接建新结构，1 个空班「我的班级」
        conn.execute(_CLASSES_SQL)
        conn.execute(_STUDENTS_V2_SQL.format(name="students"))
        conn.execute(_RECORDS_SQL)
        _ensure_indexes(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        _ensure_default_class(conn)
        result["fresh"] = True
        return result

    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver < SCHEMA_VERSION or "class_id" not in _table_cols(conn, "students"):
        result["backup"] = migrate_to_v2(conn, result)
        result["migrated"] = True
        try:
            ps = partial_slots(conn)
            result["partial_slots"] = len(ps)
            if ps:
                print(f"[migrate] 提示：有 {len(ps)} 节课点过名但部分同学没有记录，"
                      f"这些同学在 v1.4.0 里显示/导出为「空课」（v1.3.1 导出时曾按出勤填）：")
                for cname, d, p, n in ps[:200]:
                    print(f"[migrate]   {cname} {d} {PERIOD_LABELS[p]}：{n} 人没有记录")
        except Exception as ex:
            print(f"[migrate] 统计部分标记课节失败（不影响数据）：{ex}")
    conn.execute(_RECORDS_SQL)
    _ensure_indexes(conn)
    conn.commit()
    _ensure_default_class(conn)
    return result


def _plan_v2_classes(conn, cols):
    """升级分班计划（只读）。旧 students 表有 class_name 列且有非空值时，按每个不同的班名（规范化、
    忽略空格比较、最长 CLASS_NAME_MAX 字）各建一个班，空值归入 app_config.class_name（没有就用默认班名）；
    没有 class_name 列的老库只有一个班（与 v1.4.0 相同）。同一个班里重名时按 id 顺序第一个保持原名，
    后面的加 (2)(3)…（加了还撞名就继续往上加），不合并、不删除。
    返回 {"classes": [班名，当前班在第一个，其余按名字排], "current": 当前班名,
          "assign": {学生id: 班名}, "rename": {学生id: 新名字}}"""
    def clean(n):
        return normalize_class_name(n)[:CLASS_NAME_MAX].strip()
    row = conn.execute("SELECT value FROM app_config WHERE key='class_name'").fetchone()
    cfg_name = clean(row[0] if row and row[0] else "") or DEFAULT_CLASS_NAME
    by_key = {_class_key(cfg_name): cfg_name}
    has_col = "class_name" in cols
    rows = conn.execute(
        "SELECT id, name, " + ("class_name" if has_col else "NULL") + " FROM students ORDER BY id").fetchall()
    assign = {}
    for sid, _, cn in rows:
        cn = clean(cn if isinstance(cn, str) else ("" if cn is None else str(cn)))
        if not cn:
            assign[sid] = cfg_name
        else:
            assign[sid] = by_key.setdefault(_class_key(cn), cn)
    used = sorted(set(assign.values()))
    if not used or cfg_name in used:
        current = cfg_name
    else:
        # app_config 里的班名没有任何学生：当前班选考勤最多（再按人数、名字）的那个
        nrec = {}
        if _table_exists(conn, "records"):
            for sid, n in conn.execute("SELECT student_id, COUNT(*) FROM records GROUP BY student_id"):
                if sid in assign:
                    nrec[assign[sid]] = nrec.get(assign[sid], 0) + n
        npeople = {c: sum(1 for v in assign.values() if v == c) for c in used}
        current = sorted(used, key=lambda c: (-nrec.get(c, 0), -npeople[c], c))[0]
    classes = [current] + [c for c in used if c != current]
    rename, taken = {}, {}
    for sid, name, _ in rows:
        taken.setdefault(assign[sid], set()).add(name)
    seen = {}
    for sid, name, _ in rows:
        cls = assign[sid]
        if (cls, name) in seen:
            k = 2
            while f"{name}({k})" in taken[cls]:
                k += 1
            rename[sid] = f"{name}({k})"
            taken[cls].add(rename[sid])
        else:
            seen[(cls, name)] = sid
    return {"classes": classes, "current": current, "assign": assign, "rename": rename}


def migrate_to_v2(conn, report=None):
    """把 v1.2 / v1.3 的旧库原地升级为多班结构（旧名单带班级标记时按原班级分班）。
    返回升级前备份路径（空库为 None）。report（dict）会写入 classes（分出的班，只有 1 个班时为空）、renamed（改名）。"""
    n_students = _count(conn, "SELECT COUNT(*) FROM students")
    n_records = _count(conn, "SELECT COUNT(*) FROM records") if _table_exists(conn, "records") else 0

    # 1) 先备份（在任何结构改动之前）；备份失败 = 停止升级，什么都不改
    backup = None
    if n_students + n_records > 0:
        try:
            backup = make_backup("upgrade", reason=f"升级到 v{APP_VERSION} 前", conn=conn)
        except Exception as ex:
            raise MigrationError(f"升级前备份失败（{ex}），为保护数据已停止升级。原有数据没有改动。", None)

    conn.commit()
    # 2) 必须关闭外键：开着外键 DROP 旧学生表会把所有考勤级联删光
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        cols = _table_cols(conn, "students")
        # 旧版补字段步骤（v1.2.0 老库没有 joined_date / 可能没有 sort_order）
        if "sort_order" not in cols:
            conn.execute("ALTER TABLE students ADD COLUMN sort_order INTEGER DEFAULT 0")
        if "joined_date" not in cols:
            conn.execute("ALTER TABLE students ADD COLUMN joined_date TEXT")
        max_so = conn.execute("SELECT MAX(sort_order) FROM students").fetchone()[0]
        if n_students > 1 and (max_so is None or max_so == 0):
            # 只在旧库第一次升级时执行：按默认顺序补排序，其余人按 id 排在后面
            rank = {n: i for i, n in enumerate(DEFAULT_STUDENTS_ORDER)}
            rows = conn.execute("SELECT id, name FROM students ORDER BY id").fetchall()
            rows.sort(key=lambda r: (rank.get(r[1], len(rank)), r[0]))
            for i, (sid, _) in enumerate(rows):
                conn.execute("UPDATE students SET sort_order=? WHERE id=?", (i, sid))
        conn.execute(_RECORDS_SQL)

        before_students = conn.execute(
            "SELECT id, name, sort_order, joined_date FROM students ORDER BY id").fetchall()
        before_records = conn.execute(
            "SELECT id, student_id, date, period, status FROM records ORDER BY id").fetchall()
        fk_before = len(conn.execute("PRAGMA foreign_key_check(records)").fetchall())

        conn.execute(_CLASSES_SQL)
        # 分班计划：旧名单如果带 class_name 标记，按原班级分开建班；同班重名的后来者名字加 (2)(3)…
        plan = _plan_v2_classes(conn, cols)
        expect_class = {}
        existing = {r[0] for r in conn.execute("SELECT name FROM classes")}
        for i, cname in enumerate(plan["classes"]):
            if cname not in existing:
                conn.execute("INSERT INTO classes(name, sort_order, created_at) VALUES(?, ?, ?)",
                             (cname, i, _today()))
        cid_by_name = {n: i for i, n in conn.execute("SELECT id, name FROM classes")}
        default_cid = conn.execute("SELECT MIN(id) FROM classes").fetchone()[0]
        cur_cid = cid_by_name.get(plan["current"], default_cid)
        for sid, cname in plan["assign"].items():
            expect_class[sid] = cid_by_name.get(cname, default_cid)

        conn.execute("DROP TABLE IF EXISTS students_new")
        conn.execute(_STUDENTS_V2_SQL.format(name="students_new"))
        for sid, name, so, jd in before_students:
            conn.execute(
                "INSERT INTO students_new(id, class_id, name, sort_order, joined_date, active, left_date) "
                "VALUES(?, ?, ?, ?, ?, 1, NULL)",
                (sid, expect_class.get(sid, default_cid), plan["rename"].get(sid, name), so, jd))
        if _MIGRATION_FAULT == "after_copy":
            raise RuntimeError("模拟故障：写到一半失败")
        conn.execute("DROP TABLE students")
        conn.execute("ALTER TABLE students_new RENAME TO students")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_students_class ON students(class_id)")
        conn.execute("INSERT OR REPLACE INTO app_config(key, value) VALUES('current_class_id', ?)", (str(cur_cid),))
        if _MIGRATION_FAULT == "before_verify":
            conn.execute("DELETE FROM records WHERE id = (SELECT MIN(id) FROM records)")
        if _MIGRATION_FAULT == "wrong_class":
            conn.execute("UPDATE students SET class_id=? WHERE id=(SELECT MAX(id) FROM students)", (default_cid + 1,))
        if _MIGRATION_FAULT == "wrong_name":
            conn.execute("UPDATE students SET name=name||'x' WHERE id=(SELECT MAX(id) FROM students)")

        # 3) 核对：学生按 id 逐条（id/排序/加入日期完全一致，姓名一致或等于预先算好的改名，班级等于分班计划），
        #    考勤逐条完全一致，外键检查没有新增问题（原来就有的孤儿记录原样保留）；否则整体回滚
        after_students = conn.execute(
            "SELECT id, name, sort_order, joined_date, class_id FROM students ORDER BY id").fetchall()
        after_records = conn.execute(
            "SELECT id, student_id, date, period, status FROM records ORDER BY id").fetchall()
        fk_after = len(conn.execute("PRAGMA foreign_key_check(records)").fetchall())
        if len(after_students) != len(before_students):
            raise RuntimeError(f"核对失败：学生 {len(before_students)} → {len(after_students)}")
        for (sid, name, so, jd), (sid2, name2, so2, jd2, cid2) in zip(before_students, after_students):
            if (sid2, so2, jd2) != (sid, so, jd) or name2 != plan["rename"].get(sid, name) \
                    or cid2 != expect_class.get(sid, default_cid):
                raise RuntimeError(f"核对失败：学生 id={sid} 不一致")
        if after_records != before_records:
            raise RuntimeError(f"核对失败：考勤 {len(before_records)} → {len(after_records)}")
        if fk_after > fk_before:
            raise RuntimeError(f"核对失败：外键问题 {fk_before} → {fk_after}")
        if len(plan["classes"]) > 1:
            print(f"[migrate] 按原班级分成 {len(plan['classes'])} 个班：" + "、".join(
                f"{n}（{sum(1 for v in plan['assign'].values() if v == n)} 人）" for n in plan["classes"])
                + f"；当前班级：{plan['current']}")
        for sid, newname in sorted(plan["rename"].items()):
            print(f"[migrate] 同班重名：id={sid} 改名为『{newname}』（{plan['assign'].get(sid)}）")
        if report is not None:
            report["classes"] = list(plan["classes"]) if len(plan["classes"]) > 1 else []
            report["renamed"] = dict(plan["rename"])
            report["orphan_records"] = fk_after

        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        print(f"[migrate] 升级完成：{len(after_students)} 名学生、{len(after_records)} 条考勤，备份：{backup}")
    except Exception as ex:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[migrate] 升级失败，已回滚：{ex}")
        raise MigrationError(f"数据升级没有完成（{ex}）。原有数据没有改动。", backup)
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    return backup


# ----------------- 1.3 班级 -----------------

CLASS_NAME_MAX = 20


def normalize_class_name(name):
    return re.sub(r"\s+", " ", (name or "").replace("\u3000", " ")).strip()


def _class_key(name):
    return re.sub(r"\s+", "", (name or "").replace("\u3000", ""))


def validate_class_name(name, exclude_id=None):
    """返回 (规范化后的名字, 错误信息或 None)。比较时忽略空格。"""
    n = normalize_class_name(name)
    if not n:
        return n, "班级名称不能为空"
    if len(n) > CLASS_NAME_MAX:
        return n, f"班级名称最长 {CLASS_NAME_MAX} 个字"
    for cid, cname in get_db_connection().execute("SELECT id, name FROM classes").fetchall():
        if cid != exclude_id and _class_key(cname) == _class_key(n):
            return n, f"已有同名班级『{cname}』"
    return n, None


def list_classes():
    """[(id, name, 在名单人数)]，按 sort_order。"""
    return get_db_connection().execute(
        "SELECT c.id, c.name, (SELECT COUNT(*) FROM students s WHERE s.class_id=c.id AND s.active=1) "
        "FROM classes c ORDER BY c.sort_order, c.id").fetchall()


def get_current_class_id():
    conn = get_db_connection()
    cur = db_get_config("current_class_id")
    if cur is not None and str(cur).isdigit():
        if conn.execute("SELECT 1 FROM classes WHERE id=?", (int(cur),)).fetchone():
            return int(cur)
    row = conn.execute("SELECT id FROM classes ORDER BY sort_order, id LIMIT 1").fetchone()
    return row[0] if row else None


def get_class_name(cid=None):
    cid = cid if cid is not None else get_current_class_id()
    row = get_db_connection().execute("SELECT name FROM classes WHERE id=?", (cid,)).fetchone()
    return row[0] if row else DEFAULT_CLASS_NAME


def set_current_class(cid):
    db_set_config("current_class_id", int(cid))
    load_students_order()


def create_class(name):
    n, err = validate_class_name(name)
    if err:
        raise ValueError(err)
    conn = get_db_connection()
    so = (conn.execute("SELECT MAX(sort_order) FROM classes").fetchone()[0] or 0) + 1
    cur = conn.execute("INSERT INTO classes(name, sort_order, created_at) VALUES(?,?,?)", (n, so, _today()))
    conn.commit()
    return cur.lastrowid


def rename_class(cid, name):
    n, err = validate_class_name(name, exclude_id=cid)
    if err:
        raise ValueError(err)
    conn = get_db_connection()
    conn.execute("UPDATE classes SET name=? WHERE id=?", (n, cid))
    conn.commit()
    return n


def class_stats(cid):
    conn = get_db_connection()
    students = _count(conn, "SELECT COUNT(*) FROM students WHERE class_id=? AND active=1", (cid,))
    records = _count(conn, "SELECT COUNT(*) FROM records WHERE student_id IN (SELECT id FROM students WHERE class_id=?)", (cid,))
    return {"students": students, "records": records}


def delete_class(cid):
    """删除一个班（含其学生和考勤）。至少保留 1 个班。删前自动备份。返回备份路径。"""
    conn = get_db_connection()
    if _count(conn, "SELECT COUNT(*) FROM classes") <= 1:
        raise ValueError("至少要保留 1 个班级")
    name = get_class_name(cid)
    has_data = _count(conn, "SELECT COUNT(*) FROM students WHERE class_id=?", (cid,)) > 0
    backup = make_backup("op", reason=f"操作前（删除班级「{name}」）") if has_data else None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM records WHERE student_id IN (SELECT id FROM students WHERE class_id=?)", (cid,))
        conn.execute("DELETE FROM students WHERE class_id=?", (cid,))
        conn.execute("DELETE FROM classes WHERE id=?", (cid,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    db_set_config("current_class_id", get_current_class_id())   # 删掉的是当前班 → 自动切到第一个班
    load_students_order()
    return backup


# ----------------- 1.4 名单 -----------------

def load_students_order():
    """把当前班级 active=1 的学生按排序载入全局缓存。"""
    global STUDENTS_ORDER, STUDENT_JOINED
    cid = get_current_class_id()
    rows = get_db_connection().execute(
        "SELECT name, joined_date FROM students WHERE class_id=? AND active=1 ORDER BY sort_order, id", (cid,)).fetchall()
    STUDENTS_ORDER = [r[0] for r in rows]
    STUDENT_JOINED = {r[0]: r[1] for r in rows}


def get_class_students(cid=None, include_inactive=False):
    """[dict(id, name, sort_order, joined_date, active, left_date)]：在名单的按排序在前，已移出的在后。"""
    cid = cid if cid is not None else get_current_class_id()
    sql = "SELECT id, name, sort_order, joined_date, active, left_date FROM students WHERE class_id=?"
    if not include_inactive:
        sql += " AND active=1"
    sql += " ORDER BY active DESC, sort_order, id"
    keys = ("id", "name", "sort_order", "joined_date", "active", "left_date")
    return [dict(zip(keys, r)) for r in get_db_connection().execute(sql, (cid,)).fetchall()]


def find_name_in_other_classes(name, cid=None):
    """[(student_id, class_id, class_name, 记录条数)]：其他班里在名单上的同名同学。"""
    cid = cid if cid is not None else get_current_class_id()
    return get_db_connection().execute(
        "SELECT s.id, c.id, c.name, (SELECT COUNT(*) FROM records r WHERE r.student_id=s.id) "
        "FROM students s JOIN classes c ON c.id=s.class_id "
        "WHERE s.name=? AND s.class_id!=? AND s.active=1 ORDER BY c.sort_order, c.id", (name, cid)).fetchall()


def roster_is_dirty(editing, saved):
    """editing: [{"id","name",...}]；saved: [(id, name)] 数据库里当前班的名单（按顺序）。
    改名、换顺序、增删、移班都算有改动。"""
    return [(e.get("id"), e.get("name")) for e in editing] != [tuple(x) for x in saved]


def plan_roster_changes(cid, entries):
    """根据编辑缓存算出保存时要做的事（不写库）。冲突时抛 ValueError。
    entries: [{"id": 学生id或None, "name": 姓名}]（顺序即排序）。"""
    conn = get_db_connection()
    rows = {r[0]: {"id": r[0], "class_id": r[1], "name": r[2], "active": r[3]}
            for r in conn.execute("SELECT id, class_id, name, active FROM students WHERE class_id=?", (cid,))}
    names = []
    resolved = []
    used_ids = {e["id"] for e in entries if e.get("id") is not None}
    for e in entries:
        n = (e.get("name") or "").strip()
        if not n:
            raise ValueError("名单里有空白姓名")
        if n in names:
            raise ValueError(f"名单里『{n}』出现了两次，同班姓名不能重复（可改成「{n}(2)」）")
        names.append(n)
        sid = e.get("id")
        if sid is None:
            # 和本班某个不在编辑列表里的人同名：当作同一个人（已移出的 = 恢复；刚删又加回 = 不变）
            for r in rows.values():
                if r["name"] == n and r["id"] not in used_ids:
                    sid = r["id"]
                    used_ids.add(sid)
                    break
        resolved.append((sid, n))
    final_ids = {sid for sid, _ in resolved if sid is not None}
    # 改名/新增撞上本班「不在新名单里」的人（已移出或这次移出）
    for sid, n in resolved:
        for r in rows.values():
            if r["name"] == n and r["id"] != sid and r["id"] not in final_ids:
                raise ValueError(f"本班有已移出名单的同学也叫『{n}』，请加区分标记，或者直接把『{n}』加回来")
    other = {}
    ids_other = [sid for sid, _ in resolved if sid is not None and sid not in rows]
    for sid in ids_other:
        r = conn.execute("SELECT s.id, s.class_id, s.name, s.active, c.name FROM students s "
                         "JOIN classes c ON c.id=s.class_id WHERE s.id=?", (sid,)).fetchone()
        if not r:
            raise ValueError("要移入的同学已不存在，请重新添加")
        other[sid] = {"id": r[0], "class_id": r[1], "name": r[2], "active": r[3], "class_name": r[4]}

    plan = {"resolved": resolved, "added": [], "renamed": [], "removed": [], "reactivated": [], "moved": []}
    for sid, n in resolved:
        if sid is None:
            plan["added"].append(n)
        elif sid in other:
            plan["moved"].append((sid, other[sid]["class_id"], other[sid]["class_name"], other[sid]["name"], n))
            if other[sid]["name"] != n:
                plan["renamed"].append((sid, other[sid]["name"], n))
        else:
            r = rows[sid]
            if not r["active"]:
                plan["reactivated"].append((sid, n))
            if r["name"] != n:
                plan["renamed"].append((sid, r["name"], n))
    for r in rows.values():
        if r["active"] and r["id"] not in final_ids:
            cnt = _count(conn, "SELECT COUNT(*) FROM records WHERE student_id=?", (r["id"],))
            plan["removed"].append((r["id"], r["name"], cnt))
    return plan


def apply_roster_changes(cid, entries, purge_removed=False):
    """保存名单：改名原地 UPDATE（记录跟着 id 走）；移出 = 软删除（active=0，保留记录）；
    勾选永久删除时才删人删记录；有移出/改名/移班时先自动备份。返回 plan + backup。"""
    plan = plan_roster_changes(cid, entries)
    conn = get_db_connection()
    risky = plan["removed"] or plan["renamed"] or plan["moved"]
    backup = None
    if risky:
        parts = []
        if plan["removed"]:
            parts.append(f"移出 {len(plan['removed'])} 人")
        if plan["renamed"]:
            parts.append(f"改名 {len(plan['renamed'])} 人")
        if plan["moved"]:
            parts.append(f"移班 {len(plan['moved'])} 人")
        backup = make_backup("op", reason="操作前（保存名单：" + "，".join(parts) + "）")
    today = _today()
    class_has_records = _count(conn, "SELECT COUNT(*) FROM records WHERE student_id IN "
                                     "(SELECT id FROM students WHERE class_id=?)", (cid,)) > 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        touch = {sid for sid, _, _ in plan["renamed"]} | {m[0] for m in plan["moved"]}
        for sid in touch:   # 先改成临时名，避免互换/连环改名时撞上 UNIQUE(class_id, name)
            conn.execute("UPDATE students SET name=? WHERE id=?", (f"__tmp_rename_{sid}__", sid))
        for sid, _, _ in plan["removed"]:
            if purge_removed:
                conn.execute("DELETE FROM records WHERE student_id=?", (sid,))
                conn.execute("DELETE FROM students WHERE id=?", (sid,))
            else:
                conn.execute("UPDATE students SET active=0, left_date=? WHERE id=?", (today, sid))
        for idx, (sid, n) in enumerate(plan["resolved"]):
            if sid is None:
                conn.execute("INSERT INTO students(class_id, name, sort_order, joined_date, active) VALUES(?,?,?,?,1)",
                             (cid, n, idx, today if class_has_records else None))
            else:
                conn.execute("UPDATE students SET class_id=?, name=?, sort_order=?, active=1, left_date=NULL WHERE id=?",
                             (cid, n, idx, sid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    load_students_order()
    plan["backup"] = backup
    plan["purged"] = bool(purge_removed and plan["removed"])
    plan["moved_from"] = sorted({m[1] for m in plan["moved"]})
    return plan


def update_db_roster(new_names, cid=None):
    """兼容旧接口：按名字保存当前班名单（同名 = 同一个人；不在新名单里的软删除）。"""
    cid = cid if cid is not None else get_current_class_id()
    cur = {s["name"]: s["id"] for s in get_class_students(cid, include_inactive=True)}
    return apply_roster_changes(cid, [{"id": cur.get(n), "name": n} for n in new_names])


def move_students(student_ids, to_cid, from_cid=None):
    """把学生连同考勤移到另一个班。目标班已有同名（含已移出）则拦下。删改前自动备份。"""
    conn = get_db_connection()
    from_cid = from_cid if from_cid is not None else get_current_class_id()
    if not student_ids:
        raise ValueError("请先勾选要移动的同学")
    target_names = {r[0] for r in conn.execute("SELECT name FROM students WHERE class_id=?", (to_cid,))}
    names = [conn.execute("SELECT name FROM students WHERE id=?", (sid,)).fetchone()[0] for sid in student_ids]
    clash = [n for n in names if n in target_names]
    if clash:
        raise ValueError(f"目标班已有同名的同学：{'、'.join(clash)}，请先改名再移")
    backup = make_backup("op", reason=f"操作前（移班 {len(student_ids)} 人到「{get_class_name(to_cid)}」）")
    base = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM students WHERE class_id=?", (to_cid,)).fetchone()[0]
    try:
        conn.execute("BEGIN IMMEDIATE")
        for i, sid in enumerate(student_ids):
            conn.execute("UPDATE students SET class_id=?, sort_order=? WHERE id=?", (to_cid, base + 1 + i, sid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    load_students_order()
    return backup


def shared_slots(cid_a, cid_b):
    """两个班在同一天同一节都有记录的课节（以前用合并名单点名留下的）。
    返回 [(date, period, {cid: (记录数, 非出勤数)})]。"""
    conn = get_db_connection()
    data = {}
    for cid in (cid_a, cid_b):
        for d, p, n, ab in conn.execute(
                "SELECT r.date, r.period, COUNT(*), SUM(r.status!='出勤') FROM records r "
                "JOIN students s ON s.id=r.student_id WHERE s.class_id=? GROUP BY r.date, r.period", (cid,)):
            data.setdefault((d, p), {})[cid] = (n, ab or 0)
    return [(d, p, v) for (d, p), v in sorted(data.items()) if len(v) == 2]


def resolve_shared_slots(decisions):
    """decisions: [(date, period, keep_cid, drop_cid)]；keep_cid=None 表示两个班都有课（不处理）。
    删除 drop_cid 班学生在该节的记录。删前自动备份。返回 (删除条数, 备份路径)。"""
    todo = [d for d in decisions if d[2] is not None]
    if not todo:
        return 0, None
    conn = get_db_connection()
    backup = make_backup("op", reason=f"操作前（串课处理 {len(todo)} 节）")
    removed = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for d, p, keep, drop in todo:
            cur = conn.execute("DELETE FROM records WHERE date=? AND period=? AND student_id IN "
                               "(SELECT id FROM students WHERE class_id=?)", (d, p, drop))
            removed += cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return removed, backup


# ----------------- 1.5 考勤记录（全部按班级） -----------------

def get_records(date_str, period, cid=None):
    cid = cid if cid is not None else get_current_class_id()
    rows = get_db_connection().execute("""
        SELECT s.name, r.status FROM records r JOIN students s ON r.student_id = s.id
        WHERE r.date = ? AND r.period = ? AND s.class_id = ? AND s.active = 1
    """, (date_str, period, cid)).fetchall()
    return {name: status for name, status in rows}


def slot_taken(date_str, period, cid=None):
    """这一节是否点过名：本班任何一个学生（含已移出）在这一节有记录。"""
    cid = cid if cid is not None else get_current_class_id()
    return get_db_connection().execute(
        "SELECT 1 FROM records r JOIN students s ON s.id=r.student_id "
        "WHERE r.date=? AND r.period=? AND s.class_id=? LIMIT 1", (date_str, period, cid)).fetchone() is not None


def _student_id(conn, name, cid):
    row = conn.execute("SELECT id FROM students WHERE name=? AND class_id=? AND active=1", (name, cid)).fetchone()
    return row[0] if row else None


def save_record(date_str, period, name, status, cid=None):
    cid = cid if cid is not None else get_current_class_id()
    conn = get_db_connection()
    sid = _student_id(conn, name, cid)
    if sid is None:  # 名单里已不存在该学生（例如刚被移出），跳过而不是闪退
        print(f"[save_record] 名单中找不到 {name}，已跳过")
        return
    conn.execute("""
        INSERT INTO records (student_id, date, period, status) VALUES (?, ?, ?, ?)
        ON CONFLICT(student_id, date, period) DO UPDATE SET status=excluded.status
    """, (sid, date_str, period, status))
    conn.commit()


def bulk_save_records(date_str, period, name_status_dict, cid=None):
    cid = cid if cid is not None else get_current_class_id()
    conn = get_db_connection()
    for name, status in name_status_dict.items():
        sid = _student_id(conn, name, cid)
        if sid is None:
            print(f"[bulk_save_records] 名单中找不到 {name}，已跳过")
            continue
        conn.execute("""
            INSERT INTO records (student_id, date, period, status) VALUES (?, ?, ?, ?)
            ON CONFLICT(student_id, date, period) DO UPDATE SET status=excluded.status
        """, (sid, date_str, period, status))
    conn.commit()


def get_available_months(cid=None, today=None):
    """本班有非「空课」记录的月份（不含未来日期）。"""
    cid = cid if cid is not None else get_current_class_id()
    today = today or _today()
    rows = get_db_connection().execute("""
        SELECT DISTINCT substr(r.date, 1, 7) FROM records r JOIN students s ON s.id=r.student_id
        WHERE r.status != '空课' AND s.class_id=? AND r.date <= ?
    """, (cid, today)).fetchall()
    return sorted(r[0] for r in rows)


def effective_statuses(roster, records, date_str):
    """点名页每个人显示什么（v1.4.0 规则，和复制汇总、导出完全一致）：
    有记录 → 记录值；入班日期晚于该日且没记录 → 未入班；其余没记录 → 空课。
    roster: [(name, joined_date)]；records: {name: status}。"""
    out = {}
    for name, joined in roster:
        if name in records:
            out[name] = records[name]
        elif joined and date_str < joined:
            out[name] = NOT_JOINED
        else:
            out[name] = "空课"
    return out


def _md(date_str):
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        return dt.month, dt.day
    except Exception:
        return None, None


def build_summary(date_str, period, class_name, statuses, taken, unmarked=0, today=None):
    """复制微信汇总。返回 (要复制的文字或 None, 提示语或 None, 类型)。
    类型：untaken / future / allnoclass / ok。
    statuses: [(name, 显示状态)]；taken: 本班这一节有没有任何记录；unmarked: 点过名但没有记录的在班人数。"""
    today = today or _today()
    m, d = _md(date_str)
    md = f"{m}月{d}日" if m else date_str
    slot = PERIOD_LABELS[period]
    if not taken:
        if date_str > today:
            return None, f"{md} {slot}还没到，暂无点名记录，未复制", "future"
        return None, f"{md} {slot}暂无点名记录，未复制。请先点名（点「全员出勤」，再点缺勤的同学）", "untaken"
    considered = [s for _, s in statuses if s != NOT_JOINED]
    if considered and all(s == "空课" for s in considered):
        return None, f"{md} {slot}已标记为全员空课，无需汇报", "allnoclass"
    header = f"{m}.{d} {class_name} {slot.replace('节', '')}" if m else f"{date_str} {class_name} {slot.replace('节', '')}"
    groups = {"旷课": [], "病假": [], "事假": []}
    for name, s in statuses:
        if s in groups:
            groups[s].append(name)
    lines = [header]
    if not any(groups.values()):
        lines.append("全")
    else:
        if groups["旷课"]:
            lines.append(f"未到：{' '.join(groups['旷课'])}")
        if groups["病假"]:
            lines.append(f"病假：{' '.join(groups['病假'])}")
        if groups["事假"]:
            lines.append(f"事假：{' '.join(groups['事假'])}")
    hint = f"注意：有 {unmarked} 人这节没有标记（显示为空课），没有写进汇总" if unmarked else None
    return "\n".join(lines), hint, "ok"


def export_filename(month_label, class_name):
    safe = re.sub(r'[\\/:*?"<>|\s]+', "", class_name or "") or "班级"
    return f"{month_label}{safe}上课出勤.xlsx"


def export_db_to_excel(target_ym, class_name=None, cid=None, today=None):
    """导出本班某月考勤。格子规则（与点名页一致）：有记录照填；入班前 / 移出后留空；其余（没有记录）= 空课。
    一整天都没有非「空课」记录的日期、以及未来日期不导出。"""
    cid = cid if cid is not None else get_current_class_id()
    class_name = class_name or get_class_name(cid)
    today = today or _today()
    conn = get_db_connection()
    raw_records = conn.execute("""
        SELECT r.date, r.student_id, r.period, r.status FROM records r JOIN students s ON r.student_id = s.id
        WHERE substr(r.date, 1, 7) = ? AND s.class_id = ? AND r.date <= ?
    """, (target_ym, cid, today)).fetchall()
    if not raw_records:
        return None, f"本班暂无 {target_ym} 的考勤记录"

    from collections import defaultdict
    grouped = defaultdict(lambda: defaultdict(dict))
    for date_str, sid, period, status in raw_records:
        grouped[date_str][period][sid] = status
    filtered = {d: pdata for d, pdata in grouped.items()
                if any(st != "空课" for sd in pdata.values() for st in sd.values())}
    if not filtered:
        return None, f"该月份 {target_ym} 内暂无任何有效的非‘空课’考勤记录"
    sorted_dates = sorted(filtered)

    month_start = f"{target_ym}-01"
    roster = conn.execute(
        "SELECT id, name, joined_date, left_date FROM students WHERE class_id=? AND active=1 ORDER BY sort_order, id",
        (cid,)).fetchall()
    # 已移出的同学：移出日期晚于当月 1 号，或当月在本班有任何记录（含 1 号移出当天有记录、移出日期为空的情况）都要进表，
    # 否则他当月的记录会在表上丢失
    roster += conn.execute(
        "SELECT id, name, joined_date, left_date FROM students s WHERE class_id=? AND active=0 AND "
        "(left_date > ? OR EXISTS (SELECT 1 FROM records r WHERE r.student_id = s.id AND substr(r.date, 1, 7) = ?)) "
        "ORDER BY sort_order, id", (cid, month_start, target_ym)).fetchall()
    try:
        month_label = f"{int(target_ym.split('-')[1])}月"
    except Exception:
        month_label = target_ym
    filename = export_filename(month_label, class_name)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for d_idx, date_str in enumerate(sorted_dates):
        try:
            dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            formatted_date = f"{dt.month:02d}月{dt.day:02d}日"
        except Exception:
            formatted_date = date_str
        block_v, block_h = d_idx // 3, d_idx % 3
        start_row = block_v * (len(roster) + 1) + 1
        start_col = block_h * 7 + 1
        ws.cell(row=start_row, column=start_col, value="姓名")
        ws.cell(row=start_row, column=start_col + 1, value=formatted_date)
        for p in range(5):
            ws.cell(row=start_row, column=start_col + 2 + p, value=PERIOD_LABELS[p])
        day_data = filtered[date_str]
        for s_idx, (sid, name, joined, left) in enumerate(roster):
            row = start_row + 1 + s_idx
            ws.cell(row=row, column=start_col, value=name)
            ws.cell(row=row, column=start_col + 1, value=None)
            for p in range(5):
                pdata = day_data.get(p, {})
                if sid in pdata:
                    status = pdata[sid]
                elif (joined and date_str < joined) or (left and date_str >= left):
                    status = None
                else:
                    status = "空课"
                cell = ws.cell(row=row, column=start_col + 2 + p, value=status)
                if status in ["旷课", "病假", "事假"]:
                    cell.font = Font(color="FF0000")
    out_path = os.path.join(os.path.dirname(DB_PATH), filename)
    wb.save(out_path)
    return out_path, None


# 兼容度极高的一键导入名册逻辑（规则见 name_import.py）
def parse_names_from_excel(filepath):
    names, info, err = parse_names(filepath)
    if err:
        return None, err
    return names, None


# ----------------- 1.6 辅助布局函数 -----------------
def build_import_preview(names, info, new_cnt, dup_lines, current):
    """导入确认框内容：上面来源，中间固定高度的小框列出全部姓名（滚动条一直显示），
    下面的重复提醒和「替换/合并追加」说明在小框外，不随滚动。"""
    name_rows = [
        ft.Row([ft.Text(f"{i}.", size=12, color=ft.Colors.GREY_500, width=34, text_align=ft.TextAlign.RIGHT),
                ft.Text(n, size=14, color=ft.Colors.BLACK, expand=True),
                ft.Text("新" if n not in current else "", size=11, color=ft.Colors.BLUE_600)],
               spacing=6)
        for i, n in enumerate(names, 1)]
    name_box = ft.Container(
        content=ft.Column(name_rows, spacing=2, scroll=IMPORT_PREVIEW_SCROLLBAR),
        height=IMPORT_PREVIEW_HEIGHT,
        padding=ft.Padding.only(left=4, right=12, top=6, bottom=6),
        border=ft.Border.all(1, ft.Colors.GREY_300), border_radius=8, bgcolor=ft.Colors.GREY_50)
    tail = [ft.Text(x, size=12, color=ft.Colors.ORANGE_800) for x in dup_lines]
    tail.append(ft.Text("「替换」会用导入结果覆盖当前名单并立即保存；「合并追加」只把新名字加到末尾并立即保存。", size=12, color=ft.Colors.GREY_700))
    return ft.Column(
        [ft.Text(f"来源：{info}", size=12, color=ft.Colors.GREY_700),
         ft.Text(f"全部姓名（共 {len(names)} 个{'，可在框内上下滑动' if len(names) > 8 else ''}；"
                 f"其中 {new_cnt} 人不在当前名单中，标「新」）",
                 size=12, color=ft.Colors.GREY_700),
         name_box] + tail,
        tight=True, spacing=8, width=320)


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


def card_style(status):
    """返回 (边框, 阴影, 背景色)。"""
    color_hex = STATUS_STYLES.get(status, ("#6b7280", ""))[0]
    if status == "出勤":
        return ft.Border.all(1, UI_LINE), ft.BoxShadow(blur_radius=2, offset=ft.Offset(0, 1), color="#0a000000"), ft.Colors.WHITE
    if status == "旷课":
        return ft.Border.all(2, color_hex), ft.BoxShadow(spread_radius=1, blur_radius=8, color="#1fef4444"), "#fef2f2"
    if status == "病假":
        return ft.Border.all(2, color_hex), ft.BoxShadow(spread_radius=1, blur_radius=8, color="#1fea580c"), "#fff7ed"
    if status == "事假":
        return ft.Border.all(2, color_hex), ft.BoxShadow(spread_radius=1, blur_radius=8, color="#1fd97706"), "#fefce8"
    # 空课 / 未入班
    return ft.Border.all(1, UI_LINE), ft.BoxShadow(blur_radius=2, offset=ft.Offset(0, 1), color="#0a000000"), ft.Colors.WHITE


RESCUE_DIR_NAME = "rescue_export"


def export_rescue_files(backup=None):
    """失败页「导出数据」：用 sqlite3 backup API 只读复制当前数据库（原库不动），写上本软件的备份标识，
    连同升级前备份（有才加）一起放进数据文件旁的 rescue_export/。返回 (导出目录, 文件路径列表)。
    导出的文件都能被「从文件恢复」（validate_backup / prepare_restore_file）直接认出来。"""
    out_dir = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), RESCUE_DIR_NAME)
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    files = []
    if os.path.isfile(DB_PATH):
        dst = os.path.join(out_dir, f"点名助手_当前数据_{ts}.db")
        tmp = dst + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        s = sqlite3.connect(_ro_uri(DB_PATH), uri=True)
        d = sqlite3.connect(tmp)
        try:
            s.backup(d)
            d.execute("PRAGMA journal_mode=DELETE")
            stamp_backup_file(d, "升级失败时导出（当前数据）")
        finally:
            d.close(); s.close()
        os.replace(tmp, dst)
        files.append(dst)
    if backup and os.path.isfile(backup):
        dst = os.path.join(out_dir, f"点名助手_升级前备份_{ts}.db")
        shutil.copy2(backup, dst)
        files.append(dst)
    return out_dir, files



# ----------------- 1.x 更新弹窗（关于页 / 升级失败页共用） -----------------

def _lanzou_help_text_control():
    head, tail = LANZOU_HELP_TEXT.split("最上面", 1)
    return ft.Text(spans=[ft.TextSpan(head),
                          ft.TextSpan("最上面", ft.TextStyle(weight=ft.FontWeight.BOLD)),
                          ft.TextSpan(tail)],
                   size=11, color=ft.Colors.GREY_600)


def show_update_dialog(page, info, open_link, notify):
    """发现新版本弹窗（关于页 / 失败页共用）。"""
    def later(e2):
        page.pop_dialog()

    def download(url):
        def handler(e2):
            page.pop_dialog()
            open_link(url)
        return handler

    gitee_btn = ft.Button(
        content=ft.Row([ft.Icon(ft.Icons.DOWNLOAD_ROUNDED, color=ft.Colors.WHITE, size=20),
                        ft.Text("国内直链（Gitee）", color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD)],
                       spacing=8, tight=True),
        bgcolor="#C71D23", on_click=download(info.get("gitee_apk_url") or ""),
        tooltip="国内直链，推荐优先使用")
    github_btn = ft.Button(
        content=ft.Row([ft.Image(src="github_mark_white.png", width=20, height=20),
                        ft.Text("GitHub 下载", color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD)],
                       spacing=8, tight=True),
        bgcolor="#24292F", on_click=download(info["github_apk_url"]),
        tooltip="用浏览器直接下载最新安装包")
    lanzou_btn = ft.Button(
        content=ft.Row([ft.Icon(ft.Icons.CLOUD_DOWNLOAD_ROUNDED, color=ft.Colors.WHITE, size=20),
                        ft.Text("蓝奏云下载", color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD)],
                       spacing=8, tight=True),
        bgcolor=ft.Colors.BLUE_600, on_click=download(info["lanzou_url"]),
        tooltip="打开蓝奏云下载页（直链下不动时用）")
    lanzou_code = info.get("lanzou_code", "")

    def copy_lanzou_code(e2):
        async def set_clip():
            await ft.Clipboard().set(lanzou_code)
        page.run_task(set_clip)
        notify("提取码已复制")

    lanzou_block = lanzou_btn
    if lanzou_code:
        lanzou_block = ft.Column([
            lanzou_btn,
            ft.Row([ft.Text("提取码：", size=14, color=ft.Colors.GREY_800),
                    ft.Text(lanzou_code, size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_800,
                            selectable=True),
                    ft.TextButton("复制", icon=ft.Icons.CONTENT_COPY_ROUNDED, on_click=copy_lanzou_code,
                                  tooltip="复制蓝奏云提取码")],
                   spacing=4, alignment=ft.MainAxisAlignment.CENTER,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.STRETCH, tight=True)
    if info.get("lanzou_url"):
        # 蓝奏云下载说明（灰色小字，「最上面」加粗）：文字见 LANZOU_HELP_TEXT
        if lanzou_block is lanzou_btn:
            lanzou_block = ft.Column([lanzou_btn], spacing=2,
                                     horizontal_alignment=ft.CrossAxisAlignment.STRETCH, tight=True)
        lanzou_block.controls.append(_lanzou_help_text_control())
    buttons = [b for b, u in (
        (gitee_btn, info.get("gitee_apk_url") or ""),
        (github_btn, info["github_apk_url"]),
        (lanzou_block, info["lanzou_url"]),
    ) if u]
    page.show_dialog(ft.AlertDialog(
        title=ft.Text(f"发现新版本 v{info['version']}"),
        content=ft.Column(
            [ft.Text(f"当前 v{APP_VERSION} (build {APP_BUILD}) → 新版 v{info['version']} (build {info['build']})",
                     size=12, color=ft.Colors.GREY_600),
             ft.Container(
                 content=ft.Column([ft.Text(info["notes"] or "（没有更新说明）", size=13)],
                                   scroll=ft.ScrollMode.AUTO, tight=True),
                 height=180, padding=8, border=ft.Border.all(1, ft.Colors.GREY_300), border_radius=8),
             ft.Text("下载完点安装即可覆盖安装，名单和考勤都会保留。", size=12, color=ft.Colors.GREY_700),
             ft.Column(buttons, spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)],
            tight=True, spacing=10, width=320),
        actions=[ft.TextButton("以后再说", on_click=later)],
        actions_alignment=ft.MainAxisAlignment.END))


def show_share_download_dialog(page, info, open_link, notify):
    """分享应用退路：本机读不到安装包时，弹出与「检查更新」类似的 Gitee / GitHub / 蓝奏云选项。
    只打开浏览器链接，不在 App 内静默下载或安装。"""
    info = info or {}

    def later(e2):
        page.pop_dialog()

    def download(url):
        def handler(e2):
            page.pop_dialog()
            if url:
                open_link(url)
        return handler

    gitee_url = (info.get("gitee_apk_url") or "").strip()
    github_url = (info.get("github_apk_url") or "").strip()
    lanzou_url = (info.get("lanzou_url") or "").strip()
    lanzou_code = (info.get("lanzou_code") or "").strip()

    gitee_btn = ft.Button(
        content=ft.Row([ft.Icon(ft.Icons.DOWNLOAD_ROUNDED, color=ft.Colors.WHITE, size=20),
                        ft.Text("国内直链（Gitee）", color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD)],
                       spacing=8, tight=True),
        bgcolor="#C71D23", on_click=download(gitee_url),
        tooltip="国内直链，推荐优先使用")
    github_btn = ft.Button(
        content=ft.Row([ft.Image(src="github_mark_white.png", width=20, height=20),
                        ft.Text("GitHub 下载", color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD)],
                       spacing=8, tight=True),
        bgcolor="#24292F", on_click=download(github_url),
        tooltip="用浏览器直接下载安装包")
    lanzou_btn = ft.Button(
        content=ft.Row([ft.Icon(ft.Icons.CLOUD_DOWNLOAD_ROUNDED, color=ft.Colors.WHITE, size=20),
                        ft.Text("蓝奏云下载", color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD)],
                       spacing=8, tight=True),
        bgcolor=ft.Colors.BLUE_600, on_click=download(lanzou_url),
        tooltip="打开蓝奏云下载页（直链下不动时用）")

    def copy_lanzou_code(e2):
        async def set_clip():
            await ft.Clipboard().set(lanzou_code)
        page.run_task(set_clip)
        notify("提取码已复制")

    lanzou_block = lanzou_btn
    if lanzou_code:
        lanzou_block = ft.Column([
            lanzou_btn,
            ft.Row([ft.Text("提取码：", size=14, color=ft.Colors.GREY_800),
                    ft.Text(lanzou_code, size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_800,
                            selectable=True),
                    ft.TextButton("复制", icon=ft.Icons.CONTENT_COPY_ROUNDED, on_click=copy_lanzou_code,
                                  tooltip="复制蓝奏云提取码")],
                   spacing=4, alignment=ft.MainAxisAlignment.CENTER,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.STRETCH, tight=True)
    if lanzou_url:
        if lanzou_block is lanzou_btn:
            lanzou_block = ft.Column([lanzou_btn], spacing=2,
                                     horizontal_alignment=ft.CrossAxisAlignment.STRETCH, tight=True)
        lanzou_block.controls.append(_lanzou_help_text_control())
    buttons = [b for b, u in (
        (gitee_btn, gitee_url),
        (github_btn, github_url),
        (lanzou_block, lanzou_url),
    ) if u]
    page.show_dialog(ft.AlertDialog(
        title=ft.Text("分享安装包"),
        content=ft.Column(
            [ft.Text("本机读不到已安装的安装包，请任选一个渠道自行下载（下载后点开安装即可，名单和考勤都会保留）。",
                     size=13, color=ft.Colors.GREY_800),
             ft.Column(buttons, spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
             if buttons else ft.Text("暂无可用下载地址", size=13, color=ft.Colors.RED_700)],
            tight=True, spacing=10, width=320),
        actions=[ft.TextButton("关闭", on_click=later)],
        actions_alignment=ft.MainAxisAlignment.END))


def show_lanzou_fallback_dialog(page, open_link, notify, message):
    """失败页查不到新版 / 已是最新 / 检查失败：引导去蓝奏云，不死胡同。"""
    url = UPDATE_CONFIG.get("default_lanzou_url", "")
    code = UPDATE_CONFIG.get("default_lanzou_code", "")

    def later(e2):
        page.pop_dialog()

    def go_lanzou(e2):
        page.pop_dialog()
        if url:
            open_link(url)

    def copy_code(e2):
        async def set_clip():
            await ft.Clipboard().set(code)
        page.run_task(set_clip)
        notify("提取码已复制")

    lanzou_btn = ft.Button(
        content=ft.Row([ft.Icon(ft.Icons.CLOUD_DOWNLOAD_ROUNDED, color=ft.Colors.WHITE, size=20),
                        ft.Text("蓝奏云下载", color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD)],
                       spacing=8, tight=True),
        bgcolor=ft.Colors.BLUE_600, on_click=go_lanzou,
        tooltip="打开蓝奏云文件夹下载最新安装包")
    controls = [ft.Text(message, size=13, color=ft.Colors.GREY_900), lanzou_btn]
    if code:
        controls.append(ft.Row([ft.Text("提取码：", size=14, color=ft.Colors.GREY_800),
                                ft.Text(code, size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_800,
                                        selectable=True),
                                ft.TextButton("复制", icon=ft.Icons.CONTENT_COPY_ROUNDED, on_click=copy_code)],
                               spacing=4, alignment=ft.MainAxisAlignment.CENTER,
                               vertical_alignment=ft.CrossAxisAlignment.CENTER))
        controls.append(_lanzou_help_text_control())
    page.show_dialog(ft.AlertDialog(
        title=ft.Text("请去蓝奏云下载"),
        content=ft.Column(controls, tight=True, spacing=10, width=320),
        actions=[ft.TextButton("关闭", on_click=later)],
        actions_alignment=ft.MainAxisAlignment.END))


def show_startup_error(page, ex):
    """数据升级/打开失败：不进入 App、不新建空库，只显示说明、导出数据、检查更新和日志。"""
    import traceback
    print("[startup] 启动失败：" + "".join(traceback.format_exception(type(ex), ex, ex.__traceback__)))
    backup = getattr(ex, "backup_path", None)
    # 失败页走不到 main() 里注册分享服务那一步，这里自己注册
    share_service = ft.Share()
    page.services.append(share_service)
    url_launcher = ft.UrlLauncher()
    page.services.append(url_launcher)

    # 完整路径只进本地 print 日志；界面只显示文件名
    print(f"[startup] 升级前备份路径：{backup}")
    print(f"[startup] 数据文件路径：{DB_PATH}")
    err_summary = redact_sensitive_text(str(ex), collect_student_names_for_redact())
    info_lines = [
        ("升级前的备份在：" + friendly_data_path(backup)) if backup
        else "升级前的备份：没有生成（升级在备份前就停止了）。",
        "数据文件：" + friendly_data_path(DB_PATH),
        "",
        f"错误信息：{err_summary}",
    ]
    def _safe_logs():
        return redact_sensitive_text(get_app_logs(), collect_student_names_for_redact())
    log_field = ft.TextField(value=_safe_logs(), multiline=True, read_only=True, min_lines=8, max_lines=14,
                             text_size=11, expand=True)
    log_row = ft.Row([log_field], visible=False)   # 放进 Row 并 expand，日志框铺满整行
    update_state = {"busy": False}

    def notify(text, is_error=False):
        page.show_dialog(ft.SnackBar(content=ft.Text(text, color=ft.Colors.WHITE),
                                     bgcolor=ft.Colors.RED_500 if is_error else ft.Colors.GREEN_600,
                                     duration=ft.Duration(seconds=8)))

    def is_mobile():
        return page.platform in ["android", "ios", ft.PagePlatform.ANDROID, ft.PagePlatform.IOS]

    def open_link(url):
        async def go():
            try:
                await url_launcher.launch_url(url, mode=ft.LaunchMode.EXTERNAL_APPLICATION)
            except Exception as ex2:
                notify(f"打开浏览器失败：{ex2}", is_error=True)
        page.run_task(go)

    def export_data(e):
        try:
            out_dir, files = export_rescue_files(backup)
        except Exception as ex2:
            notify(f"导出失败：{ex2}", is_error=True)
            return
        if not files:
            notify("没有找到可导出的数据文件。", is_error=True)
            return
        print(f"[rescue] 已导出 {len(files)} 个文件到 {out_dir}")
        if is_mobile():
            async def do_share():
                try:
                    await share_service.share_files([ft.ShareFile.from_path(f) for f in files],
                                                    title="点名助手数据（升级失败时导出）")
                except Exception as ex3:
                    print(f"[rescue] 分享失败，目录={out_dir}，错误={ex3}")
                    notify(f"唤起分享失败，文件已保存在应用数据目录内的备份/数据文件「{os.path.basename(out_dir)}」（{redact_sensitive_text(str(ex3))}）", is_error=True)
            page.run_task(do_share)
        else:
            notify(f"已导出 {len(files)} 个文件，保存在：" + friendly_data_path(out_dir))

    def toggle_log(e):
        log_field.value = _safe_logs()
        log_row.visible = not log_row.visible
        log_btn.content = "收起日志" if log_row.visible else "查看日志"
        page.update()

    def copy_log(e):
        async def set_clip():
            await ft.Clipboard().set(_safe_logs())
        page.run_task(set_clip)
        notify("运行日志已复制")

    def copy_email(e):
        async def set_clip():
            await ft.Clipboard().set(DEVELOPER_EMAIL)
        page.run_task(set_clip)
        notify("已复制")

    def set_check_busy(busy):
        update_state["busy"] = busy
        check_btn.content = UPDATE_CHECKING_TEXT if busy else "检查更新"
        check_btn.disabled = busy
        try:
            check_btn.update()
        except Exception:
            page.update()

    def on_startup_check_update(e=None):
        if update_state["busy"]:
            return
        set_check_busy(True)
        notify(UPDATE_CHECKING_TEXT)

        async def run():
            import asyncio
            try:
                result, data = await asyncio.to_thread(check_for_update)
            except Exception as ex2:
                result, data = "fail", [str(ex2)]
            finally:
                set_check_busy(False)
            if result == "new":
                show_update_dialog(page, data, open_link, notify)
            elif result == "latest":
                show_lanzou_fallback_dialog(
                    page, open_link, notify,
                    f"当前已是最新版本 v{APP_VERSION}。请直接去蓝奏云下载最新安装包，覆盖安装后名单和考勤都会保留。")
            else:
                show_lanzou_fallback_dialog(
                    page, open_link, notify,
                    "检查更新失败，请检查网络；也可以直接去蓝奏云下载最新安装包，覆盖安装后名单和考勤都会保留。")
        page.run_task(run)

    tip = ft.Container(
        content=ft.Column([
            ft.Row([ft.Icon(ft.Icons.INFO_OUTLINE, color=ft.Colors.AMBER_800, size=18),
                    ft.Text("请这样做", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.AMBER_900)], spacing=6),
            ft.Text(
                "1. 先点「导出数据」。弹出系统分享后，选微信发给文件传输助手或好友；"
                "如果没看到微信，点「更多」找一下，也可以先存到文件再发。\n"
                "2. 再点「检查更新」，下载修复版，直接覆盖安装（名单和考勤会保留）。\n"
                "3. 如果没有修复版，把下面的日志和导出的文件发给开发者（邮箱可一键复制）。",
                size=13, color=ft.Colors.GREY_900),
            ft.Text("不要卸载 App，也不要安装旧版本（卸载会清空数据）。", size=13,
                    weight=ft.FontWeight.BOLD, color=ft.Colors.RED_700),
        ], spacing=6),
        bgcolor="#fff8e1", border=ft.Border.all(1, "#f5d68a"), border_radius=10, padding=12,
    )
    export_btn = ft.FilledButton("导出数据（名单+考勤）", icon=ft.Icons.IOS_SHARE, on_click=export_data, expand=True,
                                 style=ft.ButtonStyle(bgcolor=ft.Colors.BLUE_700, color=ft.Colors.WHITE,
                                                      padding=ft.Padding(left=16, right=16, top=14, bottom=14),
                                                      shape=ft.RoundedRectangleBorder(radius=10)))
    check_btn = ft.OutlinedButton("检查更新", icon=ft.Icons.SYSTEM_UPDATE_ALT_ROUNDED,
                                  on_click=on_startup_check_update, expand=True,
                                  style=ft.ButtonStyle(color=ft.Colors.BLUE_700, bgcolor=ft.Colors.WHITE,
                                                       side=ft.BorderSide(1.5, ft.Colors.BLUE_700),
                                                       padding=ft.Padding(left=16, right=16, top=14, bottom=14),
                                                       shape=ft.RoundedRectangleBorder(radius=10)))
    email_row = ft.Row([
        ft.Text(spans=[ft.TextSpan("开发者邮箱："),
                       ft.TextSpan(DEVELOPER_EMAIL, ft.TextStyle(color=ft.Colors.BLUE_800,
                                                                 weight=ft.FontWeight.W_600))],
                size=13, expand=True, selectable=True),
        ft.OutlinedButton("复制", icon=ft.Icons.CONTENT_COPY_ROUNDED, on_click=copy_email,
                          tooltip="复制开发者邮箱"),
    ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)
    log_btn = ft.OutlinedButton("查看日志", icon=ft.Icons.ARTICLE_OUTLINED, on_click=toggle_log)
    copy_btn = ft.TextButton("复制日志", icon=ft.Icons.COPY, on_click=copy_log)

    page.add(ft.Column([
        ft.Row([ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=ft.Colors.RED_600, size=40),
                ft.Column([ft.Text("数据升级没有完成", size=20, weight=ft.FontWeight.BOLD, color=ft.Colors.RED_700),
                           ft.Text("原有数据没有改动，已退回升级前的样子。", size=13, color=ft.Colors.GREY_800)],
                          spacing=2, expand=True)], spacing=10),
        tip,
        ft.Row([export_btn]),
        ft.Row([check_btn]),
        email_row,
        ft.Row([log_btn, copy_btn], spacing=8),
        log_row,
        ft.Text("\n".join(info_lines), size=12, color=ft.Colors.GREY_700, selectable=True),
    ], scroll=ft.ScrollMode.AUTO, expand=True, spacing=14))
    page.update()



# ----------------- 2. Flet 主应用 -----------------

def main(page: ft.Page):
    page.title = "点名助手"
    page.theme = ft.Theme(
        font_family="Microsoft YaHei, PingFang SC, Segoe UI, sans-serif",
        # 1.5.2：拖动排序时 Flutter 默认给被拖行包一层「画布色 Material + 阴影」的方块（Flet 不开放
        # proxyDecorator），会露出白色直角和方形阴影。把画布色设透明、默认阴影色设透明，这层方块就看不见了，
        # 只剩我们自己给被拖行加的圆角蓝框+蓝色阴影；弹窗/菜单阴影单独在各自主题里补回。
        color_scheme=ft.ColorScheme(primary=UI_BLUE, shadow=ft.Colors.TRANSPARENT),
        canvas_color=ft.Colors.TRANSPARENT,
        # 1.5.2：所有弹窗统一白底圆角 16（渲染图 07）
        dialog_theme=ft.DialogTheme(
            bgcolor=ft.Colors.WHITE,
            shape=ft.RoundedRectangleBorder(radius=16),
            title_text_style=ft.TextStyle(size=18, weight=ft.FontWeight.W_800, color=UI_INK),
            shadow_color=ft.Colors.BLACK54,
        ),
        popup_menu_theme=ft.PopupMenuTheme(shadow_color=ft.Colors.BLACK38, color=ft.Colors.WHITE),
    )
    page.theme_mode = ft.ThemeMode.LIGHT
    page.window_width = 460
    page.window_height = 820
    page.window_resizable = True
    # 1.5.2：页面不留外边距，侧栏/遮罩才能贴满全屏；内边距挪到各页内部
    page.padding = 0
    page.bgcolor = UI_BG

    # 初始化本地数据库（必要时原地升级）；失败时只显示错误页，绝不新建空库
    try:
        init_info = init_db()
    except Exception as ex:
        show_startup_error(page, ex)
        return
    # v1.4.2 取消每日备份；v1.5.0：有考勤时每周一自动备一份（寒暑假不开 App 则不备）
    # 手机上已有的 attendance_daily_* 旧备份不主动删，仍可在列表里查看和恢复
    try:
        weekly_backup_if_needed()
    except Exception as ex:
        print(f"[backup] 每周自动备份失败（不影响使用）：{ex}")
    load_students_order()

    # 注册系统原生分享服务
    share_service = ft.Share()
    page.services.append(share_service)

    # 状态变量
    current_date = datetime.date.today().strftime("%Y-%m-%d")
    current_period = 0 # 0-4
    show_anomaly_only = False
    search_query = ""
    attendance_cache = {}      # {name: 显示状态}
    slot_is_taken = False      # 本班这一节有没有任何记录
    roll_call_stale = False    # 点名页未挂载时数据变了，切回时再重建
    nav_index = 0
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

    # SnackBar 也走 page.show_dialog 入栈：凡是要关对话框的地方，一律先 pop_dialog 再 show_snackbar
    def show_snackbar(text, is_error=False):
        page.show_dialog(
            ft.SnackBar(
                content=ft.Text(text, color=ft.Colors.WHITE),
                bgcolor=ft.Colors.RED_500 if is_error else ft.Colors.GREEN_600
            )
        )

    def is_mobile():
        return page.platform in ["android", "ios", ft.PagePlatform.ANDROID, ft.PagePlatform.IOS]

    def share_file(path, title):
        if is_mobile():
            async def do_share():
                try:
                    await share_service.share_files([ft.ShareFile.from_path(os.path.abspath(path))], title=title)
                except Exception as ex:
                    show_snackbar(f"唤起分享失败，文件已保存在：{path}（{ex}）", is_error=True)
            page.run_task(do_share)
        else:
            show_snackbar(f"文件位置：{path}")

    try:
        dt = datetime.datetime.strptime(current_date, "%Y-%m-%d")
        date_display_init = f"{dt.month:02d}-{dt.day:02d}"
    except Exception:
        date_display_init = f"{current_date}"

    date_text = ft.Text(date_display_init, color=UI_BLUE, weight=ft.FontWeight.W_600, size=13)

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

    # 1.5.2 顶栏浅蓝胶囊（渲染图 01 .chip）：无灰框、无描边
    def top_chip(controls, on_click=None):
        return ft.Container(
            content=ft.Row(controls, spacing=4, tight=True,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=UI_BLUE_SOFT, border_radius=16,
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            on_click=on_click, ink=on_click is not None,
        )

    date_btn = top_chip(
        [ft.Icon(ft.Icons.CALENDAR_TODAY_OUTLINED, color=UI_BLUE, size=14), date_text],
        on_click=lambda _: page.show_dialog(date_picker))

    # 课节选择：浅蓝胶囊 + 弹出菜单（替代原灰框 Dropdown，逻辑不变）
    period_text = ft.Text(PERIOD_LABELS[current_period], color=UI_BLUE, weight=ft.FontWeight.W_600, size=13)

    def on_period_change(idx):
        nonlocal current_period
        current_period = int(idx)
        period_text.value = PERIOD_LABELS[current_period]
        for j, it in enumerate(period_menu.items):
            it.checked = (j == current_period)
        load_attendance_from_db()
        rebuild_roster_ui()
        page.update()

    period_menu = ft.PopupMenuButton(
        content=top_chip([period_text, ft.Icon(ft.Icons.ARROW_DROP_DOWN_ROUNDED, color=UI_BLUE, size=18)]),
        items=[ft.PopupMenuItem(content=ft.Text(label), checked=(i == current_period),
                                on_click=lambda _, i=i: on_period_change(i))
               for i, label in enumerate(PERIOD_LABELS)],
        menu_position=ft.PopupMenuPosition.UNDER,
        padding=0,
        tooltip="选择节次",
        bgcolor=ft.Colors.WHITE,
        shape=ft.RoundedRectangleBorder(radius=12),
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
        prefix_icon=ft.Icon(ft.Icons.SEARCH, size=18, color=UI_MUTED),
        height=38,
        dense=True,
        text_size=13,
        hint_style=ft.TextStyle(size=13, color=UI_MUTED),
        content_padding=ft.Padding(14, 8, 14, 8),
        expand=True,
        bgcolor=ft.Colors.WHITE,
        filled=True,
        fill_color=ft.Colors.WHITE,
        border_radius=20,
        border_color=UI_LINE,
        focused_border_color=UI_BLUE,
        color=UI_INK,
        on_focus=lambda e: set_keyboard_mode(True),
        on_blur=lambda e: set_keyboard_mode(False),
    )

    def on_filter_toggle(e):
        nonlocal show_anomaly_only
        show_anomaly_only = filter_switch.value
        rebuild_roster_ui()
        page.update()

    filter_switch = ft.Switch(
        label="只看异常",
        value=show_anomaly_only,
        on_change=on_filter_toggle,
        active_color=ft.Colors.AMBER_600,
        inactive_track_color="#cfd8dc",
        inactive_thumb_color=ft.Colors.WHITE,
        track_outline_color=ft.Colors.TRANSPARENT,
        label_text_style=ft.TextStyle(size=12, color=UI_MUTED),
    )

    # 「本节还没点名」提示行
    slot_hint_text = ft.Text("", size=12, color="#f57f17", weight=ft.FontWeight.W_600)
    slot_hint = ft.Container(content=slot_hint_text, bgcolor="#fff8e1", border_radius=8,
                             padding=ft.Padding.symmetric(horizontal=10, vertical=7), visible=False)

    roster_column = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True, spacing=8)

    # ----------------- 4. 业务逻辑与重绘 -----------------

    def load_attendance_from_db():
        nonlocal attendance_cache, slot_is_taken
        cid = get_current_class_id()
        db_records = get_records(current_date, current_period, cid)
        slot_is_taken = slot_taken(current_date, current_period, cid)
        attendance_cache = effective_statuses(
            [(n, STUDENT_JOINED.get(n)) for n in STUDENTS_ORDER], db_records, current_date)

    def unmarked_count():
        """点过名的节里，在班但没有任何记录的人数（显示为空课）。"""
        if not slot_is_taken:
            return 0
        recs = get_records(current_date, current_period)
        return sum(1 for n in STUDENTS_ORDER if n not in recs and attendance_cache.get(n) != NOT_JOINED)

    def refresh_slot_hint():
        if not STUDENTS_ORDER:
            slot_hint.visible = False
            return
        if not slot_is_taken:
            slot_hint_text.value = "本节还没点名：先点「全员出勤」，再点缺勤的同学"
            slot_hint.visible = True
        else:
            n = unmarked_count()
            slot_hint_text.value = f"本节有 {n} 人还没有标记（显示为空课）" if n else ""
            slot_hint.visible = bool(n)

    def update_student_card_state(name, new_status):
        if name not in student_card_map:
            return
        info = student_card_map[name]
        color_hex, bg_color = STATUS_STYLES.get(new_status, ("#6b7280", "#1a6b7280"))
        info["status_text"].value = new_status
        info["status_text"].color = color_hex
        info["status_badge"].border = ft.Border.all(1, ft.Colors.TRANSPARENT)
        info["status_badge"].bgcolor = bg_color
        border, shadow, bg = card_style(new_status)
        c = info["container"]
        c.border, c.shadow, c.bgcolor = border, shadow, bg
        c.update()

    def after_mark(name, status, was_taken):
        attendance_cache[name] = status
        try:
            telemetry_note_roll_mark(current_period)
        except Exception:
            pass
        if show_anomaly_only or not was_taken:
            # 第一次标记：这节变成「点过名」，提示行要刷新
            rebuild_roster_ui()
            page.update()
        else:
            update_student_card_state(name, status)
            refresh_slot_hint()
            slot_hint.update()

    # 快速切换 出勤 / 旷课（与 v1.3.1 一致：出勤 → 旷课，其余 → 出勤）
    def toggle_attendance_fast(name):
        curr = attendance_cache.get(name, "空课")
        next_status = "旷课" if curr == "出勤" else "出勤"
        was_taken = slot_is_taken
        save_record(current_date, current_period, name, next_status)
        load_attendance_from_db()
        after_mark(name, next_status, was_taken)

    def show_status_selector(name):
        curr_status = attendance_cache.get(name, "空课")

        def select_status(status):
            was_taken = slot_is_taken
            save_record(current_date, current_period, name, status)
            load_attendance_from_db()
            page.pop_dialog()  # 关闭选择器弹窗
            after_mark(name, status, was_taken)

        status_buttons = []
        for st in RECORD_STATUSES:
            color_hex, bg_color = STATUS_STYLES[st]
            is_selected = (st == curr_status)
            status_buttons.append(
                ft.Container(
                    content=ft.Row([
                        ft.Icon(icon=ft.Icons.CHECK if is_selected else ft.Icons.RADIO_BUTTON_UNCHECKED,
                                color=color_hex, size=20),
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
            content=ft.Column(status_buttons, spacing=8, tight=True, width=280),
            actions=[ft.TextButton(content="取消", on_click=lambda _: page.pop_dialog())],
            actions_alignment=ft.MainAxisAlignment.END
        )
        page.show_dialog(dlg)

    def section_title(icon, text, color):
        return ft.Container(
            content=ft.Row([ft.Icon(icon, color=color, size=18),
                            ft.Text(text, size=14, weight=ft.FontWeight.BOLD, color=color)], spacing=5),
            padding=ft.Padding.only(top=5, bottom=5))

    def empty_text(text):
        return ft.Container(content=ft.Text(text, color=ft.Colors.GREY_500, size=13),
                            alignment=ft.Alignment.CENTER, padding=50)

    def rebuild_roster_ui():
        nonlocal roll_call_stale
        roll_call_stale = False
        roster_column.controls.clear()
        student_card_map.clear()
        refresh_slot_hint()

        # 当前班名单为空（新装 / 新建的班）：显示导入提示
        if not STUDENTS_ORDER:
            def go_import(e):
                go_tab(1)
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
            return

        def build_card(name):
            status = attendance_cache.get(name, "空课")
            color_hex, bg_color = STATUS_STYLES.get(status, ("#6b7280", "#1a6b7280"))
            status_text = ft.Text(status, size=12, color=color_hex, weight=ft.FontWeight.W_500)
            status_badge = ft.Container(content=status_text, border=ft.Border.all(1, ft.Colors.TRANSPARENT), border_radius=8,
                                        padding=ft.Padding.symmetric(horizontal=8, vertical=4), bgcolor=bg_color)
            card_row = ft.Row(
                [ft.Text(name, size=15, weight=ft.FontWeight.W_600, color=UI_INK),
                 ft.GestureDetector(content=status_badge, on_tap=lambda _, n=name: show_status_selector(n))],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
            border, shadow, bg = card_style(status)
            card_container = ft.Container(
                content=card_row, padding=ft.Padding.symmetric(horizontal=10, vertical=12), border=border,
                border_radius=12, bgcolor=bg,
                on_click=lambda _, n=name: toggle_attendance_fast(n),
                on_long_press=lambda _, n=name: show_status_selector(n),
                shadow=shadow)
            student_card_map[name] = {"container": card_container, "status_text": status_text,
                                      "status_badge": status_badge}
            return card_container

        matched_names = []
        for name in STUDENTS_ORDER:
            status = attendance_cache.get(name, "空课")
            if show_anomaly_only and status in ("出勤", NOT_JOINED):
                continue
            if search_query and (search_query not in name.lower()):
                if search_query not in get_pinyin_initials(name):
                    continue
            matched_names.append(name)

        if show_anomaly_only:
            if not slot_is_taken:
                roster_column.controls.append(empty_text("本节还没有点名记录"))
                return
            kuangke_names = [n for n in matched_names if attendance_cache.get(n) == "旷课"]
            other_names = [n for n in matched_names if attendance_cache.get(n) != "旷课"]
            if not kuangke_names and not other_names:
                roster_column.controls.append(empty_text("没有找到匹配的异常记录"))
            else:
                if kuangke_names:
                    roster_column.controls.append(section_title(ft.Icons.ERROR_OUTLINE_ROUNDED, "未到学生 (旷课)", ft.Colors.RED_600))
                    roster_column.controls.extend(chunk_controls([build_card(n) for n in kuangke_names]))
                if kuangke_names and other_names:
                    roster_column.controls.append(ft.Container(
                        height=25, content=ft.Divider(color=ft.Colors.GREY_300, height=1, thickness=1),
                        alignment=ft.Alignment.CENTER, padding=ft.Padding.symmetric(vertical=5)))
                if other_names:
                    roster_column.controls.append(section_title(ft.Icons.INFO_OUTLINE_ROUNDED, "其他异常 (请假/空课)", ft.Colors.AMBER_700))
                    roster_column.controls.extend(chunk_controls([build_card(n) for n in other_names]))
        else:
            if matched_names:
                roster_column.controls.extend(chunk_controls([build_card(n) for n in matched_names]))
            else:
                roster_column.controls.append(empty_text("没有找到匹配的学生记录"))

    def refresh_roll_call():
        """数据变了：点名页挂载着就立刻重建，否则等切回点名页再重建（避免未挂载时重建导致重复渲染）。"""
        nonlocal roll_call_stale
        load_attendance_from_db()
        if nav_index == 0:
            rebuild_roster_ui()
        else:
            roll_call_stale = True

    # ----------------- 5. 全局操作事件 -----------------

    def on_all_present(e):
        # 每位在班同学写一条明确的「出勤」记录（入班日期晚于所选日期的同学跳过）
        bulk = {n: "出勤" for n in STUDENTS_ORDER if attendance_cache.get(n) != NOT_JOINED}
        bulk_save_records(current_date, current_period, bulk)
        try:
            telemetry_note_roll_mark(current_period)
        except Exception:
            pass
        load_attendance_from_db()
        rebuild_roster_ui()
        page.update()
        show_snackbar("已一键标记全员为：出勤")

    def on_all_noclass(e):
        bulk = {n: "空课" for n in STUDENTS_ORDER if attendance_cache.get(n) != NOT_JOINED}
        bulk_save_records(current_date, current_period, bulk)
        try:
            telemetry_note_roll_mark(current_period)
        except Exception:
            pass
        load_attendance_from_db()
        rebuild_roster_ui()
        page.update()
        show_snackbar("已一键标记全员为：空课")

    def on_copy_summary(e):
        if not STUDENTS_ORDER:
            show_snackbar("当前班级还没有名单，请先导入", is_error=True)
            return
        text, hint, kind = build_summary(
            current_date, current_period, get_class_name(),
            [(n, attendance_cache.get(n, "空课")) for n in STUDENTS_ORDER],
            slot_is_taken, unmarked=unmarked_count())
        if text is None:
            show_snackbar(hint, is_error=(kind != "allnoclass"))
            return

        async def set_clip():
            await ft.Clipboard().set(text)
        page.run_task(set_clip)
        if hint:
            show_snackbar("已复制考勤汇总。" + hint, is_error=True)
        else:
            show_snackbar("已按微信格式复制考勤汇总到剪贴板！")

    def on_export_excel(e):
        cid = get_current_class_id()
        available_months = get_available_months(cid)
        if not available_months:
            show_snackbar("本班暂无任何有效的考勤记录", is_error=True)
            return
        class_name = get_class_name(cid)

        def execute_export(ym):
            filename, err = export_db_to_excel(ym, class_name, cid=cid)
            if err:
                show_snackbar(f"导出失败：{err}", is_error=True)
            elif is_mobile():
                telemetry_note_feature("export", page)
                share_file(filename, f"{ym}考勤表")
            else:
                telemetry_note_feature("export", page)
                show_snackbar(f"📊 已导出考勤 Excel：{filename}")

        if len(available_months) == 1:
            execute_export(available_months[0])
            return

        def create_click_handler(target_ym):
            def handler(_):
                page.pop_dialog()
                execute_export(target_ym)
            return handler

        month_buttons = []
        for ym in available_months:
            try:
                month_label = f"{int(ym.split('-')[1])}月"
            except Exception:
                month_label = ym
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
        page.show_dialog(ft.AlertDialog(
            title=ft.Text("选择要导出的月份", size=18, weight=ft.FontWeight.BOLD),
            content=ft.Column(month_buttons, spacing=8, tight=True, width=280, scroll=ft.ScrollMode.AUTO),
            actions=[ft.TextButton(content="取消", on_click=lambda _: page.pop_dialog())],
            actions_alignment=ft.MainAxisAlignment.END
        ))

    # ----------------- 6. 顶栏与抽屉开关（1.5.0） -----------------

    # 自定义侧栏（方案 D：半透明遮罩+白面板；不用 page.drawer，避免名单页系统左缘误开）
    drawer_state = {"open": False, "layer": None}

    def _drawer_width():
        try:
            w = float(page.width or 0)
        except Exception:
            w = 0
        if w <= 0:
            w = 412
        return max(240, min(320, round(w * 0.68)))

    def open_drawer(e=None):
        telemetry_note_feature("drawer_open", page)
        drawer_state["open"] = True
        layer = drawer_state.get("layer")
        if layer is not None:
            panel = drawer_state.get("panel")
            scrim = drawer_state.get("scrim")
            if panel is not None:
                pw = _drawer_width()
                panel.width = pw + 1          # 左侧 1px 边框推到屏幕外（只留右边线+右圆角）
                panel.offset = ft.Offset(-1, 0)
            if scrim is not None:
                scrim.opacity = 0
            layer.visible = True
            try:
                page.update()
            except Exception:
                pass
            if panel is not None:
                panel.offset = ft.Offset(0, 0)
            if scrim is not None:
                scrim.opacity = 1
            try:
                page.update()
            except Exception:
                pass

    def close_drawer(e=None):
        drawer_state["open"] = False
        layer = drawer_state.get("layer")
        if layer is not None:
            layer.visible = False
            try:
                page.update()
            except Exception:
                pass

    # 1.5.2：输入法弹出时不把底栏/FAB 顶上来（Flet 无 resizeToAvoidBottomInset，改为键盘期间隐藏）
    kb_state = {"on": False}

    def set_keyboard_mode(on):
        on = bool(on)
        if kb_state["on"] == on:
            return
        kb_state["on"] = on
        for name in ("frost_nav", "fab_holder"):
            ctl = kb_refs.get(name)
            if ctl is not None:
                ctl.visible = not on
        try:
            page.update()
        except Exception:
            pass

    kb_refs = {}

    def on_media_change(e):
        try:
            insets = getattr(e, "view_insets", None)
            bottom = float(getattr(insets, "bottom", 0) or 0) if insets is not None else 0
        except Exception:
            bottom = 0
        set_keyboard_mode(bottom > 0)

    try:
        page.on_media_change = on_media_change
    except Exception:
        pass

    # 更新小红点：启动缓存/静默检查后亮；已是最新或装上新版后灭；关下载窗不灭
    try:
        _update_badge_init = update_badge_should_show(load_update_cache())
    except Exception:
        _update_badge_init = False

    def _make_corner_update_dot():
        return ft.Container(
            width=8, height=8, bgcolor=ft.Colors.RED_500, border_radius=8,
            visible=_update_badge_init, right=4, top=4)

    # 1.5.2 顶栏图标：36×36 透明底、图标 22（渲染图 .icon-btn）
    def top_icon_btn(icon, tooltip, on_click):
        return ft.IconButton(
            icon=icon, icon_color=UI_BLUE, icon_size=22, tooltip=tooltip, on_click=on_click,
            width=36, height=36, padding=0, style=ft.ButtonStyle(padding=0))

    menu_update_dot = _make_corner_update_dot()
    menu_btn = ft.Stack(
        [top_icon_btn(ft.Icons.MENU_ROUNDED, "设置", open_drawer), menu_update_dot],
        width=36, height=36,
    )

    copy_btn = top_icon_btn(ft.Icons.CONTENT_COPY_OUTLINED, "复制微信考勤汇总", on_copy_summary)
    export_btn = top_icon_btn(ft.Icons.FILE_DOWNLOAD_ROUNDED, "导出考勤 Excel", on_export_excel)

    def make_top_bar(left, mid, right, mid_inset=76):
        """左右各 76px 等宽翼 + 中间绝对居中（不受两侧按钮数量影响）。"""
        return ft.Container(
            content=ft.Stack(
                [
                    ft.Container(content=mid, left=mid_inset, right=mid_inset, top=0, bottom=0,
                                 alignment=ft.Alignment.CENTER),
                    ft.Container(content=ft.Row([left], spacing=0, alignment=ft.MainAxisAlignment.START),
                                 width=76, left=0, top=0, bottom=0, alignment=ft.Alignment.CENTER_LEFT),
                    ft.Container(content=ft.Row(right, spacing=4, alignment=ft.MainAxisAlignment.END),
                                 width=76, right=0, top=0, bottom=0, alignment=ft.Alignment.CENTER_RIGHT),
                ],
                height=38,
            ),
            padding=ft.Padding(10, 6, 10, 8),
        )

    # 点名顶栏：左汉堡 | 中日期+节次绝对居中 | 右复制/导出图标
    roll_top_bar = make_top_bar(
        menu_btn,
        ft.Row([date_btn, period_menu], spacing=6, alignment=ft.MainAxisAlignment.CENTER,
               vertical_alignment=ft.CrossAxisAlignment.CENTER),
        [copy_btn, export_btn])

    search_row = ft.Row([search_input, filter_switch], alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def quick_btn(mark, mark_color, label, on_click):
        return ft.Container(
            content=ft.Row(
                [ft.Text(mark, size=13, weight=ft.FontWeight.W_800, color=mark_color),
                 ft.Text(label, size=13, weight=ft.FontWeight.W_600, color=UI_INK)],
                spacing=6, alignment=ft.MainAxisAlignment.CENTER, tight=True),
            expand=True, bgcolor=ft.Colors.WHITE, border=ft.Border.all(1, UI_LINE), border_radius=12,
            padding=ft.Padding.symmetric(horizontal=8, vertical=9), alignment=ft.Alignment.CENTER,
            on_click=on_click, ink=True)

    global_buttons = ft.Row(
        [quick_btn("✓", UI_OK, "全员出勤", on_all_present),
         quick_btn("zzz", UI_BLUE, "全员空课", on_all_noclass)],
        spacing=10)

    # 点名页顶部可横滑区域（人名网格之外）；不再用分隔线
    roll_swipe_header = ft.Column(
        [roll_top_bar,
         ft.Container(
             content=ft.Column([search_row, global_buttons, slot_hint], spacing=8, tight=True,
                               horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
             padding=ft.Padding(12, 0, 12, 0))],
        spacing=0, tight=True)

    # ----------------- 7. 名单管理（自动保存） -----------------

    editing = []
    undo_state = {"entry": None, "index": None, "token": 0, "class_id": None}

    def clear_undo_state():
        """切班或失效时清空撤销；抬高 token 使已弹出的「撤销」按钮失效。"""
        undo_state["entry"] = None
        undo_state["index"] = None
        undo_state["class_id"] = None
        undo_state["token"] += 1
    fab_expanded = {"v": False}

    def saved_roster():
        return [(s["id"], s["name"]) for s in get_class_students()]

    def roster_dirty():
        # 1.5.0 自动保存后正常为 False；保留函数供测试/兼容
        return roster_is_dirty(editing, saved_roster())

    def inactive_rows():
        ids = {e["id"] for e in editing if e.get("id") is not None}
        return [s for s in get_class_students(include_inactive=True) if not s["active"] and s["id"] not in ids]

    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    def show_action_snackbar(text, action_label, on_action, ms=5000):
        def _act(e):
            on_action()
        page.show_dialog(ft.SnackBar(
            content=ft.Text(text, color=ft.Colors.WHITE),
            bgcolor=ft.Colors.GREEN_600,
            action=ft.SnackBarAction(label=action_label, text_color=ft.Colors.AMBER_200, on_click=_act),
            duration=ms,
        ))

    def persist_roster(purge=False, after=None, snack=None):
        """把 editing 立即写入数据库（自动保存）。"""
        try:
            result = apply_roster_changes(get_current_class_id(), editing, purge_removed=purge)
        except Exception as ex:
            show_snackbar(f"保存失败，名单没有改动：{ex}", is_error=True)
            init_roster_manage_ui()
            return None
        init_roster_manage_ui()
        refresh_roll_call()
        cur = get_current_class_id()
        for src in result.get("moved_from", []):
            slots = shared_slots(cur, src)
            if slots:
                if after:
                    after()
                open_shared_slots_dialog(cur, src, slots)
                return result
        if after:
            after()
        if snack:
            show_snackbar(snack)
        return result

    def append_entry(entry, snack=None):
        editing.append(entry)
        persist_roster(snack=snack)

    def open_add_student_dialog(e=None):
        collapse_fab()
        name_input = dialog_field(hint_text="输入新成员姓名...")
        set_keyboard_mode(True)

        def confirm(e2):
            name = normalize_name(name_input.value)
            if not name:
                name_input.error_text = "姓名不能为空"
                name_input.update()
                return
            buffer_names = [x["name"] for x in editing]
            if name in buffer_names:
                alt = disambiguate(name, buffer_names + [s["name"] for s in get_class_students(include_inactive=True)])

                def add_alt(e3):
                    page.pop_dialog()
                    append_entry({"id": None, "name": alt, "orig": None}, snack=f"已添加『{alt}』")

                page.pop_dialog()
                page.show_dialog(ft.AlertDialog(
                    title=ft.Text("姓名重复"),
                    content=ft.Text(f"本班已经有『{name}』。如果是另一位同名同学，可以添加为『{alt}』。"),
                    actions=[ft.TextButton("取消", on_click=lambda _: page.pop_dialog()),
                             ft.TextButton(f"添加为 {alt}", on_click=add_alt)],
                    actions_alignment=ft.MainAxisAlignment.END))
                return
            for s in inactive_rows():
                if s["name"] == name:
                    page.pop_dialog()
                    append_entry({"id": s["id"], "name": name, "orig": name},
                                 snack=f"已恢复『{name}』，原考勤记录保留")
                    return
            others = find_name_in_other_classes(name)
            if others:
                sid, _ocid, oname, ocnt = others[0]

                def move_here(e3):
                    page.pop_dialog()
                    append_entry({"id": sid, "name": name, "orig": name, "from_class": oname},
                                 snack=f"已将『{name}』从『{oname}』移入本班")

                def add_new(e3):
                    page.pop_dialog()
                    append_entry({"id": None, "name": name, "orig": None}, snack=f"已添加『{name}』")

                page.pop_dialog()
                page.show_dialog(ft.AlertDialog(
                    title=ft.Text("是同一个人吗？"),
                    content=ft.Text(f"『{name}』在『{oname}』里（{ocnt} 条考勤记录），是同一个人吗？"),
                    actions=[ft.TextButton("取消", on_click=lambda _: page.pop_dialog()),
                             ft.TextButton("是另一个人，新加一个", on_click=add_new),
                             ft.TextButton("移到本班（带考勤）", on_click=move_here)],
                    actions_alignment=ft.MainAxisAlignment.END))
                return
            page.pop_dialog()
            append_entry({"id": None, "name": name, "orig": None}, snack=f"已添加『{name}』")

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("添加同学"),
            content=ft.Column([name_input], tight=True, width=300),
            actions=[dialog_action("取消", lambda _: page.pop_dialog()),
                     dialog_action("添加", confirm)],
            actions_alignment=ft.MainAxisAlignment.END,
            on_dismiss=lambda e: set_keyboard_mode(False)))

    # ---- 导入 ----
    def apply_imported_names(imported_names, mode):
        nonlocal editing
        before = [dict(x) for x in editing]
        by_name = {x["name"]: x for x in editing}
        inactive = {s["name"]: s for s in inactive_rows()}

        def entry_for(n):
            if n in by_name:
                return by_name[n]
            if n in inactive:
                return {"id": inactive[n]["id"], "name": n, "orig": n}
            return {"id": None, "name": n, "orig": None}

        if mode == "replace":
            editing = [entry_for(n) for n in imported_names]
            msg = f"已导入并保存 {len(imported_names)} 人（替换）"
        else:
            added = [entry_for(n) for n in imported_names if n not in by_name]
            editing = editing + added
            msg = f"已合并追加并保存 {len(added)} 名新同学"

        cross = []
        for x in editing:
            if x.get("id") is None:
                found = find_name_in_other_classes(x["name"])
                if found:
                    cross.append((x, found[0]))
        if not cross:
            persist_roster(snack=msg)
            return
        by_class = {}
        for x, (sid, ocid, oname, ocnt) in cross:
            n_all, n_rec = by_class.get(oname, (0, 0))
            by_class[oname] = (n_all + 1, n_rec + (1 if ocnt else 0))
        desc = "；".join(f"{c} 人已经在『{k}』，其中 {r} 人有考勤记录" for k, (c, r) in by_class.items())

        def do_move(e2):
            page.pop_dialog()
            for x, (sid, ocid, oname, ocnt) in cross:
                x["id"], x["orig"], x["from_class"] = sid, x["name"], oname
            persist_roster(snack=msg)

        def as_new(e2):
            page.pop_dialog()
            persist_roster(snack=msg)

        def cancel(e2):
            nonlocal editing
            page.pop_dialog()
            editing = before
            rebuild_manage_list_ui()

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("有同学已经在别的班"),
            content=ft.Text(f"名册里有 {desc}。是否把他们连同考勤记录一起移到本班？"),
            actions=[ft.TextButton("取消", on_click=cancel),
                     ft.TextButton("当作新同学", on_click=as_new),
                     ft.TextButton("移过来（推荐）", on_click=do_move)],
            actions_alignment=ft.MainAxisAlignment.END))

    def handle_imported_file(filepath):
        imported_names, info, err, dups = parse_names_detail(filepath)
        if err:
            show_snackbar(f"导入解析失败: {err}", is_error=True)
            return
        if not imported_names:
            show_snackbar("表格中未找到任何像姓名的内容，请检查文件", is_error=True)
            return
        current = {x["name"] for x in editing}
        new_cnt = len([n for n in imported_names if n not in current])
        dup_lines = [
            f"⚠ 表里『{n}』出现了 {c} 次，只导入了 1 个；如果确实是两位同学，请导入后手动添加『{n}(2)』"
            for n, c in list(dups.items())[:3]]
        if len(dups) > 3:
            dup_lines.append(f"⚠ 另外还有 {len(dups) - 3} 个名字重复：{'、'.join(list(dups)[3:])}")

        def do(mode):
            page.pop_dialog()
            apply_imported_names(imported_names, mode)

        page.show_dialog(ft.AlertDialog(
            title=ft.Text(f"识别到 {len(imported_names)} 个姓名"),
            content=build_import_preview(imported_names, info, new_cnt, dup_lines, current),
            actions=[
                ft.TextButton("取消", on_click=lambda _: page.pop_dialog()),
                ft.TextButton("合并追加", on_click=lambda _: do("merge")),
                ft.TextButton("替换", on_click=lambda _: do("replace"), style=ft.ButtonStyle(color=ft.Colors.RED_600)),
            ],
            actions_alignment=ft.MainAxisAlignment.END
        ))

    # ---- 保存 ----
    # 名单变更即时写库（自动保存）
    def save_roster_action(purge=False, after=None):
        return persist_roster(purge=purge, after=after)

    def init_roster_manage_ui():
        nonlocal editing
        editing = [{"id": s["id"], "name": s["name"], "orig": s["name"]} for s in get_class_students()]
        rebuild_manage_list_ui()

    # 1.5.2：长按拖动时震动 + 被拖行变蓝色浮起（渲染图 05 .row.lifted）
    haptic = ft.HapticFeedback()
    page.services.append(haptic)
    lift_state = {"row": None}

    def _row_rest_style(row):
        row.border = ft.Border.all(1, UI_LINE)
        row.shadow = ft.BoxShadow(blur_radius=2, offset=ft.Offset(0, 1), color="#0a000000")
        row.scale = 1

    async def on_manage_reorder_start(e):
        try:
            await haptic.heavy_impact()
        except Exception:
            pass
        idx = getattr(e, "old_index", None)
        if idx is None or not (0 <= idx < len(manage_list.controls)):
            return
        row = manage_list.controls[idx]
        lift_state["row"] = row
        row.border = ft.Border.all(1.5, UI_BLUE)
        row.shadow = ft.BoxShadow(blur_radius=24, offset=ft.Offset(0, 10), color="#471565c0")
        row.scale = 1.03
        try:
            row.update()
        except Exception:
            pass

    def on_manage_reorder_end(e):
        row = lift_state.get("row")
        lift_state["row"] = None
        if row is not None and row in manage_list.controls:
            _row_rest_style(row)
            try:
                row.update()
            except Exception:
                pass

    manage_list = ft.ReorderableListView(
        expand=True, spacing=8, show_default_drag_handles=False,
        padding=ft.Padding(12, 4, 12, 110),
        on_reorder=lambda e: on_manage_reorder(e),
        on_reorder_start=on_manage_reorder_start,
        on_reorder_end=on_manage_reorder_end,
    )

    # 1.5.2 弹窗输入框与按钮（渲染图 07）
    def dialog_field(**kw):
        kw.setdefault("autofocus", True)
        return ft.TextField(
            border_radius=10, filled=True, fill_color="#fafafa", bgcolor="#fafafa",
            border_color=UI_LINE, focused_border_color=UI_BLUE, text_size=15,
            hint_style=ft.TextStyle(size=15, color="#9e9e9e"),
            content_padding=ft.Padding(12, 10, 12, 10), color=UI_INK,
            on_focus=lambda e: set_keyboard_mode(True),
            on_blur=lambda e: set_keyboard_mode(False),
            **kw)

    def dialog_action(text, on_click, color=None):
        return ft.TextButton(
            content=ft.Text(text, size=15, weight=ft.FontWeight.W_700, color=color or UI_BLUE),
            on_click=on_click)

    def open_rename_dialog(i):
        entry = editing[i]
        name_input = dialog_field(value=entry["name"])
        set_keyboard_mode(True)

        def confirm(e2):
            new = normalize_name(name_input.value)
            others = [x["name"] for j, x in enumerate(editing) if j != i]
            if not new:
                name_input.error_text = "姓名不能为空"
            elif new in others:
                name_input.error_text = f"本班已经有『{new}』，可改成「{disambiguate(new, others)}」"
            elif any(s["name"] == new for s in inactive_rows()):
                name_input.error_text = f"本班有已移出名单的同学也叫『{new}』，请加区分标记，或者直接把『{new}』加回来"
            else:
                entry["name"] = new
                page.pop_dialog()
                persist_roster(snack=f"已改名为『{new}』")
                return
            name_input.update()

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("修改姓名"),
            content=ft.Column([
                ft.Text(f"原名：{entry.get('orig') or entry['name']}", size=13, color=UI_MUTED),
                name_input,
                ft.Text("考勤记录会保留在新名字下", size=12, color=UI_MUTED),
            ], tight=True, width=300, spacing=10),
            actions=[dialog_action("取消", lambda _: page.pop_dialog()),
                     dialog_action("确定", confirm)],
            actions_alignment=ft.MainAxisAlignment.END,
            on_dismiss=lambda e: set_keyboard_mode(False)))

    def confirm_remove_student(i):
        entry = editing[i]
        name = entry["name"]
        rec_cnt = 0
        if entry.get("id") is not None:
            rec_cnt = _count(get_db_connection(),
                             "SELECT COUNT(*) FROM records WHERE student_id=?", (entry["id"],))
        # 勾选文字用独立 Text，避免大字号撑出框
        purge_cb = ft.Checkbox(value=False)
        purge_label = ft.Text(
            "同时永久删除他们的考勤记录（不推荐）",
            size=13, color=ft.Colors.GREY_800, expand=True,
            no_wrap=False,
        )

        def confirm(e2):
            purge = bool(purge_cb.value)
            page.pop_dialog()
            removed = editing.pop(i)
            remove_cid = get_current_class_id()
            result = persist_roster(purge=purge)
            if result is None:
                # persist 失败会从库重载 editing；勿留下脏 undo_state
                clear_undo_state()
                return
            if purge:
                clear_undo_state()
                show_snackbar(f"已永久删除『{name}』及其考勤记录")
                return
            # 仅写库成功后记录撤销（绑定原班级）
            undo_state["token"] += 1
            token = undo_state["token"]
            undo_state["entry"] = dict(removed)
            undo_state["index"] = i
            undo_state["class_id"] = remove_cid

            def do_undo():
                if undo_state["token"] != token or undo_state["entry"] is None:
                    show_snackbar("已过可撤销时间", is_error=True)
                    return
                if not undo_remove_allowed(undo_state.get("class_id"), get_current_class_id()):
                    clear_undo_state()
                    show_snackbar("已切换班级，无法撤销", is_error=True)
                    return
                ent = undo_state["entry"]
                idx = min(undo_state["index"], len(editing))
                undo_state["entry"] = None
                undo_state["class_id"] = None
                editing.insert(idx, ent)
                persist_roster(snack=f"已撤销，恢复『{ent['name']}』")

            show_action_snackbar(f"已删除『{name}』", "撤销", do_undo, ms=6000)

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("确认删除"),
            content=ft.Column([
                ft.Text(f"将移出名单：『{name}』\n"
                        f"{'其 ' + str(rec_cnt) + ' 条考勤记录会保留：' if rec_cnt else '没有考勤记录。'}\n"
                        f"{'导出在班期间的月份时照常出现，之后留空；以后把同名的人加回来，记录也会恢复。' if rec_cnt else ''}"),
                ft.Container(
                    content=ft.Row(
                        [purge_cb, purge_label], spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        expand=True,
                    ),
                    padding=ft.Padding.only(top=8)),
            ], tight=True, width=300),
            actions=[
                ft.TextButton("取消", on_click=lambda _: page.pop_dialog()),
                ft.TextButton("确认删除", on_click=confirm, style=ft.ButtonStyle(color=ft.Colors.RED_600)),
            ],
            actions_alignment=ft.MainAxisAlignment.END))

    def on_manage_reorder(e):
        old_i, new_i = e.old_index, e.new_index
        if old_i is None or new_i is None or old_i == new_i:
            return
        # ReorderableListView 有时 new_index 指向插入位，需按 Flutter 习惯修正
        item = editing.pop(old_i)
        if new_i > old_i:
            new_i -= 1
        editing.insert(new_i, item)
        persist_roster()

    def rebuild_manage_list_ui():
        rows = []
        for idx, entry in enumerate(editing):
            label = entry["name"]
            if entry.get("orig") and entry["orig"] != entry["name"]:
                label += f"（原：{entry['orig']}）"
            if entry.get("from_class"):
                label += f"（从{entry['from_class']}移入）"
            row = ft.Container(
                content=ft.Row(
                    [
                        ft.Text(label, size=16, weight=ft.FontWeight.W_600, color=UI_INK, expand=True),
                        ft.IconButton(icon=ft.Icons.EDIT, icon_size=20, icon_color=UI_BLUE,
                                      width=34, height=34, padding=0, style=ft.ButtonStyle(padding=0),
                                      tooltip="改名", on_click=lambda _, i=idx: open_rename_dialog(i)),
                        ft.IconButton(icon=ft.Icons.DELETE, icon_size=20, icon_color=UI_DANGER,
                                      width=34, height=34, padding=0, style=ft.ButtonStyle(padding=0),
                                      tooltip="删除", on_click=lambda _, i=idx: confirm_remove_student(i)),
                        ft.ReorderableDragHandle(
                            content=ft.Container(
                                content=ft.Icon(ft.Icons.MENU_ROUNDED, color="#90a4ae", size=20),
                                width=34, height=34, alignment=ft.Alignment.CENTER)),
                    ],
                    spacing=2,
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.Padding(14, 11, 10, 11),
                margin=ft.Margin.only(bottom=8),
                border_radius=12,
                bgcolor=ft.Colors.WHITE,
                animate_scale=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
                key=str(entry.get("id") or f"new-{idx}-{entry['name']}"),
            )
            _row_rest_style(row)
            rows.append(row)
        manage_list.controls = rows
        try:
            page.update()
        except Exception:
            pass

    async def on_import_click(e):
        files = await file_picker.pick_files(allowed_extensions=["xlsx", "csv", "xls"])
        if files and files[0].path:
            handle_imported_file(files[0].path)
        elif files:
            show_snackbar("无法读取所选文件的路径", is_error=True)

    # 1.5.2：「导入」只留文字（渲染图 .import-btn）
    import_icon_btn = ft.TextButton(
        content=ft.Text("导入", size=14, weight=ft.FontWeight.W_700, color=UI_BLUE),
        on_click=on_import_click,
        style=ft.ButtonStyle(padding=ft.Padding.symmetric(horizontal=10, vertical=8)))

    # ---- 移到其他班 + 串课处理 ----
    def open_shared_slots_dialog(cid_a, cid_b, slots):
        name_a, name_b = get_class_name(cid_a), get_class_name(cid_b)
        dropdowns = []
        rows = []
        for d, p, info in slots:
            dd = ft.Dropdown(value="both", dense=True, width=170, options=[
                ft.dropdown.Option("both", "两个班都有课"),
                ft.dropdown.Option("a", f"只属于{name_a}"),
                ft.dropdown.Option("b", f"只属于{name_b}"),
            ])
            dropdowns.append((d, p, dd))
            m, day = _md(d)
            rows.append(ft.Row([
                ft.Text(f"{m}月{day}日 {PERIOD_LABELS[p]}\n{name_a} {info[cid_a][0]} 条 / {name_b} {info[cid_b][0]} 条",
                        size=12, expand=True),
                dd], spacing=6))

        def confirm(e2):
            decisions = []
            for d, p, dd in dropdowns:
                if dd.value == "a":
                    decisions.append((d, p, cid_a, cid_b))
                elif dd.value == "b":
                    decisions.append((d, p, cid_b, cid_a))
            page.pop_dialog()
            try:
                n, _ = resolve_shared_slots(decisions)
            except Exception as ex:
                show_snackbar(f"处理失败，记录没有改动：{ex}", is_error=True)
                return
            refresh_roll_call()
            page.update()
            show_snackbar(f"串课已处理：删除了 {n} 条不属于该班的记录（删前已自动备份）" if n else "已保留两个班的记录")

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("检查串课"),
            content=ft.Column(
                [ft.Text(f"下面 {len(slots)} 节课两个班都有记录（以前用合并名单点名留下的）。请逐节选择归属；"
                         f"选「只属于某班」会删掉另一个班在这一节的记录，删前自动备份。", size=12)] + rows,
                tight=True, scroll=ft.ScrollMode.AUTO, height=360, width=340),
            actions=[ft.TextButton("稍后再说", on_click=lambda _: page.pop_dialog()),
                     ft.TextButton("确定", on_click=confirm)],
            actions_alignment=ft.MainAxisAlignment.END))

    def open_move_dialog(e=None):
        collapse_fab()
        cur = get_current_class_id()
        others = [c for c in list_classes() if c[0] != cur]
        if not others:
            show_snackbar("还没有其他班级，请先在班级列表里新建", is_error=True)
            return
        studs = get_class_students(cur)
        if not studs:
            show_snackbar("本班名单为空", is_error=True)
            return
        boxes = [ft.Checkbox(label=s["name"], value=False, data=s) for s in studs]
        joined_opts = sorted({s["joined_date"] for s in studs if s["joined_date"]})
        err_text = ft.Text("", color=ft.Colors.RED_600, size=12)
        target_dd = ft.Dropdown(label="移到", value=str(others[0][0]), dense=True,
                                options=[ft.dropdown.Option(str(c[0]), c[1]) for c in others])

        def select_by_joined(e2):
            v = joined_dd.value
            for b in boxes:
                b.value = (b.data["joined_date"] == v)
            page.update()

        joined_dd = ft.Dropdown(label="按入班日期全选", dense=True, on_select=select_by_joined,
                                options=[ft.dropdown.Option(d, d) for d in joined_opts], visible=bool(joined_opts))

        def confirm(e2):
            ids = [b.data["id"] for b in boxes if b.value]
            to_cid = int(target_dd.value)
            if not ids:
                err_text.value = "请先勾选要移的同学"
                err_text.update()
                return
            try:
                move_students(ids, to_cid, from_cid=cur)
            except ValueError as ex:
                err_text.value = str(ex)
                err_text.update()
                return
            except Exception as ex:
                err_text.value = f"移班失败，数据没有改动：{ex}"
                err_text.update()
                return
            page.pop_dialog()
            init_roster_manage_ui()
            refresh_roll_call()
            page.update()
            slots = shared_slots(cur, to_cid)
            if slots:
                open_shared_slots_dialog(cur, to_cid, slots)
            else:
                show_snackbar(f"已把 {len(ids)} 人移到『{get_class_name(to_cid)}』，考勤记录一起移过去了")

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("移到其他班"),
            content=ft.Column([target_dd, joined_dd, err_text] + boxes, tight=True, scroll=ft.ScrollMode.AUTO,
                              height=400, width=300),
            actions=[ft.TextButton("取消", on_click=lambda _: page.pop_dialog()),
                     ft.TextButton("移动", on_click=confirm)],
            actions_alignment=ft.MainAxisAlignment.END))

    # ---- 班级列表（切换 / 新建 / 重命名 / 删除） ----
    class_name_text = ft.Text(get_class_name(), size=14, weight=ft.FontWeight.W_700, color=UI_BLUE,
                              no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS)

    def refresh_all():
        load_students_order()
        class_name_text.value = get_class_name()
        init_roster_manage_ui()
        refresh_roll_call()
        page.update()

    def switch_to(cid):
        # 切班即失效「撤销」（抬高 token）；不在这里 pop_dialog，避免误关班级列表等对话框
        clear_undo_state()
        set_current_class(cid)
        refresh_all()

    def open_rename_class(cid):
        old = get_class_name(cid)
        name_input = ft.TextField(value=old, autofocus=True)

        def confirm(e2):
            try:
                n = rename_class(cid, name_input.value)
            except ValueError as ex:
                name_input.error_text = str(ex)
                name_input.update()
                return
            page.pop_dialog()
            class_name_text.value = get_class_name()
            page.update()
            show_snackbar(f"班级已改名为：{n}")

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("重命名班级"),
            content=ft.Column([name_input, ft.Text("只改班名，考勤不受影响。", size=12, color=ft.Colors.GREY_600)],
                              tight=True, width=280),
            actions=[ft.TextButton("取消", on_click=lambda _: page.pop_dialog()),
                     ft.TextButton("确定", on_click=confirm)],
            actions_alignment=ft.MainAxisAlignment.END))

    def open_delete_class(cid):
        name = get_class_name(cid)
        st = class_stats(cid)
        if st["students"] == 0 and st["records"] == 0:
            text = f"确定删除空班级『{name}』吗？"
        else:
            text = (f"将删除『{name}』的 {st['students']} 名同学和 {st['records']:,} 条考勤记录。"
                    f"删除前会自动备份，可在『数据备份与恢复』里找回。")

        def confirm(e2):
            page.pop_dialog()
            try:
                delete_class(cid)
            except Exception as ex:
                show_snackbar(f"删除失败：{ex}", is_error=True)
                return
            refresh_all()
            show_snackbar(f"已删除『{name}』")

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("删除班级"),
            content=ft.Text(text),
            actions=[ft.TextButton("取消", on_click=lambda _: page.pop_dialog()),
                     ft.TextButton("删除", on_click=confirm, style=ft.ButtonStyle(color=ft.Colors.RED_600))],
            actions_alignment=ft.MainAxisAlignment.END))

    def open_class_list(e=None):
        classes = list_classes()
        cur = get_current_class_id()
        only_one = len(classes) <= 1

        def pick(cid):
            def handler(_):
                page.pop_dialog()
                switch_to(cid)
                show_snackbar(f"已切换到：{get_class_name(cid)}")
            return handler

        def menu_rename(cid):
            def handler(_):
                page.pop_dialog()
                open_rename_class(cid)
            return handler

        def menu_delete(cid):
            def handler(_):
                page.pop_dialog()
                open_delete_class(cid)
            return handler

        rows = []
        for cid, cname, cnt in classes:
            rows.append(ft.Container(
                content=ft.Row([
                    ft.Icon(ft.Icons.CHECK, color=ft.Colors.BLUE_800, size=18, opacity=1 if cid == cur else 0),
                    ft.Text(cname, size=15, weight=ft.FontWeight.BOLD if cid == cur else None, expand=True),
                    ft.Text(f"{cnt} 人", size=12, color=ft.Colors.GREY_600),
                    ft.PopupMenuButton(items=[
                        ft.PopupMenuItem(content=ft.Text("重命名"), on_click=menu_rename(cid)),
                        ft.PopupMenuItem(content=ft.Text("删除"), on_click=menu_delete(cid), disabled=only_one),
                    ]),
                ], spacing=6),
                on_click=pick(cid), padding=ft.Padding.symmetric(horizontal=6, vertical=2), border_radius=6))
        new_input = ft.TextField(hint_text="新班级名称", dense=True, expand=True)

        def do_create(e2):
            try:
                cid = create_class(new_input.value)
            except ValueError as ex:
                new_input.error_text = str(ex)
                new_input.update()
                return
            page.pop_dialog()
            switch_to(cid)
            show_snackbar(f"已新建并切换到『{get_class_name(cid)}』，请导入名单")

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("切换班级", size=18, weight=ft.FontWeight.BOLD),
            content=ft.Column(rows + [ft.Divider(), ft.Row([new_input, ft.TextButton("＋新建", on_click=do_create)])],
                              tight=True, scroll=ft.ScrollMode.AUTO, width=320),
            actions=[ft.TextButton("关闭", on_click=lambda _: page.pop_dialog())],
            actions_alignment=ft.MainAxisAlignment.END))

    # 名单顶栏：左汉堡 | 中班级胶囊居中偏长 | 右导入
    class_chip = ft.Container(
        content=ft.Row([
            ft.Icon(ft.Icons.SCHOOL_ROUNDED, color=UI_BLUE, size=16),
            ft.Container(content=class_name_text, expand=False),
            ft.Icon(ft.Icons.ARROW_DROP_DOWN_ROUNDED, color=UI_BLUE, size=18, opacity=0.7),
        ], spacing=6, alignment=ft.MainAxisAlignment.CENTER, tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER),
        on_click=open_class_list,
        ink=True,
        bgcolor=UI_BLUE_SOFT,
        border_radius=18,
        padding=ft.Padding.symmetric(horizontal=18, vertical=8),
        alignment=ft.Alignment.CENTER,
    )

    roster_menu_update_dot = _make_corner_update_dot()
    roster_menu_btn = ft.Stack(
        [top_icon_btn(ft.Icons.MENU_ROUNDED, "设置", open_drawer), roster_menu_update_dot],
        width=36, height=36,
    )

    # 名单顶栏：左汉堡 | 班级胶囊绝对居中（宽） | 右「导入」文字
    roster_top_bar = make_top_bar(roster_menu_btn, class_chip, [import_icon_btn], mid_inset=54)

    # FAB / SpeedDial
    # 1.5.2 FAB 速拨（渲染图 04）：左白色标签 + 右圆形小按钮，弹出动画；主按钮 ＋ 旋转 45° 成 ×
    _dial_anim = ft.Animation(180, ft.AnimationCurve.EASE_OUT)

    def _apply_fab_state(open_):
        fab_expanded["v"] = open_
        for k, item in enumerate(fab_dial_items):
            item.opacity = 1 if open_ else 0
            item.scale = 1 if open_ else 0.85
            item.offset = ft.Offset(0, 0) if open_ else ft.Offset(0, 0.35 + 0.15 * k)
            item.ignore_interactions = not open_
        fab_icon.rotate = ft.Rotate(angle=(math.pi / 4) if open_ else 0)

    def collapse_fab():
        _apply_fab_state(False)
        try:
            fab_stack.update()
        except Exception:
            pass

    def toggle_fab(e=None):
        _apply_fab_state(not fab_expanded["v"])
        fab_stack.update()

    def make_dial_item(label, icon, icon_color, on_click):
        def _click(e):
            on_click(e)
        return ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        content=ft.Text(label, size=13, weight=ft.FontWeight.W_600, color=UI_INK),
                        bgcolor=ft.Colors.WHITE, border_radius=8,
                        padding=ft.Padding.symmetric(horizontal=10, vertical=6),
                        shadow=ft.BoxShadow(blur_radius=8, offset=ft.Offset(0, 2), color="#1f000000"),
                        on_click=_click),
                    ft.Container(
                        content=ft.Icon(icon, color=icon_color, size=22),
                        width=44, height=44, border_radius=22, bgcolor=ft.Colors.WHITE,
                        border=ft.Border.all(1, UI_LINE), alignment=ft.Alignment.CENTER,
                        shadow=ft.BoxShadow(blur_radius=8, offset=ft.Offset(0, 2), color="#1f000000"),
                        on_click=_click, ink=True),
                ],
                spacing=8, tight=True, alignment=ft.MainAxisAlignment.END,
                vertical_alignment=ft.CrossAxisAlignment.CENTER),
            opacity=0, scale=0.85, offset=ft.Offset(0, 0.35), ignore_interactions=True,
            animate_opacity=_dial_anim, animate_scale=_dial_anim, animate_offset=_dial_anim,
        )

    fab_move_btn = make_dial_item("移到其他班", ft.Icons.OPEN_WITH_ROUNDED, "#6a1b9a", open_move_dialog)
    fab_add_btn = make_dial_item("添加同学", ft.Icons.PERSON_ADD_ALT_1_ROUNDED, UI_BLUE, open_add_student_dialog)
    fab_dial_items = [fab_add_btn, fab_move_btn]   # 由近到远（动画错开）
    fab_actions = ft.Column(
        [fab_move_btn, fab_add_btn], spacing=10, tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.END)
    fab_icon = ft.Icon(ft.Icons.ADD_ROUNDED, color=ft.Colors.WHITE, size=30,
                       rotate=ft.Rotate(angle=0), animate_rotation=_dial_anim)
    fab_main = ft.Container(
        content=fab_icon, width=56, height=56, border_radius=28, bgcolor=UI_BLUE,
        alignment=ft.Alignment.CENTER, on_click=toggle_fab, ink=True, tooltip="添加 / 移动",
        shadow=ft.BoxShadow(blur_radius=16, offset=ft.Offset(0, 6), color="#661565c0"))
    fab_stack = ft.Column(
        [fab_actions, fab_main], spacing=10, tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.END)
    fab_holder = ft.Container(content=fab_stack, right=18, bottom=90)

    roster_manage_view = ft.Stack(
        [
            ft.Column(
                [ft.Container(height=UI_TOP_INSET), roster_top_bar, manage_list],
                expand=True, spacing=0),
            fab_holder,
        ],
        expand=True,
    )

    # 点名页：保留 PageView 左右滑切页；人名区尽量吸收横滑，减少误切（个别机型拦不住为已知限制）
    def absorb_h(_e=None):
        return None

    name_scroll_area = ft.GestureDetector(
        content=roster_column,
        on_horizontal_drag_start=absorb_h,
        on_horizontal_drag_update=absorb_h,
        on_horizontal_drag_end=absorb_h,
        expand=True,
    )

    def on_left_edge_drag_end(e):
        # 仅点名页：左缘右滑拉开自定义侧栏；名单页不挂此手势
        v = getattr(e, "primary_velocity", None) or 0
        if v > 180:
            open_drawer()

    left_edge = ft.Container(
        width=18, left=0, top=0, bottom=0,
        content=ft.GestureDetector(
            on_horizontal_drag_end=on_left_edge_drag_end,
            content=ft.Container(expand=True),
        ),
    )

    roll_call_view = ft.Stack(
        [
            ft.Column(
                [ft.Container(height=UI_TOP_INSET - 8), roll_swipe_header,
                 ft.Container(content=name_scroll_area, expand=True, padding=ft.Padding(12, 0, 12, 0))],
                expand=True, spacing=8,
            ),
            left_edge,
        ],
        expand=True,
    )

    # ----------------- 8. 侧边抽屉（方案 D，原关于页） -----------------

    DRAWER_INK = "#0f172a"
    DRAWER_SUB = "#94a3b8"
    DRAWER_HAIR = "#f1f5f9"

    def make_drawer_tile(icon, title, subtitle="", on_click=None, badge=None, last=False):
        """渲染图 02D：36px 浅蓝图标块 + 标题/副标题 + 细箭头；行间 1px 发丝线（末行无）。"""
        title_controls = [ft.Text(title, size=14.5, weight=ft.FontWeight.W_600, color=DRAWER_INK)]
        if badge is not None:
            title_controls.append(badge)
        return ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        content=ft.Icon(icon, color=UI_BLUE, size=18),
                        width=36, height=36, bgcolor="#eff6ff", border_radius=10,
                        alignment=ft.Alignment.CENTER),
                    ft.Column(
                        [
                            ft.Row(title_controls, spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                            ft.Text(subtitle, size=11.5, color=DRAWER_SUB, weight=ft.FontWeight.W_500)
                            if subtitle else ft.Container(height=0),
                        ],
                        spacing=2, tight=True, expand=True),
                    ft.Icon(ft.Icons.CHEVRON_RIGHT_ROUNDED, color="#cbd5e1", size=20),
                ],
                spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=12, vertical=13),
            border=None if last else ft.Border.only(bottom=ft.BorderSide(1, DRAWER_HAIR)),
            on_click=on_click if on_click else lambda _: show_snackbar("该功能已预留，正在开发中..."),
            ink=True,
        )

    def show_logs_dialog(e):
        close_drawer()
        log_text = redact_sensitive_text(get_app_logs(), collect_student_names_for_redact()) or "暂无运行日志。"
        text_field = ft.TextField(
            value=log_text, multiline=True, read_only=True, min_lines=8, max_lines=15,
            text_size=12, bgcolor=ft.Colors.GREY_50, border_color=ft.Colors.GREY_300, color=ft.Colors.BLACK)

        def copy_logs(e2):
            async def set_clip():
                await ft.Clipboard().set(log_text)
            page.run_task(set_clip)
            show_snackbar("日志已成功复制到剪贴板！")

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("系统运行日志", size=18, weight=ft.FontWeight.BOLD),
            content=ft.Container(content=text_field, width=350, height=300),
            actions=[
                ft.TextButton("复制并关闭", on_click=lambda e: (page.pop_dialog(), copy_logs(e))),
                ft.TextButton("关闭", on_click=lambda _: page.pop_dialog())
            ],
            actions_alignment=ft.MainAxisAlignment.END))

    def fmt_time(t):
        return t.strftime("%m-%d %H:%M")

    def open_restore_confirm(item):
        def confirm(e2):
            page.pop_dialog()
            try:
                restore_backup(item["path"])
            except Exception as ex:
                show_snackbar(f"恢复失败，现在的数据没有改动：{ex}", is_error=True)
                return
            telemetry_note_feature("restore", page)
            refresh_all()
            go_tab(0)
            show_snackbar(f"已恢复到 {fmt_time(item['time'])} 的数据")

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("恢复备份"),
            content=ft.Text(f"将用 {fmt_time(item['time'])} 的备份（{item['classes']} 个班 / {item['students']} 人 / "
                            f"{item['records']:,} 条考勤）替换现在所有班级的名单和考勤。\n"
                            f"恢复前会把现在的数据再备份一份，恢复错了还能再恢复回来。"),
            actions=[ft.TextButton("取消", on_click=lambda _: page.pop_dialog()),
                     ft.TextButton("恢复", on_click=confirm, style=ft.ButtonStyle(color=ft.Colors.RED_600))],
            actions_alignment=ft.MainAxisAlignment.END))

    def open_file_restore_confirm(staging, info, fname):
        def cancel(e2):
            page.pop_dialog()
            discard_restore_file(staging)

        def confirm(e2):
            page.pop_dialog()
            try:
                restore_backup(staging)
            except Exception as ex:
                discard_restore_file(staging)
                show_snackbar(f"恢复失败：{ex}", is_error=True)
                return
            discard_restore_file(staging)
            telemetry_note_feature("restore", page)
            refresh_all()
            go_tab(0)
            show_snackbar("已从文件恢复（恢复前的数据已另外备份）")

        reason = f"\n备份说明：{info['reason']}" if info.get("reason") else ""
        origin = restore_origin_label(info)
        warn = restore_origin_warning(info)
        warn_line = ("\n" + warn) if warn else ""
        page.show_dialog(ft.AlertDialog(
            modal=True,
            title=ft.Text("从文件恢复"),
            content=ft.Text(f"文件：{fname}\n{origin}\n"
                            f"内容：{info['classes']} 个班 / {info['students']} 人 / {info['records']:,} 条考勤{reason}"
                            f"{warn_line}\n\n"
                            f"将用它替换现在所有班级的名单和考勤。恢复前会把现在的数据再备份一份，恢复错了还能再恢复回来。"),
            actions=[ft.TextButton("取消", on_click=cancel),
                     ft.TextButton("恢复", on_click=confirm, style=ft.ButtonStyle(color=ft.Colors.RED_600))],
            actions_alignment=ft.MainAxisAlignment.END))

    async def on_restore_from_file(e=None):
        page.pop_dialog()
        try:
            files = await file_picker.pick_files(dialog_title="选择点名助手的备份文件",
                                                 file_type=ft.FilePickerFileType.ANY, allow_multiple=False)
        except Exception as ex:
            show_snackbar(f"打开文件选择失败：{ex}", is_error=True)
            return
        if not files:
            return
        f = files[0]
        if not f.path:
            show_snackbar("读不到这个文件的位置，请先把文件保存到手机里再选择", is_error=True)
            return
        try:
            staging, info = prepare_restore_file(f.path)
        except Exception as ex:
            show_snackbar(f"不能恢复：{ex}。现在的数据没有改动", is_error=True)
            return
        open_file_restore_confirm(staging, info, f.name or os.path.basename(f.path))

    def open_backup_dialog(e=None):
        close_drawer()
        items = list_backups()

        def restore_handler(item):
            def handler(_):
                page.pop_dialog()
                open_restore_confirm(item)
            return handler

        def share_handler(item):
            def handler(_):
                page.pop_dialog()
                share_file(item["path"], "点名助手数据备份")
            return handler

        def backup_now(_):
            page.pop_dialog()
            try:
                make_backup("manual", reason="手动备份", force=True)
            except Exception as ex:
                show_snackbar(f"备份失败：{ex}", is_error=True)
                return
            telemetry_note_feature("backup", page)
            open_backup_dialog()

        rows = []
        for it in items:
            desc = (f"{it['classes']} 个班 / {it['students']} 人 / {it['records']:,} 条" if it["ok"]
                    else "这份备份已损坏")
            rows.append(ft.Container(
                content=ft.Row([
                    ft.Column([
                        ft.Text(f"{fmt_time(it['time'])}  {it['label']}", size=14, weight=ft.FontWeight.BOLD),
                        ft.Text(desc + (f"\n{it['reason']}" if it["reason"] else ""), size=11, color=ft.Colors.GREY_600),
                    ], spacing=2, expand=True, tight=True),
                    ft.TextButton("恢复", on_click=restore_handler(it), disabled=not it["ok"]),
                    ft.TextButton("分享", on_click=share_handler(it)),
                ], spacing=2),
                border=ft.Border.all(1, ft.Colors.GREY_300), border_radius=8,
                padding=ft.Padding.symmetric(horizontal=8, vertical=4)))
        if not rows:
            rows.append(ft.Text("还没有备份。", color=ft.Colors.GREY_500))

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("数据备份与恢复", size=18, weight=ft.FontWeight.BOLD),
            content=ft.Column(
                [ft.Text("升级前、删除/改名/移班前、恢复前都会自动备份；有考勤时每周一也会自动备一份。"
                         "备份在手机本地，卸载 App 会一起删除；换手机前请用「分享」把备份发到微信或电脑上保存，"
                         "之后用「从文件恢复」选这个文件导回来。", size=12,
                         color=ft.Colors.GREY_700)] + rows,
                tight=True, scroll=ft.ScrollMode.AUTO, height=420, width=340),
            actions=[ft.TextButton("从文件恢复", on_click=on_restore_from_file),
                     ft.TextButton("立即备份", on_click=backup_now),
                     ft.TextButton("关闭", on_click=lambda _: page.pop_dialog())],
            actions_alignment=ft.MainAxisAlignment.END))

    url_launcher = ft.UrlLauncher()
    page.services.append(url_launcher)
    update_state = {"busy": False}

    def open_link(url):
        async def go():
            try:
                await url_launcher.launch_url(url, mode=ft.LaunchMode.EXTERNAL_APPLICATION)
            except Exception as ex:
                show_snackbar(f"打开浏览器失败：{ex}", is_error=True)
        page.run_task(go)

    def open_update_dialog(info):
        show_update_dialog(page, info, open_link, show_snackbar)

    update_tile_dot = ft.Container(
        width=8, height=8, bgcolor=ft.Colors.RED_500, border_radius=8, visible=_update_badge_init)
    update_tile = make_drawer_tile(ft.Icons.SYSTEM_UPDATE_ALT_ROUNDED, "检查更新", f"当前版本 v{APP_VERSION}",
                                   on_click=lambda e: on_check_update(e), badge=update_tile_dot)
    # subtitle Text 是 Column 第二个控件
    update_tile_subtitle = update_tile.content.controls[1].controls[1]

    def set_update_badges(show):
        """汉堡与「检查更新」行的小红点；关下载窗不关红点。"""
        vis = bool(show)
        for dot in (menu_update_dot, roster_menu_update_dot, update_tile_dot):
            dot.visible = vis
        try:
            page.update()
        except Exception:
            pass

    def set_update_tile_status(checking):
        update_tile_subtitle.value = UPDATE_CHECKING_TEXT if checking else f"当前版本 v{APP_VERSION}"
        update_tile_subtitle.color = UI_BLUE if checking else DRAWER_SUB
        try:
            update_tile_subtitle.update()
        except Exception:
            pass

    def on_check_update(e=None):
        close_drawer()
        if update_state["busy"]:
            return
        telemetry_note_feature("check_update", page)
        update_state["busy"] = True
        set_update_tile_status(True)
        show_snackbar("正在检查更新…")

        async def run():
            import asyncio
            try:
                result, data = await asyncio.to_thread(check_for_update)
            except Exception as ex:
                result, data = "fail", [str(ex)]
            finally:
                update_state["busy"] = False
                set_update_tile_status(False)
            try:
                cache = load_update_cache()
                info = data if result in ("new", "latest") and isinstance(data, dict) else None
                cache = apply_update_check_to_cache(cache, result, info)
                save_update_cache(cache)
                set_update_badges(update_badge_should_show(cache))
            except Exception as ex:
                print(f"[update] 写入更新缓存失败：{ex}")
            if result == "new":
                open_update_dialog(data)
            elif result == "latest":
                show_snackbar(f"已是最新版本 v{APP_VERSION}")
            else:
                show_snackbar("检查失败，请检查网络后再试", is_error=True)
        page.run_task(run)

    def open_feedback_dialog(e=None):
        close_drawer()
        telemetry_note_feature("feedback", page)

        def copy_email(_=None):
            async def set_clip():
                await ft.Clipboard().set(DEVELOPER_EMAIL)
            page.run_task(set_clip)
            show_snackbar("已复制")

        page.show_dialog(ft.AlertDialog(
            title=ft.Text("问题反馈"),
            content=ft.Column([
                ft.Text("把问题现象、截图和运行日志发到开发者邮箱：", size=13, color=ft.Colors.GREY_800),
                ft.Row([
                    ft.Text(DEVELOPER_EMAIL, size=14, weight=ft.FontWeight.W_600,
                            color=ft.Colors.BLUE_800, selectable=True, expand=True),
                    ft.OutlinedButton("复制", icon=ft.Icons.CONTENT_COPY_ROUNDED, on_click=copy_email),
                ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ], tight=True, spacing=10, width=320),
            actions=[ft.TextButton("关闭", on_click=lambda _: page.pop_dialog())],
            actions_alignment=ft.MainAxisAlignment.END))

    def share_app_apk(e=None):
        """优先分享本机已安装 APK；读不到则弹 GitHub / 蓝奏云下载对话框（与检查更新一致，不静默安装）。"""
        close_drawer()
        telemetry_note_feature("share_app", page)
        show_snackbar("正在准备分享…")

        async def run():
            import asyncio
            try:
                src = await asyncio.to_thread(find_installed_apk_path)
                if src:
                    dest = await asyncio.to_thread(copy_apk_to_share_cache, src)
                    if is_mobile():
                        await share_service.share_files(
                            [ft.ShareFile.from_path(os.path.abspath(dest))],
                            title="点名助手安装包")
                    else:
                        show_snackbar("安装包已准备好：" + friendly_data_path(dest))
                    return
                # 读不到本包 → 弹出下载渠道（GitHub 直链 / 蓝奏云），让用户自己选源打开
                info = await asyncio.to_thread(resolve_download_info_for_share)
                show_share_download_dialog(page, info, open_link, show_snackbar)
            except Exception as ex:
                print(f"[share-apk] 失败：{ex}")
                show_snackbar("分享失败，请稍后重试或从检查更新里下载", is_error=True)

        page.run_task(run)

    def open_source_repo(e=None):
        close_drawer()
        open_link(SOURCE_REPO_URL)

    drawer_header = ft.Container(
        content=ft.Row([
            ft.Container(
                content=ft.Icon(ft.Icons.LAYERS_ROUNDED, size=22, color=UI_BLUE),
                width=40, height=40, bgcolor=UI_BLUE_SOFT, border_radius=12,
                alignment=ft.Alignment.CENTER),
            ft.Column([
                ft.Text("点名助手", size=16, weight=ft.FontWeight.W_700, color=DRAWER_INK),
                ft.Text("学委量化管理提效神器", size=11.5, color=DRAWER_SUB, weight=ft.FontWeight.W_500),
            ], spacing=3, tight=True),
        ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        padding=ft.Padding(20, 52, 20, 18),
    )

    drawer_version = ft.Container(
        content=ft.Column([
            ft.Text(f"v{APP_VERSION} (build {APP_BUILD})", size=12, color="#64748b",
                    weight=ft.FontWeight.W_600),
            ft.Text("© 2026 HMUDQ-CS", size=10.5, color=DRAWER_SUB),
        ], spacing=2, tight=True, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        padding=ft.Padding(20, 14, 20, 22),
        border=ft.Border.only(top=ft.BorderSide(1, DRAWER_HAIR)),
        alignment=ft.Alignment.CENTER,
    )

    drawer_list = ft.Container(
        content=ft.Column(
            [
                update_tile,
                make_drawer_tile(ft.Icons.BACKUP_ROUNDED, "数据备份与恢复",
                                 "备份 · 恢复 · 分享", on_click=open_backup_dialog),
                make_drawer_tile(ft.Icons.BUG_REPORT_OUTLINED, "导出运行日志",
                                 "反馈 Bug 时附上", on_click=show_logs_dialog),
                make_drawer_tile(ft.Icons.FEEDBACK_OUTLINED, "问题反馈",
                                 "建议与故障报告", on_click=open_feedback_dialog),
                make_drawer_tile(ft.Icons.SHARE_OUTLINED, "分享应用",
                                 "一键分享安装包", on_click=share_app_apk),
                make_drawer_tile(ft.Icons.CODE_ROUNDED, "访问项目源码地址",
                                 "GitHub 开源仓库", on_click=open_source_repo, last=True),
            ],
            spacing=0, scroll=ft.ScrollMode.AUTO,
        ),
        padding=ft.Padding(8, 4, 8, 8),
        expand=True,
    )

    # 贴左、只右侧圆角：Flutter 不允许「非等宽边框 + 圆角」，故用 Border.all 并把面板向左推 1px，
    # 左边线落到屏幕外，视觉上只剩右侧发丝线 + 右上/右下 16 圆角。
    drawer_panel = ft.Container(
        content=ft.Column(
            [
                drawer_header,
                drawer_list,
                drawer_version,
            ],
            spacing=0, expand=True,
        ),
        width=_drawer_width() + 1,
        bgcolor=ft.Colors.WHITE,
        border=ft.Border.all(1, "#e5e7eb"),
        border_radius=ft.BorderRadius.only(top_right=16, bottom_right=16),
        shadow=ft.BoxShadow(blur_radius=36, color="#1f0f172a", offset=ft.Offset(12, 0)),
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        left=-1, top=0, bottom=0,
        padding=0,
        offset=ft.Offset(0, 0),
        animate_offset=ft.Animation(220, ft.AnimationCurve.EASE_OUT_CUBIC),
    )
    drawer_scrim = ft.Container(
        left=0, top=0, right=0, bottom=0,
        bgcolor="#660f172a",
        on_click=close_drawer,
        opacity=1,
        animate_opacity=ft.Animation(200, ft.AnimationCurve.EASE_OUT),
    )
    drawer_layer = ft.Stack(
        [drawer_scrim, drawer_panel],
        expand=True,
        visible=False,
        left=0, top=0, right=0, bottom=0,
    )
    drawer_state["panel"] = drawer_panel
    drawer_state["scrim"] = drawer_scrim
    drawer_state["layer"] = drawer_layer
    page.drawer = None  # 不用系统 NavigationDrawer；点名页用自定义左缘手势，名单页仅汉堡

    # about_view 变量保留供结构检查；实际入口是抽屉
    about_view = ft.Column([
        ft.Text(f"v{APP_VERSION} (build {APP_BUILD})"),
        ft.Text(f"当前版本 v{APP_VERSION}"),
    ])

    # ----------------- 9. PageView（保留手指滑切页）+ 毛玻璃底栏 -----------------

    def on_pageview_change(e):
        nonlocal nav_index
        idx = int(getattr(e.control, "selected_index", 0) or 0)
        nav_index = idx
        sync_nav_ui(idx)
        if idx == 0 and roll_call_stale:
            load_attendance_from_db()
            rebuild_roster_ui()
        if idx != 1:
            collapse_fab()
        page.update()

    pages = ft.PageView(
        controls=[
            ft.Container(content=roll_call_view, expand=True),
            ft.Container(content=roster_manage_view, expand=True),
        ],
        selected_index=0,
        horizontal=True,
        expand=True,
        on_change=on_pageview_change,
    )

    def go_tab(idx):
        nonlocal nav_index
        nav_index = idx
        pages.selected_index = idx
        sync_nav_ui(idx)
        page.update()
        if idx == 0 and roll_call_stale:
            load_attendance_from_db()
            rebuild_roster_ui()
            page.update()
        if idx != 1:
            collapse_fab()

    def sync_nav_ui(idx):
        for i, item in enumerate(nav_items):
            active = (i == idx)
            item["icon"].color = UI_BLUE if active else UI_MUTED
            item["label"].color = UI_BLUE if active else UI_MUTED
            item["label"].weight = ft.FontWeight.W_600
            item["pill"].bgcolor = UI_BLUE_PILL if active else None

    def make_nav_item(icon_data, label, idx):
        ic = ft.Icon(icon_data, color=UI_MUTED, size=22)
        lb = ft.Text(label, size=11, color=UI_MUTED, weight=ft.FontWeight.W_600)
        pill = ft.Container(
            content=ft.Column([ic, lb], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            width=96, padding=ft.Padding.symmetric(horizontal=8, vertical=8), border_radius=18,
            on_click=lambda _, i=idx: go_tab(i), ink=True,
            animate=ft.Animation(160, ft.AnimationCurve.EASE_OUT))
        box = ft.Container(content=pill, alignment=ft.Alignment.CENTER)
        return {"icon": ic, "label": lb, "pill": pill, "box": box}

    nav_items = [
        make_nav_item(ft.Icons.FORMAT_LIST_BULLETED_ROUNDED, "上课点名", 0),
        make_nav_item(ft.Icons.PEOPLE_ROUNDED, "名单管理", 1),
    ]
    sync_nav_ui(0)

    # 1.5.2 悬浮毛玻璃底栏（渲染图 .nav）：左右 12、底 10、高 68、圆角 22
    frost_nav = ft.Container(
        content=ft.Row([it["box"] for it in nav_items], alignment=ft.MainAxisAlignment.SPACE_EVENLY,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor=ft.Colors.with_opacity(0.82, ft.Colors.WHITE),
        blur=ft.Blur(20, 20),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.7, ft.Colors.WHITE)),
        border_radius=22,
        shadow=ft.BoxShadow(blur_radius=20, offset=ft.Offset(0, 6), color="#0a000000"),
        height=68,
        bottom=10, left=12, right=12,
    )
    kb_refs["frost_nav"] = frost_nav
    kb_refs["fab_holder"] = fab_holder

    # 点名页人名区底部留白，滚到底时名字能完整露在毛玻璃上方
    roster_column.spacing = 8
    # Column.scroll 已有；用底部占位
    def _ensure_bottom_spacer():
        # rebuild_roster_ui 末尾追加 spacer；这里包一层
        pass

    _orig_rebuild = rebuild_roster_ui

    def rebuild_roster_ui_with_pad():
        _orig_rebuild()
        if STUDENTS_ORDER:
            roster_column.controls.append(ft.Container(height=96))

    # 覆盖同名函数供后续调用
    rebuild_roster_ui = rebuild_roster_ui_with_pad

    main_body = ft.Stack([pages, frost_nav, drawer_layer], expand=True)

    # 兼容旧测试：仍暴露 roll_call_view / VIEWS
    VIEWS = [roll_call_view, roster_manage_view, about_view]

    # 不用系统 NavigationBar（改自定义毛玻璃底栏）
    page.navigation_bar = None

    roster_column.controls.append(
        ft.Container(
            content=ft.ProgressRing(color=ft.Colors.BLUE_800),
            alignment=ft.Alignment.CENTER, padding=100)
    )
    page.add(main_body)
    page.update()

    async def initial_load(e=None):
        import asyncio
        await asyncio.sleep(0.01)
        load_attendance_from_db()
        rebuild_roster_ui()
        init_roster_manage_ui()
        page.update()
        if init_info.get("migrated"):
            msg = f"数据已升级到 v{APP_VERSION}，升级前已自动备份"
            if init_info.get("classes"):
                msg += f"；已按原班级分成 {len(init_info['classes'])} 个班（{'、'.join(init_info['classes'])}）"
            if init_info.get("renamed"):
                msg += f"；{len(init_info['renamed'])} 位同班重名同学名字后加了 (2) 等编号，可在名单管理里改"
            show_snackbar(msg)

    page.run_task(initial_load)

    # 静默检查更新：同一自然日最多一次；有新版只亮小红点，绝不自动弹窗/SnackBar
    async def silent_update_check(e=None):
        import asyncio
        try:
            cache = load_update_cache()
            set_update_badges(update_badge_should_show(cache))
            if not should_auto_check_update(cache):
                return
            result, data = await asyncio.to_thread(check_for_update)
            info = data if result in ("new", "latest") and isinstance(data, dict) else None
            cache = apply_update_check_to_cache(cache, result, info)
            save_update_cache(cache)
            set_update_badges(update_badge_should_show(cache))
        except Exception as ex:
            print(f"[update] 静默检查失败：{ex}")

    page.run_task(silent_update_check)

    # 匿名使用统计（默认关闭；失败不影响界面）
    global _telemetry_session_start
    try:
        _telemetry_session_start = datetime.datetime.now()
        # 若同进程内昨夜有未发日汇总，启动时尝试 flush（关闭时 no-op）
        telemetry_flush_roll_day(page)
        hour, weekday = local_hour_weekday()
        maybe_send_telemetry(
            "app_open",
            platform=telemetry_platform_from_page(page),
            local_hour=hour,
            local_weekday=weekday,
        )
    except Exception:
        pass

    # 有可靠关闭钩子则发 app_session；没有也不硬掰
    def _on_telemetry_session_end(e=None):
        try:
            telemetry_end_session(page)
        except Exception:
            pass

    try:
        if hasattr(page, "on_disconnect"):
            prev = page.on_disconnect

            def _disconnect_wrap(e=None):
                _on_telemetry_session_end(e)
                if callable(prev):
                    try:
                        prev(e)
                    except Exception:
                        pass
            page.on_disconnect = _disconnect_wrap
        elif hasattr(page, "on_close"):
            prev = getattr(page, "on_close", None)

            def _close_wrap(e=None):
                _on_telemetry_session_end(e)
                if callable(prev):
                    try:
                        prev(e)
                    except Exception:
                        pass
            page.on_close = _close_wrap
    except Exception:
        pass


if __name__ == '__main__':
    # 也可以作为一个 Web 服务启动以支持多端同步 (如: ft.app(target=main, view=ft.AppView.WEB_BROWSER))
    # 默认直接以桌面程序运行，本地读写速度最快
    ft.run(main)
