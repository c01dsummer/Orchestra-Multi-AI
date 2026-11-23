import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { collectPullRequest, githubPullRequests, loopbackPullRequests, READ_PULL_REQUEST } from './platform.mjs';
import { createPlatformObserver, readerInputFor } from './workflow.mjs';

const head = 'a'.repeat(40);
const oldHead = 'b'.repeat(40);
const ref = { repo: 'example/project', repositoryId: 'repo-node', number: 7, head };
const url = 'https://github.com/example/project/pull/7';
const grant = { ...ref, platform: 'github', operation: READ_PULL_REQUEST };
const comment = { id: 'comment-node', url: `${url}#issuecomment-3456789012`, author: { login: 'visitor' },
  body: 'fixture comment', createdAt: '2026-01-01T00:00:00Z' };
const review = { id: 'review-node', url: `${url}#pullrequestreview-42`, author: { login: 'reviewer' },
  body: 'fixture review', submittedAt: '2026-01-01T00:00:01Z', state: 'CHANGES_REQUESTED', commit: { oid: oldHead } };
const page = (nodes, totalCount = nodes.length, endCursor = null) => ({
  nodes, totalCount, pageInfo: { hasNextPage: endCursor !== null, endCursor },
});

function payload(overrides = {}, repository = {}) {
  return { data: { repository: { id: ref.repositoryId, nameWithOwner: ref.repo, ...repository,
    pullRequest: { id: 'pr-node', number: ref.number, url, state: 'OPEN', isDraft: false, headRefOid: head,
      reviewDecision: 'CHANGES_REQUESTED', mergeable: 'MERGEABLE',
      commits: { nodes: [{ commit: { oid: head, statusCheckRollup: { state: 'SUCCESS' } } }] },
      comments: page([comment]), reviews: page([review]), ...overrides },
  } } };
}

async function serve(server, t) {
  const sockets = new Set();
  server.on('connection', socket => {
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(async () => {
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => server.close(resolve));
  });
  return `127.0.0.1:${server.address().port}`;
}

async function upstream(t, handlers, { tls, proxy, ca } = {}) {
  const requests = [];
  const listener = async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks));
    requests.push({ method: request.method, url: request.url, ...body, socket: request.socket.remotePort });
    handlers[requests.length - 1]?.(response);
  };
  function serverError(error) {
    throw error;
  }
  const server = tls ? https.createServer(tls, listener) : http.createServer(listener);
  server.on('error', serverError);
  const origin = `${tls ? 'https' : 'http'}://${await serve(server, t)}`;
  return { adapter: loopbackPullRequests(origin, { proxy, ca }), requests, origin };
}

const respond = value => response => response.end(JSON.stringify(value));
const collect = (adapter, options = {}) => collectPullRequest(adapter, { grant, ref, ...options });

function unknown(result, reason, count) {
  assert.equal(result.external.status, 'UNKNOWN');
  assert.equal(result.external.reason, reason);
  assert.equal(result.external.attempts.length, count);
  assert.deepEqual(result.readerInput, { prs: null });
}

function sent(t, result, server, statuses) {
  assert.equal(server.requests.length, statuses.length);
  assert.equal(result.external.attempts.length, statuses.length);
  for (const [i, status] of statuses.entries()) {
    const { requestFlushedMs, firstResponse } = result.external.attempts[i];
    assert.equal(typeof requestFlushedMs, 'number');
    assert.equal(firstResponse?.status ?? null, status);
    assert.equal(server.requests[i].method, 'POST');
    assert.equal(server.requests[i].url, '/graphql');
    assert.match(server.requests[i].query, /^query /);
  }
  t.diagnostic(JSON.stringify({ upstreamRequests: server.requests.length, status: result.external.status,
    reason: result.external.reason, attempts: result.external.attempts.map(({ reference, ...attempt }) => attempt) }));
}

