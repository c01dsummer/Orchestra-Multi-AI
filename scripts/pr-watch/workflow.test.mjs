import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createPlatformObserver } from './workflow.mjs';

const head = 'a'.repeat(40);
const old = 'b'.repeat(40);
const history = [
  { workId: 'ADHOC-20261020-CODEX-X', kind: 'run', id: 'run-1', cost: 'UNKNOWN' },
  { workId: 'WORK/42', kind: 'author', family: 'codex' },
  { workId: 'WORK/17', kind: 'run', id: 'different' },
];

function scenario(taskProvider = 'task-a', platformState = 'Doing') {
  const bound = (provider, id) => ({ source: { provider, installation: 'fixture', version: 'fixture-1' },
    ref: { id, url: `https://${provider}.invalid/objects/${id}` } });
  const bindings = {
    task: bound(taskProvider, taskProvider === 'task-a' ? 'A-42' : 'record_42'),
    change: { ...bound('forge', 'pr-42'), repositoryId: 'repo-1' },
    review: { ...bound('reviewer', 'review-collection'), reviewerIds: ['peer-1'] },
  };
  const reads = [];
  const rows = [{ id: 'event-1', revision: 'r1', changeId: 'pr-42', head: old, reviewer: 'peer-1',
    state: 'changes_requested', url: 'https://reviewer.invalid/events/1',
    findings: [{ id: 'finding-1', severity: 'major', url: 'https://reviewer.invalid/findings/1' }] },
  { id: 'event-2', revision: 'r1', changeId: 'pr-42', head, reviewer: 'peer-1', state: 'approved',
    url: 'https://reviewer.invalid/events/2', findings: [] }];
  const data = {
    task: { state: platformState, owner: 'owner', project: 'project', parent: 'group-1' },
    change: { repositoryId: 'repo-1', head, checks: [{ id: 'check-1', head, state: 'success' }] },
    review: { records: [rows[0]], next: 'next', total: 2 },
  };
  const lastPage = { records: [...rows], next: null, total: 2 };
  const grants = Object.entries(bindings).map(([port, binding]) => ({ ...binding, operation: `${port}:read`, workId: 'WORK/42', head }));
  const adapters = Object.fromEntries(Object.entries(bindings).map(([port, binding]) => [port, {
    enabled: true, sourceKind: 'synthetic', source: { ...binding.source }, capabilities: [`${port}:read`],
    read({ cursor }) {
      reads.push([port, cursor]);
      return { status: 'COMPLETE', ...binding, data: port === 'review' && cursor !== null ? lastPage : data[port] };
    },
    write() { assert.fail('workflow invoked a write'); },
    run() { assert.fail('workflow invoked a model'); },
  }]));
  return { bindings, adapters, rows, data, lastPage, grants, reads,
    observe(options = {}) {
      return createPlatformObserver({ bindings, adapters, sourceKind: 'synthetic', workId: 'WORK/42', historicalIds: [history[0].workId],
        states: { Doing: 'in_progress', active: 'in_progress', Closed: 'done' } })({ head, grants, history, ...options });
    } };
}

test('three synthetic providers compose without granting writes and replays keep history', async () => {
  const fixture = scenario();
  const initial = await fixture.observe();
  assert.equal(initial.external.status, 'COMPLETE');
  assert.deepEqual(fixture.reads, [['task', null], ['change', null], ['review', null], ['review', 'next']]);
  assert.deepEqual(initial.history, history.slice(0, 2));
  assert.equal(initial.facts.task.state, 'in_progress');
  assert.equal(initial.facts.task.parent, 'group-1');
  assert.equal(initial.facts.change.checks[0].head, head);
  assert.equal(initial.facts.reviews[0].atCurrentHead, false);
  assert.equal(initial.facts.reviews[0].findings[0].id, 'finding-1');
  assert.equal(initial.facts.reviews[1].state, 'approved');
  assert.equal(initial.facts.reviews[1].trustedReviewer, true);
  assert.equal(Object.hasOwn(initial, 'approved'), false);
  assert.equal(initial.newReviews.length, 2);
  const replay = await fixture.observe({ seenReviews: initial.newReviews });
  assert.equal(replay.external.status, 'COMPLETE');
  assert.deepEqual(replay.newReviews, []);
  assert.deepEqual(replay.history, initial.history);
});

test('alternative platform identifiers and status dictionaries retain the same work and history', async () => {
  const first = await scenario().observe();
  const second = await scenario('task-b', 'active').observe();
  assert.notEqual(first.facts.task.ref.id, second.facts.task.ref.id);
  assert.equal(second.facts.task.state, first.facts.task.state);
  assert.deepEqual(second.identity, first.identity);
  assert.deepEqual(second.history, first.history);
});

