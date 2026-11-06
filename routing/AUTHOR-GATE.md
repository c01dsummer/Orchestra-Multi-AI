# 作者启动门禁契约（可选，策略版本 author-launch/v2）

本文写给**自备包装程序**的使用者：在发出模型请求之前做作者门禁，拒绝高风险任务的第二家作者，由首个写入者锁定写方家族，隔离探测运行，任何来源无法证明时默认拒绝；并规定启动许可、拒绝审计和交付证据的字段语义。

> **边界**
>
> - 本仓库只提供本契约、虚构测试向量 [`examples/author-gate/vectors.json`](../examples/author-gate/vectors.json) 和一个标准库参考判定 [`tools/author-gate-reference/`](../tools/author-gate-reference/README.md)。参考判定只做纯计算：不读写来源、不加锁、不调用模型，**不是执行器，也不是强制门禁**。
> - 强制点在使用者自备的私有执行器（包装程序）。只有执行器在每个入口都按本文持锁判定、落盘许可，门禁才算生效；文档、向量或参考测试通过，都不代表任何环境已经启用。
> - 没有包装程序时，本文不新增义务，仍按 [跨家复审](../process/review.md) 人工核对作者与复审引擎。实现本契约也不改变复审规则、合并批准或任何流程。
> - 引擎一律用占位：〈引擎A〉〈引擎B〉是成对的高风险家族，〈引擎C〉代表其他家；向量里分别对应虚构的 engine-alpha、engine-beta、engine-gamma。

## 一、要解决的问题

1. **第二家作者**：高风险任务一旦被成对的两家都写过，就没有合规的模型复审可用。会造成这种局面的启动必须在请求发出前拒绝。知情声明不是批准；替补和重试都按最终家族判断。
2. **写方锁**：第一个获准执行的非复审启动锁定写方家族；成功、失败、超时、空输出都不释放。
3. **探测隔离**：连通性或状态探测只能走当日、本助手、独立的临时编号，在中性目录里发固定的最小提示，不挂业务任务，不加载业务指令。
4. **失败默拒**：来源读不了、格式坏、锁取不到、隔离无法证明，一律拒绝，不猜。
5. **不恢复模型复审**：已经两家都写过的任务，门禁不会让它重新具备模型复审资格。

## 二、名词

- **家族**：同一家模型服务，与 [规则表格式](FORMAT.md) 中的「引擎」同义。同一家换模型、换凭据档位仍是同一家族。`human` 表示人工作者，只能来自受管登记。
- **受管登记 / 不受信登记**：受管登记随使用者的私有配置正本发布，可以写作者、parents 关系、高风险编号和写方锁。运行侧（如日志目录）的登记不受信，只能补 parents 关系与引擎作者；其中出现 `human` 或写方锁一律拒绝。
- **关联集**：本次任务编号，加上两份登记的 parents 关系双向、多跳连到的全部编号。
- **作者家族 A**：关联集内非复审运行记录的 `engine` 与 `actual_engine_families`（失败、空输出、超时都算），登记中的作者，以及**未作废的非复审许可**（在途、崩溃遗留都算）。
- **本次家族 E**：本次 `engine`，加上解析后模型名按受管表识别出的家族；识别不出的不计入。
- **高风险成对家族**：受管策略的 `high_risk_pair`，例如〈引擎A〉与〈引擎B〉。
- **临时编号**：没有正式任务时问答或探测用的编号，只对创建它的那一天、那个助手有效。经 parents 连到正式任务的临时编号按正式任务处理。
- **启动许可**：宿主在模型进程启动前写入许可账本的一行记录。它是作者与写方锁的证据，也是交付证据检查的依据。
- **知情声明**：形如 `〈任务编号〉:〈家族〉:〈角色〉` 的逐字确认，只防误操作，不是批准。

## 三、判定位置：两段式，同一判定函数

1. **早检**：在建运行目录、预占额度之前调用判定，只做拒绝和审计，不写许可。早检时模型可能尚未解析（为空），此时不判「无法证明家族」，留给权威许可重判。
2. **权威许可**：在宿主侧、模型命令行进程启动之前取全局排他锁（有限等待，超时 `AUTHOR-LOCK-BUSY`），在锁内重读全部来源、重算判定。通过后向许可账本追加**一行**许可，把文件与所在目录落盘（fsync），然后释放锁、启动进程。
   - 同一家族内的凭据或额度回退在签许可之前完成，许可记录最终档位。
   - 许可锁与额度账本锁不嵌套。
   - 隔离（沙箱）入口由宿主在沙箱启动前签许可并封存进运行上下文。沙箱内层**不签许可**，只核对封存许可与本次身份一致；缺失即 `AUTHOR-PERMIT-MISSING`。
