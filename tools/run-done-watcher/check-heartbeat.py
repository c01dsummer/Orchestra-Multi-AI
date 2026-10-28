#!/usr/bin/env python3
"""由独立的心跳检查定时任务执行的一次性心跳检查；不联网、不发送通知。"""

import argparse
import datetime as dt
import json
import math
from pathlib import Path
import sys

TZ = dt.timezone(dt.timedelta(hours=8), name="UTC+08:00")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heartbeat", required=True)
    parser.add_argument("--max-age-minutes", type=float, default=15)
    args = parser.parse_args(argv)
    if not math.isfinite(args.max_age_minutes) or args.max_age_minutes <= 0:
        parser.error("心跳过期分钟数必须为正有限数")
    checked = dt.datetime.now(TZ)
    result = {"checked_at": checked.isoformat(timespec="seconds"), "ok": False}
    try:
        heartbeat = json.loads(Path(args.heartbeat).read_text(encoding="utf-8"))
        updated = dt.datetime.fromisoformat(heartbeat["updated_at"])
        if updated.tzinfo is None:
            raise ValueError()
        pending = heartbeat.get("pending", 0)
        oldest = heartbeat.get("oldest_pending_age_seconds", 0)
        if (type(pending) is not int or pending < 0
                or type(oldest) not in (int, float) or not math.isfinite(oldest) or oldest < 0):
            raise ValueError()
        zone = heartbeat["timezone"]
        if not isinstance(zone, str) or not zone:
            raise ValueError()
        if zone != "UTC+08:00":
            result["detail"] = "版本不一致，请同时升级并重启 watcher"
            result["reason"] = "invalid_heartbeat_payload"
            raise ValueError()
        age = (checked - updated).total_seconds()
        result["age_seconds"] = round(age, 1)
        if age < -60:
            reason = "心跳时间在未来，需检查时钟"
        elif age > args.max_age_minutes * 60:
            reason = "心跳过期，watcher 可能停止或卡住"
        elif heartbeat.get("stopping"):
            reason = "watcher 已请求停止"
        elif heartbeat.get("health") != "正常" or not heartbeat.get("ledger_readable"):
            reason = "watcher 存活但账本或队列需检查"
        elif pending and oldest > args.max_age_minutes * 60:
            reason = "待投递队列积压超时"
        else:
            reason = "心跳正常"
            result["ok"] = True
        result["reason"] = reason
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        result.setdefault("reason", "心跳缺失、不可读或格式无效")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