const second = { ...comment, id: 'second-node', url: `${url}#issuecomment-3456789013`, createdAt: '2026-01-01T00:00:02Z' };
const firstPage = payload({ comments: page([comment], 2, 'next-comments') });
const lastPage = payload({ comments: page([comment, second], 2) });

test('complete pages preserve large legacy IDs and exact replay, with independent cursors and fresh sockets', async t => {
  const server = await upstream(t, [response => {
    response.writeEarlyHints({ link: '</style.css>; rel=preload' });
    respond(firstPage)(response);
  }, respond(lastPage)]);
  const result = await collect(server.adapter);
  sent(t, result, server, [103, 200]);
  assert.equal(result.external.attempts[0].finalStatus, 200);
  assert.deepEqual(server.requests.map(request => request.variables), [
    { owner: 'example', name: 'project', number: 7, comments: true, reviews: true, afterComment: null, afterReview: null },
    { owner: 'example', name: 'project', number: 7, comments: true, reviews: false, afterComment: 'next-comments' },
  ]);
  assert.notEqual(server.requests[0].socket, server.requests[1].socket);
  assert.equal(result.external.status, 'COMPLETE');
  assert.equal(result.external.sourceKind, 'synthetic');
  assert.deepEqual(result.external.requested, ref);
  assert.deepEqual(result.external.reference, { platform: 'github', repositoryId: ref.repositoryId,
    repo: ref.repo, platformId: 'pr-node', number: 7, url, head });
  assert.deepEqual(result.readerInput.prs[0].comments.map(row => row.id), ['3456789012', '3456789013']);
  assert.equal(result.external.events.comments[0].platformId, comment.id);
  assert.equal(result.external.events.reviews[0].reviewedHead, oldHead);
  assert.equal(result.readerInput.prs[0].reviews[0].head_sha, oldHead);
  assert.equal(Object.hasOwn(result, 'accepted'), false);
});

test('scope, capability, operation and pre-cancellation stop before transport', async t => {
  const server = await upstream(t, []);
  for (const wrong of [{ operation: 'pull-request:write' }, { operation: 'review:write' }, { platform: 'unbound' },
    { number: 8 }, { repositoryId: 'wrong' }, { head: oldHead }]) {
    const result = await collect(server.adapter, { grant: { ...grant, ...wrong } });
    assert.equal(result.external.status, 'DENIED');
    assert.equal(result.external.reason, 'NOT_AUTHORIZED');
    assert.deepEqual(result.external.attempts, []);
  }
  unknown(await collect({ ...server.adapter, capabilities: [] }), 'CAPABILITY_UNKNOWN', 0);
  unknown(await collect(server.adapter, { signal: AbortSignal.abort() }), 'CANCELLED', 0);
  assert.equal(server.requests.length, 0);
  const absentEnv = 'FAL202_TEST_ABSENT_TOKEN';
  assert.equal(process.env[absentEnv], undefined);
  const missing = await collect(githubPullRequests({ tokenEnv: absentEnv }));
  assert.equal(missing.external.reason, 'MISSING_CREDENTIAL');
  assert.equal(missing.external.status, 'DENIED');
  assert.deepEqual(missing.external.attempts, []);
});

