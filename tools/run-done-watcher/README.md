# 完成事件 watcher

仅依赖 Python 3 标准库。路由配置复制 config.example.json 后自行填写环境变量名，真实环境变量值不写进文件。对账另外复制 reconcile.config.example.json，配置 aliases（旧名到正式名，同路由且禁止链式别名）和非空 required_agents。reconcile.py 必须传 --config 路由文件和 --policy 对账配置。

运行离线回归：`PYTHONDONTWRITEBYTECODE=1 python3 selftest.py`。测试禁止 socket，HTTP 使用注入的假发送器；file 模式只写测试临时目录。

HTTP 首次接入必须显式选择 --start-at-end 或 --allow-history；有 cursor 或末尾接入 pending 时拒绝 --allow-history。只有 HTTP 的显式历史扫描写历史选择日志。日常重启不带接入参数。

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

心跳的 `timezone` 字段值改为 `UTC+08:00`。新旧版本的 watcher 和检查器混用时，心跳会被判为格式无效，造成误报，不会误放行。升级时同时更新 watcher 和检查器，重启 watcher，确认心跳已重写后再恢复巡检；依赖该字段的外部消费方也须同步更新。

状态绑定仅保存规范化账本路径与 file 输出目录路径的 SHA-256 摘要，仍会拒绝更换账本、模式或配置。绑定格式升级为 schema 3；旧版状态目录会被拒绝复用，升级时须使用独立状态目录并重新核对接入边界。

在仓库根目录运行全部离线回归：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tools/run-done-watcher -v
```
