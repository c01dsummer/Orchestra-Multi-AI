#!/usr/bin/env python3
"""仅依赖标准库的本地检查；错误信息不包含外部内容。"""

import glob
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent.parent
ROLES = ("调度", "开发", "复审", "调研", "平台对接", "审批", "用量", "配置管理")
PROCESSES = ("dispatch", "handoff", "review", "merge", "change-rules", "sync")
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules", "build", "dist"}


def fail(message):
    print("失败：" + message, file=sys.stderr)
    raise SystemExit(1)


def require(condition, message):
    if not condition:
        fail(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def engine(value):
    return nonempty(value) and not any(char.isspace() for char in value)


def load_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def constant(value):
        raise ValueError()

    try:
        # utf-8 intentionally rejects a BOM instead of silently stripping it.
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                          parse_constant=constant)
    except (OSError, UnicodeError, ValueError, RecursionError):
        fail("JSON 读取失败；请检查编码、重复键、特殊数值和语法。")


def core_files():
    files = ["SKILL.md", "README.md", "CHANGELOG.md", "LICENSE", ".gitignore",
             ".github/workflows/format-check.yml", "roles/README.md", "process/README.md",
             "routing/FORMAT.md", "routing/template.rules.json", "examples/README.md",
             "examples/team.json", "examples/rules.json", "examples/projects.md",
             "examples/CHANGELOG.md", "examples/private-words.txt",
             "examples/profiles/代码员.md", "examples/profiles/把关员.md",
             "scripts/check.sh", "scripts/check.py", "scripts/export.sh"]
    files += ["roles/" + role + ".md" for role in ROLES]
    files += ["process/" + process + ".md" for process in PROCESSES]
    files += ["tools/run-done-watcher/" + name for name in (
        "README.md", ".gitignore", "run-done-watcher", "check-heartbeat.py", "reconcile.py",
        "config.example.json", "reconcile.config.example.json", "selftest.py", "test_selftest.py")]
    directories = ("roles", "routing", "process", "examples", "examples/profiles",
                   "scripts", "tools/run-done-watcher")
    require(all((ROOT / path).is_file() for path in files)
            and all((ROOT / path).is_dir() for path in directories), "核心目录或文件缺失。")
    for name in ("check.sh", "export.sh"):
        require(os.access(ROOT / "scripts" / name, os.X_OK), "脚本缺少可执行权限。")
    print("通过：核心文件、8 类角色、6 项流程及辅助组件齐全。")


def rules(path, template=False):
    table = load_json(path)
    require(isinstance(table, dict)
            and not set(table) - {"version", "name", "description", "reviewers", "routes"},
            "规则表必须是格式版本 1 规定的对象。")
    require(type(table.get("version")) is int and table["version"] == 1,
            "规则表 version 必须是整数 1。")
    for field in ("name", "description"):
        require(field not in table or nonempty(table[field]), "规则表标题或说明必须是非空字符串。")
    reviewers = table.get("reviewers")
    require(isinstance(reviewers, dict) and bool(reviewers), "reviewers 必须是非空对象。")
    for author, allowed in reviewers.items():
        require(engine(author) and isinstance(allowed, list) and bool(allowed)
                and all(engine(value) for value in allowed), "复审配对须使用非空引擎名数组。")
        require(author not in allowed and len(allowed) == len(set(allowed)),
                "复审配对禁止自审或重复引擎。")
    if "routes" in table:
        require(isinstance(table["routes"], dict), "routes 必须是对象。")
        for task, route in table["routes"].items():
            require(nonempty(task) and isinstance(route, dict)
                    and not set(route) - {"description", "candidates"}, "路由字段或任务类型无效。")
            require("description" not in route or nonempty(route["description"]), "路由说明不能为空。")
            candidates = route.get("candidates")
            require(isinstance(candidates, list) and bool(candidates), "路由 candidates 必须是非空数组。")
            for candidate in candidates:
                require(isinstance(candidate, dict)
                        and not set(candidate) - {"engine", "model", "effort"}
                        and engine(candidate.get("engine")), "候选必须包含合法 engine。")
                require(all(field not in candidate or nonempty(candidate[field])
                            for field in ("model", "effort")), "候选模型或强度必须是非空字符串。")
    if not template:
        require(not re.search(r"〈[^〉]*〉", json.dumps(table, ensure_ascii=False)),
                "示例或本地规则表仍有未替换占位符。")
    return table