for (const [name, handler, status, reason] of [
  ['421 is terminal', response => response.writeHead(421).end(), 421, 'HTTP_STATUS'],
  ['307 location is not followed', response => response.writeHead(307, { location: '/other' }).end(), 307, 'HTTP_STATUS'],
  ['disconnect before headers', response => response.destroy(), null, 'TRANSPORT_ERROR'],
  ['disconnect after first headers', response => {
    response.writeHead(200, { 'content-length': '40' });
    response.write('{');
    setImmediate(() => response.destroy());
  }, 200, 'PARTIAL_BODY'],
  ['GraphQL partial result', respond({ ...payload(), errors: [{ message: 'fixture partial' }] }), 200, 'GRAPHQL_ERRORS'],
  ['source repository mismatch', respond(payload({}, { id: 'other' })), 200, 'UNKNOWN_SOURCE'],
  ['old PR head', respond(payload({ headRefOid: oldHead })), 200, 'STALE_HEAD'],
  ['draft PR', respond(payload({ isDraft: true })), 200, 'OUTSIDE_READER_SCOPE'],
  ['unknown platform state', respond(payload({ mergeable: 'UNKNOWN' })), 200, 'UNKNOWN_STATUS'],
  ['unknown author', respond(payload({ reviews: page([{ ...review, author: null }]) })), 200, 'UNKNOWN_EVENT'],
  ['unknown event URL', respond(payload({ reviews: page([{ ...review, url: `${url}#unknown` }]) })), 200, 'UNKNOWN_EVENT_SOURCE'],
  ['claimed completeness without all records', respond(payload({ comments: page([], 1) })), 200, 'INCOMPLETE_PAGE'],
  ['unsupported encoding', response => response.writeHead(200, { 'content-encoding': 'gzip' }).end(), 200, 'CONTENT_ENCODING'],
  ['invalid UTF-8', response => response.end(Buffer.from([0x22, 0xff, 0x22])), 200, 'INVALID_JSON'],
  ['invalid JSON', response => response.end('{'), 200, 'INVALID_JSON'],
]) {
  test(name, async t => {
    const server = await upstream(t, [handler]);
    const result = await collect(server.adapter);
    unknown(result, reason, 1);
    sent(t, result, server, [status]);
    if (reason === 'STALE_HEAD') assert.equal(result.external.attempts[0].reference.head, oldHead);
  });
}

test('HTTP permission response is distinct from business or platform acceptance', async t => {
  const server = await upstream(t, [response => response.writeHead(403).end()]);
  const result = await collect(server.adapter);
  sent(t, result, server, [403]);
  assert.equal(result.external.status, 'DENIED');
  assert.deepEqual(result.readerInput, { prs: null });
});

for (const [name, value, reason] of [
  ['cursor replay', firstPage, 'PAGINATION_LOOP'],
  ['same event ID with changed body', payload({ comments: page([{ ...comment, body: 'changed' }, second], 2) }), 'EVENT_CONFLICT'],
  ['repository state changed during pagination', payload({ reviewDecision: 'APPROVED' }), 'SNAPSHOT_CHANGED'],
  ['head changed during pagination', payload({ headRefOid: oldHead }), 'STALE_HEAD'],
]) {
  test(name, async t => {
    const server = await upstream(t, [respond(firstPage), respond(value)]);
    const result = await collect(server.adapter);
    unknown(result, reason, 2);
    sent(t, result, server, [200, 200]);
    assert.equal(result.external.reference.head, head);
    assert.deepEqual(result.external.events.comments.map(event => event.platformId), [comment.id]);
  });
}

test('page budget retains previously observed events and first response', async t => {
  const server = await upstream(t, [respond(firstPage)]);
  const result = await collect(server.adapter, { maxPages: 1 });
  unknown(result, 'PAGE_LIMIT', 1);
  sent(t, result, server, [200]);
  assert.equal(result.external.events.reviews[0].id, '42');
});

test('body budget rejects excessive declared and streamed bytes', async t => {
  const server = await upstream(t, [
    response => response.writeHead(200, { 'content-length': '1000' }).end(),
    response => { response.write('a'.repeat(65)); response.end(); },
  ]);
  for (const bodyBytes of [0, 65]) {
    const result = await collect(server.adapter, { maxBytes: 64 });
    unknown(result, 'BODY_LIMIT', 1);
    assert.equal(result.external.attempts[0].firstResponse.status, 200);
    assert.equal(result.external.attempts[0].bodyBytes, bodyBytes);
  }
  assert.equal(server.requests.length, 2);
});

