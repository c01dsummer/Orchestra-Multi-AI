#!/usr/bin/env bash
set -euo pipefail

if (( $# > 1 )); then
  printf '用法：bash scripts/export.sh [输出文件.tar.gz]\n' >&2
  exit 1
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
bash "$script_dir/check.sh"
python3 -I -B - "$script_dir/.." "${1:-orchestra-v1.tar.gz}" <<'PY'
import gzip
import os
from pathlib import Path
import runpy
import sys
import tarfile

root = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2])
# Every exported file is explicitly approved; never recurse through local directories.
public = ("README.md", "SKILL.md", "CHANGELOG.md", "LICENSE", ".gitignore",
          ".github/workflows/format-check.yml",
          "roles/README.md", "roles/调度.md", "roles/开发.md", "roles/复审.md", "roles/调研.md",
          "roles/平台对接.md", "roles/审批.md", "roles/用量.md", "roles/配置管理.md",
          "process/README.md", "process/dispatch.md", "process/handoff.md", "process/review.md",
          "process/merge.md", "process/change-rules.md", "process/sync.md",
          "routing/FORMAT.md", "routing/template.rules.json",
          "examples/README.md", "examples/team.json", "examples/rules.json", "examples/projects.md",
          "examples/CHANGELOG.md", "examples/private-words.txt",
          "examples/profiles/巡检员.md", "examples/profiles/代码员.md", "examples/profiles/把关员.md",
          "examples/profiles/检索员.md", "examples/profiles/联络员.md", "examples/profiles/核准员.md",
          "examples/profiles/记账员.md", "examples/profiles/管理员.md",
          "scripts/check.sh", "scripts/check.py", "scripts/export.sh",
          "tools/run-done-watcher/README.md", "tools/run-done-watcher/.gitignore",
          "tools/run-done-watcher/config.example.json", "tools/run-done-watcher/run-done-watcher",
          "tools/run-done-watcher/watcher.py", "tools/run-done-watcher/test_selftest.py",
          "tools/run-done-watcher/selftest.py", "tools/run-done-watcher/check-heartbeat.py",
          "tools/run-done-watcher/reconcile.py", "tools/run-done-watcher/reconcile.config.example.json")


def members():
    # Reuse the checker's runtime exclusions, and reject any unapproved source file.
    files_under = runpy.run_path(str(root / "scripts/check.py"))["files_under"]
    approved = set(public)
    if any(path.relative_to(root).as_posix() not in approved for path, _ in files_under(root)):
        raise ValueError()
    for entry in public:
        path = root / entry
        if (not path.is_file() or any(part.is_symlink() for part in (path, *path.parents))
                or not path.resolve().is_relative_to(root)):
            raise ValueError()
        yield path


created = False
try:
    if not output.name.endswith(".tar.gz"):
        raise ValueError()
    with output.open("xb") as target:
        created = True
        with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as archive:
                for path in members():
                    info = archive.gettarinfo(str(path), arcname="orchestra/" + path.relative_to(root).as_posix())
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                    with path.open("rb") as source:
                        archive.addfile(info, source)
    print("导出通过：已生成通用技能包；不含配置、版本库和运行产物。")
except FileExistsError:
    print("导出失败：输出文件已存在，请指定新文件名。", file=sys.stderr)
    raise SystemExit(1)
except Exception:
    if created:
        output.unlink(missing_ok=True)
    print("导出失败：请检查输出文件后缀、父目录、权限及公开文件；异常原文不输出。", file=sys.stderr)
    raise SystemExit(1)
PY