3. **覆盖面**：经包装程序的每个入口（命令行、后台调度、助手封装、重试、预检）都必须走到上面两点。绕开包装程序直接调用模型不会产生许可，交付证据检查判失败（第十节）。
4. **崩溃**：写许可前崩溃，无许可也未启动；写入后、启动前崩溃，孤儿许可照样计作者、占锁，其中消费的例外不能再用。写入或落盘失败 `AUTHOR-PERMIT-WRITE`，账本读不了 `AUTHOR-PERMIT-READ`，格式坏 `AUTHOR-PERMIT-FORMAT`，都拒绝。
5. **事后家族不符**：运行后发现实际家族与许可不同，照实记录、不改写。作者集随之扩大，下次启动按本文被拒，交付证据检查失败。

## 四、判定顺序

参考判定按下列顺序检查，遇到第一个不满足的规则即拒绝；硬判定会列出全部违规码，首个为主码。私有实现可以多列码，但不得放行参考判定拒绝的情形。

1. 受管策略须完整有效，否则 `AUTHOR-POLICY`。请求缺字段、有未知字段或取值非法：`AUTHOR-LAUNCH-DENIED`。
2. 探测携带业务输入：`PROBE-INPUT`。
3. 用途字段（第六节）：`PROBE-PURPOSE-CONFLICT`、`PROBE-PURPOSE-AMBIGUOUS`。
4. 探测引用例外 `AUTHOR-EXCEPTION-PROBE`，复审引用例外 `AUTHOR-EXCEPTION-MISMATCH`；复审带探测标志 `PROBE-IDENTITY`，复审带知情声明也拒绝。
5. 读来源：
   - 全局锁取不到 `AUTHOR-LOCK-BUSY`；
   - 登记读不了 `AUTHOR-SOURCE-REGISTRY-READ`，结构坏 `AUTHOR-SOURCE-REGISTRY-FORMAT`；关联集内 parents 关系无效 `AUTHOR-SOURCE-RELATIONS`；关联集内作者或写方锁登记无效，或写方锁来自不受信登记，`AUTHOR-SOURCE-REGISTRY-ENTRY`；
   - 运行记录读不了 `AUTHOR-SOURCE-LEDGER-READ`，解码失败 `AUTHOR-SOURCE-LEDGER-FORMAT`，关联集内记录写着未知家族 `AUTHOR-SOURCE-LEDGER-ENTRY`；
   - 许可账本读不了 `AUTHOR-PERMIT-READ`，关联集内记录损坏 `AUTHOR-PERMIT-FORMAT`；
   - 不受信登记在关联集内写了 `human`：拒绝。
   - 关联集外的坏历史只告警跳过，不升级为全局拒绝。
6. 风险：关联集内任一编号登记为高风险，即按高风险。已登记高风险却自报 normal、复审未显式写风险、高风险复审不是成对家族：拒绝。
7. 复审：不抢锁、不计作者，交给复审门禁。复审家族属于作者家族、作者集为空，或不在全部作者的 `reviewers` 允许列表中时拒绝，与 [跨家复审](../process/review.md) 同一规则。放行的复审同样签许可，供交付证据检查。
8. 临时编号与探测身份、目录、用户级指令（第六节）：`PROBE-IDENTITY`、`PROBE-ISOLATION-CWD`、`PROBE-ISOLATION-UNVERIFIED`。
9. 引用例外时先核对例外（第七节）。例外不可用时，例外码为主码，其余硬判定码随后。
10. 硬判定（下方伪代码）。
11. 要求知情声明的正式任务，声明须逐字等于 `〈任务编号〉:〈家族〉:〈角色〉`。声明排在硬判定之后，不解除任何拒绝。
12. 权威许可阶段写许可失败：`AUTHOR-PERMIT-WRITE`。