test('binding capability and grant are independent of facts and review approval', async () => {
  const cases = [
    [f => { f.grants[0].operation = 'task:write'; }, 'DENIED', 'NOT_AUTHORIZED', 0],
    [f => { f.grants[0].head = old; }, 'DENIED', 'NOT_AUTHORIZED', 0],
    [f => { f.grants[0].source = { ...f.grants[0].source, installation: 'other' }; }, 'DENIED', 'NOT_AUTHORIZED', 0],
    [f => { f.adapters.task.enabled = false; }, 'UNKNOWN', 'ADAPTER_DISABLED', 0],
    [f => { f.adapters.task.capabilities = []; }, 'UNKNOWN', 'CAPABILITY_UNKNOWN', 0],
    [f => { f.adapters.task.sourceKind = 'configured'; }, 'UNKNOWN', 'UNKNOWN_SOURCE_OR_VERSION', 0],
    [f => { f.adapters.task.source.version = 'next'; }, 'UNKNOWN', 'UNKNOWN_SOURCE_OR_VERSION', 0],
    [f => { f.adapters.task.read = () => ({ status: 'DENIED' }); }, 'DENIED', 'PLATFORM_DENIED', 0],
    [f => { f.data.change.repositoryId = 'rogue'; }, 'UNKNOWN', 'UNKNOWN_REPOSITORY', 2],
    [f => { f.data.change.head = old; }, 'UNKNOWN', 'STALE_HEAD', 2],
    [f => { f.data.change.checks[0].head = old; }, 'UNKNOWN', 'UNKNOWN_CHECKS', 2],
    [f => { f.data.task.state = 'unregistered'; }, 'UNKNOWN', 'UNKNOWN_TASK_STATE', 1],
    [f => { f.lastPage.records = [f.rows[0]]; }, 'UNKNOWN', 'INCOMPLETE_REVIEW_PAGES', 4],
    [f => { f.lastPage.next = 'next'; }, 'UNKNOWN', 'REVIEW_CURSOR_LOOP', 4],
    [f => { f.lastPage.records = [{ ...f.rows[0], state: 'approved' }, f.rows[1]]; }, 'UNKNOWN', 'CONFLICTING_REVIEW', 4],
  ];
  for (const [modify, status, reason, count] of cases) {
    const fixture = scenario();
    modify(fixture);
    const outcome = await fixture.observe();
    assert.equal(outcome.external.status, status, reason);
    assert.equal(outcome.external.reason, reason);
    assert.equal(fixture.reads.length, count, reason);
    assert.equal(outcome.facts, null);
    assert.deepEqual(outcome.newReviews, []);
    assert.deepEqual(outcome.history, history.slice(0, 2));
  }
});

test('untrusted reviewer and old-head approval are retained as facts', async () => {
  const fixture = scenario();
  fixture.rows[1].reviewer = 'unknown-reviewer';
  fixture.rows[1].head = old;
  const outcome = await fixture.observe();
  assert.equal(outcome.external.status, 'COMPLETE');
  assert.equal(outcome.facts.reviews[1].atCurrentHead, false);
  assert.equal(outcome.facts.reviews[1].trustedReviewer, false);
  assert.equal(outcome.facts.reviews[1].state, 'approved');
});

test('mismatched response provenance and conflicting historical replay fail closed', async () => {
  const forged = scenario();
  const reply = forged.adapters.task.read;
  forged.adapters.task.read = request => ({ ...reply(request), ref: { id: 'forged', url: forged.bindings.task.ref.url } });
  assert.equal((await forged.observe()).external.reason, 'UNKNOWN_SOURCE_OR_VERSION');

  const fixture = scenario();
  const first = await fixture.observe();
  fixture.rows[0].state = 'approved';
  const attempted = await fixture.observe({ seenReviews: first.newReviews });
  assert.equal(attempted.external.reason, 'CONFLICTING_REVIEW');
  assert.equal(first.newReviews[0].state, 'changes_requested');
});

