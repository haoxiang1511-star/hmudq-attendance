# -*- coding: utf-8 -*-
"""匿名使用统计骨架：默认关闭、payload 白名单、install_id 稳定；不联网。"""
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import main as app
import telemetry as tel


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class TelemetryDefaults(unittest.TestCase):
    def test_hard_switch_off_and_empty_endpoint(self):
        self.assertIs(app.TELEMETRY_ENABLED, False)
        self.assertIs(tel.TELEMETRY_ENABLED, False)
        self.assertEqual(app.TELEMETRY_ENDPOINT, "")
        self.assertEqual(tel.TELEMETRY_ENDPOINT, "")

    def test_disabled_noop_no_network(self):
        calls = []

        def boom(req, timeout=None):
            calls.append((getattr(req, "full_url", None), timeout))
            raise AssertionError("should not hit network when disabled")

        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(app.maybe_send_telemetry(
                "app_open", data_dir=d, opener=boom))
            self.assertFalse(tel.maybe_send_telemetry(
                "app_open", data_dir=d, app_version="1.5.0", app_build=107,
                opener=boom))
            # 启用但 endpoint 空 → 仍 no-op
            self.assertFalse(tel.maybe_send_telemetry(
                "app_open", data_dir=d, app_version="1.5.0", app_build=107,
                enabled=True, endpoint="", opener=boom))
            self.assertFalse(tel.maybe_send_telemetry(
                "app_open", data_dir=d, app_version="1.5.0", app_build=107,
                enabled=True, endpoint="   ", opener=boom))
        self.assertEqual(calls, [])
        # 关闭时不应写 install_id 文件
        with tempfile.TemporaryDirectory() as d:
            app.maybe_send_telemetry("app_open", data_dir=d, opener=boom)
            self.assertFalse(os.path.exists(os.path.join(d, tel.INSTALL_ID_FILENAME)))


class TelemetryPayload(unittest.TestCase):
    FORBIDDEN = {
        "name", "names", "student", "students", "class", "class_name",
        "class_id", "path", "db_path", "phone", "roster", "attendance",
        "record", "records", "android_id", "device_id",
    }

    def test_payload_whitelist_only(self):
        p = app.build_telemetry_payload(
            "id-abc", app.APP_VERSION, app.APP_BUILD, event="app_open",
            ts="2026-10-28T12:00:00Z", local_hour=9, local_weekday=3,
            platform="android")
        self.assertTrue(set(p.keys()).issubset(set(app.TELEMETRY_PAYLOAD_KEYS)))
        for req in ("install_id", "app_version", "app_build", "event", "ts",
                    "local_hour", "local_weekday", "platform"):
            self.assertIn(req, p)
        self.assertEqual(p["install_id"], "id-abc")
        self.assertEqual(p["app_version"], app.APP_VERSION)
        self.assertEqual(p["app_build"], app.APP_BUILD)
        self.assertEqual(p["event"], "app_open")
        self.assertEqual(p["ts"], "2026-10-28T12:00:00Z")
        self.assertEqual(p["local_hour"], 9)
        self.assertEqual(p["local_weekday"], 3)
        self.assertEqual(p["platform"], "android")
        for bad in self.FORBIDDEN:
            self.assertNotIn(bad, p)
        raw = json.dumps(p)
        for bad in ("姓名", "班级", "考勤", "名单", "/data/", "attendance.db"):
            self.assertNotIn(bad, raw)

    def test_extra_forbidden_keys_stripped(self):
        p = tel.build_telemetry_payload(
            "x", "1.5.0", 107, event="app_open",
            extra={"name": "张三", "class_name": "信管", "path": "/tmp/a.db",
                   "local_hour": 99})  # local_hour already set by builder; extra ignored if present
        self.assertNotIn("name", p)
        self.assertNotIn("class_name", p)
        self.assertNotIn("path", p)

    def test_session_seconds_capped(self):
        self.assertEqual(tel.session_seconds_capped(-5), 0)
        self.assertEqual(tel.session_seconds_capped(100), 100)
        self.assertEqual(tel.session_seconds_capped(99999), tel.SESSION_SECONDS_CAP)
        p = tel.build_telemetry_payload(
            "x", "1.5.0", 107, event="app_session", session_seconds=99999,
            local_hour=1, local_weekday=1, platform="desktop")
        self.assertEqual(p["session_seconds"], tel.SESSION_SECONDS_CAP)

    def test_illegal_event_returns_none(self):
        self.assertIsNone(tel.build_telemetry_payload(
            "x", "1.5.0", 107, event="hack_me"))
        self.assertIsNone(tel.build_telemetry_payload(
            "x", "1.5.0", 107, event="feature", feature="not_a_feature"))

    def test_feature_payload_ok(self):
        p = tel.build_telemetry_payload(
            "x", "1.5.0", 107, event="feature", feature="export",
            local_hour=12, local_weekday=5, platform="android")
        self.assertEqual(p["feature"], "export")
        self.assertEqual(p["event"], "feature")

    def test_roll_call_day_ranges(self):
        p = tel.build_telemetry_payload(
            "x", "1.5.0", 107, event="roll_call_day",
            marks_count=-3, periods_used=99, local_date="2026-10-28",
            local_hour=8, local_weekday=2, platform="android")
        self.assertEqual(p["marks_count"], 0)
        self.assertEqual(p["periods_used"], tel.PERIODS_USED_CAP)
        self.assertEqual(p["local_date"], "2026-10-28")
        # 非法日期形态不带
        p2 = tel.build_telemetry_payload(
            "x", "1.5.0", 107, event="roll_call_day",
            marks_count=1, periods_used=1, local_date="bad",
            local_hour=8, local_weekday=2)
        self.assertNotIn("local_date", p2)

    def test_local_hour_weekday_helper(self):
        now = datetime.datetime(2026, 10, 28, 15, 30)  # Wednesday
        h, w = tel.local_hour_weekday(now)
        self.assertEqual(h, 15)
        self.assertEqual(w, 3)  # Mon=1 … Wed=3

    def test_local_hour_clamped_in_payload(self):
        p = tel.build_telemetry_payload(
            "x", "1.5.0", 107, event="app_open",
            local_hour=99, local_weekday=0, platform="ios")
        self.assertTrue(0 <= p["local_hour"] <= 23)
        self.assertTrue(1 <= p["local_weekday"] <= 7)
        self.assertEqual(p["platform"], "ios")


