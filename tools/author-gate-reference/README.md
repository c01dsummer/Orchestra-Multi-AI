# 作者启动门禁参考判定

[作者启动门禁契约](../../routing/AUTHOR-GATE.md)（策略版本 author-launch/v2）的标准库参考实现，配合虚构向量 [`examples/author-gate/vectors.json`](../../examples/author-gate/vectors.json) 使用。

- **纯计算**：只按调用方给出的快照计算判定、许可记录、拒绝审计记录与交付证据结论；不读写来源、不加锁、不落盘、不调用模型或网络。命令行入口只读取指定的向量文件。
- **不是执行器，也不是强制门禁**：门禁只在使用者自备的私有执行器按契约持锁判定、落盘许可时生效。私有执行器不应导入本目录代码，用同构测试对照向量即可。
- 需要 Python 3.10 或更新版本，仅用标准库。

在仓库根目录运行：

```bash
python3 -B tools/author-gate-reference/author_gate.py examples/author-gate/vectors.json
python3 -B -m unittest discover -s tools/author-gate-reference -v
```

第一条逐条输出「通过」或「不符」及不符的字段名，全部通过时退出码为 0，有不符为 1，文件无法读取或格式无效为 2；不输出向量内容。第二条为离线测试，CI 的「离线测试」任务同样运行。

## 接口

| 函数 | 作用 |
| --- | --- |
| `evaluate(request, snapshot, policy, stage="early")` | 一次启动的判定；`stage` 为 early（早检，例外不比对提示摘要）或 permit（权威许可） |
| `issue_permit(request, snapshot, policy, permit_id, at, entry)` | 权威许可的纯计算版本：放行时返回许可记录与 `permit_sha256`，拒绝时返回拒绝审计记录；不写任何文件 |
| `permit_evidence(row, rows, records, require=False, outer_run_id=None)` | 一条运行记录的交付证据结论：verified、invalid、missing 或 legacy，附错误说明 |
| `run_document(document)` | 逐条核对向量文件，返回每条的编号、是否通过与不符的字段名 |

入参不会被修改。所有拒绝都带固定诊断码，异常输入一律默拒，不抛出异常（`stage` 取值非法除外，那是调用方错误）。

## 输入

**policy**（受管策略，字段不缺不多，否则 `AUTHOR-POLICY`）

| 字段 | 说明 |
| --- | --- |
| `version` | 固定为 author-launch/v2 |
| `families` | 已知引擎家族；不含 `human` |
| `high_risk_pair` | 两个不同的成对家族 |
| `writer_lock_scope` | all 或 high |
| `model_families` | 解析后模型名到家族的受管表；表外视为无法识别 |
| `harness_families` | 只能连到一家的入口到家族的映射 |
| `default_harness` | 各家族未指定入口时的默认入口 |
| `ack_required` | 正式任务是否要求知情声明 |
| `reviewers` | 与规则表同义的复审配对，供复审门禁使用 |
| `probe` | `task_markers`（标签提示词表）、`instruction_files`（受管指令文件名单）、`input_sha256`（固定探测提示的摘要） |

**snapshot**（调用方读到的来源，省略的字段按空值处理）

| 字段 | 说明 |
| --- | --- |
| `registry` | 受管登记：`high_risk`、`parents`、`authors`、`writer_locks` |
| `log_registry` | 不受信登记，字段同上；其中的写方锁与 `human` 作者会被拒绝 |
| `runs` | 运行记录；读取 `work_id`、`engine`、`role`、`actual_engine_families`，其余字段忽略 |
| `permits` | 许可账本中的许可与作废记录 |
| `adhoc` | 临时编号表：`{编号: {"day": 当日标识, "agent": 助手}}`；表外编号视为正式任务 |
| `exceptions` | 受管例外文档，或 null |
| `unavailable` | 读不到或取不到的项：registry、runs、permits、launch_lock、exceptions、permit_write、refusal_audit |

**request**（本次启动；`engine`、`role`、`work_id` 必填，其余可省略，出现未知字段即拒绝）

| 字段 | 说明 |
| --- | --- |
| `engine`、`role`、`work_id` | 家族、角色（code、other、review）、规范任务编号 |
| `risk` | 自报风险 normal 或 high，可省略 |
| `model`、`harness` | 解析后的模型名与入口；早检时模型可为空 |
| `agent`、`today` | 发起助手与受管时区的当日标识，用于临时编号 |
| `ack` | 知情声明 |
| `probe`、`purpose`、`task` | 探测标志、用途、任务标签 |
| `business_input` | 是否携带提示文本、提示文件、非空标准输入、提交或额外授权 |
| `cwd` | 探测目录：null 表示由运行器自建；否则为调用方的核查结果 `absolute`、`real_directory`、`owned_by_caller`、`mode`、`empty`、`names_on_path` |
| `home` | 用户级指令核查结果：`instruction_files_present`、`entry_masks_home` |
| `exception_id`、`input_sha256`、`now` | 例外引用、本次提示字节摘要、带时区的当前时间 |
| `run_id`、`auth_resolved` | 写入许可记录的运行标识与最终凭据档位标签 |

## 输出

`evaluate` 返回 `allowed`、`code`（主码）、`codes`、`reason`、`missing_approval`、`purpose`、`affects_lock`（放行的非复审启动为 true）、`exception_id`、`work_id`、`engine`、`role`、`risk`、`linked_ids`、`authors_before`、`lock_before` 与 `warnings`（关联集外被跳过的坏历史）。

`AUTHOR-LAUNCH-DENIED` 的 `reason` 取值：request、snapshot、risk-required、risk-conflict、risk-invalid、review-ack、review-high-risk-family、review-author、review-no-author、review-pairing、human-author-untrusted、adhoc-identity、adhoc-role、ack，以及形状检查没有预见的异常输入 unexpected（同样默拒）。`PROBE-ISOLATION-CWD` 的 `reason` 指出未通过的核查项，`PROBE-PURPOSE-CONFLICT` 的 `reason` 指出矛盾类型。

## 向量格式

```text
format     固定为 author-gate-vectors/1
policy     共用的受管策略
vectors    [{id, covers, note, policy?（浅覆盖共用策略）, snapshot, stage?, permit_id?, request, expect}]
sequences  [{id, covers, note, policy?, snapshot, steps}]
```

`expect` 中出现的键逐一与判定结果比较；`permit` 只比较给出的许可字段，为 null 表示不应签发许可。序列的步骤共用一份快照：带 `request` 的步骤是一次启动，权威许可放行时把许可追加进快照；`{"run": 运行记录}` 追加运行记录；`{"void": {"permit_id", "work_id"}}` 追加作废记录。序列只模拟锁内串行，不代替真实并发或崩溃测试。