test('deadline and cancellation stop a pending read without redispatch', async t => {
  const cancel = new AbortController();
  const server = await upstream(t, [() => {}, response => {
    response.writeHead(200).flushHeaders();
    setTimeout(() => cancel.abort(), 60).unref();
  }]);
  const expired = await collect(server.adapter, { timeoutMs: 100 });
  unknown(expired, 'TIMEOUT', 1);
  sent(t, expired, server, [null]);
  const cancelled = await collect(server.adapter, { signal: cancel.signal });
  unknown(cancelled, 'CANCELLED', 1);
  assert.equal(cancelled.external.attempts[0].firstResponse.status, 200);
  assert.equal(server.requests.length, 2);
});

function localCertificate(t) {
  const dir = mkdtempSync(join(tmpdir(), 'pr-watch-tls-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const cert = join(dir, 'cert.pem');
  const key = join(dir, 'key.pem');
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
    '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1', '-keyout', key, '-out', cert], { stdio: 'ignore' });
  return { key: readFileSync(key), cert: readFileSync(cert) };
}

test('TLS uses configured CA and explicit environment proxy, including NO_PROXY', async t => {
  const tls = localCertificate(t);
  const server = await upstream(t, [respond(payload()), response => response.writeHead(421).end(),
    respond(payload())], { tls });
  const noCa = await collect(server.adapter);
  unknown(noCa, 'TRANSPORT_ERROR', 1);
  assert.equal(noCa.external.attempts[0].requestFlushedMs, null);
  assert.equal(server.requests.length, 0);
  const tunnels = [];
  const proxy = http.createServer();
  proxy.on('connect', (request, client, pending) => {
    tunnels.push(request.url);
    const target = new URL(server.origin);
    assert.equal(request.url, target.host);
    const upstream = net.connect(Number(target.port), target.hostname, () => {
      client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      upstream.write(pending);
      client.pipe(upstream).pipe(client);
    });
    client.on('close', () => upstream.destroy());
    upstream.on('error', () => client.destroy());
  });
  const proxyAddress = await serve(proxy, t);
  for (const name of ['HTTPS_PROXY', 'https_proxy', 'NO_PROXY', 'no_proxy']) {
    const original = process.env[name];
    t.after(() => { if (original === undefined) delete process.env[name]; else process.env[name] = original; });
    delete process.env[name];
  }
  process.env.HTTPS_PROXY = `http://${proxyAddress}`;
  const adapter = loopbackPullRequests(server.origin, { proxy: 'environment', ca: tls.cert });
  const throughProxy = await collect(adapter);
  assert.equal(throughProxy.external.status, 'COMPLETE');
  sent(t, throughProxy, server, [200]);
  assert.deepEqual(tunnels, [new URL(server.origin).host]);
  const misdirected = await collect(adapter);
  unknown(misdirected, 'HTTP_STATUS', 1);
  sent(t, misdirected, { requests: server.requests.slice(1) }, [421]);
  assert.equal(tunnels.length, 2);
  process.env.NO_PROXY = '127.0.0.1';
  const bypass = await collect(adapter);
  assert.equal(bypass.external.status, 'COMPLETE');
  assert.equal(server.requests.length, 3);
  assert.equal(tunnels.length, 2);
});