class TelemetryInstallId(unittest.TestCase):
    def test_install_id_stable_same_dir(self):
        with tempfile.TemporaryDirectory() as d:
            a = app.get_or_create_install_id(d)
            b = app.get_or_create_install_id(d)
            self.assertEqual(a, b)
            self.assertTrue(len(a) >= 8)
            path = os.path.join(d, tel.INSTALL_ID_FILENAME)
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), a)

    def test_install_id_differs_across_dirs(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            self.assertNotEqual(
                app.get_or_create_install_id(d1),
                app.get_or_create_install_id(d2))


class TelemetrySendWhenEnabled(unittest.TestCase):
    def test_posts_json_when_enabled(self):
        calls = []

        def opener(req, timeout=None):
            body = req.data
            calls.append({
                "url": req.full_url,
                "timeout": timeout,
                "method": getattr(req, "method", None) or req.get_method(),
                "body": body,
                "ctype": req.headers.get("Content-type") or req.headers.get("Content-Type"),
            })
            return FakeResp(b"ok")

        with tempfile.TemporaryDirectory() as d:
            ok = tel.maybe_send_telemetry(
                "app_open",
                data_dir=d,
                app_version=app.APP_VERSION,
                app_build=app.APP_BUILD,
                enabled=True,
                endpoint="https://example.com/t",
                opener=opener,
                timeout=2,
                local_hour=10,
                local_weekday=1,
                platform="android",
            )
            self.assertTrue(ok)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["url"], "https://example.com/t")
            self.assertEqual(calls[0]["timeout"], 2)
            self.assertEqual(calls[0]["method"], "POST")
            payload = json.loads(calls[0]["body"].decode("utf-8"))
            self.assertTrue(set(payload.keys()).issubset(set(tel.TELEMETRY_PAYLOAD_KEYS)))
            self.assertEqual(payload["app_version"], app.APP_VERSION)
            self.assertEqual(payload["app_build"], app.APP_BUILD)
            self.assertEqual(payload["event"], "app_open")
            self.assertEqual(payload["local_hour"], 10)
            self.assertEqual(payload["platform"], "android")
            iid = app.get_or_create_install_id(d)
            self.assertEqual(payload["install_id"], iid)

    def test_illegal_event_not_sent(self):
        calls = []

        def opener(req, timeout=None):
            calls.append(1)
            return FakeResp(b"ok")

        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(tel.maybe_send_telemetry(
                "nope", data_dir=d, app_version="1.5.0", app_build=107,
                enabled=True, endpoint="https://example.com/t", opener=opener))
            self.assertFalse(tel.maybe_send_telemetry(
                "feature", feature="evil", data_dir=d, app_version="1.5.0",
                app_build=107, enabled=True, endpoint="https://example.com/t",
                opener=opener))
            self.assertEqual(calls, [])
            self.assertFalse(os.path.exists(os.path.join(d, tel.INSTALL_ID_FILENAME)))

    def test_failure_swallowed(self):
        def boom(req, timeout=None):
            raise urllib.error.URLError("down")

        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(tel.maybe_send_telemetry(
                "app_open", data_dir=d, app_version="1.5.0", app_build=107,
                enabled=True, endpoint="https://example.com/t", opener=boom))


