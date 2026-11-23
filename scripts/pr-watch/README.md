# PR 采集适配接口

这个接口采集一个已授权、固定 head 的开放 PR。第一条路径将 GitHub GraphQL 的 PR 元数据、提交检查汇总、PR 会话评论和已发起评审映射到对账器的输入格式。

## 调用和权限

`collectPullRequest(adapter, { grant, ref, signal })` 接收调用方根据现行批准解析出的 `grant`，以及本次要读取的 `ref`。授权需匹配 `pull-request:read`、平台、仓库全名、平台仓库 ID、PR 编号和完整 head SHA。构造函数中的平台绑定由部署方提供。`githubPullRequests({ tokenEnv, proxy, ca })` 固定访问 `https://api.github.com/graphql`，从指定环境变量读取凭据。凭据范围须允许读取该仓库、PR、会话评论、评审和该提交的检查事实。缺凭据、范围不符和 HTTP 401/403 返回 `DENIED`，能力未知返回 `UNKNOWN`。

适配器公开 `platform`、`sourceKind`、`capabilities` 和 `readPage(ref, cursors, { maxBytes, signal })`。每页返回 `reference`、`snapshot`、`pages` 和传输 `attempt`，或者返回 `reason` 和已观察的 `attempt`。合成实现 `loopbackPullRequests(origin)` 只接受显式回环地址，使用相同查询、响应解析和接口。适配器应在取消或超时时结束当前请求并交回已有事实。

`external.status` 是采集结果。`COMPLETE` 表示本次观察获得了完整快照，`UNKNOWN` 表示信息不足或有冲突。`readerInput` 独立供既有对账器判断。未知或拒绝时，`readerInput` 为 `{ "prs": null }`，现有 Reader 将其判为 `degraded_unknown`。合法 `readerInput` 也要继续经过现有可信作者及 head 规则。PR ready/merge、写工单、提交评审或付费执行各需原有权限。审批状态、候选结果和 HTTP 成功均仅作为相应事实。

## 分页、引用与历史

- 评论与评审独立推进游标。每连接每页最多 50 条，总计最多 32 次查询。验证游标向前、总数稳定、末页唯一条数完整，以及跨页 PR 身份、head 和汇总状态一致。GraphQL 返回 `errors`、页缺失、截断或内容冲突均返回 `UNKNOWN`。该接口观察分页期间的这些变化，GitHub 的多页查询本身没有在此验证为原子快照。
- 回执保留仓库和 PR 的原始 ID、链接、可信源所见 head、事件节点 ID、原链接、作者、发生时间和评审绑定的 head。Reader 使用链接里的十进制事件 ID，作为字符串保留原 `comments:<id>`、`reviews:<id>` 键和已有水位线。完全相同的重复事件合并，内容或身份冲突进入 `UNKNOWN`。已观察的前页引用与传输事实随失败保留。
- 当前 Reader 对草稿缺少独立处理，本适配拒绝将其投成开放 PR 快照。旧 head 的 PR 快照返回 `UNKNOWN`。旧 head 的历史评审保留，既有 Reader 决定对当前 head 的候选资格。
- 采集器本身无账本、状态或通知写入。执行侧沿用既有 PR 水位线、历史轮次、run/费用权威和事件修订流程。来源或权限变化应以原记录对账，重复读取无模型执行。

## 传输与证据

| 项 | 合同 |
| --- | --- |
| 发送 | Node `http`/`https` 原生 HTTP/1.1，每页新连接，一次固定 `query` POST。记录请求 `finish` 时相对单调时钟的 `requestFlushedMs`。该事实表示客户端完成交付给本地传输层，测试另由上游服务记录收到的请求。空值表示客户端未观察到完整交付。 |
| 首个响应 | 记录首个 HTTP 状态和相对时刻（含 1xx），另记最终状态和观测到的体字节数。HTTPS 代理 CONNECT 与 GraphQL HTTP 响应分别观察。 |
| 重试与重定向 | 客户端为零重试、零跳转。421、3xx、限流和断连结束当前观察，保留已有事实。既有调度退避由原消费者控制。 |
| 代理与 CA | 默认 HTTPS 使用环境中的 `HTTPS_PROXY`/`https_proxy` 与 `NO_PROXY`/`no_proxy`。显式 `proxy: 'direct'` 使用直连。TLS 校验始终开启，默认沿用 Node CA。部署方可额外提供 CA 内容。合成 HTTP 只支持回环直连。 |
| 期限、取消 | 默认 10 秒共用单调时限，覆盖分页、连接、响应和最终投影。可配置到 60 秒。外部取消销毁请求，保留先前观察，随后无自动发送。上游是否停止处理需由外部证据确认。 |
| 大小和解码 | 响应头最多 16 KiB，体每页最多 1 MiB（可下调）。同时检查 Content-Length 和流式长度。仅接受 identity 与严格 UTF-8 JSON。部分响应和超限返回 `UNKNOWN`。 |

