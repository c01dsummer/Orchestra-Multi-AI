# 模型工具配置

三个入口需要 Bash 4.4+、Python 3（标准库）、jq、GNU coreutils 和常见工具 awk、sed、grep、find（还使用 cut、sort、paste 等 coreutils）；run-model 运行时也会用 Git 检测仓库以提供提交标签，提交功能另需 util-linux 的 setsid 和 Linux procfs。只有实际调用时才需要对应引擎 CLI。脚本直接从本目录运行；移动时须一起移动共享文件。

设置 `ORCHESTRA_CONFIG` 为 JSON 文件路径。未设置时从 PATH 找 claude、codex、dsh；日志使用 `$XDG_STATE_HOME/orchestra/runs`，未设置 XDG_STATE_HOME 时用当前目录的 `./model-runs`；规则表默认在日志目录的 rules.json。配置文件不存在或格式错误退出 2，帮助不加载配置。

配置优先级：命令行模型/强度 > engines 中的默认值 > 引擎默认。`ORCHESTRA_LOG_DIR`、`ORCHESTRA_RULES` 覆盖对应配置路径。空模型/强度与 null 均表示不传该参数。`--file` 可独立覆盖 run-estimate/check-run 的账本路径。

配置格式为 version=1 的 JSON 对象，字段见 examples/runtime.json：

| 字段 | 含义 |
| --- | --- |
| log_dir | 运行目录；每次运行子目录与 runs.jsonl 都在这里 |
| rules_file | 复审配对表；只有 review 且带 work-id 才读取 |
| timezone | 运行编号与账本时间的时区，默认 UTC |
| environment_defaults | 路径展开的环境变量默认值；进程已有变量优先，不修改进程环境 |
| engines.*.command | 非空字符串数组：可执行程序及其固定前置参数；经数组调用，无 shell 字符串拼接 |
| engines.*.model / effort | 未显式指定时使用的模型与强度；deepseek 不支持 effort |
| engines.claude.environment_script | 可选；真正调用前 source 的使用者信任脚本；可能执行代码，仅填写自己控制的文件。默认不读取登录文件 |
| engines.codex.sessions_dir | 可选；从匹配 thread id 的会话记录补充实际模型，默认关闭 |
| engines.deepseek.profile / provider / model_id | headless profile、模型补丁的 provider 和目标 id |
| agents | 供人阅读的虚构/私有名单；保留原程序行为，任意非空 --agent 都可记账 |

路径和 command 元素支持 `$VAR`、`${VAR}`，未定义变量报错。相对路径以配置文件所在目录为基准（无配置时以当前目录为基准）；command 的相对可执行路径由运行 cwd/PATH 解析，建议使用环境变量展开的绝对路径。不展开波浪号，不执行命令替换。配置不存密钥。认证交给引擎或预先准备的环境。

run-model 保留三家适配、目录授权、提示词三种输入、提交保护、记账字段与引擎退出码；配置默认模型/强度会记入 requested_*。显式 --model "" / --effort "" 取消配置默认值。deepseek 模型名接受服务支持的单行字符串，例如 deepseek-flash；写入补丁时转义，不内置固定名单。CLI 版本须与参数兼容。dsh 用显式 `--profile` 选择 profile，示例为 headless；provider/model_id 默认为占位符 my-provider/my-model，实际打补丁前应按自己 profile 配置替换。command/profile/provider/model_id 均可配置，公开示例不代表账户默认值。

目录不可进入、解析失败、结果为空或含换行时退出 2，不启动引擎；启动前再次 cd，失败立即退出。Claude 权限路径还拒绝通配符、圆/花括号、反斜线、逗号、竖线及制表符，防止字符串改变权限模式含义。Claude 始终使用 --permission-prompts none；默认允许 cwd 的 Read/Glob/Grep/Edit/Write，额外目录扩展这些文件工具的授权，--allow-web 独立追加 WebSearch/WebFetch。

**目录参数不是文件系统隔离。** Claude 保留的 Bash 白名单包含 git/node/npm/npx、mkdir/cp/diff/ls/cat、head/wc/find/rg/grep、sed/awk/chmod/rm/mv、tar/test/command/which、env -i/bwrap/bash/sh、python3/timeout/tee/uname。这些命令仍能访问运行用户有权访问的其他文件；--read-dir 只表示文件工具额外读授权，不能阻止 Bash 写入该目录或其他目录。Codex 的 --read-dir 只记账，--write-dir 传给其沙箱；DeepSeek 拒绝这两个参数。需要只读隔离时，由调用者使用外部 OS 沙箱限制进程权限。

复审兼容行为：仅同一 work_id 的 role=code 账本记录算作者（失败也算）；同家拒绝，复审方须出现在每家作者的配对列表中；无作者仅警告。role/work-id 仍可省略，role 也接受 other；规则 version 不检查，只检查 reviewers 为对象。它与 routing/FORMAT.md 第五节的严格契约有差距，未在搬迁中擅自收紧。routes 和任何临时策略均不自动执行；其他角色、未记账参与及父子工作仍需人工核对。

当引擎未输出有效 JSON（例如命令不存在）时，实际模型列表归一为 []，确保失败仍记账；这是对来源脚本空输出记账缺口的修正。

run-estimate 保留成功样本过滤、中位数/P80、少样本逐级放宽，退出码 0/1/2 不变。check-run 新增帮助和 --file；相对 output_path 以账本目录为准，绝对路径照旧；--recent 格式错误返回非零。账本和产出会包含提示词等使用者数据，不应加入公开仓库。

run-model 使用 umask 077，新建运行目录权限为 700，提示词、输出、账本等文件为 600；已有目录/文件权限不自动收紧，使用者应事先核对。账本目录创建或追加失败会明确报错并退出 1（错误包含引擎退出码与产物位置），引擎成功也不会掩盖记账失败。成功记账时仍返回引擎退出码。追加未提供并发事务保证；多进程写同一账本应由调用者串行调度。

离线回归：在仓库根目录运行 `PYTHONDONTWRITEBYTECODE=1 python3 scripts/selftest.py`。只使用临时目录与假引擎，不调用真实 CLI 或提交；CI 同样运行。覆盖目录错误、网页授权、配置覆盖、旧账本、估时边界、权限与文档示例。mode 000 用例需普通用户执行，不能用可绕过目录权限的 root。