```text
lock = 写方锁(A, 受管登记的写方锁)                  # none | locked(家族) | conflict | unknown
E    = {engine} ∪ {受管表识别出的解析后模型家族}
若 E 含两家及以上                                    → AUTHOR-FAMILY-PREFLIGHT
若 高风险，模型已解析却识别不出家族，且入口不能证明只连到 engine 这一家
                                                     → AUTHOR-FAMILY-UNPROVABLE
若 写方锁生效（writer_lock_scope 为 all，或本次为高风险）：
    lock 为 conflict                                 → AUTHOR-LOCK-CONFLICT
    lock 为 unknown                                  → AUTHOR-LOCK-UNKNOWN
    lock 为 locked(F)，engine ≠ F，且例外未带 lock_exception
                                                     → AUTHOR-LOCK-MISMATCH
若 高风险，成对家族 ⊆ A ∪ E，且没有核对通过的例外：
    成对家族 ⊆ A（已双作者，不因本次没有新增而放行）  → AUTHOR-LAUNCH-DUAL-EXISTING
    否则                                             → AUTHOR-LAUNCH-SECOND-FAMILY
```

### 诊断码

一次拒绝可以列出多个码，首个为主码。错误输出给出主码与固定说明，不输出来源内容或提示内容。拒绝后如实报告：不换入口、不换任务编号或临时编号、不换引擎、不改报角色或用途重试。

| 诊断码 | 含义 | 可由什么解除 |
| --- | --- | --- |
| `AUTHOR-FAMILY-PREFLIGHT` | 解析出的模型属于另一家 | 不可解除；改正引擎或模型 |
| `AUTHOR-FAMILY-UNPROVABLE` | 高风险，模型家族在请求前无法证明 | 不可解除；改用受管表内的模型名或只连一家的入口 |
| `AUTHOR-LOCK-CONFLICT` | 关联任务的写方锁登记互相冲突 | 负责人核实后修正登记 |
| `AUTHOR-LOCK-UNKNOWN` | 已有多家作者且没有核实登记 | 核实原写家，经批准登记写方锁 |
| `AUTHOR-LOCK-MISMATCH` | 写方锁属于另一家 | 带 `lock_exception` 的受管例外 |
| `AUTHOR-LAUNCH-DUAL-EXISTING` | 高风险关联集已有成对两家作者 | 先登记写方锁（否则同时报 `AUTHOR-LOCK-UNKNOWN`，例外不可用），再用受管例外；之后仍不恢复模型复审 |
| `AUTHOR-LAUNCH-SECOND-FAMILY` | 本次启动会让第二家成为高风险作者 | 受管例外；scope 为 all 时通常同时报 `AUTHOR-LOCK-MISMATCH`，须带 `lock_exception`；之后仍不恢复模型复审 |
| `AUTHOR-LOCK-BUSY` | 全局启动锁在有限等待内取不到 | 不可解除；稍后用同一身份重试 |
| `AUTHOR-PERMIT-READ`、`AUTHOR-PERMIT-WRITE`、`AUTHOR-PERMIT-FORMAT` | 许可账本或锁读不了、写不进或格式坏 | 不可解除；修复状态后用同一身份重试 |
| `AUTHOR-PERMIT-MISSING` | 隔离入口内层没有宿主封存的许可 | 不可解除；只能经宿主入口启动 |
| `AUTHOR-POLICY` | 受管策略缺失、版本不符或无效 | 修复受管策略 |
| `AUTHOR-SOURCE-REGISTRY-READ`、`AUTHOR-SOURCE-REGISTRY-FORMAT`、`AUTHOR-SOURCE-REGISTRY-ENTRY`、`AUTHOR-SOURCE-RELATIONS` | 登记读不了、结构坏，或关联集内的作者、写方锁、parents 关系无效 | 人工核对来源，不自动修复 |
| `AUTHOR-SOURCE-LEDGER-READ`、`AUTHOR-SOURCE-LEDGER-FORMAT`、`AUTHOR-SOURCE-LEDGER-ENTRY` | 运行记录读不了、解码失败，或关联集内记录写着未知家族 | 人工核对来源 |
| `PROBE-INPUT` | 探测携带业务输入 | 去掉业务输入，或不按探测运行 |
| `PROBE-IDENTITY` | 探测挂正式任务或关联临时编号，或角色不是 other（含复审） | 改用当日、本助手、独立的临时编号 |
| `PROBE-PURPOSE-CONFLICT` | 用途与探测标志矛盾，或取值非法 | 改正声明 |
| `PROBE-PURPOSE-AMBIGUOUS` | 没写用途、没带探测标志，任务标签却读作探测或状态检查 | 探测请带探测标志；业务排障请显式写 `purpose=work` |
| `PROBE-ISOLATION-CWD` | 探测目录不中性 | 不传目录，让运行器自建；或换成核验通过的目录 |
| `PROBE-ISOLATION-UNVERIFIED` | 用户级指令文件存在而本入口不能证明已遮蔽，或没有核查结果 | 改读本地状态，或用不发模型请求的用量工具 |
| `AUTHOR-EXCEPTION-SOURCE`、`AUTHOR-EXCEPTION-FORMAT`、`AUTHOR-EXCEPTION-NOT-FOUND`、`AUTHOR-EXCEPTION-REVOKED`、`AUTHOR-EXCEPTION-EXPIRED`、`AUTHOR-EXCEPTION-CONSUMED`、`AUTHOR-EXCEPTION-PROBE`、`AUTHOR-EXCEPTION-MISMATCH`、`AUTHOR-EXCEPTION-SCOPE`、`AUTHOR-EXCEPTION-INPUT` | 受管例外不可用，或不精确对应本次启动（第七节） | 另行批准并经受管发布流程写入 |
| `AUTHOR-LAUNCH-DENIED` | 没有专属码的其他拒绝：请求格式、复审门禁、临时编号身份、知情声明、风险冲突、不受信登记中的 `human` | 按说明改正 |

