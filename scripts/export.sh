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
import sys
import tarfile

root = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2])
# Explicit public roots prevent unrelated local files from entering the archive.
public = ("README.md", "SKILL.md", "CHANGELOG.md", "LICENSE", ".gitignore",
          "roles", "process", "routing", "examples", "scripts", "tools", ".github")
excluded = {".git", "config", "private", "__pycache__", ".pytest_cache", ".venv",
            "node_modules", "build", "dist"}


def members():
    for entry in public:
        base = root / entry
        if base.is_file():
            yield base
            continue
        for current, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in excluded and not name.startswith("state"))
            for name in sorted(files):
                path = Path(current) / name
                if (name in excluded or name.startswith(".env")
                        or name.endswith((".pyc", ".pyo", ".tmp", ".log", ".tar.gz", ".jsonl", ".sqlite3"))):
                    continue
                if path.is_symlink() or not path.is_file():
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