test('durable review history survives reordering, adapter upgrades and new revisions', async () => {
  const fixture = scenario();
  fixture.rows[0].findings.push({ id: 'finding-2', severity: 'minor', url: 'https://reviewer.invalid/findings/2' });
  const first = await fixture.observe();
  const persisted = JSON.parse(JSON.stringify(first.newReviews, (_, value) => value && typeof value === 'object'
    && !Array.isArray(value) ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))) : value));
  persisted[0].findings.reverse();
  const repeat = await fixture.observe({ seenReviews: persisted });
  assert.equal(repeat.external.status, 'COMPLETE');
  assert.deepEqual(repeat.newReviews, []);

  fixture.bindings.review.source.version = 'fixture-2';
  fixture.adapters.review.source.version = 'fixture-2';
  const upgraded = await fixture.observe({ seenReviews: persisted });
  assert.equal(upgraded.external.status, 'COMPLETE');
  assert.equal(upgraded.facts.reviews[0].source.version, 'fixture-2');
  assert.equal(persisted[0].source.version, 'fixture-1');
  assert.deepEqual(upgraded.newReviews, []);

  fixture.rows[1].revision = 'r2';
  fixture.rows[1].state = 'dismissed';
  const revised = await fixture.observe({ seenReviews: persisted });
  assert.equal(revised.external.status, 'COMPLETE');
  assert.deepEqual(revised.newReviews.map(event => [event.id, event.revision, event.state]), [['event-2', 'r2', 'dismissed']]);
  assert.equal(persisted[1].state, 'approved');
  const history = [...persisted, ...revised.newReviews];
  const combined = await fixture.observe({ seenReviews: history });
  assert.equal(combined.external.status, 'COMPLETE');
  assert.deepEqual(combined.newReviews, []);
  fixture.rows[1].state = 'approved';
  assert.equal((await fixture.observe({ seenReviews: history })).external.reason, 'CONFLICTING_REVIEW');
});

test('workflow retains the validated facts across later provider reads', async () => {
  const fixture = scenario();
  const readChange = fixture.adapters.change.read;
  const readReview = fixture.adapters.review.read;
  fixture.adapters.change.read = args => {
    Object.assign(fixture.data.task, { owner: 'different-owner', state: 'Closed' });
    return readChange(args);
  };
  fixture.adapters.review.read = args => {
    Object.assign(fixture.data.change.checks[0], { head: old, state: 'failure' });
    return readReview(args);
  };
  const result = await fixture.observe();
  assert.equal(result.external.status, 'COMPLETE');
  assert.equal(result.facts.task.owner, 'owner');
  assert.equal(result.facts.task.platformState, 'Doing');
  assert.deepEqual(result.facts.change.checks, [{ id: 'check-1', head, state: 'success' }]);
});

test('a snapshot cannot silently combine competing revisions of the same review', async () => {
  const fixture = scenario();
  fixture.lastPage.records = [{ ...fixture.rows[0], revision: 'r2' }, fixture.rows[1]];
  const result = await fixture.observe();
  assert.equal(result.external.reason, 'CONFLICTING_REVIEW');
  assert.deepEqual(result.newReviews, []);
});

test('review observations reject partial pages, excess rows, a changing total and excess pages', async () => {
  const truncated = scenario();
  const read = truncated.adapters.review.read;
  truncated.adapters.review.read = request => request.cursor === null ? read(request) : { status: 'PARTIAL' };
  assert.equal((await truncated.observe()).external.reason, 'PARTIAL_OBSERVATION');

  const excessive = scenario();
  excessive.data.review.records = Array.from({ length: 51 }, () => excessive.rows[0]);
  assert.equal((await excessive.observe()).external.reason, 'INCOMPLETE_REVIEW_PAGES');

  const total = scenario();
  total.lastPage.total += 1;
  assert.equal((await total.observe()).external.reason, 'INCOMPLETE_REVIEW_PAGES');

  const long = scenario();
  long.adapters.review.read = ({ cursor }) => {
    const index = Number(cursor ?? 0);
    return { status: 'COMPLETE', ...long.bindings.review,
      data: { total: 33, next: String(index + 1), records: [{ ...long.rows[0], id: 'review-' + index }] } };
  };
  const bounded = await long.observe();
  assert.equal(bounded.external.reason, 'REVIEW_PAGE_LIMIT');
  assert.equal(bounded.external.observations.filter(observation => observation.port === 'review').length, 32);
});

test('deadline and cancellation return UNKNOWN and suppress later reads', async () => {
  const hung = scenario();
  hung.adapters.task.read = () => new Promise(() => {});
  assert.equal((await hung.observe({ timeoutMs: 10 })).external.reason, 'TIMEOUT');
  assert.deepEqual(hung.reads, []);
  const cancelled = scenario();
  assert.equal((await cancelled.observe({ signal: AbortSignal.abort() })).external.reason, 'CANCELLED');
  assert.deepEqual(cancelled.reads, []);
});