`missing_approval` 字段按主码给出解除所需的批准类型：`AUTHOR-LOCK-CONFLICT` 为 writer_lock_reconciliation，`AUTHOR-LOCK-UNKNOWN` 为 writer_lock_registration，`AUTHOR-LOCK-MISMATCH` 为 lock_exception，两个第二家码为 second_family_exception，其余为 null。

## 五、写方锁

- **派生，不另存锁值**：受管登记的写方锁优先（不受信登记中的写方锁一律拒绝）；否则取作者家族中唯一的引擎家族；没有引擎作者为未锁（`human` 不形成引擎锁）；多家且无核实登记为 unknown，阻塞待核，不猜。
- **首个许可即锁的证据**：首个非复审许可写入后锁即成立。失败、超时、取消、空输出都不释放。
- **作废**：只有宿主拿到确定性证据，证明模型进程从未启动，才追加 `{"type": "void", "reason": "never_started"}`；证据不足就保留。不提供人工解锁命令。
- **关联变化**：新的 parents 关系把两个不同家族的关联集连在一起时，锁变为 unknown 或 conflict，受影响的启动暂停，双方记录都保留。
- **作用范围** `writer_lock_scope`：`all` 为默认，所有风险等级都按锁拒绝；`high` 只对高风险按锁拒绝。在 all 下，跨家情形大多先被写方锁挡住，第二家规则仍作为独立判定和审计口径保留。是否改为 high 由使用者按自己的风险规则决定。
- **迁移**：派生规则本身就是迁移，不写任何记录。已有多家历史的任务启用后会被阻塞，直到负责人核实并经批准登记写方锁。
- **临时编号**同样受锁约束；撞锁只能等待或改读本地状态，不能换编号。

## 六、探测隔离

- **身份**：只接受当日、本助手、未关联正式任务的临时编号，角色 other。挂正式任务、经 parents 连到正式任务、角色不是 other（含复审）：`PROBE-IDENTITY`。过期或属于其他助手的临时编号，以及用 other 以外的非复审角色运行独立临时编号，按身份规则拒绝（`AUTHOR-LAUNCH-DENIED`），提示改用当日、本助手的编号，不自动改号，也不自动补关系。
- **输入**：拒绝提示文本、提示文件、非空或仍在等待的标准输入、提交、额外读写目录、联网和可写沙箱（`PROBE-INPUT`），只发送固定的最小提示。
- **用途字段** `purpose` 取 probe、status 或 work。带探测标志时只能是 probe 或 status（省略视为 probe），写 work 拒绝；不带探测标志却写 probe 或 status 拒绝；取值非法拒绝；以上均为 `PROBE-PURPOSE-CONFLICT`。没写用途、没带探测标志，而任务**标签**命中受管提示词表时为 `PROBE-PURPOSE-AMBIGUOUS`，对所有角色生效。标签先做 NFKC 归一与小写，把空白及 `_ - . / :` 归一为单个空格；ASCII 提示词按词边界匹配，其余按子串匹配。只看标签，不扫提示正文，提示词表也不是万能识别器。
- **中性目录由运行器强制**：不传目录时，运行器在受管位置自建权限 0700 的空目录，用完删除。自传目录必须同时满足：绝对路径；真实目录且路径上没有符号链接；属当前用户；权限 0700；为空；自身及全部祖先目录不含受管名单中的指令文件名。否则 `PROBE-ISOLATION-CWD`。名单由使用者按所用命令行工具的文档核定，向量里用的是虚构名字。
- **用户级指令**：所选命令行工具的用户级指令文件存在，而本入口不能证明已遮蔽（例如沙箱把这些位置换成空目录，并给工具私有配置目录）时为 `PROBE-ISOLATION-UNVERIFIED`；没有核查结果同样拒绝。不能带病放行，改读本地状态或用不发模型请求的用量工具。
- **顺序**：全部探测校验先于业务输入、写方锁与硬判定。
- **作者**：探测仍计入该临时编号的作者，受写方锁约束；撞锁拒绝，不改编号。
- **不承诺**：只保证中性目录与已知的指令文件，不承诺命令行工具零文件访问；固定提示也不能保证产不出实质内容。

