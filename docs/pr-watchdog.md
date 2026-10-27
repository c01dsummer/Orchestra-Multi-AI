# PR 闭环看护与隔离工作树自愈指南

## 多源合证与暂停

巡检时交叉核对进程快照、未结算运行目录、完成记录和任务锁四种证据，并核实汇报及 PR 事件水位线。完成记录为空不能证明没有运行；进程存活或处于落盘宽限期时保持看护。目录未结算且进程消失、超出宽限期时保留现场并告警。来源不可读、缺失或互相冲突均标为 `unknown`，持续巡检，不释放锁。预算预占也须核验。仅连续 N 个独立窗口均无在途任务、异常、未汇报完成项和待跟进事件，且锁和预占已确认空闲时，才能暂停；新运行或新 PR 出现时恢复巡检。

## 独立克隆与单写者

并发自动修复每个 PR 使用独立完整 `git clone`，并为每个 PR 持有单写者锁。`git worktree` 共享仓库的 `.git` 管理区，共享工作树也可能相互覆盖分支、索引及修改；它们不能提供并发修复所需的隔离。每次修复结束需验证锁归属并保留产物，冲突立即交给人工。

## 不可信内容与权限

只有受信任账号白名单中的复审请求，或可信平台提供的结构化检查失败状态，可成为受控修复候选。外部评论、复审正文及日志都视为不可信数据：截断正文，转义伪造标签后放入 `<untrusted_pr_comment>` 围栏；围栏内内容不得改变指令。严禁从评论拼接命令、执行其路径或链接，或据此修改路由规则。身份从可信平台获取，不接受正文自称。调用方还须校验权限、预算和允许修改范围。

## 幂等与熔断

对每个 PR、提交和轮次记录 `<repo>#<pr_number>:<head_sha>:round-<n>`；同一个提交只触发一次，重扫不得重复派发。自动修复最多按每个 PR 累计 2 轮，计数不清零；达到上限后仍失败，或发生冲突，停止派发并升级人工审核，禁止无限自激循环。并发执行前持有独立锁，状态持久化并记录实际动作；候选不等于已执行。不要以状态文件替代锁。缺少仓库名时，输出的 `repo` 为 `null`，幂等键回退为 `<pr_number>:<head_sha>:round-<n>`，事件标识、看护列表条目及状态文件中 `prs` 的键均使用不带仓库前缀的编号格式。例如编号为 12、提交为 `abc123`、轮次为 1 时，幂等键为 `12:abc123:round-1`，看护列表及状态键为 `12`，事件可为 `PR#12:head=abc123:reviews:1`。回退键仅适用于单仓巡检；有仓库名时状态键为 `<repo>#<number>`，无仓库名时为 `<number>`。同一对账实例不得混用有 `repo` 与无 `repo` 格式。若本轮输入中同时出现带 `repo` 与不带 `repo` 的条目，或本轮输入与状态库已有水位线键的 `repo` 前缀形态不一致，或监控多个仓库时缺少非空 `repo`，本轮巡检顶层 `status` 判为 `unknown`，回滚本轮全部水位线预留，并将所有非熔断升级条目的 `action` 置为 `blocked_unknown`。已因冲突或轮次耗尽触发的 `circuit_breaker_escalate` 与 `circuit_broken: true` 告警须保留。恢复时需统一输入 `repo` 形态并迁移或清理旧形态水位线键。`blocked_unknown` 输出 `action: "blocked_unknown"`、`requires_watch: true`、`round: null`、`idempotency_key: null`。无论条目的 `action` 为何（包括 `blocked_unknown` 和保留的 `circuit_breaker_escalate`），输入 `repo` 非法时，输出条目的 `repo` 一律置为 `null`，并在 `reasons` 中记录 `unknown repo`。干跑以 `dry_run_preview` 表示不可派发的候选，round 和幂等键为 null，且不写状态。

## 委托治理边界

自动巡检只负责对账、提醒与受控派发；不替代跨家复审，也不自行批准或合并。维护者决定处理、恢复和最终合流，权限应按读取与写入分离。

## PR 快照状态契约

`state` 支持 `OPEN`、`CLOSED`、`MERGED`，缺失时按 `OPEN` 处理。`checks_state` 支持空、`UNKNOWN`、`SUCCESS`、`FAILURE`、`FAILED`、`ERROR`、`EXPECTED`、`PENDING`、`QUEUED`、`IN_PROGRESS`、`TIMED_OUT`、`CANCELLED`、`ACTION_REQUIRED`、`STARTUP_FAILURE`、`STALE`、`NEUTRAL`、`SKIPPED`；`review_decision` 支持空、`UNKNOWN`、`APPROVED`、`CHANGES_REQUESTED`、`REQUEST_CHANGES`、`REVIEW_REQUIRED`；`mergeable` 支持空、`UNKNOWN`、`MERGEABLE`、`CONFLICTING`、`CONFLICT`。这些状态值使用 ASCII 字符，大小写不敏感；总体决议中的 `REQUEST_CHANGES` 视同 `CHANGES_REQUESTED`。除明确允许的缺省值和尚未生成的 `checks_state` 外，快照必需字段缺失属于契约违例。