回执中的 `sourceKind` 标记来源配置或合成测试。生产网络中代理的额外请求次数需独立观测。公开测试日志只含合成数据的计数、状态和时长。私有 `readerInput` 可能包含评论和评审正文，沿现行权限保存。

## 运行验收

需要 Node 24.5+ 和 OpenSSL。公共合成套件无需平台凭据。

```sh
node --test scripts/pr-watch/platform.test.mjs
```

组合用例通过显式路径接入同一版本的 Reader 与测试夹具。`PR_WATCH_READER_ROOT` 和 `PR_WATCH_READER_HARNESS` 分别指向夹具数据根和 Python 夹具入口。`PR_WATCH_READER_BINARY` 指向夹具调用的 Reader，用于校验回执中的摘要。

```sh
PR_WATCH_READER_ROOT=/path/to/fixture-root \
PR_WATCH_READER_HARNESS=/path/to/reader-fixture.py \
PR_WATCH_READER_BINARY=/path/to/reader \
PR_WATCH_READER_PYTHON=/usr/bin/python3.13 \
node --test scripts/pr-watch/platform.test.mjs
```

未配置夹具时组合用例报告 `NOT_RUN`。配置三个路径后，夹具通过标准输入接收合成快照，通过标准输出返回 JSON 回执。组合用例核对 Reader 摘要、四次 dry-run 和每次判定。回执记录实际 Python 版本、夹具字节是否一致，以及每次调用后有无创建运行日志目录。

## 任务、托管和复审组合

`createPlatformObserver({ bindings, adapters, sourceKind, workId, historicalIds, states })` 创建只读组合。调用方提供稳定工作 ID、已核定的历史别名与各平台状态映射。原任务 ID、链接、状态、负责人、项目与父项随回执保留。作者、费用和运行历史沿调用方已有记录保留。任务关系字段只用于记录关系。

三个端口各自需要匹配 `task:read`、`change:read` 或 `review:read` 的授权，绑定工作 ID、完整 head、提供方、安装、接口版本和对象引用。适配器声明 `sourceKind`、`source`、`enabled`、`capabilities`，并实现 `read({ ref, head, cursor, signal })`。读取回执包含 `status`、`source`、`ref`、`data`。`synthetic` 和 `configured` 必须与调用时声明一致。响应缺项、版本或能力未知、分页冲突、截止或取消返回 `UNKNOWN` 及具体原因。权限不足或平台拒绝返回 `DENIED`。合成任务与复审端口为进程内读取，HTTP 传输行为由上述托管适配器提供。

托管端口校验绑定的仓库 ID 和精确 head，检查记录逐项关联 head。复审端口保留复审者、版本、状态、发现及原始链接，单独标记现行 head 与可信复审者。最多读取 32 页，每页最多 50 条，核对总数与游标。相同事件可重复读取，内容冲突时保留已有观察并停止投影。组合调用仅观察外部事实。业务接受和模型执行沿用原有授权和消费入口。

复审的稳定身份由提供方、安装、集合和事件 ID 组成。历史按稳定身份和复审修订查重。键顺序变化和同一修订经兼容接口升级后仍是重复观察。新修订独立记录，旧修订留在历史中。同修订内容冲突或单次分页夹入两个修订时返回 `UNKNOWN`，保留已取得的观察。

`readerInputFor(workflow, pullRequestObservation)` 只在来源类型、提供方、仓库、PR 和 head 一致且两个观察完整时传递原 PR 快照。Reader 继续按自身规则核对可信作者、检查、复审和历史。

```sh
node --test scripts/pr-watch/platform.test.mjs scripts/pr-watch/workflow.test.mjs
```

使用上文的 `PR_WATCH_READER_*` 路径运行该命令，可在同一回放中核验实际 Reader。
