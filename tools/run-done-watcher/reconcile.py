#!/usr/bin/env python3
"""只读账本和 dry-run 日志，按助手/run_id 对账；输出不含账本原文。"""

import argparse
from collections import Counter, defaultdict
import datetime as dt
import json
from pathlib import Path
import re
import sys

TZ = dt.timezone(dt.timedelta(hours=8), name="UTC+08:00")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")


def timestamp(value):
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--log", required=True, action="append", help="dry-run events.jsonl，可重复")
    parser.add_argument("--config", required=True)
    parser.add_argument("--policy", required=True, help="对账配置：aliases 和 required_agents")
    parser.add_argument("--since", help="含起点，按完成时间；必须含时区")
    parser.add_argument("--until", help="不含终点，按完成时间；必须含时区")
    parser.add_argument("--min-events", type=int, default=10, help="验收所需唯一事件数，默认 10")
    parser.add_argument("--diagnostic", action="store_true", help="仅诊断，输出不能作为停旧轮询凭证")
    parser.add_argument("--require-agent", action="append", default=[])
    parser.add_argument("--min-per-agent", type=int, default=1)
    parser.add_argument("--old-reports", help="旧轮询回报证据 JSONL")
    parser.add_argument("--expected-routine", action="append", default=[], help="可重复：canonical助手=旧routine名称")
    parser.add_argument("--scenario-evidence", help="四类场景人工签认证据 JSON")
    args = parser.parse_args(argv)
    try:
        checked = dt.datetime.now(TZ)
        expected_routines = dict(item.split("=", 1) for item in args.expected_routine)
        since = timestamp(args.since) if args.since else None
        until = timestamp(args.until) if args.until else None
        if args.min_events < 0 or (since and until and since >= until):
            raise ValueError()
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if not isinstance(config, dict) or not config:
            raise ValueError()
        canonical, destinations = {}, {}
        for agent, route in config.items():
            destination = route if isinstance(route, str) else route["url_env"]
            if not isinstance(agent, str) or not isinstance(destination, str):
                raise ValueError()
            canonical[agent] = agent
            destinations[agent] = destination
        policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
        aliases = policy["aliases"]
        required_agents = policy["required_agents"]
        if (not isinstance(aliases, dict) or not isinstance(required_agents, list)
                or not required_agents or any(not isinstance(x, str) or not x for x in required_agents)
                or any(not isinstance(a, str) or not a or not isinstance(b, str) or not b
                       or a == b or b in aliases for a, b in aliases.items())):
            raise ValueError()
        valid_aliases = True
        for alias, target in aliases.items():
            if alias not in canonical or target not in canonical or destinations[alias] != destinations[target]:
                valid_aliases = False
            else:
                canonical[alias] = target

        def in_window(ended):
            return (since is None or ended >= since) and (until is None or ended < until)

        def read_jsonl(path, source):
            with Path(path).open("rb") as stream:
                for line in stream:
                    if not line.endswith(b"\n"):
                        invalid[source + "_partial"] += 1
                        break
                    try:
                        yield json.loads(line)
                    except (ValueError, UnicodeError, RecursionError):
                        invalid[source] += 1

        invalid = Counter()
        gates = []
        required = set(required_agents) | set(args.require_agent)
        if not args.diagnostic:
            if args.min_events < 10 or args.min_per_agent < 1:
                gates.append("生产事件门槛不得降低")
            if not since or not until:
                gates.append("必须指定两个完整 UTC+08:00 自然日的窗口")
            elif (since.astimezone(TZ).time() != dt.time() or until.astimezone(TZ).time() != dt.time()
                  or until - since != dt.timedelta(days=2)
                  or until.astimezone(TZ) != checked.replace(hour=0, minute=0, second=0, microsecond=0)):
                gates.append("窗口必须为检查当天之前最近两个完整 UTC+08:00 自然日")
            if not required <= set(canonical.values()) or not valid_aliases:
                gates.append("配置须覆盖必需助手及同路由别名")
        ledger_content, watcher_content = {}, {}
        raw_agents = Counter()
        observed_days = set()
        watcher_raw_agents = Counter()
        outside = Counter()
        ledger, watcher = Counter(), Counter()
        ledger_agents = defaultdict(set)
        log_modes = Counter()
        for row in read_jsonl(args.ledger, "ledger"):
            try:
                if (not isinstance(row, dict) or not isinstance(row.get("run_id"), str)
                        or not RUN_ID.fullmatch(row["run_id"])
                        or not isinstance(row.get("agent"), str)
                        or not 0 < len(row["agent"]) <= 100
                        or type(row.get("exit_code")) is not int):
                    raise ValueError()
                ended = timestamp(row["end_time"])
                ledger_agents[row["run_id"]].add(canonical.get(row["agent"], row["agent"]))
                if row["agent"] not in canonical:
                    outside[row["agent"]] += 1
                if row["agent"] in canonical and in_window(ended):
                    key = (canonical[row["agent"]], row["run_id"])
                    ledger[key] += 1
                    raw_agents[row["agent"]] += 1
                    observed_days.add(ended.astimezone(TZ).date())
                    ledger_content[key] = (row["exit_code"], ended.astimezone(TZ).isoformat())
                    ledger_agents[row["run_id"]].add(key[0])
            except (KeyError, ValueError, TypeError, AttributeError):
                invalid["ledger"] += 1
        for path in args.log:
            for row in read_jsonl(path, "log"):
                try:
                    if not isinstance(row, dict):
                        raise ValueError()
                    if row.get("event") != "发现完成事件":
                        continue
                    if row.get("status") == "ignored" and row.get("agent") not in canonical:
                        continue
                    if row.get("status") != "dry-run":
                        log_modes[str(row.get("status")) if row.get("status") in
                                  {"pending", "ignored", "dry-run"} else "unknown"] += 1
                        continue
                    if (row.get("agent") not in canonical or not isinstance(row.get("run_id"), str)
                            or not RUN_ID.fullmatch(row["run_id"])):
                        raise ValueError()
                    # 旧初稿缺少完成时间，不能凭日志写入时间伪装成完成时间。
                    ended = timestamp(row["end_time"])
                    if in_window(ended):
                        key = (canonical[row["agent"]], row["run_id"])
                        watcher[key] += 1
                        watcher_raw_agents[row["agent"]] += 1
                        if type(row.get("exit_code")) is not int:
                            raise ValueError()
                        watcher_content[key] = (row["exit_code"], ended.astimezone(TZ).isoformat())
                except (KeyError, ValueError, TypeError, AttributeError):
                    invalid["log"] += 1
        report_agents = {}
        for agent in sorted(set(canonical.values())):
            expected = {key[1] for key in ledger if key[0] == agent}
            observed = {key[1] for key in watcher if key[0] == agent}
            report_agents[agent] = {
                "ledger_unique": len(expected), "watcher_unique": len(observed),
                "matched": len(expected & observed),
                "missing_in_watcher": sorted(expected - observed),
                "unexpected_in_watcher": sorted(observed - expected),
                "duplicate_ledger": {key[1]: n for key, n in sorted(ledger.items())
                                     if key[0] == agent and n > 1},
                "duplicate_watcher": {key[1]: n for key, n in sorted(watcher.items())
                                      if key[0] == agent and n > 1}}
        conflicts = {key: sorted(agents) for key, agents in sorted(ledger_agents.items()) if len(agents) > 1}
        differences = any(r["missing_in_watcher"] or r["unexpected_in_watcher"]
                          or r["duplicate_ledger"] or r["duplicate_watcher"]
                          for r in report_agents.values())
        mismatches = [list(k) for k in sorted(ledger_content.keys() & watcher_content.keys())
                      if ledger_content[k] != watcher_content[k]]
        evidence = Counter()
        if args.old_reports:
            for row in read_jsonl(args.old_reports, "old_reports"):
                try:
                    key = (canonical[row["agent"]], row["run_id"])
                    content = (row["exit_code"], timestamp(row["end_time"]).astimezone(TZ).isoformat())
                    if (type(row.get("exit_code")) is not int
                            or row.get("reported") is not True or not isinstance(row.get("origin"), str)
                            or not row["origin"].strip() or not isinstance(row.get("routine"), str)
                            or not row["routine"].strip() or not row.get("reported_at")
                            or timestamp(row["reported_at"]) < timestamp(row["end_time"])
                            or timestamp(row["reported_at"]) > checked
                            or (key[0] in expected_routines and row["routine"] != expected_routines[key[0]])
                            or content != ledger_content.get(key)):
                        raise ValueError()
                    evidence[key] += 1
                except (KeyError, TypeError, ValueError, AttributeError):
                    invalid["old_reports"] += 1
        scenarios = {}
        if args.scenario_evidence:
            scenarios = json.loads(Path(args.scenario_evidence).read_text(encoding="utf-8"))
        if not args.diagnostic:
            for name in ("success_failure", "duplicate", "restart", "retry"):
                try:
                    item = scenarios[name]
                    ids = item["run_ids"]
                    if (item.get("passed") is not True or not isinstance(ids, list) or not ids
                            or any(not isinstance(x, str) for x in ids)
                            or not set(ids) <= {k[1] for k in ledger}
                            or not isinstance(item["reviewer"], str) or not item["reviewer"].strip()
                            or not isinstance(item["evidence"], str) or not item["evidence"].strip()
                            or timestamp(item["reviewed_at"]) > checked):
                        raise ValueError()
                    if name == "success_failure":
                        codes = [v[0] for k, v in ledger_content.items() if k[1] in ids]
                        if not (0 in codes and any(x != 0 for x in codes)):
                            raise ValueError()
                except (KeyError, TypeError, ValueError, AttributeError):
                    gates.append("场景证据缺失或无效：" + name)
            if any(sum(1 for k in ledger if k[0] == a) < args.min_per_agent for a in required):
                gates.append("每助手事件数不足")
            if since and until and len(observed_days) < (until.astimezone(TZ).date() - since.astimezone(TZ).date()).days:
                gates.append("窗口存在无完成事件的整日，需继续采证")
            if any(not raw_agents[a] or not watcher_raw_agents[a] for a in aliases):
                gates.append("缺少配置别名覆盖")
            if not args.old_reports or set(evidence) != set(ledger) or any(n != 1 for n in evidence.values()):
                gates.append("缺少逐事件旧轮询 reported/origin 回报证据或证据重复")
        passed = not (differences or conflicts or invalid or log_modes or mismatches or gates) and len(ledger) >= args.min_events
        print(json.dumps({"checked_at": dt.datetime.now(TZ).isoformat(timespec="seconds"),
                          "passed": passed, "acceptance": passed and not args.diagnostic,
                          "diagnostic": args.diagnostic, "gate_errors": gates,
                          "content_mismatches": mismatches, "out_of_scope_agents": dict(outside),
                          "old_reports_matched": len(evidence), "required_events": args.min_events,
                          "ledger_unique": len(ledger), "agents": report_agents,
                          "conflicting_run_ids": conflicts,
                          "invalid_lines": dict(invalid), "non_dry_run_events": dict(log_modes)},
                         ensure_ascii=False, indent=2))
        return 0 if passed else 1
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print("对账失败：文件、配置或时间范围无效；未输出原文。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