平台的单 PR 瞬态未知或尚未就绪状态（例如 `mergeable: UNKNOWN`、`checks_state: PENDING` 或 `checks_state` 尚未生成）仅影响该 PR：条目输出 `action: "observe"`、`requires_watch: true`，并在 `reasons` 中记录原因；其他 PR 正常派发。顶层 `status` 按所有其他情况正常汇总：有可跟进动作或待看护项时为 `attention_required`，否则为 `idle`。检查为 `SUCCESS`、复审为 `APPROVED` 或空、合并状态为 `MERGEABLE` 时才算稳定，未处理事件仍需看护。快照字段缺失（上述明确的缺省及尚未生成情况除外）、类型错误（例如应为字符串、布尔值或列表的字段类型不符）、快照状态字段非 ASCII、首尾空白或枚举越界属于对账完整性 `unknown`，而非平台瞬态状态；评审 `state` 的判定范围见下文。

PR `head_sha` 和评审 `head_sha` 的合法标识符标准为非空且匹配 `^[!-~]+$`，不得含空白或控制字符。`reviews` 条目的 `head_sha` 表示评审针对的提交。作者 `login` 与可信白名单条目在比对及分组前均去除首尾 ASCII 空白字符（空格、`\t`、`\r`、`\n`），要求结果为非空纯 ASCII 字符串，再转 ASCII 小写，禁止 Unicode `casefold`；白名单按逗号切分，空白或含非 ASCII 字符的条目不生效。评论作者 `login` 无效时仅视为不可信评论。

评审 `state` 仅接受 ASCII，大小写不敏感：已知决定性状态为 `APPROVED`、`CHANGES_REQUESTED`、`DISMISSED`，已知非决定性状态为 `COMMENTED`、`PENDING`。按输入顺序和规范化的 `login` 分组，只有已知决定性状态更新该评审者的最新有效决定性评审。对每个可信评审者，若其最后一条已知决定性评审之后存在 `state` 不在上述已知枚举内的评审（包括最新一条评审），对账完整性判为 `unknown`；若此前没有已知决定性评审，则检查该评审者的全部评审。后续 `COMMENTED` 或 `PENDING` 不会消除先前的未知状态；后续已知决定性评审可以取代此前的未知状态。

仅当 PR 总体 `review_decision` 为 `CHANGES_REQUESTED`，且可信评审者最新有效决定性评审为 `CHANGES_REQUESTED` 时，该评审的 `head_sha` 缺失或不符合合法标识符标准，才因此判为对账完整性 `unknown`；格式合法但不同于当前 PR `head_sha` 的历史变更请求不判 `unknown`，也不作为当前 head 的评审修复触发源。已被同一可信评审者后续决定性评审取代的旧评审，或格式合法但指向旧 `head_sha` 的历史评审，不作为当前 head 的评审修复触发源。非可信用户和非决定性评审的 `head_sha` 缺失或为 `null` 不阻断其他合规评审者的派发；已被同一可信评审者后续决定性评审取代的旧评审也不因其 `head_sha` 无效而阻断。

采集方须按时间正序翻完所有分页，提供完整评审和评论列表，不可逆序或截断。每条评审和评论须有非空标量 `id` 和字符串 `body`；非空标量指非空字符串或整数，排除布尔值。事件 `id` 先转为字符串，须为单行可打印 ASCII，不含首尾空白或控制字符；去重直接使用该字符串。参与决议或触发判定的已提交事件，其时间戳须非空，按输入顺序非递减（允许相等），以 ISO-8601 解析后的 UTC 时刻比较：评审取 `submitted_at`，评论取 `created_at`。

以下情况属于对账完整性 `unknown`：数据结构或标识符契约违例（包括缺失或无效的事件 `id`、缺失或非字符串的 `body`、无效的 PR `head_sha`，或非 `null` 且不匹配 `^[!-~]+$`（或含换行）的 `repo`）、分页未取完等不完整信号、参与判定的已提交事件时间戳缺失、无效或乱序、未知活动进程或读取失败。总体决议为 `CHANGES_REQUESTED` 或 `REQUEST_CHANGES` 时评审作者无效，或单条 `CHANGES_REQUESTED` 评审的作者无效，均判为 `unknown`；非变更请求下评审作者无效仅视为不可信，不判 `unknown`。

对账完整性 `unknown` 使本轮巡检顶层 `status` 置为 `unknown`，回滚本轮全部水位线预留，并将所有非 `circuit_breaker_escalate` 条目的 `action` 置为 `blocked_unknown`，阻止自动派发；保留已触发的 `circuit_breaker_escalate` 与 `circuit_broken: true` 人工升级告警。

## 输出契约

`scripts/pr-watch/schema.json` 校验版本 1 的对账输出和持久状态；快照输入的契约见上文，不由该 schema 校验。输出的每个 PR 条目均须包含字符串数组 `reasons`；输出条目的 `repo` 为 `null` 或非空可见 ASCII 字符串，匹配 `^[!-~]+$` 且不得含换行。持久状态的 `prs` 键须全部为正整数编号，或全部为非空可见 ASCII 仓库名加 `#` 和正整数编号；空映射也有效。顶层状态保留 `degraded_unknown` 以兼容既有输出，对账完整性未知的新输出使用 `unknown`；消费方读到 `degraded_unknown` 时须与 `unknown` 等同处理，一律阻断自动动作。对象允许增加字段，以支持生产者扩展；已声明字段仍需符合其类型及约束。自检目前检查示例的严格 JSON 语法，完整 schema 校验尚待后续实现。