def team(path, table):
    data = load_json(path)
    require(isinstance(data, dict) and nonempty(data.get("name"))
            and isinstance(data.get("members"), list) and bool(data["members"]), "团队名称或成员列表无效。")
    names, available = set(), set()
    for member in data["members"]:
        require(isinstance(member, dict) and nonempty(member.get("name"))
                and member["name"] not in names and member.get("role") in ROLES
                and nonempty(member.get("description")), "成员名称、角色或职责无效或重名。")
        engines = member.get("engines")
        require(isinstance(engines, list) and bool(engines) and all(engine(item) for item in engines)
                and len(engines) == len(set(engines)), "成员引擎列表无效或重复。")
        if member["role"] == "开发":
            require(set(engines) <= table["reviewers"].keys(), "开发角色的引擎缺少作者复审配对。")
        names.add(member["name"])
        available.update(engines)
    referenced = set(table["reviewers"])
    referenced.update(value for values in table["reviewers"].values() for value in values)
    referenced.update(candidate["engine"] for route in table.get("routes", {}).values()
                      for candidate in route["candidates"])
    require(referenced <= available, "规则表引擎与团队可用引擎不一致。")
    require(not re.search(r"〈[^〉]*〉", json.dumps(data, ensure_ascii=False)), "团队名单仍有未替换占位符。")


def front_value(lines, key):
    matches = [(i, re.fullmatch(re.escape(key) + r":\s*(.*)", line))
               for i, line in enumerate(lines)]
    matches = [(i, match.group(1).strip()) for i, match in matches if match]
    if len(matches) != 1:
        return ""
    index, value = matches[0]
    if value in (">", ">-", ">+", "|", "|-", "|+"):
        parts = []
        for line in lines[index + 1:]:
            if line and not line[0].isspace():
                break
            parts.append(line.strip())
        return " ".join(parts).strip()
    if value.startswith("#") or value.casefold() in ("null", "~"):
        return ""
    return re.sub(r"\s+#.*$", "", value).strip("\"'").strip()


def reference_exists(reference):
    reference = unquote(reference.strip().removeprefix("<").removesuffix(">"))
    if not reference or reference.startswith("#") or urlsplit(reference).scheme:
        return True
    reference = reference.split("#", 1)[0].split("?", 1)[0]
    if reference.startswith("config/") and not (ROOT / "config").exists():
        reference = "examples/" + reference[len("config/"):]
    reference = re.sub(r"<[^>]+>|〈[^〉]+〉|\$\{[^}]+\}", "*", reference)
    for match in glob.glob(str(ROOT / reference)):
        path = Path(match).resolve()
        if path.is_relative_to(ROOT) and path.exists():
            return True
    return False


def skill():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    require(bool(lines) and lines[0] == "---" and "---" in lines[1:], "SKILL.md 缺少 front matter。")
    end = lines.index("---", 1)
    require(front_value(lines[1:end], "name") == "orchestra"
            and bool(front_value(lines[1:end], "description")), "SKILL.md 的 name 或 description 无效。")
    body = "\n".join(lines[end + 1:])
    refs = re.findall(r"!?\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)", body)
    refs += re.findall(r"^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)", body, re.M)
    refs += [ref for ref in re.findall(r"`([^`\n]+)`", body)
             if re.fullmatch(r"[^\s`]+\.(?:md|json|ya?ml|sh|py|txt|toml)(?:#[^\s]*)?|[^\s`]+/", ref)]
    prose = re.sub(r"!?\[[^\]]*\]\([^\n)]*\)", "", body)
    prose = re.sub(r"^\s*\[[^\]]+\]:[^\n]*", "", prose, flags=re.M)
    prose = re.sub(r"`[^`\n]+`|(?:https?://|mailto:)\S+", "", prose)
    refs += re.findall(r"(?<![A-Za-z0-9_/.:~%-])(?:[A-Za-z0-9_.-]+/)*(?:<[^>\n]+>|〈[^〉\n]+〉|[A-Za-z0-9_.-]+)\.(?:md|json|ya?ml|sh|py|txt|toml)\b", prose)
    require(all(reference_exists(ref) for ref in refs), "SKILL.md 引用的本地文件或目录不存在。")
    print("通过：技能入口 front matter 与全部本地引用有效。")


