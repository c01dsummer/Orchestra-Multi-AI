#!/usr/bin/env python3
"""纯离线自测：临时假账本、可注入假发送器；禁止创建 socket。"""

import contextlib
import datetime as dt
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from unittest import mock


SCRIPT = Path(__file__).with_name("run-done-watcher")
LOADER = importlib.machinery.SourceFileLoader("run_done_watcher", str(SCRIPT.with_name("watcher.py")))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
MODULE = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(MODULE)
URL_ENV = "RDW_SELFTEST_URL"
OTHER_ENV = "RDW_SELFTEST_OTHER_URL"


def event(run_id, agent="示例助手甲", exit_code=0):
    return {"run_id": run_id, "agent": agent,
            "end_time": "2026-10-24T03:05:00+08:00", "exit_code": exit_code,
            "prompt_head": "此字段不得进入请求或状态"}


def line(record):
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


class FakeSender:
    def __init__(self):
        self.received = []
        self.responses = []

    def __call__(self, request, timeout):
        self.received.append({"body": json.loads(request.data),
                              "path": urllib.parse.urlsplit(request.full_url).path,
                              "key": request.get_header("Idempotency-key"),
                              "headers": dict(request.header_items()), "method": request.method})
        response = self.responses.pop(0) if self.responses else 200
        if isinstance(response, Exception):
            raise response
        return response


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.socket_guard = mock.patch.object(socket, "socket", side_effect=AssertionError("离线测试禁止 socket"))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)
        self.server = FakeSender()  # 仅保留旧测试属性名；它是普通可调用对象，不是服务。
        self.url = "https://selftest.invalid/test-only-url"
        self.temp = tempfile.TemporaryDirectory(prefix="rdw-selftest-", dir=SCRIPT.parent)
        self.root = Path(self.temp.name)
        self.ledger = self.root / "ledger.jsonl"
        self.ledger.write_bytes(b"")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"示例助手甲": URL_ENV, "示例助手甲旧名": URL_ENV,
                                           "示例助手乙": OTHER_ENV, "示例助手丙": OTHER_ENV},
                                          ensure_ascii=False), encoding="utf-8")
        self.state = self.root / "state"
        self.env = {URL_ENV: self.url, OTHER_ENV: self.url.replace("test-only-url", "other-route")}
        self.server.received = []
        self.server.responses = []

    def tearDown(self):
        self.temp.cleanup()

    def append(self, *records):
        with self.ledger.open("ab") as stream:
            for record in records:
                stream.write(line(record))

    def args(self, *extra):
        return ["--ledger", str(self.ledger), "--state-dir", str(self.state),
                "--config", str(self.config), "--ignore-agent", "示例范围外助手", "--retry-base", "0.03", "--retry-max", "0.06",
                "--http-timeout", "0.2", *[x for x in extra if x != "--no-history-choice"]]

    def run_once(self, *extra, expected=0, environment=None):
        # 每次完整重建/关闭 watcher，验证磁盘持久状态；真实子进程见 file 模式测试。
        child_env = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1",
                     **(self.env if environment is None else environment)}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", [str(SCRIPT), *self.args("--once", *extra)]), \
                mock.patch.dict(os.environ, child_env, clear=True), \
                mock.patch.object(MODULE.signal, "signal"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            returncode = MODULE.main(sender=self.server)
        result = subprocess.CompletedProcess([], returncode, stdout.getvalue(), stderr.getvalue())
        self.assertEqual(result.returncode, expected, result.stderr)
        for value in self.env.values():
            self.assertNotIn(value, result.stdout + result.stderr)
        return result

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.state / "state.sqlite3")) as db, db:
            return db.execute("SELECT run_id,status,attempts FROM events ORDER BY run_id").fetchall()

    def heartbeat(self):
        return json.loads((self.state / "heartbeat.json").read_text())

    @contextlib.contextmanager
    def watcher(self, *extra):
        with mock.patch.object(sys, "argv", [str(SCRIPT), *self.args(*extra)]), \
                mock.patch.dict(os.environ, self.env, clear=True):
            watcher = MODULE.Watcher(MODULE.parse_args(), sender=self.server)
            try:
                yield watcher
            finally:
                watcher.close()

    def test_01_new_line_and_minimal_payload(self):
        self.run_once("--allow-history")
        self.append(event("new-1"))
        self.run_once()
        self.assertEqual(len(self.server.received), 1)
        request = self.server.received[0]
        self.assertEqual(request["body"]["run_id"], "new-1")
        self.assertEqual(request["key"], "run_done:new-1")
        self.assertEqual(set(request["body"]),
                         {"event", "event_id", "run_id", "agent", "end_time", "exit_code"})
        self.assertEqual(self.rows(), [("new-1", "delivered", 1)])
        heartbeat = self.heartbeat()
        self.assertEqual(heartbeat["timezone"], "UTC+08:00")
        self.assertEqual(dt.datetime.fromisoformat(heartbeat["updated_at"]).utcoffset(),
                         dt.timedelta(hours=8))

    def test_02_duplicate_across_restart(self):
        self.append(event("dup"), event("dup"))
        self.run_once("--allow-history")
        self.append(event("dup"))
        self.run_once()
        self.assertEqual(len(self.server.received), 1)

    def test_03_cursor_restart_and_new_event(self):
        self.append(event("first"))
        self.run_once("--allow-history")
        first_offset = self.heartbeat()["cursor"]["offset"]
        self.assertEqual(first_offset, self.ledger.stat().st_size)
        self.append(event("second"))
        self.run_once()
        self.run_once()
        self.assertEqual([r["body"]["run_id"] for r in self.server.received], ["first", "second"])
        self.assertGreater(self.heartbeat()["cursor"]["offset"], first_offset)

    def test_04_partial_and_bad_lines(self):
        partial = line(event("partial"))
        self.ledger.write_bytes(b"{bad json}\n\xff\n[]\n" + line({"run_id": "missing-fields"}) + partial[:25])
        self.run_once("--allow-history", expected=2)
        self.assertEqual(len(self.server.received), 0)
        offset = self.heartbeat()["cursor"]["offset"]
        self.assertEqual(offset, self.ledger.stat().st_size - 25)
        with self.ledger.open("ab") as stream:
            stream.write(partial[25:])
        self.run_once(expected=2)
        self.assertEqual(self.rows(), [("partial", "delivered", 1)])
        self.assertEqual(self.heartbeat()["invalid_lines"], 4)

    def test_05_failed_post_persists_and_recovers(self):
        self.server.responses = [503, 200]
        self.append(event("retry"))
        self.run_once("--allow-history", expected=2)
        self.assertEqual(self.rows(), [("retry", "pending", 1)])
        self.assertEqual(self.heartbeat()["failed_pending"], 1)
        time.sleep(0.07)
        self.run_once()
        self.assertEqual(self.rows(), [("retry", "delivered", 2)])
        self.assertEqual(len(self.server.received), 2)
        self.assertEqual(self.server.received[0]["key"], self.server.received[1]["key"])
        self.run_once()
        self.assertEqual(len(self.server.received), 2)

    def test_06_dry_run_no_env_and_no_requests(self):
        self.append(event("dry"))
        self.run_once("--dry-run", environment={})
        self.run_once("--dry-run", environment={})
        self.assertEqual(self.rows(), [("dry", "dry-run", 0)])
        self.assertEqual(self.server.received, [])
        self.run_once(expected=1)  # dry-run 状态禁止直接转正式发送。
        self.assertEqual(self.server.received, [])

    def test_07_routes_alias_and_out_of_scope(self):
        self.append(event("dev"), event("alias", "示例助手甲旧名"),
                    event("paper", "示例助手乙", 1), event("third", "示例助手丙", -9),
                    event("other", "示例范围外助手"))
        self.run_once("--allow-history")
        routes = {r["body"]["run_id"]: r["path"] for r in self.server.received}
        self.assertEqual(routes, {"dev": "/test-only-url", "alias": "/test-only-url",
                                  "paper": "/other-route", "third": "/other-route"})
        self.assertIn(("other", "ignored", 0), self.rows())

    def test_08_atomic_ingest_survives_before_delivery(self):
        self.append(event("queued-before-crash"))
        with self.watcher("--allow-history") as watcher:
            watcher.ingest()  # 提交游标/队列后关闭，模拟投递前进程终止。
        self.assertEqual(self.rows(), [("queued-before-crash", "pending", 0)])
        self.run_once()
        self.assertEqual(len(self.server.received), 1)
        self.assertEqual(self.rows(), [("queued-before-crash", "delivered", 1)])

    def test_09_missing_env_never_drops_event(self):
        self.append(event("no-url"))
        self.run_once("--allow-history", expected=2, environment={})
        self.assertEqual(self.rows(), [("no-url", "pending", 1)])
        self.assertEqual(self.server.received, [])
        time.sleep(0.07)
        self.run_once()
        self.assertEqual(self.rows(), [("no-url", "delivered", 2)])

    def test_10_redirect_is_not_followed_and_url_not_saved(self):
        self.append(event("redirect"))
        self.server.responses = [302]
        self.run_once("--allow-history", expected=2)
        self.assertEqual(len(self.server.received), 1)
        self.assertEqual(self.rows(), [("redirect", "pending", 1)])
        handler = MODULE.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, self.url))
        for path in self.state.iterdir():
            if path.is_file():
                data = path.read_bytes()
                self.assertNotIn(self.url.encode(), data)
                self.assertNotIn(b"test-only-url", data)

    def test_11_single_process_lock(self):
        with self.watcher("--allow-history"):
            self.run_once(expected=1)

    def test_12_start_at_end_keeps_partial(self):
        partial = line(event("finish-later"))
        self.ledger.write_bytes(line(event("old")) + partial[:20])
        self.run_once("--start-at-end")
        self.assertEqual(self.rows(), [])
        with self.ledger.open("ab") as stream:
            stream.write(partial[20:])
        self.run_once()
        self.assertEqual(self.rows(), [("finish-later", "delivered", 1)])
        self.run_once("--start-at-end", expected=1)

    def test_13_replacement_rescan_dedup_and_warn(self):
        self.append(event("kept"))
        self.run_once("--allow-history")
        replacement = self.root / "replacement"
        replacement.write_bytes(line(event("kept")) + line(event("rotated-new")))
        os.replace(replacement, self.ledger)
        self.run_once(expected=2)
        self.assertEqual(len(self.server.received), 2)
        self.assertEqual(self.heartbeat()["ledger_resets"], 1)

    def test_14_backoff_increases_and_is_capped(self):
        self.append(event("backoff"))
        self.server.responses = [500, 500, 500]
        with self.watcher("--allow-history") as watcher:
            watcher.ingest()
            for expected_delay in (0.03, 0.06, 0.06):
                before = time.time()
                watcher.deliver()
                next_attempt = watcher.db.execute("SELECT next_attempt FROM events").fetchone()[0]
                self.assertGreaterEqual(next_attempt - before, expected_delay - 0.005)
                self.assertLess(next_attempt - time.time(), expected_delay + 0.05)
                # 不用等待退避，调整测试假时钟对应的队列到期时间。
                with watcher.db:
                    watcher.db.execute("UPDATE events SET next_attempt=0")
        self.assertEqual(self.rows(), [("backoff", "pending", 3)])

    def test_15_once_drains_more_than_one_batch(self):
        self.append(*(event("batch-%02d" % i) for i in range(12)))
        self.run_once("--allow-history", "--batch-size", "3")
        self.assertEqual(len(self.server.received), 12)
        self.assertTrue(all(status == "delivered" for _, status, _ in self.rows()))

    def test_16_oversized_line_and_tail_recovery(self):
        self.ledger.write_bytes(b"x" * (MODULE.MAX_LINE + 20))
        self.run_once("--allow-history")
        self.assertEqual(self.heartbeat()["cursor"]["offset"], 0)
        with self.ledger.open("ab") as stream:
            stream.write(b"\n" + line(event("after-large")))
        self.run_once(expected=2)
        self.assertEqual(self.rows(), [("after-large", "delivered", 1)])
        self.assertEqual(self.heartbeat()["invalid_lines"], 1)

    def test_17_connection_failure_is_sanitized_and_persisted(self):
        self.append(event("unreachable"))
        endpoint = "https://selftest.invalid/private-test-only"
        self.server.responses = [urllib.error.URLError(endpoint)]
        result = self.run_once("--allow-history", expected=2, environment={URL_ENV: endpoint})
        self.assertNotIn(endpoint, result.stdout + result.stderr)
        self.assertNotIn(endpoint, (self.state / "events.jsonl").read_text())
        self.assertEqual(self.rows(), [("unreachable", "pending", 1)])

    def configure_headers(self, body=False):
        route = {"url_env": URL_ENV, "headers_env": "RDW_TEST_HEADERS"}
        if body:
            route["body_template_env"] = "RDW_TEST_BODY"
        self.config.write_text(json.dumps({"示例助手甲": route}), encoding="utf-8")
        self.env["RDW_TEST_HEADERS"] = json.dumps({"Authorization": "Bearer fake-secret-only",
                                                     "X-Test-Key": "fake-second-secret"})

    def test_18_configured_headers_and_body_never_saved(self):
        self.configure_headers(body=True)
        self.env["RDW_TEST_BODY"] = json.dumps({"data": "$event", "id": "$run_id",
                                                  "label": "本机测试", "list": ["$exit_code"]})
        self.append(event("configured", exit_code=1))
        self.run_once("--allow-history")
        request = self.server.received[0]
        self.assertEqual(request["headers"]["Authorization"], "Bearer fake-secret-only")
        self.assertEqual(request["body"]["data"]["run_id"], "configured")
        self.assertEqual(request["body"]["id"], "configured")
        self.assertEqual(request["body"]["list"], [1])
        self.assertEqual(request["method"], "POST")
        for path in self.state.iterdir():
            if path.is_file():
                for value in (b"fake-secret-only", b"fake-second-secret", self.url.encode(),
                              "本机测试".encode(), b"prompt_head"):
                    self.assertNotIn(value, path.read_bytes())

    def test_19_missing_or_invalid_headers_stay_pending(self):
        self.configure_headers()
        self.assertEqual(json.loads(r'{"Authorization":"bad\u000d\u000aheader"}')["Authorization"],
                         "bad" + chr(13) + chr(10) + "header")
        self.run_once("--start-at-end")
        self.append(event("bad-headers"))
        for raw in (None, "not-json", "[]", r'{"Authorization":"bad\u000d\u000aheader"}',
                    '{"Content-Length":"0"}', '{"Authorization":23}'):
            environment = dict(self.env)
            if raw is None:
                del environment["RDW_TEST_HEADERS"]
            else:
                environment["RDW_TEST_HEADERS"] = raw
            self.run_once(expected=2, environment=environment)
            with contextlib.closing(sqlite3.connect(self.state / "state.sqlite3")) as db, db:
                db.execute("UPDATE events SET next_attempt=0")
        self.assertEqual(self.server.received, [])
        self.run_once()
        self.assertEqual(self.rows(), [("bad-headers", "delivered", 7)])

    def test_20_file_transport_real_subprocess_restart_no_env(self):
        self.append(event("file-1"))
        outbox = self.root / "outbox"
        args = [sys.executable, str(SCRIPT), *self.args("--once", "--transport", "file", "--outbox", str(outbox))]
        for _ in range(2):
            result = subprocess.run(args, env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p.name for p in outbox.iterdir()], ["file-1.json"])
        self.assertEqual(json.loads((outbox / "file-1.json").read_text())["run_id"], "file-1")
        self.assertEqual(self.rows(), [("file-1", "delivered", 1)])
        self.run_once("--allow-history", expected=1)  # file 状态不能误作正式 HTTP 状态。
        self.assertEqual(self.heartbeat()["mode"], "file")
        self.assertEqual(self.server.received, [])

    def test_21_each_tick_refreshes_heartbeat_and_health(self):
        self.append(event("heartbeat"))
        with self.watcher("--dry-run") as watcher:
            with mock.patch.object(MODULE, "atomic_json", wraps=MODULE.atomic_json) as writer:
                watcher.tick()
                first_calls = writer.call_count
                watcher.tick()
                self.assertGreater(writer.call_count, first_calls)
                self.assertGreaterEqual(first_calls, 2)
            self.assertTrue(self.heartbeat()["ledger_readable"])
            self.assertEqual(self.heartbeat()["health"], "正常")
            self.ledger.unlink()
            watcher.tick()
            self.assertFalse(self.heartbeat()["ledger_readable"])
            self.assertEqual(self.heartbeat()["health"], "需检查")

    def check_heartbeat(self, expected):
        result = subprocess.run([sys.executable, str(SCRIPT.with_name("check-heartbeat.py")),
                                 "--heartbeat", str(self.state / "heartbeat.json")],
                                env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr)
        return json.loads(result.stdout)

    def test_22_heartbeat_independent_checker(self):
        self.check_heartbeat(1)  # 缺失
        self.run_once("--dry-run")
        self.assertTrue(self.check_heartbeat(0)["ok"])
        path = self.state / "heartbeat.json"
        heartbeat = self.heartbeat()
        for change in ({"updated_at": (dt.datetime.now(MODULE.TZ) - dt.timedelta(minutes=16)).isoformat()},
                       {"updated_at": (dt.datetime.now(MODULE.TZ) + dt.timedelta(minutes=2)).isoformat()},
                       {"stopping": True}, {"health": "需检查"}):
            path.write_text(json.dumps({**heartbeat, **change}), encoding="utf-8")
            self.assertFalse(self.check_heartbeat(1)["ok"])
        path.write_text(json.dumps({**heartbeat, "timezone": "legacy"}), encoding="utf-8")
        mismatch = self.check_heartbeat(1)
        self.assertFalse(mismatch["ok"])
        self.assertEqual(mismatch["reason"], "invalid_heartbeat_payload")
        self.assertEqual(mismatch["detail"], "版本不一致，请同时升级并重启 watcher")
        path.write_text("invalid", encoding="utf-8")
        self.check_heartbeat(1)

    def reconcile(self, expected, *extra):
        loader = importlib.machinery.SourceFileLoader("reconcile_test", str(SCRIPT.with_name("reconcile.py")))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        class Clock(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 22, 12, tzinfo=MODULE.TZ).astimezone(tz)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(module.dt, "datetime", Clock), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(["--ledger", str(self.ledger), "--config", str(self.config),
                "--policy", str(SCRIPT.with_name("reconcile.config.example.json")),
                "--log", str(self.state / "events.jsonl"),
                *([] if "--old-reports" in extra or "--production" in extra else ["--diagnostic"]),
                *[x for x in extra if x != "--production"]])
        result = subprocess.CompletedProcess([], code, stdout.getvalue(), stderr.getvalue())
        self.assertEqual(result.returncode, expected, result.stderr)
        if expected == 2:
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr.strip(), "对账失败：文件、配置或时间范围无效；未输出原文。")
            return None
        return json.loads(result.stdout)

    def test_23_reconcile_twelve_events_all_routes(self):
        agents = ["示例助手甲", "示例助手甲旧名", "示例助手乙", "示例助手丙"]
        self.append(*(event("parallel-%02d" % i, agents[i % 4], i % 3 - 1) for i in range(12)))
        self.run_once("--dry-run")
        report = self.reconcile(0)
        self.assertTrue(report["passed"])
        self.assertEqual(report["ledger_unique"], 12)
        self.assertEqual(report["agents"]["示例助手甲"]["matched"], 6)
        self.assertEqual(report["agents"]["示例助手乙"]["matched"], 3)
        self.assertEqual(report["agents"]["示例助手丙"]["matched"], 3)

    def test_24_reconcile_missing_duplicate_unexpected(self):
        self.append(*(event("reconcile-%02d" % i) for i in range(10)))
        self.run_once("--dry-run")
        path = self.state / "events.jsonl"
        logs = [json.loads(s) for s in path.read_text().splitlines()]
        logs = logs[1:] + [logs[1], {**logs[2], "run_id": "unexpected"}]
        path.write_bytes(b"".join(line(r) for r in logs))
        self.append(event("reconcile-03"))
        report = self.reconcile(1)
        agent = report["agents"]["示例助手甲"]
        self.assertEqual(agent["missing_in_watcher"], ["reconcile-00"])
        self.assertEqual(agent["unexpected_in_watcher"], ["unexpected"])
        self.assertEqual(agent["duplicate_watcher"], {"reconcile-01": 2})
        self.assertEqual(agent["duplicate_ledger"], {"reconcile-03": 2})

    def test_25_reconcile_window_invalid_and_conflict(self):
        self.append(event("same", "示例助手甲"), event("same", "示例助手丙"))
        self.run_once("--dry-run", expected=2)
        report = self.reconcile(1, "--min-events", "0")
        self.assertEqual(report["conflicting_run_ids"], {"same": sorted(["示例助手丙", "示例助手甲"])})
        report = self.reconcile(1, "--since", "2026-10-25T00:00:00+08:00", "--min-events", "0")
        self.assertEqual(report["ledger_unique"], 0)
        with self.ledger.open("ab") as stream:
            stream.write(b"bad-json\n")
        self.assertEqual(self.reconcile(1)["invalid_lines"]["ledger"], 1)

    def test_26_http_error_and_bad_template_are_sanitized(self):
        self.configure_headers(body=True)
        self.append(event("contract"))
        self.run_once("--allow-history", expected=2)  # 模板未注入，不发送
        self.assertEqual(self.server.received, [])
        self.env["RDW_TEST_BODY"] = "not-json"
        for body, response in (("not-json", 200), (json.dumps("$event"),
                               urllib.error.HTTPError(self.url, 401, "fake-secret-only", {}, None))):
            self.env["RDW_TEST_BODY"] = body
            self.server.responses = [response]
            with contextlib.closing(sqlite3.connect(self.state / "state.sqlite3")) as db, db:
                db.execute("UPDATE events SET next_attempt=0")
            self.run_once(expected=2)
        self.assertEqual(self.rows(), [("contract", "pending", 3)])

    def test_27_real_http_adapter_uses_timeout_without_socket(self):
        with self.watcher("--allow-history") as watcher:
            response = mock.MagicMock()
            response.__enter__.return_value.status = 204
            with mock.patch.object(watcher.http, "open", return_value=response) as opened:
                self.assertEqual(watcher.send_http("fake-request", 3), 204)
                opened.assert_called_once_with("fake-request", timeout=3)

    def test_28_reconcile_ignores_other_agents_but_requires_ten(self):
        self.append(event("in-scope"), event("out-of-scope", "示例范围外助手"))
        self.run_once("--dry-run")
        self.assertFalse(self.reconcile(1)["passed"])
        self.assertTrue(self.reconcile(0, "--min-events", "1")["passed"])
        self.assertEqual(self.reconcile(0, "--min-events", "1")["non_dry_run_events"], {})

    def test_29_reconcile_rejects_partial_input_and_formal_log(self):
        self.append(event("formal"))
        self.run_once("--allow-history")
        report = self.reconcile(1, "--min-events", "0")
        self.assertEqual(report["non_dry_run_events"], {"pending": 1})
        with self.ledger.open("ab") as stream:
            stream.write(b'{"half":')
        self.assertEqual(self.reconcile(1)["invalid_lines"]["ledger_partial"], 1)

    def test_30_dry_run_and_file_never_read_webhook_values(self):
        self.configure_headers(body=True)
        self.append(event("no-env-access"))
        for extra in (("--dry-run",), ("--transport", "file", "--outbox", str(self.root / "outbox"))):
            self.state = self.root / ("dry-state" if "--dry-run" in extra else "file-state")
            with self.watcher(*extra) as watcher:
                with mock.patch.object(MODULE.os.environ, "get", side_effect=AssertionError("禁止读取环境值")):
                    watcher.tick()
        self.assertEqual(self.server.received, [])

    def test_31_unknown_template_pending_and_escape(self):
        self.configure_headers(body=True)
        self.env["RDW_TEST_BODY"] = json.dumps({"nested": ["$unknown"]})
        self.append(event("unknown"))
        self.run_once("--allow-history", expected=2)
        self.assertEqual(self.rows(), [("unknown", "pending", 1)])
        self.assertFalse(self.server.received)
        self.assertEqual(MODULE.Watcher.render_template("$$literal", {}), "$literal")

    def test_32_initial_boundary_restart_and_unreadable(self):
        self.append(event("history"))
        with self.watcher("--start-at-end"):
            pass  # binding 已落库，模拟首次 ingest 前中断
        self.run_once("--no-history-choice")
        self.assertFalse(self.server.received)
        self.append(event("fresh"))
        self.run_once()
        self.assertEqual(self.rows(), [("fresh", "delivered", 1)])
        self.state = self.root / "missing-ledger-state"
        self.ledger.unlink()
        self.run_once("--start-at-end", expected=2)
        self.append(event("history-after-unreadable"))
        self.run_once("--no-history-choice")
        self.assertEqual(self.rows(), [])

    def test_33_legacy_binding_without_cursor_fails_closed(self):
        with self.watcher("--allow-history"):
            pass
        self.append(event("history"))
        self.run_once("--no-history-choice", expected=1)
        self.assertFalse(self.server.received)
        self.run_once("--start-at-end")
        self.assertFalse(self.server.received)

    def production_fixture(self):
        agents = ["示例助手甲", "示例助手甲旧名", "示例助手乙", "示例助手丙"]
        rows = [{**event("accept-%d" % i, agents[i % 4], i % 2),
                 "end_time": "2026-10-%02dT12:00:00+08:00" % (20 + i % 2)} for i in range(12)]
        self.append(*rows)
        self.run_once("--dry-run")
        evidence = self.root / "reports.jsonl"
        evidence.write_bytes(b"".join(line({**r, "reported": True, "origin": "synthetic-origin",
            "routine": "synthetic-old-routine", "reported_at": "2026-10-22T00:00:00+08:00"}) for r in rows))
        scenarios = self.root / "scenarios.json"
        scenarios.write_text(json.dumps({name: {"passed": True, "run_ids": ["accept-0", "accept-1"],
            "reviewer": "合成验收人", "evidence": "合成场景记录，不能生产放行", "reviewed_at": "2026-10-22T00:00:00+08:00"}
            for name in ("success_failure", "duplicate", "restart", "retry")}))
        return ["--old-reports", str(evidence), "--scenario-evidence", str(scenarios), "--since", "2026-10-20T00:00:00+08:00",
                "--until", "2026-10-22T00:00:00+08:00"]

    def test_34_production_acceptance_gates(self):
        args = self.production_fixture()
        self.assertTrue(self.reconcile(0, *args)["acceptance"])
        self.assertFalse(self.reconcile(1, *args, "--min-events", "1")["acceptance"])
        self.reconcile(1, *args, "--until", "2026-10-21T00:00:00+08:00")
        self.reconcile(1, *args, "--until", "2026-10-22T01:00:00+08:00")
        report = self.reconcile(1, "--production")
        self.assertIn("必须指定两个完整 UTC+08:00 自然日的窗口", report["gate_errors"])
        path = self.state / "events.jsonl"
        original = path.read_text()
        for field, value in (("exit_code", 9), ("end_time", "2020-01-01T00:00:00+08:00")):
            logs = [json.loads(x) for x in original.splitlines()]
            logs[0][field] = value
            path.write_bytes(b"".join(line(x) for x in logs))
            self.reconcile(1, *args)
        path.write_text(original)
        Path(args[1]).write_text("")
        self.reconcile(1, *args)

    def test_35_single_source_and_missing_config_rejected(self):
        self.append(*({**event("single-%d" % i), "end_time": "2026-10-20T12:00:00+08:00"} for i in range(10)))
        self.run_once("--dry-run")
        report = self.reconcile(1, "--production", "--since", "2026-10-20T00:00:00+08:00",
                                "--until", "2026-10-22T00:00:00+08:00")
        self.assertIn("每助手事件数不足", report["gate_errors"])
        self.config.write_text(json.dumps({"示例助手甲": URL_ENV}))
        self.assertIn("配置须覆盖必需助手及同路由别名", self.reconcile(1, "--production")["gate_errors"])

    def test_36_conflict_ignored_and_retry_alert(self):
        self.append(event("collision"), event("collision", "示例助手丙"), event("ignored", "unknown"))
        self.server.responses = [401]
        self.run_once("--allow-history", expected=2)
        self.assertEqual(self.heartbeat()["conflicting_events"], 1)
        self.assertEqual(self.heartbeat()["ignored"], 1)
        with self.watcher() as watcher:
            with watcher.db:
                watcher.db.execute("UPDATE events SET attempts=5 WHERE status='pending'")
            watcher.heartbeat()
        self.assertEqual(self.heartbeat()["retry_alert_count"], 1)
        self.assertEqual(self.heartbeat()["health"], "需检查")

    def test_37_unexpected_exception_sanitized(self):
        with mock.patch.object(MODULE, "Watcher", side_effect=TypeError("secret-url")):
            result = self.run_once("--allow-history", expected=1)
        self.assertNotIn("secret-url", result.stderr)

    def test_38_once_stops_at_initial_size(self):
        self.append(event("first"))
        with self.watcher("--allow-history", "--once", "--batch-size", "1") as watcher:
            watcher.ingest()
            self.append(event("later"))
            watcher.ingest()
            self.assertEqual(watcher.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        self.run_once()
        self.assertEqual(len(self.server.received), 2)

    def test_39_fresh_http_requires_explicit_choice(self):
        self.append(event("history-1"), event("history-2"))
        self.run_once("--no-history-choice", expected=1)
        self.assertFalse(self.server.received)
        with contextlib.closing(sqlite3.connect(self.state / "state.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='cursor'").fetchall(), [])
        self.run_once("--allow-history")
        self.assertEqual(len(self.server.received), 2)

    def test_40_old_reports_strict_validation(self):
        args = self.production_fixture()
        path = Path(args[1])
        original = path.read_bytes()
        rows = [json.loads(x) for x in original.splitlines()]
        variants = [rows[:-1], rows + [rows[0]]]
        for value in (True, 1.0, 99):
            changed = [dict(r) for r in rows]
            changed[1]["exit_code"] = value
            variants.append(changed)
        changed = [dict(r) for r in rows]
        changed[0]["reported_at"] = "2999-01-01T00:00:00+08:00"
        variants.append(changed)
        for variant in variants:
            with self.subTest(variant=variants.index(variant)):
                path.write_bytes(b"".join(line(x) for x in variant))
                self.assertFalse(self.reconcile(1, *args)["acceptance"])
        path.write_bytes(original)
        self.reconcile(1, *args, "--expected-routine", "示例助手甲=另一个routine")
        self.assertTrue(self.reconcile(0, *args, "--expected-routine",
                                      "示例助手甲=synthetic-old-routine")["acceptance"])

    def test_41_days_required_agent_and_outside_conflict(self):
        args = self.production_fixture()
        report = self.reconcile(1, *args, "--until", "2026-10-23T00:00:00+08:00")
        self.assertIn("窗口存在无完成事件的整日，需继续采证", report["gate_errors"])
        self.reconcile(1, *args, "--require-agent", "额外助手")
        config = json.loads(self.config.read_text())
        config["额外助手"] = "EXTRA_URL"
        self.config.write_text(json.dumps(config))
        self.assertIn("每助手事件数不足", self.reconcile(1, *args, "--require-agent", "额外助手")["gate_errors"])
        self.append(event("accept-0", "范围外助手"))
        self.assertIn("accept-0", self.reconcile(1, *args)["conflicting_run_ids"])

    def test_42_diagnostic_never_acceptance(self):
        self.append(*(event("diag-%d" % i) for i in range(10)))
        self.run_once("--dry-run")
        report = self.reconcile(0)
        self.assertTrue(report["passed"])
        self.assertFalse(report["acceptance"])

    def test_43_once_partial_at_initial_limit(self):
        partial = line(event("partial-limit"))
        self.ledger.write_bytes(line(event("complete")) + partial[:20])
        with self.watcher("--allow-history", "--once", "--batch-size", "1") as watcher:
            watcher.ingest()
            with self.ledger.open("ab") as stream:
                stream.write(partial[20:])
            watcher.ingest()
            self.assertEqual(watcher.db.execute("SELECT run_id FROM events").fetchall(), [("complete",)])
        self.run_once()
        self.assertEqual({r[0] for r in self.rows()}, {"complete", "partial-limit"})

    def test_44_scenarios_are_hard_gates(self):
        args = self.production_fixture()
        path = Path(args[3])
        original = json.loads(path.read_text())
        for name in original:
            changed = dict(original)
            del changed[name]
            path.write_text(json.dumps(changed))
            self.assertIn("场景证据缺失或无效：" + name, self.reconcile(1, *args)["gate_errors"])
        path.write_text(json.dumps(original))
        self.assertTrue(self.reconcile(0, *args)["acceptance"])
        self.reconcile(1, *args[:2], *args[4:])

    def test_45_health_ack_preserves_counts_and_new_alerts(self):
        self.append(event("known", "示例范围外助手"))
        self.run_once("--allow-history")
        self.assertEqual(self.heartbeat()["health"], "正常")
        self.append(event("unknown", "疑似更名"))
        self.run_once(expected=2)
        self.run_once("--ack-anomalies")
        self.assertEqual(self.heartbeat()["ignored"], 2)
        self.assertEqual(self.heartbeat()["health"], "正常")
        self.append(event("unknown2", "疑似更名"))
        self.run_once(expected=2)
        self.assertEqual(self.heartbeat()["health"], "需检查")

    def test_46_ack_does_not_hide_pending(self):
        self.append(event("failed"))
        self.server.responses = [401]
        self.run_once("--allow-history", expected=2)
        self.run_once("--ack-anomalies", expected=2)
        self.assertEqual(self.heartbeat()["health"], "需检查")

    def test_47_checker_detects_stale_pending(self):
        self.run_once("--start-at-end")
        heartbeat = self.heartbeat()
        heartbeat.update(pending=1, oldest_pending_age_seconds=901)
        (self.state / "heartbeat.json").write_text(json.dumps(heartbeat))
        self.assertIn("积压", self.check_heartbeat(1)["reason"])

    def test_48_initialization_then_unflagged_restart(self):
        self.append(event("historical"))
        self.run_once("--start-at-end")
        self.append(event("after-boundary"))
        self.run_once("--no-history-choice")
        self.run_once("--no-history-choice")
        self.assertEqual([r["body"]["run_id"] for r in self.server.received], ["after-boundary"])
        self.run_once("--start-at-end", expected=1)

    def test_49_cumulative_anomalies_ack_and_recur(self):
        self.run_once("--start-at-end")
        with self.watcher() as watcher:
            with watcher.db:
                for key in ("invalid_lines", "ledger_resets", "conflicting_events"):
                    watcher.count(key)
        self.run_once(expected=2)
        self.run_once("--ack-anomalies")
        for key in ("invalid_lines", "ledger_resets", "conflicting_events"):
            self.assertEqual(self.heartbeat()[key], 1)
        self.assertEqual(self.heartbeat()["health"], "正常")
        with self.watcher() as watcher:
            with watcher.db:
                watcher.count("conflicting_events")
        self.run_once(expected=2)
        self.assertEqual(self.heartbeat()["unreviewed_anomalies"]["conflicting_events"], 1)

    def test_50_pending_rejects_history_in_all_modes(self):
        for mode in ((), ("--dry-run",), ("--transport", "file", "--outbox", str(self.root / "outbox"))):
            with self.subTest(mode=mode):
                self.state = self.root / ("pending-" + str(len(mode)))
                self.ledger.unlink(missing_ok=True)
                self.run_once("--start-at-end", *mode, expected=2)
                self.append(event("history-1"), event("history-2"))
                result = self.run_once("--allow-history", *mode, expected=1)
                self.assertIn("start_at_end_pending", result.stderr)
                self.assertIn("人工核对", result.stderr)
                self.assertIn("新独立状态目录", result.stderr)
                with contextlib.closing(sqlite3.connect(self.state / "state.sqlite3")) as db:
                    self.assertEqual(dict(db.execute("SELECT key,value FROM meta"))["start_at_end_pending"], "1")
                    self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone())
                self.assertEqual(self.rows(), [])
                self.assertFalse(self.server.received)
                logs = (self.state / "events.jsonl").read_text()
                self.assertNotIn("显式允许历史扫描", logs)
                self.assertNotIn("首次从末尾接入", logs)
                self.assertFalse((self.root / "outbox").exists())

    def test_51_pending_position_failure_then_resume(self):
        self.ledger.unlink()
        self.run_once("--start-at-end", expected=2)
        self.append(event("history-1"), event("history-2"))
        with mock.patch.object(MODULE.Watcher, "end_boundary", side_effect=OSError("定位失败")):
            self.run_once("--no-history-choice", expected=1)
        with contextlib.closing(sqlite3.connect(self.state / "state.sqlite3")) as db:
            self.assertEqual(dict(db.execute("SELECT key,value FROM meta"))["start_at_end_pending"], "1")
            self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone())
        self.run_once("--no-history-choice")
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.server.received)
        with contextlib.closing(sqlite3.connect(self.state / "state.sqlite3")) as db:
            meta = dict(db.execute("SELECT key,value FROM meta"))
            self.assertNotIn("start_at_end_pending", meta)
            self.assertEqual(json.loads(meta["cursor"])["offset"], self.ledger.stat().st_size)
        self.append(event("fresh"))
        self.run_once("--no-history-choice")
        self.assertEqual([r["body"]["run_id"] for r in self.server.received], ["fresh"])
        self.assertNotIn("显式允许历史扫描", (self.state / "events.jsonl").read_text())

    def test_52_same_agent_content_conflict(self):
        self.append(event("collision"), event("collision", exit_code=1))
        self.run_once("--allow-history", expected=2)
        self.assertEqual(self.heartbeat()["conflicting_events"], 1)
        self.assertEqual(self.heartbeat()["health"], "需检查")
        self.assertEqual([r["body"]["exit_code"] for r in self.server.received], [0])

    def test_53_cursor_rejects_history_all_modes(self):
        for number, mode in enumerate(((), ("--dry-run",), ("--transport", "file", "--outbox", str(self.root / "outbox")))):
            with self.subTest(mode=mode):
                self.state = self.root / ("cursor-" + str(number))
                self.ledger.write_bytes(line(event("old")))
                self.run_once("--start-at-end", *mode)
                before = self.heartbeat()["cursor"]
                self.append(event("fresh"))
                result = self.run_once("--allow-history", *mode, expected=1)
                self.assertIn("已有游标", result.stderr)
                self.assertEqual(self.rows(), [])
                self.assertEqual(self.heartbeat()["cursor"], before)
                self.assertNotIn("显式允许历史扫描", (self.state / "events.jsonl").read_text())
                self.run_once(*mode)
                self.assertEqual([r[0] for r in self.rows()], ["fresh"])

    def test_54_history_log_http_only(self):
        for number, mode in enumerate(((), ("--dry-run",), ("--transport", "file", "--outbox", str(self.root / "outbox")))):
            self.state = self.root / ("history-log-" + str(number))
            self.append(event("log-%d" % number))
            self.run_once("--allow-history", *mode)
            self.assertEqual("显式允许历史扫描" in (self.state / "events.jsonl").read_text(), number == 0)

    def test_55_old_complete_window_rejected(self):
        args = self.production_fixture()
        self.assertTrue(self.reconcile(0, *args)["acceptance"])
        # 将完整证据整体前移一天，保持字段和两日覆盖一致。
        for path in (self.ledger, self.state / "events.jsonl", Path(args[1]), Path(args[3])):
            text = path.read_text()
            for day in (20, 21, 22):
                text = text.replace("2026-10-%02d" % day, "2026-10-%02d" % (day - 1))
            path.write_text(text)
        report = self.reconcile(1, *args, "--since", "2026-10-19T00:00:00+08:00", "--until", "2026-10-21T00:00:00+08:00")
        self.assertEqual(report["gate_errors"], ["窗口必须为检查当天之前最近两个完整 UTC+08:00 自然日"])
        self.assertFalse(report["acceptance"])

    def test_56_helper_has_no_implicit_history(self):
        self.assertNotIn("--allow-history", self.args())
        self.run_once(expected=1)
        self.assertFalse(self.server.received)

    def test_57_policy_controls_alias_and_required_agents(self):
        args = self.production_fixture()
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({"aliases": {}, "required_agents": ["示例助手甲旧名"]}))
        report = self.reconcile(0, *args, "--policy", str(policy))
        self.assertEqual(report["agents"]["示例助手甲旧名"]["matched"], 3)
        policy.write_text(json.dumps({"aliases": {}, "required_agents": ["缺席示例助手"]}))
        self.assertIn("每助手事件数不足", self.reconcile(1, *args, "--policy", str(policy))["gate_errors"])

    def test_58_policy_rejects_self_alias(self):
        self.assert_invalid_policy({"示例助手甲": "示例助手甲"}, 2)

    def test_59_policy_rejects_alias_chain(self):
        self.assert_invalid_policy({"示例助手甲旧名": "示例助手甲", "示例助手甲": "示例助手乙"}, 2)

    def test_60_policy_rejects_missing_target(self):
        self.assert_invalid_policy({"示例助手甲旧名": "不存在的示例助手"}, 1)

    def test_61_policy_rejects_different_route(self):
        self.assert_invalid_policy({"示例助手甲旧名": "示例助手乙"}, 1)

    def test_62_policy_rejects_empty_required(self):
        self.assert_invalid_policy({}, 2, required=[])

    def assert_invalid_policy(self, aliases, expected, required=None):
        args = self.production_fixture()
        policy = self.root / "invalid-policy.json"
        policy.write_text(json.dumps({"aliases": aliases,
            "required_agents": ["示例助手甲", "示例助手乙", "示例助手丙"] if required is None else required}))
        report = self.reconcile(expected, *args, "--policy", str(policy))
        if expected == 1:
            self.assertFalse(report["acceptance"])
            self.assertIn("配置须覆盖必需助手及同路由别名", report["gate_errors"])

    def test_63_reconcile_cli_requires_policy(self):
        result = subprocess.run([sys.executable, str(SCRIPT.with_name("reconcile.py")),
            "--ledger", str(self.ledger), "--log", str(self.state / "events.jsonl"),
            "--config", str(self.config), "--diagnostic"],
            env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--policy", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_64_file_outbox_errors_use_retry_backoff(self):
        self.append(event("file-retry"))
        for failure in ("mkdir", "atomic_json"):
            with self.subTest(failure=failure):
                self.state = self.root / (failure + "-state")
                outbox = self.root / (failure + "-outbox")
                if failure == "mkdir":
                    outbox.write_text("regular file", encoding="utf-8")
                else:
                    outbox.mkdir()
                    (outbox / "file-retry.tmp").mkdir()
                with self.watcher("--transport", "file", "--outbox", str(outbox)) as watcher, \
                        mock.patch.object(MODULE.time, "time", return_value=1000) as clock:
                    watcher.tick()
                    status, attempts, next_attempt, error = watcher.db.execute(
                        "SELECT status,attempts,next_attempt,last_error FROM events").fetchone()
                    self.assertEqual((status, attempts, error), ("pending", 1, "文件写入失败"))
                    self.assertEqual(next_attempt, 1000 + watcher.args.retry_base)
                    watcher.tick()  # 退避尚未到期，不应重复投递。
                    self.assertEqual(self.rows(), [("file-retry", "pending", 1)])
                    clock.return_value = next_attempt
                    watcher.tick()
                    self.assertEqual(self.rows(), [("file-retry", "pending", 2)])
                    retry_at = watcher.db.execute("SELECT next_attempt FROM events").fetchone()[0]
                    self.assertEqual(retry_at, next_attempt + watcher.args.retry_max)
                logs = [json.loads(row) for row in (self.state / "events.jsonl").read_text().splitlines()]
                failures = [row for row in logs if row["event"] == "投递失败待补报"]
                self.assertEqual([row["reason"] for row in failures], ["文件写入失败"] * 2)
                self.assertEqual([row["retry_in_seconds"] for row in failures], [0.03, 0.06])


if __name__ == "__main__":
    # 直接运行时显示各测试的详细结果。
    unittest.main(verbosity=2)