const scope = process.env.PR_WATCH_READER_ROOT;
const harness = process.env.PR_WATCH_READER_HARNESS;
const executable = process.env.PR_WATCH_READER_BINARY;
test('fixed real reader preserves history and keeps external completeness separate from dry-run decisions',
  { skip: scope || harness || executable ? false : 'Reader harness unavailable (NOT_RUN)' }, async t => {
    assert.ok(scope && harness && executable, 'Configure reader root, harness and binary together');
    const current = { ...review, commit: { oid: head } };
    const server = await upstream(t, [respond(firstPage), respond(lastPage),
      respond(payload({ reviews: page([current]) })), response => response.writeHead(421).end()]);
    const oldReview = await collect(server.adapter);
    const currentReview = await collect(server.adapter);
    const incomplete = await collect(server.adapter);
    sent(t, oldReview, { requests: server.requests.slice(0, 2) }, [200, 200]);
    assert.equal(server.requests.length, 4);
    assert.equal(oldReview.external.status, 'COMPLETE');
    assert.equal(currentReview.external.status, 'COMPLETE');
    unknown(incomplete, 'HTTP_STATUS', 1);
    const binding = (provider, id, link = `https://${provider}.invalid/${id}`) => ({
      source: { provider, installation: 'test', version: '1' }, ref: { id, url: link },
    });
    const bindings = { task: binding('tasks', 'work-7'),
      change: { ...binding('github', 'pr-node', url), repositoryId: ref.repositoryId },
      review: { ...binding('reviews', 'pr-7'), reviewerIds: ['reviewer'] } };
    const data = { task: { state: 'Active', owner: 'owner', project: 'project', parent: null },
      change: { repositoryId: ref.repositoryId, head, checks: [{ id: 'test', head, state: 'success' }] },
      review: { records: [{ id: 'other-approval', revision: 'r1', changeId: 'pr-node', head, state: 'approved',
        url: 'https://reviews.invalid/other-approval', reviewer: 'external', findings: [] }], next: null, total: 1 } };
    const adapters = Object.fromEntries(Object.entries(bindings).map(([port, source]) => [port, {
      enabled: true, sourceKind: 'synthetic', source: source.source, capabilities: [`${port}:read`],
      read: () => ({ status: 'COMPLETE', ...source, data: data[port] }),
    }]));
    const grants = Object.entries(bindings).map(([port, source]) => ({ ...source, operation: `${port}:read`, head, workId: 'TEST-7' }));
    const observer = createPlatformObserver({ bindings, adapters, sourceKind: 'synthetic',
      workId: 'TEST-7', states: { Active: 'in_progress' } });
    const workflow = await observer({ head, grants });
    assert.equal(workflow.external.status, 'COMPLETE');
    assert.equal(workflow.facts.reviews[0].trustedReviewer, false);
    const blocked = await observer({ head, grants: grants.slice(0, 2) });
    assert.deepEqual(readerInputFor(blocked, currentReview), { prs: null });
    assert.deepEqual(readerInputFor(workflow, { ...currentReview,
      external: { ...currentReview.external, sourceKind: 'configured' } }), { prs: null });
    assert.deepEqual(readerInputFor(workflow, oldReview), oldReview.readerInput);
    assert.deepEqual(readerInputFor(workflow, currentReview), currentReview.readerInput);
    const readerSha = createHash('sha256').update(readFileSync(executable)).digest('hex');
    const stdout = execFileSync(process.env.PR_WATCH_READER_PYTHON ?? 'python3',
      ['-I', '-B', harness, scope], {
      env: { PATH: process.env.PATH, LANG: 'C.UTF-8' }, timeout: 25_000,
      input: JSON.stringify({ old_review: readerInputFor(workflow, oldReview), current_review: readerInputFor(workflow, currentReview),
        unknown: readerInputFor(workflow, incomplete) }), stdio: ['pipe', 'pipe', 'pipe'], encoding: 'utf8',
    });
    assert.ok(stdout.trim(), 'Missing Reader receipt');
    const { python_version: pythonVersion, ...receipt } = JSON.parse(stdout);
    assert.match(pythonVersion, /^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$/);
    assert.deepEqual(receipt, {
      reader_sha256: readerSha,
      old_review: 'observe',
      current_review: 'dry_run_preview',
      repeat: 'idempotent_skip',
      incomplete: 'degraded_unknown',
      reader_dry_runs: 4,
      fixture_files_identical: true,
      run_logs_absent: true,
    });
    t.diagnostic(JSON.stringify({ python_version: pythonVersion, ...receipt }));
  });
