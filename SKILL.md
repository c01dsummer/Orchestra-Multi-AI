---
name: orchestra
description: 多 AI 助手分工协作技能。接活、派工、交接、跨家复审、按规则表选模型或修改团队规矩时读取；模型调用执行器由使用者自备。
---

# 多助手协作（v1）

开工前先读，按五步执行。安装与边界见 `README.md`，版本记录见 `CHANGELOG.md`。

## 五步用法

1. 读 `config/team.json`，找到自己的角色与可用引擎；没有 `config/` 就停下，请使用者先配置，不猜。
2. 读 `roles/<你的角色>.md` 与 `roles/README.md`，按职责和禁止事项做。角色名严格使用名单里的值。
3. 调模型前查 `config/rules.json`：选引擎、模型与强度，遵守替补约定；格式见 `routing/FORMAT.md`，空白模板见 `routing/template.rules.json`。执行器自备；没有包装程序时直接按表选模型，并人工或用自有脚本核对跨家复审。
4. 按 `process/README.md` 进入派工、交接、复审及批准合并流程，记录任务编号、全部实际引擎与模型、产出及下一角色。复审按 `reviewers` 取全部作者允许列表的交集，再排除作者引擎。项目按采用记录生效后，结单前读 `process/ticket-closure.md`，按 L0 / L1 / L2 与卡型核对 DoD，在既有状态块和回执中记录终态；取消写裁决及去向，重复核对全量覆盖。
5. 改规矩走 `process/change-rules.md`，批准合并后按 `process/sync.md` 检查、备份、同步与抽查；配置版本取自 `config/CHANGELOG.md`。

## 四条底线

1. 复审不用作者用过的那家引擎，同一家换模型也不行，失败的作者运行同样计入。
2. 不自行合并自己的改动；任何合并均需使用者明确批准。
3. 改规矩走 `process/change-rules.md`，只改正本，批准合并后再同步抄本。
4. 密钥、令牌、登录内容与本机路径不进任何文件。

## 目录速查

- `roles/`：8 类角色职责、禁止事项与设定模板。
- `process/`：7 项流程，含统一任务关单标准 v1.0；按项目采用记录的生效日执行，不追溯历史已结任务单，运行失败与质量不过分别处理。
- `routing/`：规则表格式和空白模板。
- `examples/`：虚构团队、本地配置起点及完整设定示例。
- `scripts/check.sh`、`scripts/export.sh`：本地自检和通用包导出。
- `tools/run-done-watcher/`：可选完成通知组件；运行结束不代表复审通过。