class TelemetryRollCallCounters(unittest.TestCase):
    def setUp(self):
        tel.reset_roll_call_day_counters()

    def tearDown(self):
        tel.reset_roll_call_day_counters()

    def test_note_roll_mark_accumulates(self):
        now = datetime.datetime(2026, 10, 28, 9, 0)
        tel.note_roll_mark(0, now=now)
        tel.note_roll_mark(0, now=now)
        tel.note_roll_mark(2, now=now)
        snap = tel.get_roll_call_day_snapshot()
        self.assertEqual(snap["marks_count"], 3)
        self.assertEqual(snap["periods_used"], 2)
        self.assertEqual(snap["local_date"], "2026-10-28")

    def test_flush_disabled_clears_without_network(self):
        calls = []

        def boom(req, timeout=None):
            calls.append(1)
            raise AssertionError("no net")

        now = datetime.datetime(2026, 10, 28, 9, 0)
        tel.note_roll_mark(1, now=now)
        with tempfile.TemporaryDirectory() as d:
            ok = tel.flush_roll_call_day_telemetry(
                data_dir=d, app_version="1.5.0", app_build=107,
                enabled=False, endpoint="https://example.com/t", opener=boom)
            self.assertFalse(ok)
        self.assertEqual(calls, [])
        snap = tel.get_roll_call_day_snapshot()
        self.assertEqual(snap["marks_count"], 0)

    def test_flush_sends_when_enabled(self):
        calls = []

        def opener(req, timeout=None):
            calls.append(json.loads(req.data.decode("utf-8")))
            return FakeResp(b"ok")

        now = datetime.datetime(2026, 10, 28, 9, 0)
        tel.note_roll_mark(0, now=now)
        tel.note_roll_mark(3, now=now)
        with tempfile.TemporaryDirectory() as d:
            ok = tel.flush_roll_call_day_telemetry(
                data_dir=d, app_version=app.APP_VERSION, app_build=app.APP_BUILD,
                enabled=True, endpoint="https://example.com/t", opener=opener,
                platform="android")
            self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["event"], "roll_call_day")
        self.assertEqual(calls[0]["marks_count"], 2)
        self.assertEqual(calls[0]["periods_used"], 2)
        self.assertEqual(calls[0]["local_date"], "2026-10-28")
        self.assertTrue(set(calls[0].keys()).issubset(set(tel.TELEMETRY_PAYLOAD_KEYS)))


class TelemetryWhitelistConstants(unittest.TestCase):
    def test_events_and_features(self):
        self.assertIn("app_open", tel.TELEMETRY_EVENTS)
        self.assertIn("app_session", tel.TELEMETRY_EVENTS)
        self.assertIn("roll_call_day", tel.TELEMETRY_EVENTS)
        self.assertIn("feature", tel.TELEMETRY_EVENTS)
        for f in ("export", "backup", "restore", "check_update",
                  "share_app", "feedback", "drawer_open"):
            self.assertIn(f, tel.TELEMETRY_FEATURES)
        self.assertEqual(app.TELEMETRY_EVENTS, tel.TELEMETRY_EVENTS)
        self.assertEqual(app.TELEMETRY_FEATURES, tel.TELEMETRY_FEATURES)


if __name__ == "__main__":
    unittest.main()