def files_under(directory):
    for current, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            require(not (Path(current) / name).is_symlink(), "检查范围含符号链接，请使用普通文件。")
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS
                         and not (Path(current) == ROOT and name == "config"))
        for name in sorted(files):
            if name == ".git" or name.endswith((".pyc", ".pyo", ".tar.gz", ".tmp", ".log")):
                continue
            path = Path(current) / name
            yield path, path.read_bytes()


def privacy():
    word_text, source = "", "none"
    if "ORCHESTRA_PRIVATE_WORDS" in os.environ:
        word_text, source = os.environ["ORCHESTRA_PRIVATE_WORDS"], "env"
    elif (ROOT / "config/private-words.txt").exists():
        word_text = (ROOT / "config/private-words.txt").read_text(encoding="utf-8")
        source = "config"
    words = [word.strip().casefold() for word in word_text.splitlines() if word.strip()]
    require(source == "none" or bool(words), "禁用词输入已提供但为空，请填写后重试。")
    key_patterns = [
        r'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----',
        r'\bgh[pousr]_' + r'[A-Za-z0-9]{20,}\b',
        r'\bgithub_' + r'pat_[A-Za-z0-9_]{20,}\b',
        r'\b(?:AKIA|ASIA)' + r'[A-Z0-9]{16}\b',
        r'\bsk-' + r'[A-Za-z0-9_-]{20,}\b',
        r'\bAIza' + r'[A-Za-z0-9_-]{30,}\b',
        r'\bxox[baprs]-' + r'[A-Za-z0-9-]{16,}\b',
        r'(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\b[\"\']?\s*[:=]\s*[\"\'][A-Za-z0-9+/=_-]{16,}[\"\']',
        r'(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\b\s*[:=]\s*[A-Za-z0-9+/=_-]{16,}(?=\s|$)',
    ]
    path_patterns = [
        r'(?:/home|/Users|/workspace|/root|/mnt|/Volumes)/[A-Za-z0-9_.-]+(?:/[^\s`\"\'<>]*)?',
        r'(?i)\b[A-Z]:[\\/][^\s`\"\'<>]+',
        r'~[\\/][^\s`\"\'<>]+',
        r'\\\\[A-Za-z0-9_.-]+\\[^\s`\"\'<>]+',
    ]
    keys = [re.compile(pattern) for pattern in key_patterns]
    paths = [re.compile(pattern) for pattern in path_patterns]
    generic = list(files_under(ROOT))
    for path, raw in generic:
        name = path.relative_to(ROOT).as_posix()
        text = raw.decode("utf-8", errors="replace")
        # A verbatim copied demonstration dictionary is input, not a private leak.
        # Environment-supplied words always scan the entire public content.
        word_content = "" if (source == "config" and name == "examples/private-words.txt"
                              and text == word_text) else text
        require(not any(word in name.casefold() or word in word_content.casefold() for word in words),
                "公开部分命中禁用词；内容不输出。")
    private = list(files_under(ROOT / "config")) if (ROOT / "config").is_dir() else []
    for path, raw in generic + private:
        text = path.relative_to(ROOT).as_posix() + "\n" + raw.decode("utf-8", errors="replace")
        require(not any(pattern.search(text) for pattern in keys), "发现密钥形态字符串；内容不输出。")
        require(not any(pattern.search(text) for pattern in paths), "发现本机路径；内容不输出。")
    if words:
        print("通过：公开部分未命中禁用词（环境变量优先，大小写不敏感）。")
    else:
        print("提示：未提供禁用词，仅跳过自定义词表扫描；可设置环境变量或创建配置词表。")
    print("通过：未发现密钥形态字符串或本机路径。")


def main():
    require(sys.version_info >= (3, 10), "需要 Python 3.10 或更新版本。")
    core_files()
    rules(ROOT / "routing/template.rules.json", template=True)
    team(ROOT / "examples/team.json", rules(ROOT / "examples/rules.json"))
    config = ROOT / "config"
    if config.exists():
        require(config.is_dir() and not config.is_symlink(), "config 必须是本地普通目录。")
        team(config / "team.json", rules(config / "rules.json"))
        print("通过：本地配置、虚构示例及空白模板的严格 JSON 校验。")
    else:
        print("提示：尚未创建 config/，本次校验示例；实际接活前请复制并修改配置。")
        print("通过：虚构团队、规则表示例及空白模板的严格 JSON 校验。")
    skill()
    privacy()
    print("自检全部通过：结构、JSON、技能入口及隐私检查完成。")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        fail("检查未完成，请检查文件、权限和运行环境；异常原文不输出。")
