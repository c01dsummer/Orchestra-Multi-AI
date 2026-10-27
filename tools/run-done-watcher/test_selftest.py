"""原有离线测试及通用包的检查、导出回归。"""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest

from selftest import WatcherTest


ROOT = Path(__file__).resolve().parents[2]


class PackageTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="orchestra-package-")
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.repo = self.temp / "orchestra"
        self.repo.mkdir()
        for name in ("README.md", "SKILL.md", "CHANGELOG.md", "LICENSE", ".gitignore",
                     "roles", "process", "routing", "examples", "scripts", "tools", "docs", ".github"):
            source, target = ROOT / name, self.repo / name
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(source, target)
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    def run_command(self, *args, expected=0, cwd=None, env=None):
        result = subprocess.run(args, cwd=cwd or self.repo, env=env or self.env,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def check(self, expected=0, env=None):
        return self.run_command("bash", "scripts/check.sh", expected=expected, env=env)

    def copy_config(self):
        config = self.repo / "config"
        shutil.copytree(self.repo / "examples", config)
        return config

    def notify_fixture(self):
        notify = self.copy_config() / "notify"
        notify.mkdir()
        shutil.copy2(self.repo / "tools/run-done-watcher/config.example.json", notify / "routes.json")
        record = {"run_id": "demo-1", "agent": "示例助手甲",
                  "end_time": "2026-10-24T12:00:00+08:00", "exit_code": 0}
        (notify / "events.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        return notify

    def watcher(self, *extra, expected=0):
        return self.run_command(sys.executable, "-B", "tools/run-done-watcher/run-done-watcher",
                                "--ledger", "config/notify/events.jsonl", "--config",
                                "config/notify/routes.json", "--state-dir", "config/notify/state-dry",
                                "--once", *extra, expected=expected)

    def binding(self, notify):
        with contextlib.closing(sqlite3.connect(notify / "state-dry/state.sqlite3")) as db:
            return json.loads(db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()[0])

    def ci(self, expected=0, contract=None):
        # The workflow reads tracked files. Stage only this disposable fixture; never commit.
        if not (self.repo / ".git").exists():
            self.run_command("git", "init", "-q")
            self.run_command("git", "add", ".")
        workflow = (self.repo / ".github/workflows/format-check.yml").read_text(encoding="utf-8")
        script = textwrap.dedent(workflow.split("          python3 -I - <<'PY'\n", 1)[1]
                                 .rsplit("          PY", 1)[0])
        env = {**self.env, "ORCHESTRA_SCOPE": "public",
               "ORCHESTRA_PRIVATE_WORDS": self.env.get("ORCHESTRA_PRIVATE_WORDS", "package-" + "private-marker"),
               "ORCHESTRA_RULES_REQUIRED_FIELDS": json.dumps(contract if contract is not None else {
                   "routing/template.rules.json": ["/version", "/reviewers"]})}
        return self.run_command(sys.executable, "-I", "-B", "-c", script, expected=expected, env=env)

    def test_readme_dry_run_then_check_export_and_unpacked_check(self):
        notify = self.notify_fixture()
        self.watcher("--dry-run")
        self.watcher("--dry-run")  # The same normalized path still binds across restarts.
        binding = self.binding(notify)
        self.assertEqual(binding["schema"], 3)
        self.assertEqual(binding["ledger_sha256"],
                         hashlib.sha256(os.fsencode((notify / "events.jsonl").resolve())).hexdigest())
        self.assertNotIn(os.fsencode(self.repo), (notify / "state-dry/state.sqlite3").read_bytes())
        self.check()
        output = self.temp / "public.tar.gz"
        self.run_command("bash", "scripts/export.sh", str(output))
        with tarfile.open(output) as archive:
            self.assertTrue(all(n.startswith("orchestra/") and ".." not in Path(n).parts
                                and "config" not in Path(n).parts for n in archive.getnames()))
            for member in archive.getmembers():
                self.assertTrue(member.isfile())
                target = self.temp / "unpacked" / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source:
                    target.write_bytes(source.read())
                target.chmod(member.mode)
        self.run_command("bash", "scripts/check.sh", cwd=self.temp / "unpacked/orchestra")
        (notify / "other.jsonl").write_bytes(b"")
        self.watcher("--dry-run", "--ledger", "config/notify/other.jsonl", expected=1)

    def test_file_binding_hashes_outbox_and_rejects_changed_outbox(self):
        notify = self.notify_fixture()
        self.watcher("--transport", "file", "--outbox", "config/notify/outbox")
        binding = self.binding(notify)
        self.assertEqual(binding["outbox_sha256"],
                         hashlib.sha256(os.fsencode((notify / "outbox").resolve())).hexdigest())
        self.assertNotIn(os.fsencode(self.repo), (notify / "state-dry/state.sqlite3").read_bytes())
        self.watcher("--transport", "file", "--outbox", "config/notify/other-outbox", expected=1)

    def test_runtime_directories_and_files_are_skipped_but_sources_are_scanned(self):
        notify = self.copy_config() / "notify"
        notify.mkdir()
        machine_path = "/" + "var/local/example"
        for name in ("state", "state-dry", "state_test"):
            directory = notify / name
            directory.mkdir()
            (directory / "binding.json").write_text(machine_path, encoding="utf-8")
        for suffix in (".sqlite3", ".sqlite3-shm", ".sqlite3-wal", ".lock", ".jsonl", ".log"):
            (notify / ("runtime" + suffix)).write_text(machine_path, encoding="utf-8")
        self.check()
        self.run_command("bash", "scripts/export.sh", str(self.temp / "runtime.tar.gz"))
        source = notify / "stateful"
        source.mkdir()
        (source / "routes.json").write_text(json.dumps({"path": machine_path}), encoding="utf-8")
        self.assertIn("发现本机路径", self.check(expected=1).stderr)

    def test_all_author_roles_require_reviewer_keys(self):
        config = self.copy_config()
        path = config / "team.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for role in ("开发", "配置管理", "调研"):
            with self.subTest(role=role):
                team = json.loads(json.dumps(original))
                next(m for m in team["members"] if m["role"] == role)["engines"].append("engine-delta")
                path.write_text(json.dumps(team, ensure_ascii=False), encoding="utf-8")
                self.assertIn("作者角色的引擎缺少作者复审配对", self.check(expected=1).stderr)

    def test_review_only_engine_can_appear_only_as_reviewer(self):
        config = self.copy_config()
        team_path, rules_path = config / "team.json", config / "rules.json"
        team = json.loads(team_path.read_text(encoding="utf-8"))
        next(m for m in team["members"] if m["role"] == "复审")["engines"].append("engine-review-only")
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
        rules["reviewers"]["engine-alpha"].append("engine-review-only")
        team_path.write_text(json.dumps(team, ensure_ascii=False), encoding="utf-8")
        rules_path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
        self.check()

    def test_config_requires_nonempty_documents_and_each_member_profile(self):
        for relative in ("team.json", "rules.json", "projects.md", "CHANGELOG.md", "profiles/巡检员.md"):
            for empty in (False, True):
                with self.subTest(file=relative, empty=empty):
                    config = self.copy_config()
                    path = config / relative
                    if empty:
                        path.write_text(" \n", encoding="utf-8")
                    else:
                        path.unlink()
                    self.check(expected=1)
                    shutil.rmtree(config)
        for empty in (False, True):
            with self.subTest(directory="profiles", empty=empty):
                config = self.copy_config()
                shutil.rmtree(config / "profiles")
                if empty:
                    (config / "profiles").mkdir()
                self.check(expected=1)
                shutil.rmtree(config)

    def test_examples_require_every_member_profile(self):
        (self.repo / "examples/profiles/管理员.md").unlink()
        self.check(expected=1)

    def test_private_dictionary_can_be_extended_in_config_or_environment(self):
        config = self.copy_config()
        dictionary = config / "private-words.txt"
        marker = "extra-" + "private-marker"
        words = dictionary.read_text(encoding="utf-8") + marker + "\n"
        dictionary.write_text(words, encoding="utf-8")
        env = dict(self.env)
        env.pop("ORCHESTRA_PRIVATE_WORDS", None)
        self.check(env=env)
        env["ORCHESTRA_PRIVATE_WORDS"] = words
        self.check(env=env)
        (self.repo / "examples/leak.md").write_text(marker.upper(), encoding="utf-8")
        self.assertIn("公开部分命中禁用词", self.check(expected=1, env=env).stderr)

    def test_example_dictionary_still_checks_keys_and_machine_paths(self):
        dictionary = self.repo / "examples/private-words.txt"
        for content in ("/" + "etc/example", 'api_key = "' + "x" * 20 + '"'):
            with self.subTest(kind="path" if content.startswith("/") else "key"):
                dictionary.write_text(content, encoding="utf-8")
                self.check(expected=1)

    def test_export_rejects_unapproved_files_from_every_public_directory(self):
        expected = {"orchestra/" + p.relative_to(self.repo).as_posix()
                    for p in self.repo.rglob("*") if p.is_file()}
        output = self.temp / "whitelist.tar.gz"
        for directory in (".", "roles", "process", "routing", "examples", "examples/profiles",
                          "scripts", "tools", "tools/run-done-watcher", ".github", ".github/workflows"):
            with self.subTest(directory=directory):
                local = self.repo / directory / "local-auth.json"
                local.write_text(json.dumps({"token": "synthetic-local-value"}), encoding="utf-8")
                self.run_command("bash", "scripts/export.sh", str(output), expected=1)
                self.assertFalse(output.exists())
                local.unlink()
        nested = self.repo / "tools/unapproved-component"
        nested.mkdir()
        (nested / "README.md").write_text("local only", encoding="utf-8")
        self.run_command("bash", "scripts/export.sh", str(output), expected=1)
        self.assertFalse(output.exists())
        shutil.rmtree(nested)
        self.run_command("bash", "scripts/export.sh", str(output))
        with tarfile.open(output) as archive:
            self.assertEqual(set(archive.getnames()), expected)

    def test_ci_requires_template_contract_and_template_fields(self):
        self.ci()
        self.ci(expected=1, contract={"examples/rules.json": ["/version", "/reviewers"]})
        path = self.repo / "routing/template.rules.json"
        rules = json.loads(path.read_text(encoding="utf-8"))
        del rules["reviewers"]
        path.write_text(json.dumps(rules), encoding="utf-8")
        self.assertIn("规则表必填项缺失", self.ci(expected=1).stdout)

    def test_local_and_ci_reject_machine_roots_without_url_or_relative_path_false_positives(self):
        path = self.repo / "examples/projects.md"
        original = path.read_text(encoding="utf-8")
        safe = "\n".join(("[relative](../tmp/example.md)", "[relative](docs/var/example.md)",
                          "[relative](./etc/example.md)", "[relative](资料/opt/example.md)",
                          "https://example.invalid/tmp/example", "https://example.invalid/etc/example",
                          "https://example.invalid/proc", "a_root_word/procurement/example"))
        path.write_text(original + "\n" + safe, encoding="utf-8")
        self.check()
        self.ci()
        for base in ("home", "Users", "workspace", "root", "mnt", "Volumes",
                     "tmp", "var", "opt", "srv", "etc", "proc"):
            for tail in ("", "/example"):
                with self.subTest(root=base, tail=tail):
                    path.write_text(original + "\n`" + "/" + base + tail + "`\n", encoding="utf-8")
                    self.assertIn("发现本机路径", self.check(expected=1).stderr)
                    self.assertIn("发现本机路径", self.ci(expected=1).stdout)
