# -*- coding: utf-8 -*-
"""匿名使用统计骨架：默认关闭，不含名单 / 考勤明细 / 姓名 / 班级 / 路径。

以后若要开启：把 TELEMETRY_ENABLED 改为 True，并填好 TELEMETRY_ENDPOINT
（你自己的 HTTPS 接收 URL）。endpoint 为空时即使开关打开也不发。

可上报（聚合、匿名）：
- app_open：启动；含本地小时/星期、粗粒度平台 → 高峰时段
- app_session：会话时长（秒，封顶 7200）
- roll_call_day：当天点名活跃汇总（次数、用过几个节次），不含谁/哪一班
- feature：功能点击（枚举白名单）
"""
import datetime
import json
import os
import uuid
import urllib.request

# 硬开关：默认关。以后人多了再打开。
TELEMETRY_ENABLED = False
# 接收 URL；空字符串时即使启用也不发。
TELEMETRY_ENDPOINT = ""
TELEMETRY_TIMEOUT = 3
INSTALL_ID_FILENAME = "install_id"
SESSION_SECONDS_CAP = 7200
PERIODS_USED_CAP = 5

TELEMETRY_EVENTS = frozenset({
    "app_open",
    "app_session",
    "roll_call_day",
    "feature",
})

TELEMETRY_FEATURES = frozenset({
    "export",
    "backup",
    "restore",
    "check_update",
    "share_app",
    "feedback",
    "drawer_open",
})

TELEMETRY_PLATFORMS = frozenset({
    "android",
    "ios",
    "desktop",
    "unknown",
})

# payload 只允许这些键（公共 + 事件可选）
TELEMETRY_PAYLOAD_KEYS = frozenset({
    "install_id",
    "app_version",
    "app_build",
    "event",
    "ts",
    "local_hour",
    "local_weekday",
    "platform",
    "session_seconds",
    "marks_count",
    "periods_used",
    "local_date",
    "feature",
})

# 内存中的当天点名汇总（不含姓名/班级）；默认关时累加也几乎零成本
_roll_day = {
    "local_date": None,  # YYYY-MM-DD
    "marks_count": 0,
    "periods": set(),
}
# 切日时未发出的上一桶（进程内）；flush 时优先发出
_pending_roll_day = None


def get_or_create_install_id(data_dir):
    """本地匿名安装 ID：首次随机 UUID，存在 data_dir/install_id；不用设备真实标识。"""
    path = os.path.join(data_dir, INSTALL_ID_FILENAME)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read().strip()
            if existing:
                return existing
        except Exception:
            pass
    new_id = str(uuid.uuid4())
    os.makedirs(data_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_id)
    os.replace(tmp, path)
    return new_id


def session_seconds_capped(raw):
    """会话秒数：负数当 0，封顶 SESSION_SECONDS_CAP。"""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 0
    if n < 0:
        n = 0
    return min(n, SESSION_SECONDS_CAP)


def local_hour_weekday(now=None):
    """返回 (local_hour 0-23, local_weekday 1=周一 … 7=周日)。"""
    now = now or datetime.datetime.now()
    hour = int(now.hour)
    if hour < 0:
        hour = 0
    if hour > 23:
        hour = 23
    # Python Monday=0 … Sunday=6 → 1…7
    weekday = int(now.weekday()) + 1
    if weekday < 1:
        weekday = 1
    if weekday > 7:
        weekday = 7
    return hour, weekday


def normalize_platform(raw):
    """粗粒度平台；不读敏感设备信息。"""
    if raw is None:
        return "unknown"
    s = str(getattr(raw, "value", raw)).lower()
    if "android" in s:
        return "android"
    if "ios" in s:
        return "ios"
    if any(x in s for x in ("windows", "macos", "linux", "desktop", "web")):
        return "desktop"
    return "unknown"


def _clamp_local_hour(v, fallback):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return fallback
    if 0 <= n <= 23:
        return n
    return fallback


def _clamp_local_weekday(v, fallback):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return fallback
    if 1 <= n <= 7:
        return n
    return fallback


