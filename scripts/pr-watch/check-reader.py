"""Exercise the existing dry-run reader with the synthetic collector's inputs."""

import hashlib
import json
from pathlib import Path
import platform
import sys
import unittest

config = Path(sys.argv[1]).resolve(strict=True)
inputs = json.load(sys.stdin)
sys.path.insert(0, str(config / 'tests'))
from support.watchdog import WatchdogFixture


class CollectorReaderTest(WatchdogFixture):
    def test_fixed_reader(self):
        self.fixture('fixture5_pr_security_circuit_breaker')
        snapshot = self.root / 'prs.json'
        state = self.root / 'state.json'
        reported = self.root / 'events.txt'
        repo_key = 'example/project#7'
        head, old_head = 'a' * 40, 'b' * 40
        event = f'example/project:PR#7:head={head}:comments:3456789012'
        reported.write_text(event + '\n')
        dry_runs = 0

        def fixture_files():
            return {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}

        def run(source, prior_heads):
            nonlocal dry_runs
            state.write_text(json.dumps({'consecutive_idle_windows': 0,
                                         'prs': {repo_key: {'round_count': len(prior_heads), 'heads': prior_heads}}}))
            snapshot.write_text(json.dumps(inputs[source]))
            before = fixture_files()
            result = self.call('--prs-json', str(snapshot), '--reported-pr-events', str(reported),
                               '--trusted-authors', 'reviewer', '--dry-run')
            self.assertEqual(fixture_files(), before)
            self.assertFalse(self.logs.exists())
            dry_runs += 1
            return result

        stale = run('old_review', [old_head])
        self.assertEqual(stale['runs']['unknown'], [])
        self.assertEqual(stale['prs']['items'][0]['action'], 'observe')
        self.assertNotIn(event, stale['prs']['unhandled_events'])
        self.assertIn(f'example/project:PR#7:head={head}:reviews:42', stale['prs']['unhandled_events'])

        current = run('current_review', [old_head])
        candidate = current['prs']['items'][0]
        self.assertEqual(candidate['action'], 'dry_run_preview')
        self.assertIsNone(candidate['idempotency_key'])
        self.assertIsNone(candidate['round'])

        duplicate = run('current_review', [old_head, head])
        self.assertEqual(duplicate['prs']['items'][0]['action'], 'idempotent_skip')

        incomplete = run('unknown', [old_head, head])
        self.assertEqual(incomplete['status'], 'degraded_unknown')
        self.assertEqual(incomplete['prs']['items'], [])
        self.assertFalse(incomplete['window_state']['can_pause_routine'])
        print(json.dumps({
            'reader_sha256': hashlib.sha256(self.script.read_bytes()).hexdigest(),
            'python_version': platform.python_version(),
            'old_review': stale['prs']['items'][0]['action'],
            'current_review': candidate['action'],
            'repeat': duplicate['prs']['items'][0]['action'],
            'incomplete': incomplete['status'],
            'reader_dry_runs': dry_runs,
            'fixture_files_identical': True,
            'run_logs_absent': True,
        }))


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0]])