## 七、受管例外

- **来源**只有受管配置中的例外文档，随配置正本发布，单次读取，拒绝符号链接。日志目录同名文件、命令行内联内容、环境变量都不是来源；命令行只能按 `approval_id` 引用。初次启用时文档为 `{"version": 1, "approvals": [], "revoked": []}`。
- **一条批准**恰好包含下列字段，不缺不多，不用通配或泛化值：
  - `approval_id`、`work_id`（规范编号）、`linked_ids_sha256`；
  - `existing_families`（启动前作者家族，升序去重）、`lock_family`（启动前写方锁家族，未锁为 null）、`new_family`、`role`（code 或 other）、`purpose`（只能是 work）；
  - `input_sha256`：本次提示字节的 SHA-256，改一个字节即不符；
  - `not_before`、`expires_at`：均带时区，前者早于后者；`max_uses`：只能是 1；
  - `approval_source`、`approval_digest`、`verified_by`：单行非空，摘要为 64 位小写十六进制；
  - `acknowledges`：恰为 `second_family_becomes_author` 与 `dual_author_blocks_model_review` 两项；
  - `lock_exception`：布尔值，为 true 才越过写方锁。
  - 文档另有 `revoked` 列表。
- **核对顺序**：引用或文档格式不符 `AUTHOR-EXCEPTION-FORMAT`；文档读不到 `AUTHOR-EXCEPTION-SOURCE`；没有该 id `AUTHOR-EXCEPTION-NOT-FOUND`；已撤销 `AUTHOR-EXCEPTION-REVOKED`；不在有效期或时间无法证明 `AUTHOR-EXCEPTION-EXPIRED`；已被任一许可引用（作废的也算）`AUTHOR-EXCEPTION-CONSUMED`；批准的输入等于固定探测提示的摘要或用途不是 work `AUTHOR-EXCEPTION-PROBE`；任务、新家族、角色、用途不符 `AUTHOR-EXCEPTION-MISMATCH`；关联集摘要、启动前作者家族或写方锁有变化（锁为 unknown 或 conflict 时例外不可用）`AUTHOR-EXCEPTION-SCOPE`；权威许可阶段提示摘要不符 `AUTHOR-EXCEPTION-INPUT`。
- **原子消费**：早检核对除提示摘要外的各项，不消费。权威许可在全局锁内复核，把 `exception_id` 写进同一条许可，即为消费：并发复用最多一个成功，崩溃遗留不会重复执行。
- **效果**：例外只放行这一次写作。用后关联集已有两家作者，非复审启动会被 `AUTHOR-LOCK-UNKNOWN`、`AUTHOR-LAUNCH-DUAL-EXISTING` 拒绝，高风险复审仍被复审门禁拒绝。**例外不恢复模型复审。**复审、探测与后台调度不接受例外。
- **信任边界**：执行器只校验批准的形状，以及它与本次启动是否精确对应，**不认证** `approval_source`、`approval_digest`、`verified_by` 的真伪；信任来自受管发布流程，即合并并经授权安装后的配置。同一用户任意改写本地配置，超出自校验能力。每一次第二家写作例外都应逐任务、逐次另行批准，并经受管发布流程写入。

## 八、启动许可

许可账本每行一条 JSON 记录。

