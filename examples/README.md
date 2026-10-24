# 虚构团队配置示例

这里的「星海协作组」、成员、项目和引擎均为虚构。`examples/` 对应本地 `config/` 的结构；在仓库根目录、尚无 `config/` 时运行：

```bash
cp -r examples config
```

`config/` 已被根目录 `.gitignore` 忽略。已有配置时先备份并比对，不重复执行复制命令。真实配置建议由使用者放在独立私有仓库管理，公开仓库只维护虚构示例。

| 文件 | 填写内容 |
|---|---|
| `team.json` | 顶层 `name` 为团队名，`members` 为非空成员列表；每人含唯一 `name`、与角色文件同名的 `role`、非空可用引擎数组 `engines`、非空职责 `description` |
| `rules.json` | 严格遵守 `routing/FORMAT.md`；`version` 是格式版本，`reviewers` 是硬约束，`routes` 按候选顺序路由 |
| `projects.md` | 项目编号、范围、承担角色与交付方式 |
| `CHANGELOG.md` | 配置内容版本与改动记录，与规则表格式版本分开 |
| `profiles/` | 按 `roles/` 模板填充后的完整设定；包含全部 8 名虚构成员，实际使用应为每名成员准备对应的 `<name>.md` 文件 |
| `private-words.txt` | 一行一个需要禁止进入公开内容的字面词，大小写不敏感 |

请在 `config/` 中替换引擎名、名单、项目和设定，确保规则表中的引擎与团队能力一致。虚构模型名同样不能直接用于真实调用。密钥、登录内容和本机路径不写入配置，执行器自行从环境获取秘密值。

完成后运行 `bash scripts/check.sh`，检查项目清单、配置改动记录和每名成员的完整设定均存在且非空。本地禁用词优先使用环境变量 `ORCHESTRA_PRIVATE_WORDS` 的多行文本，否则读取 `config/private-words.txt`。复制后可替换演示词或追加自定义词。公开示例文件 `examples/private-words.txt` 的内容豁免禁用词自匹配，其文件名、密钥形态与本机路径仍接受检查；其他公开文件照常扫描。
