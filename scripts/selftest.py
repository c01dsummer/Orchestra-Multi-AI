#!/usr/bin/env python3
"""Offline regression checks: temporary fixtures and fake engines only."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'


def main():
    with tempfile.TemporaryDirectory(prefix='.selftest-', dir=ROOT) as directory:
        temp = Path(directory)
        calls = temp / 'calls.jsonl'
        ledger = temp / 'ledger'
        config_file = temp / 'runtime.json'
        stub = temp / 'fake-engine.py'
        stub.write_text('''import json, os, sys
from pathlib import Path
engine, calls = sys.argv[1:3]
with Path(calls).open('a') as stream:
    stream.write(json.dumps({'args': sys.argv[3:], 'cwd': os.getcwd()}) + '\\n')
if engine == 'claude': print(json.dumps({'result': 'stub output', 'modelUsage': {'stub-model': {}}}))
elif engine == 'codex': print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'stub output'}}))
else: print(json.dumps({'type': 'final', 'text': 'stub output', 'model': 'stub-model'}))
sys.exit(7 if 'fail' in sys.argv else 0)
''')
        config = {'version': 1, 'log_dir': str(ledger),
                  'rules_file': str(ROOT / 'examples/review-policy.json'),
                  'engines': {e: {'command': [sys.executable, str(stub), e, str(calls)]}
                              for e in ('claude', 'codex', 'deepseek')}}
        env = dict(os.environ)
        for name in ('ORCHESTRA_LOG_DIR', 'ORCHESTRA_RULES', 'BASH_ENV', 'ALLOW_WEB'):
            env.pop(name, None)
        env.update(ORCHESTRA_CONFIG=str(config_file), PYTHONDONTWRITEBYTECODE='1',
                   GIT_CEILING_DIRECTORIES=str(ROOT))

        def invoke(command, expected=0, environment=None):
            result = subprocess.run([str(x) for x in command], cwd=temp,
                                    env=environment or env, text=True, capture_output=True)
            assert result.returncode == expected, (command, result.returncode, result.stderr)
            return result

        def call_count():
            return len(calls.read_text().splitlines()) if calls.exists() else 0

        def records():
            return [json.loads(line) for line in (ledger / 'runs.jsonl').read_text().splitlines()]

        def model(engine='claude', extra=(), expected=0, environment=None, prompt=True):
            config_file.write_text(json.dumps(config))
            command = [SCRIPTS / 'run-model', '--engine', engine, '--agent', 'demo-worker',
                       '--task', 'offline-test', '--cwd', temp]
            if prompt:
                command += ['--prompt', 'stub prompt']
            return invoke([*command, *extra], expected, environment)

        def refused(extra, environment=None, message=None):
            count = call_count()
            rows = len(records()) if (ledger / 'runs.jsonl').exists() else 0
            result = model(extra=extra, expected=2, environment=environment)
            assert call_count() == count, 'engine started after path failure'
            assert (len(records()) if (ledger / 'runs.jsonl').exists() else 0) == rows
            if message:
                assert message in result.stderr, result.stderr

        for script in ('run-model', 'check-run', 'orchestra-common.sh'):
            invoke(['bash', '-n', SCRIPTS / script])
        for script in ('run-estimate', 'orchestra_config.py', 'selftest.py'):
            compile((SCRIPTS / script).read_text(), script, 'exec')
        for script in ('run-model', 'run-estimate', 'check-run'):
            result = invoke([SCRIPTS / script, '--help'])
            assert result.stdout or result.stderr
        print('PASS syntax / three help entrypoints')

        for name in ('runs.jsonl', 'output.txt'):
            shutil.copyfile(ROOT / 'examples' / name, temp / name)
        estimate = invoke([SCRIPTS / 'run-estimate', '--engine', 'claude', '--file', temp / 'runs.jsonl'])
        assert '中位 40 秒' in estimate.stdout
        print(estimate.stdout.strip())
        recent = invoke([SCRIPTS / 'check-run', '--file', temp / 'runs.jsonl', '--recent', '3'])
        assert all(name in recent.stdout for name in ('demo-001', 'demo-002', 'demo-003'))
        check = invoke([SCRIPTS / 'check-run', '--file', temp / 'runs.jsonl', 'demo-001'])
        assert 'OK: output exists' in check.stdout
        print('PASS README offline fixtures / recent / relative output path')

        closed = temp / 'closed'
        closed.mkdir()
        closed.chmod(0)
        plain = temp / 'plain'
        plain.write_text('fixture')
        broken = temp / 'broken'
        broken.symlink_to(temp / 'missing')
        loop = temp / 'loop'
        loop.symlink_to(loop)
        try:
            for flag in ('--read-dir', '--write-dir', '--cwd'):
                for path in ('', temp / 'missing', plain, broken, loop, closed):
                    refused([flag, path], message=flag)
        finally:
            closed.chmod(0o700)
        print('PASS paths: read/write/cwd x empty/missing/file/broken-link/link-loop/mode-000; exit=2, no engine or ledger')

        # Inject local shell failures only for the target path, after script discovery.
        target = temp / 'target'
        target.mkdir()
        shell = temp / 'shell-fixture.sh'
        injected = dict(env, BASH_ENV=str(shell), TEST_TARGET=str(target))
        for body, message in (("return 1", 'cannot be resolved'), ("return 0", 'invalid path')):
            shell.write_text('pwd(){ if [ "$PWD" = "$TEST_TARGET" ]; then ' + body + '; else builtin pwd "$@"; fi; }\n')
            for flag in ('--read-dir', '--write-dir', '--cwd'):
                refused([flag, target], injected, message)
        marker = temp / 'cd-count'
        injected['TEST_MARKER'] = str(marker)
        shell.write_text('''cd(){
  if [ "${@: -1}" = "$TEST_TARGET" ]; then
    [ ! -f "$TEST_MARKER" ] || return 1
    : > "$TEST_MARKER"
  fi
  builtin cd "$@"
}
''')
        refused(['--cwd', target], injected, 'no longer accessible')
        print('PASS injected pwd failure/empty result x three flags; second cd failure: exit=2, no engine or ledger')

        for character in ('*', '?', '[', ']', '(', ')', '\\', ',', '{', '}', '|', '\t', '\n', '\r'):
            unsafe = temp / ('unsafe' + character)
            unsafe.mkdir()
            for flag in ('--read-dir', '--write-dir', '--cwd'):
                refused([flag, unsafe])
        link = temp / 'newline-target-link'
        link.symlink_to(temp / 'unsafe\n', target_is_directory=True)
        refused(['--read-dir', link], message='single-line')
        print('PASS Claude permission-pattern/control characters x three flags; newline symlink target refused')
        spaced = temp / 'valid space'
        spaced.mkdir()
        model(extra=['--read-dir', spaced, '--write-dir', target])
        last = records()[-1]
        assert last['cwd'] == str(temp) and last['read_dirs'] == [str(spaced)]
        assert last['write_dirs'] == [str(target)]
        args = json.loads(calls.read_text().splitlines()[-1])['args']
        assert '' not in args and not any(x in args for x in ('Read(//**)', 'Write(//**)'))
        print('PASS valid spaced path: exact nonempty grants and ledger paths')

        for directories in (False, True):
            for web in (False, True):
                extra = (['--read-dir', target] if directories else []) + (['--allow-web'] if web else [])
                model(extra=extra)
                args = json.loads(calls.read_text().splitlines()[-1])['args']
                assert args[args.index('--permission-prompts') + 1] == 'none'
                assert '--allowedTools' in args
                assert ('WebSearch' in args) == web and ('WebFetch' in args) == web
                assert ('--add-dir' in args) == directories
        print('PASS Claude directories x web four combinations: noninteractive, cwd grants, web independent')

        required = {'run_id', 'start_time', 'end_time', 'duration_s', 'agent', 'task', 'engine',
                    'requested_model', 'requested_effort', 'actual_models', 'actual_model_source',
                    'exit_code', 'cwd', 'prompt_sha256', 'prompt_head', 'prompt_path', 'output_path',
                    'read_dirs', 'write_dirs', 'sandbox', 'role', 'work_id', 'commits', 'commit_note'}
        for engine in ('claude', 'codex', 'deepseek'):
            config['engines'][engine].update(model='configured-model', effort='' if engine == 'deepseek' else 'high')
            model(engine)
            row = records()[-1]
            assert row['requested_model'] == 'configured-model'
            assert row['requested_effort'] == (None if engine == 'deepseek' else 'high')
            model(engine, ['--model', '', '--effort', ''])
            row = records()[-1]
            assert row['requested_model'] is None and row['requested_effort'] is None
            args = json.loads(calls.read_text().splitlines()[-1])['args']
            assert not any(x in args for x in ('--model', '-m', '--effort', '--patch'))
            assert not any(x.startswith('model_reasoning_effort=') for x in args)
            model(engine, ['--model', 'explicit-model', *([] if engine == 'deepseek' else ['--effort', 'max'])])
            row = records()[-1]
            assert row['requested_model'] == 'explicit-model'
            assert set(row) == required and row['commits'] is None and row['commit_note'] is None
            assert Path(row['output_path']).read_text().strip() == 'stub output'
            if engine == 'deepseek':
                patch = Path(row['prompt_path']).parent / 'model-patch.yml'
                assert 'provider: my-provider' in patch.read_text()
                assert 'model: "explicit-model"' in patch.read_text()
            config['engines'][engine].update(model=None, effort=None)
        model('deepseek', ['--model', 'model: "quoted" # value'])
        row = records()[-1]
        patch = (Path(row['prompt_path']).parent / 'model-patch.yml').read_text()
        assert json.loads(patch.split('    model: ', 1)[1]) == 'model: "quoted" # value'
        print('PASS three engine adapters / 24 fields / configured, explicit and empty CLI values / arbitrary escaped model patch')
        model('codex', ['--write-dir', target])
        assert records()[-1]['sandbox'] == 'workspace-write'
        model('codex', ['--write-dir', target, '--sandbox', 'read-only'], expected=2)
        model('deepseek', ['--read-dir', target], expected=2)
        model('deepseek', ['--sandbox', 'read-only', '--model', 'deepseek-flash'])
        print('PASS Codex write sandbox/conflict and DeepSeek directory refusal/sandbox/model example')

        work = 'demo-work'
        model(extra=['--role', 'code', '--work-id', work])
        before = call_count()
        model(extra=['--role', 'review', '--work-id', work], expected=2)
        assert call_count() == before
        model('codex', ['--role', 'review', '--work-id', work])
        model('codex', ['--role', 'code', '--work-id', work])
        model('deepseek', ['--role', 'review', '--work-id', work])
        model('deepseek', ['--role', 'code', '--work-id', work])
        before = call_count()
        for engine in ('claude', 'codex', 'deepseek'):
            model(engine, ['--role', 'review', '--work-id', work], expected=2)
        assert call_count() == before
        policy = temp / 'policy.json'
        policy.write_text(json.dumps({'reviewers': {'claude': ['deepseek']}}))
        config['rules_file'] = str(policy)
        model(extra=['--role', 'code', '--work-id', 'restricted'])
        model('codex', ['--role', 'review', '--work-id', 'restricted'], expected=2)
        config['rules_file'] = str(ROOT / 'examples/review-policy.json')
        print('PASS same-family refusal / one-two-three author intersection / pairing-table refusal')
        model(extra=['--role', 'review'])
        model(extra=['--role', 'review', '--work-id', 'unknown'])
        print('PASS review compatibility: missing work id or authors warns and runs')
        config['engines']['claude']['command'].append('fail')
        model(extra=['--role', 'code', '--work-id', 'failed-author'], expected=7)
        assert records()[-1]['exit_code'] == 7
        model(extra=['--role', 'review', '--work-id', 'failed-author'], expected=2)
        config['engines']['claude']['command'] = ['missing-offline-engine']
        model(expected=127)
        assert records()[-1]['exit_code'] == 127 and records()[-1]['actual_models'] == []
        config['engines']['claude']['command'] = [sys.executable, str(stub), 'claude', str(calls)]
        print('PASS engine exit=7/127 accounted; failed code run still counts as author')

        prompt_file = temp / 'prompt.txt'
        prompt_file.write_text('file prompt')
        model(extra=['--prompt-file', prompt_file], prompt=False)
        assert Path(records()[-1]['prompt_path']).read_text() == 'file prompt'
        config_file.write_text(json.dumps(config))
        result = subprocess.run([str(SCRIPTS / 'run-model'), '--engine', 'claude', '--agent', 'demo',
                                 '--task', 'stdin', '--cwd', str(temp)], input='stdin prompt', text=True,
                                capture_output=True, cwd=temp, env=env)
        assert result.returncode == 0 and Path(records()[-1]['prompt_path']).read_text() == 'stdin prompt'
        print('PASS prompt argument / file / stdin')

        old = temp / 'old.jsonl'
        old.write_text('\n'.join(json.dumps(row) for row in (
            {'run_id': 'old-missing', 'engine': 'claude'},
            {'run_id': 'old-null', 'actual_models': None, 'exit_code': 0, 'duration_s': 0})))
        old_result = invoke([SCRIPTS / 'check-run', '--file', old, '--recent', '2'])
        assert 'old-missing' in old_result.stdout and 'rc=?' in old_result.stdout
        assert 'old-null' in old_result.stdout and 'rc=0\t0s' in old_result.stdout
        print('PASS legacy recent: missing/null actual_models and missing rc/duration; zero preserved')
        boundary = temp / 'boundary.jsonl'
        for seconds in (59.4, 59.6, 60.0):
            boundary.write_text((json.dumps({'engine': 'claude', 'exit_code': 0, 'duration_s': seconds}) + '\n') * 3)
            result = invoke([SCRIPTS / 'run-estimate', '--engine', 'claude', '--file', boundary])
            assert ('预计不到 1 分钟' in result.stdout) == (seconds == 59.4)
            print(str(seconds) + ': ' + result.stdout.strip())
        print('PASS estimate rounding boundaries')

        # Parent's permissive mask must not expose newly created run data.
        mask = os.umask(0o022)
        try:
            model()
        finally:
            os.umask(mask)
        row = records()[-1]
        run_dir = Path(row['prompt_path']).parent
        assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in run_dir.iterdir() if p.is_file())
        assert stat.S_IMODE((ledger / 'runs.jsonl').stat().st_mode) == 0o600
        log = ledger / 'runs.jsonl'
        log.chmod(0o400)
        try:
            before = call_count()
            result = model(expected=1)
            assert call_count() == before + 1 and 'cannot append ledger (engine exit=0)' in result.stderr
        finally:
            log.chmod(0o600)
        # Engine can make the ledger parent unusable after successful startup.
        destructive_stub = temp / 'ledger-change.py'
        destructive_stub.write_text('''import pathlib, sys
p = pathlib.Path(sys.argv[1])
p.rename(p.with_name('saved-ledger'))
p.write_text('not a directory')
print('{"result":"stub output"}')
''')
        config['engines']['claude']['command'] = [sys.executable, str(destructive_stub), str(ledger)]
        try:
            result = model(expected=1)
            assert 'cannot create ledger directory' in result.stderr
        finally:
            ledger.unlink()
            (temp / 'saved-ledger').rename(ledger)
        print('PASS parent umask=022 -> directory=700/files=600; unwritable ledger/failed final mkdir -> exit=1')

        # Run the README command blocks verbatim in a disposable checkout without Git.
        checkout = temp / 'readme'
        checkout.mkdir()
        shutil.copytree(SCRIPTS, checkout / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'examples', checkout / 'examples')
        readme = (ROOT / 'README.md').read_text()
        blocks = [part.split('```', 1)[0] for part in readme.split('```bash\n')[1:]]
        assert len(blocks) == 2
        result = subprocess.run(['bash', '-euc', '\n'.join(blocks)], cwd=checkout, env=env,
                                text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        assert 'offline demo' in result.stdout and 'OK: output exists' in result.stdout
        print('PASS both README bash blocks verbatim: copy config -> fake command -> config env -> run -> ledger')
    print('ALL OFFLINE CHECKS PASSED (no real CLI or commit invoked)')


if __name__ == '__main__':
    main()
