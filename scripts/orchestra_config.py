"""Shared non-secret runtime configuration; no credential files are loaded."""
import json
import os
from pathlib import Path
import re
import shlex
import sys


def load():
    source = os.environ.get("ORCHESTRA_CONFIG")
    config = {}
    base = Path.cwd()
    if source:
        file = Path(source).resolve()
        base = file.parent
        with file.open(encoding="utf-8") as stream:
            config = json.load(stream)
        if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1:
            raise ValueError("configuration version must be integer 1")
    environment = dict(os.environ)
    defaults = config.get("environment_defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("environment_defaults must be an object")
    for name, value in defaults.items():
        if not isinstance(value, str):
            raise ValueError("environment defaults must be strings")
        environment.setdefault(name, value)

    def expand(value):
        if not isinstance(value, str):
            raise ValueError("configuration values must be strings")
        def substitute(match):
            name = match[1] or match[2]
            if name not in environment:
                raise ValueError("unset configuration variable: " + name)
            return environment[name]
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", substitute, value)

    def path(value):
        result = Path(expand(value))
        return str(result if result.is_absolute() else base / result)

    state = os.environ.get("XDG_STATE_HOME")
    fallback = str(Path(state) / "orchestra" / "runs") if state else "./model-runs"
    log_dir = path(os.environ.get("ORCHESTRA_LOG_DIR") or config.get("log_dir", fallback))
    engines = config.get("engines", {})
    if not isinstance(engines, dict):
        raise ValueError("engines must be an object")
    output = {
        "LOGDIR": log_dir,
        "LOG": str(Path(log_dir) / "runs.jsonl"),
        "RULES": path(os.environ.get("ORCHESTRA_RULES") or config.get("rules_file", str(Path(log_dir) / "rules.json"))),
        "RUN_TIMEZONE": config.get("timezone", "UTC"),
        "CODEX_SESSIONS": path(engines.get("codex", {}).get("sessions_dir")) if engines.get("codex", {}).get("sessions_dir") else "",
        "CLAUDE_ENV_SCRIPT": path(engines.get("claude", {}).get("environment_script")) if engines.get("claude", {}).get("environment_script") else "",
        "DEEPSEEK_PROFILE": engines.get("deepseek", {}).get("profile", "headless"),
        "DEEPSEEK_PROVIDER": engines.get("deepseek", {}).get("provider", "my-provider"),
        "DEEPSEEK_MODEL_ID": engines.get("deepseek", {}).get("model_id", "my-model"),
    }
    for engine, command in (("claude", "claude"), ("codex", "codex"), ("deepseek", "dsh")):
        settings = engines.get(engine, {})
        if not isinstance(settings, dict):
            raise ValueError("engine settings must be objects")
        cmd = settings.get("command", [command])
        if not isinstance(cmd, list) or not cmd or any(not isinstance(x, str) or not x for x in cmd):
            raise ValueError("engine command must be a nonempty string array")
        output[engine.upper() + "_COMMAND"] = [expand(x) for x in cmd]
        for name in ("model", "effort"):
            output[engine.upper() + "_" + name.upper()] = settings.get(name) or ""
    for key, value in output.items():
        if isinstance(value, list):
            continue
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            raise ValueError("configuration fields must be single-line strings")
    # These values are embedded in a generated YAML patch or passed to the CLI.
    for key in ("DEEPSEEK_PROVIDER", "DEEPSEEK_MODEL_ID", "DEEPSEEK_PROFILE"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", output[key]):
            raise ValueError("invalid profile, provider or model id")
    return output


def main():
    try:
        values = load()
        for name, value in values.items():
            if isinstance(value, list):
                print(name + "=(" + " ".join(shlex.quote(x) for x in value) + ")")
            else:
                print(name + "=" + shlex.quote(value))
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print("orchestra: cannot load configuration: " + str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