def _clamp_periods_used(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    if n < 0:
        n = 0
    return min(n, PERIODS_USED_CAP)


def _clamp_marks_count(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return n if n >= 0 else 0


def build_telemetry_payload(
    install_id,
    app_version,
    app_build,
    event="app_open",
    ts=None,
    *,
    local_hour=None,
    local_weekday=None,
    platform=None,
    session_seconds=None,
    marks_count=None,
    periods_used=None,
    local_date=None,
    feature=None,
    extra=None,
):
    """只含白名单字段。非法 event / feature 返回 None。

    extra 里若有非白名单键会被丢弃；禁止名单、考勤明细、姓名、班级、路径、手机号。
    """
    event = str(event) if event is not None else ""
    if event not in TELEMETRY_EVENTS:
        return None

    if event == "feature":
        feat = str(feature) if feature is not None else ""
        if feat not in TELEMETRY_FEATURES:
            return None

    if ts is None:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    hour_fb, weekday_fb = local_hour_weekday()
    hour = _clamp_local_hour(local_hour, hour_fb) if local_hour is not None else hour_fb
    weekday = (
        _clamp_local_weekday(local_weekday, weekday_fb)
        if local_weekday is not None else weekday_fb
    )
    plat = normalize_platform(platform) if platform is not None else "unknown"
    if plat not in TELEMETRY_PLATFORMS:
        plat = "unknown"

    payload = {
        "install_id": str(install_id),
        "app_version": str(app_version),
        "app_build": int(app_build),
        "event": event,
        "ts": str(ts),
        "local_hour": hour,
        "local_weekday": weekday,
        "platform": plat,
    }

    if event == "app_session":
        payload["session_seconds"] = session_seconds_capped(
            0 if session_seconds is None else session_seconds)

    if event == "roll_call_day":
        payload["marks_count"] = _clamp_marks_count(
            0 if marks_count is None else marks_count)
        payload["periods_used"] = _clamp_periods_used(
            0 if periods_used is None else periods_used)
        if local_date:
            # 仅接受 YYYY-MM-DD 形态，避免塞进其它内容
            ld = str(local_date).strip()
            if len(ld) == 10 and ld[4] == "-" and ld[7] == "-":
                payload["local_date"] = ld

    if event == "feature":
        payload["feature"] = str(feature)

    if extra:
        for k, v in extra.items():
            if k in TELEMETRY_PAYLOAD_KEYS and k not in payload:
                # 仍禁止敏感键名（已不在白名单）
                payload[k] = v

    return {k: payload[k] for k in payload if k in TELEMETRY_PAYLOAD_KEYS}


def reset_roll_call_day_counters():
    global _pending_roll_day
    _roll_day["local_date"] = None
    _roll_day["marks_count"] = 0
    _roll_day["periods"] = set()
    _pending_roll_day = None


def get_roll_call_day_snapshot(include_pending=False):
    """只读快照，供测试与 flush。"""
    if include_pending and _pending_roll_day and (
            not _roll_day["local_date"] or _roll_day["marks_count"] <= 0):
        return dict(_pending_roll_day)
    periods = _roll_day["periods"]
    return {
        "local_date": _roll_day["local_date"],
        "marks_count": int(_roll_day["marks_count"]),
        "periods_used": min(len(periods), PERIODS_USED_CAP),
        "periods": set(periods),
    }


def _snapshot_from_state():
    periods = _roll_day["periods"]
    return {
        "local_date": _roll_day["local_date"],
        "marks_count": int(_roll_day["marks_count"]),
        "periods_used": min(len(periods), PERIODS_USED_CAP),
    }


def record_roll_call_mark(period, now=None):
    """点名改状态成功时调用：内存累加当天次数与节次（不含姓名）。

    默认关闭时也不发网，只做极轻量计数。切日时把旧桶挂到 pending 供 flush。
    """
    global _pending_roll_day
    now = now or datetime.datetime.now()
    day = now.strftime("%Y-%m-%d")
    if _roll_day["local_date"] and _roll_day["local_date"] != day:
        if _roll_day["marks_count"] > 0:
            _pending_roll_day = _snapshot_from_state()
        _roll_day["local_date"] = day
        _roll_day["marks_count"] = 0
        _roll_day["periods"] = set()
    elif _roll_day["local_date"] is None:
        _roll_day["local_date"] = day
    _roll_day["marks_count"] = int(_roll_day["marks_count"]) + 1
    try:
        p = int(period)
    except (TypeError, ValueError):
        p = -1
    if 0 <= p <= 4:
        _roll_day["periods"].add(p)
    return get_roll_call_day_snapshot()


# 与 brief 里的别名一致
note_roll_mark = record_roll_call_mark


def flush_roll_call_day_telemetry(
    *,
    data_dir=None,
    app_version=None,
    app_build=None,
    enabled=None,
    endpoint=None,
    opener=None,
    timeout=None,
    platform=None,
    send=True,
):
    """优先发出 pending（切日留下的），否则发当前桶；send 后清空对应桶。

    关闭或无数据 → False。默认关闭时几乎立即返回。
    """
    global _pending_roll_day
    snap = None
    from_pending = False
    if _pending_roll_day and _pending_roll_day.get("marks_count", 0) > 0:
        snap = dict(_pending_roll_day)
        from_pending = True
    else:
        cur = get_roll_call_day_snapshot()
        if cur["local_date"] and cur["marks_count"] > 0:
            snap = {
                "local_date": cur["local_date"],
                "marks_count": cur["marks_count"],
                "periods_used": cur["periods_used"],
            }
    if not snap:
        return False
    ok = False
    if send:
        # data_dir 等在关闭时不会用到；启用时由调用方传入
        if data_dir is None or app_version is None or app_build is None:
            # 参数不全且开关可能打开：仍清空，避免卡死；关闭路径直接 False
            enabled_flag = TELEMETRY_ENABLED if enabled is None else bool(enabled)
            if not enabled_flag:
                if from_pending:
                    _pending_roll_day = None
                else:
                    _roll_day["marks_count"] = 0
                    _roll_day["periods"] = set()
                return False
            return False
        ok = maybe_send_telemetry(
            "roll_call_day",
            data_dir=data_dir,
            app_version=app_version,
            app_build=app_build,
            enabled=enabled,
            endpoint=endpoint,
            opener=opener,
            timeout=timeout,
            platform=platform,
            marks_count=snap["marks_count"],
            periods_used=snap["periods_used"],
            local_date=snap["local_date"],
        )
        if from_pending:
            _pending_roll_day = None
        else:
            _roll_day["marks_count"] = 0
            _roll_day["periods"] = set()
            # 保留 local_date，方便同日继续累加
    return ok


def maybe_send_telemetry(
    event="app_open",
    *,
    data_dir,
    app_version,
    app_build,
    enabled=None,
    endpoint=None,
    opener=None,
    timeout=None,
    local_hour=None,
    local_weekday=None,
    platform=None,
    session_seconds=None,
    marks_count=None,
    periods_used=None,
    local_date=None,
    feature=None,
):
    """未启用或 endpoint 空 → 立即 no-op 返回 False；启用时 POST JSON，失败静默返回 False。

    非法 event / feature → False 且不联网、不写 install_id。
    """
    enabled = TELEMETRY_ENABLED if enabled is None else bool(enabled)
    endpoint = TELEMETRY_ENDPOINT if endpoint is None else endpoint
    endpoint = (endpoint or "").strip()
    if not enabled or not endpoint:
        return False

    # 先校验，避免非法事件写出 install_id
    probe = build_telemetry_payload(
        "probe", app_version, app_build, event=event,
        local_hour=local_hour, local_weekday=local_weekday, platform=platform,
        session_seconds=session_seconds, marks_count=marks_count,
        periods_used=periods_used, local_date=local_date, feature=feature,
    )
    if probe is None:
        return False

    try:
        install_id = get_or_create_install_id(data_dir)
        payload = build_telemetry_payload(
            install_id, app_version, app_build, event=event,
            local_hour=local_hour, local_weekday=local_weekday, platform=platform,
            session_seconds=session_seconds, marks_count=marks_count,
            periods_used=periods_used, local_date=local_date, feature=feature,
        )
        if payload is None:
            return False
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": f"HMUDQ-Attendance/{app_version} (build {app_build})",
                "Accept": "application/json, text/plain, */*",
            },
            method="POST",
        )
        opener = opener or urllib.request.urlopen
        timeout = TELEMETRY_TIMEOUT if timeout is None else timeout
        with opener(req, timeout=timeout) as resp:
            try:
                resp.read(256)
            except Exception:
                pass
        return True
    except Exception:
        return False
