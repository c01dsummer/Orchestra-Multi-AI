# 通用技能改动记录

## v1

- 提供多助手协作技能入口 `SKILL.md`，明确执行器由使用者自备。
- 提供 `roles/` 中 8 类通用角色的职责、禁止事项与设定模板。
- 提供 `process/` 中派工、交接、复审、合并、改规矩、正本同步 6 项流程。
- 提供 `routing/` 中规则表格式版本 1 的说明与空白模板。
- 提供 `examples/` 中完全虚构的团队配置与 8 名成员的完整设定。
- 提供 `scripts/` 中本地自检和公开部分导出脚本。
- 提供 `tools/run-done-watcher/` 中运行完成通知、心跳检查与离线对账组件。
- 收紧导出文件白名单，校验作者引擎及完整配置，排除运行状态并以路径摘要绑定 watcher 状态。
- 补全模板契约与本机路径检查，明确示例词表豁免、合并授权及同步检查位置。

### PR 看护指南

新增 `docs/pr-watchdog.md`、`scripts/pr-watch/schema.json` 和 `examples/pr-watch/`，提供隔离修复、对账水位线与虚构事件示例。

- 明确缺少仓库名时输出的 `repo` 为 null，并同步 schema。
- 收紧输出契约，六个事件及看护字段列为必填；明确决定性评审的取代顺序，非 ASCII 状态判为 `unknown`。
- 澄清评审判定条件与作者 ASCII 规范，要求消费方对不完整分页或非单调时间戳按 `unknown` 处理。
- 对 `blocked_unknown` 强制 `requires_watch: true`，并说明单仓缺少仓库名时的回退键及适用范围。
- 明确仓库范围、可信身份归一化和输入完整性边界。

### 完成通知组件

- 完成通知组件的心跳 `timezone` 字段值改为 `UTC+08:00`，心跳检查与自检测试同步更新。新旧版本的 watcher 和检查器混用时，心跳会被判为格式无效，造成误报，不会误放行；升级步骤：先暂停心跳检查定时任务；同时更新 watcher 与 `check-heartbeat.py`，沿用原状态目录按日常方式（不带首次接入参数）重启 watcher；运行一次 `python3 tools/run-done-watcher/check-heartbeat.py --heartbeat <状态目录>/heartbeat.json`，确认输出 `"ok": true`、心跳已重写为 `UTC+08:00`；再恢复心跳检查定时任务。依赖该字段的外部消费方也须同步更新。
- 对账门槛错误文案改用 `UTC+08:00` 自然日表述，影响对账报告中的 `gate_errors`。
- 补充心跳版本混用与对账窗口自检测试。