| 字段 | 含义 |
| --- | --- |
| `type` | 固定为 permit |
| `permit_id` | 许可的唯一标识 |
| `run_id` | 本次运行标识；隔离入口记外层运行标识，内层运行须有可核对的对应记录 |
| `work_id` | 规范任务编号 |
| `linked_ids_sha256` | 关联集摘要：编号升序排成 JSON 数组（与参考实现 `linked_ids_sha256()` 相同）后的 SHA-256 |
| `authors_before` | 启动前的作者家族，升序 |
| `lock_before` | 启动前的写方锁：`{"state": "none/locked/conflict/unknown", "family": 家族或 null}` |
| `family` | 本次家族 |
| `role` | code、other 或 review |
| `risk` | normal 或 high |
| `auth_resolved` | 回退后最终凭据档位的标签，不含凭据 |
| `model` | 解析后的模型名，或 null |
| `policy` | 固定为 author-launch/v2 |
| `exception_id` | 消费的例外 id，或 null |
| `entry` | 签发入口的标签，由使用者定义（如宿主普通入口、隔离入口外层） |
| `issued_at` | 带时区的签发时间 |

- **作废记录**：`type` 为 void，另有 `permit_id`、`run_id`、`work_id`、`reason`（固定为 never_started）、`entry`、`voided_at`。void 只对在它之前出现的同 id 许可有效。
- **许可摘要** `permit_sha256`：许可记录规范 JSON（键排序、无多余空格、保留 UTF-8）的 SHA-256。运行记录写入 `launch_permit_id`、`launch_permit_sha256`，另带 `author_launch_policy`（值为 author-launch/v2）与 `purpose`。
- **只读核查**（可选）：列出没有运行结果、也未作废的许可，供负责人核对；只读，不修改。

## 九、拒绝审计

每次拒绝向拒绝审计文件（与许可账本、运行记录分开）追加一行：`ts`、`entry`、`run_id`、`work_id`、`engine`、`role`、`risk`、`linked_ids`、`authors_before`、`lock_before`、`code`、`codes`、`exception_id`。

- 不含提示内容或来源内容。
- 拒绝不写运行记录、不计作者、不留额度预占。
- 审计写不进去时仍然拒绝，并在错误输出中提示审计失败。沙箱内层不写审计。

## 十、交付证据

对 `author_launch_policy` 为 author-launch/v2 的运行记录，交付证据检查要求：

- 许可在同一许可账本中存在且唯一，`launch_permit_sha256` 等于许可摘要；
- `work_id`、`engine`（对应许可的 `family`）、`role` 一致，`run_id` 一致，或隔离入口经可核对的记录对应到外层许可；
- 同一许可只被一条运行记录引用；
- 许可未被作废（作废了却有运行记录，视为矛盾）；
- `actual_engine_families` 恰为 `[许可家族]`。

结论为 verified、invalid、missing 或 legacy。旧记录（无策略字段或旧版本）默认只标 legacy，不追溯判失败；交付入口使用严格模式，旧记录与无许可记录一律失败。人工合并、安装等只靠人记得检查的入口不受门禁强制，不能报成受保护。

## 十一、参考判定与虚构向量

在仓库根目录运行：

```bash
python3 -B tools/author-gate-reference/author_gate.py examples/author-gate/vectors.json
python3 -B -m unittest discover -s tools/author-gate-reference -v
```

- `evaluate(request, snapshot, policy, stage)` 返回判定、全部码、`missing_approval`、用途、关联集与作者等事实；`stage` 为 early 或 permit。`issue_permit(...)` 是权威许可的纯计算版本，返回许可记录与摘要，或拒绝审计记录。`permit_evidence(...)` 给出交付证据结论。输入输出字段见 [组件说明](../tools/author-gate-reference/README.md)。
- 参考判定只按调用方给出的快照与观测计算（例如目录权限、用户级指令是否存在），不核实观测真伪，也不提供锁和落盘。
- 向量分单条向量与串行序列。序列中权威许可按顺序写入，只模拟锁内串行；真实并发、崩溃和落盘须在私有执行器里用多进程测试。
- 私有执行器不应导入本仓库代码；用同构测试对照这些向量保证语义一致。两者不一致时须修正其一，不能用参考判定通过代替执行器测试。

## 十二、已知限制

- 指令文件名单、用户级指令位置与提示词表只是示例，须由使用者按所用工具核定。
- 本契约不能阻止同一用户直接改写本地配置或账本。
- 人工合并与安装入口不受本门禁强制。
- 写方锁 scope 为 all 时，已有多家历史的任务在启用后会被阻塞，直到核实登记。
