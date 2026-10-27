# 完成事件 watcher

仅依赖 Python 3 标准库。路由配置复制 config.example.json 后自行填写环境变量名，真实环境变量值不写进文件。对账另外复制 reconcile.config.example.json，配置 aliases（旧名到正式名，同路由且禁止链式别名）和非空 required_agents。reconcile.py 必须传 --config 路由文件和 --policy 对账配置。

运行离线回归：`PYTHONDONTWRITEBYTECODE=1 python3 selftest.py`。测试禁止 socket，HTTP 使用注入的假发送器；file 模式只写测试临时目录。

HTTP 首次接入必须显式选择 --start-at-end 或 --allow-history；有 cursor 或末尾接入 pending 时拒绝 --allow-history。只有 HTTP 的显式历史扫描写历史选择日志。已有 cursor 的日常重启不带接入参数；末尾接入 pending 时也可无参数续跑。若写入绑定后、首次 ingest 前中断，既无 cursor 也无 pending，须用原来的 --start-at-end 或 --allow-history 重启。

生产对账要求检查当天之前最近两个完整 UTC+08:00 自然日，且必须满足事件、每助手、别名、旧回报和场景证据门槛；其他窗口只能 --diagnostic，不能生产放行。别名和必需助手名单全部由 --policy 读取。

## 与自备执行器连接

本组件不调用模型。执行器自行写入 UTF-8 JSONL 运行记录，每行以换行结束，至少包含：唯一字符串 `run_id`、助手名 `agent`、含时区的完成时间 `end_time`、整数退出码 `exit_code`。未知助手只记录为忽略事件；路由配置中的名字须与记录一致。

在仓库根目录可先用本地记录进行 dry-run（不读取通知地址，也不发送请求）：

```bash
mkdir -p config/notify
cp tools/run-done-watcher/config.example.json config/notify/routes.json
printf '%s\n' '{"run_id":"demo-1","agent":"示例助手甲","end_time":"2026-10-24T12:00:00+08:00","exit_code":0}' > config/notify/events.jsonl
python3 tools/run-done-watcher/run-done-watcher \
  --ledger config/notify/events.jsonl --config config/notify/routes.json \
  --state-dir config/notify/state-dry --dry-run --once
```

真实通知另用独立状态目录，配置只保存环境变量名，秘密值通过环境注入。先核对接入边界，再显式选末尾接入或历史扫描；不要将 dry-run 状态改作 HTTP 状态。心跳由 `check-heartbeat.py` 独立检查，对账由 `reconcile.py` 配合策略文件执行。完成通知只表示运行结束。

心跳的 `timezone` 字段值改为 `UTC+08:00`，这是不兼容变更。新旧版本的 watcher 和检查器混用时，心跳会被判为格式无效，造成误报，不会误放行。`check-heartbeat.py` 在 `timezone` 标识不一致时返回 `reason: "invalid_heartbeat_payload"`，并附带 `detail: "版本不一致，请同时升级并重启 watcher"`。

升级步骤：先暂停心跳检查定时任务；同时更新 watcher 与 `check-heartbeat.py`。沿用原状态目录仅适用于状态绑定中已是 `"schema": 3`（状态 schema 版本 3）的目录；更早版本见下方兼容说明。已完成首次接入（已有 cursor）时按日常方式无参数重启；首次接入尚未完成时按上面的接入状态续跑。运行一次 `python3 tools/run-done-watcher/check-heartbeat.py --heartbeat <状态目录>/heartbeat.json`，确认输出 `"ok": true`、心跳已重写为 `UTC+08:00`，再恢复心跳检查定时任务。若原状态目录本身存在失败运行待处理（`failed_pending`）或未确认的账本异常（`unreviewed_anomalies`），`check-heartbeat.py` 返回 `ok: false` 属于既有业务告警，需按排障章节处理后再恢复定时任务。依赖该字段的外部消费方也须同步更新。

状态绑定仅保存规范化账本路径与 file 输出目录路径的 SHA-256 摘要，仍会拒绝更换账本、模式或配置。绑定格式升级为 schema 3；旧版状态目录会被拒绝复用，升级时须使用独立状态目录并重新核对接入边界。

在仓库根目录运行全部离线回归：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tools/run-done-watcher -v
```

## 心跳告警排障

检查心跳输出的 `reason` 与 watcher 状态：对失败运行待处理（`failed_pending`）检查投递失败原因并完成补报，对未确认的账本异常（`unreviewed_anomalies`）保留现场并排查。若历史累计的 `unreviewed_anomalies` 导致 `check-heartbeat.py` 返回 `ok: false`，核实无误后，以原有配置和状态目录运行 watcher 并附加 `--ack-anomalies`，确认并清零未确认异常计数；原始累计计数保留，未送达队列不会因此确认。处理后重新运行心跳检查，确认 `ok: true` 再恢复定时任务。
