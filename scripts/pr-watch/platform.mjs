import { postJson } from './transport.mjs';

export const READ_PULL_REQUEST = 'pull-request:read';
const GITHUB_GRAPHQL = 'https://api.github.com/graphql';
const sha = /^[0-9a-f]{40}$/;
const repositoryName = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const identifier = value => typeof value === 'string' && /^[!-~]+$/.test(value);
const checkStates = new Set(['SUCCESS', 'FAILURE', 'ERROR', 'EXPECTED', 'PENDING']);
const reviewDecisions = new Set(['', 'APPROVED', 'CHANGES_REQUESTED', 'REVIEW_REQUIRED']);
const mergeStates = new Set(['MERGEABLE', 'CONFLICTING']);
const reviewStates = new Set(['APPROVED', 'CHANGES_REQUESTED', 'DISMISSED', 'COMMENTED', 'PENDING']);
const fields = ['comments', 'reviews'];

const query = `query WatchPullRequest($owner: String!, $name: String!, $number: Int!,
    $comments: Boolean!, $reviews: Boolean!, $afterComment: String, $afterReview: String) {
  repository(owner: $owner, name: $name) {
    id nameWithOwner
    pullRequest(number: $number) {
      id number url state isDraft headRefOid reviewDecision mergeable
      commits(last: 1) { nodes { commit { oid statusCheckRollup { state } } } }
      comments(first: 50, after: $afterComment) @include(if: $comments) {
        totalCount pageInfo { hasNextPage endCursor }
        nodes { id url author { login } body createdAt }
      }
      reviews(first: 50, after: $afterReview) @include(if: $reviews) {
        totalCount pageInfo { hasNextPage endCursor }
        nodes { id url author { login } body submittedAt state commit { oid } }
      }
    }
  }
}`;

class Incomplete extends Error {}
function requireFact(condition, reason) {
  if (!condition) throw new Incomplete(reason);
}

function legacyEvent(node, field, prUrl) {
  const match = typeof node?.url === 'string' && node.url.startsWith(`${prUrl}#`)
    && (field === 'comments' ? /^issuecomment-([1-9]\d*)$/ : /^pullrequestreview-([1-9]\d*)$/)
      .exec(node.url.slice(prUrl.length + 1));
  requireFact(match && identifier(node?.id), 'UNKNOWN_EVENT_SOURCE');
  const login = node.author?.login;
  const date = field === 'comments' ? node.createdAt : node.submittedAt;
  requireFact(identifier(login) && typeof node.body === 'string', 'UNKNOWN_EVENT');
  // GitHub DateTime is ISO 8601 UTC. Pending reviews can lack a submission time.
  const pending = field === 'reviews' && node.state === 'PENDING';
  requireFact((pending && date === null) || (typeof date === 'string'
    && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$/.test(date)
    && Number.isFinite(Date.parse(date))), 'UNKNOWN_EVENT_TIME');
  const reference = { id: match[1], platformId: node.id, url: node.url, author: login, occurredAt: date };
  const row = { id: match[1], author: login, body: node.body };
  if (field === 'comments') row.created_at = date;
  else {
    const reviewHead = node.commit?.oid ?? null;
    requireFact(reviewStates.has(node.state) && (reviewHead === null || sha.test(reviewHead)), 'UNKNOWN_REVIEW');
    requireFact(node.state !== 'CHANGES_REQUESTED' || reviewHead !== null, 'UNKNOWN_REVIEW_HEAD');
    Object.assign(row, { state: node.state, head_sha: reviewHead, submitted_at: date });
    Object.assign(reference, { reviewedHead: reviewHead, state: node.state });
  }
  return { reference, row };
}

function githubPage(payload, requested, cursors, attempt) {
  requireFact(payload?.errors === undefined || (Array.isArray(payload.errors) && !payload.errors.length),
    'GRAPHQL_ERRORS');
  const repo = payload?.data?.repository;
  const pr = repo?.pullRequest;
  requireFact(repo?.nameWithOwner === requested.repo && repo.id === requested.repositoryId
    && pr?.number === requested.number && identifier(pr.id)
    && pr.url === `https://github.com/${requested.repo}/pull/${requested.number}`, 'UNKNOWN_SOURCE');
  const observedHead = typeof pr.headRefOid === 'string' && sha.test(pr.headRefOid) ? pr.headRefOid : null;
  const reference = { platform: 'github', repositoryId: repo.id, repo: repo.nameWithOwner,
    platformId: pr.id, number: pr.number, url: pr.url, head: observedHead };
  attempt.reference = reference;
  const commit = pr.commits?.nodes?.[0]?.commit;
  const checksState = commit?.statusCheckRollup?.state;
  const reviewDecision = pr.reviewDecision === null ? '' : pr.reviewDecision;
  requireFact(observedHead, 'UNKNOWN_HEAD');
  requireFact(observedHead === requested.head, 'STALE_HEAD');
  // The mapped reader has no isDraft gate. Do not give it an open draft snapshot.
  requireFact(pr.state === 'OPEN' && pr.isDraft === false, 'OUTSIDE_READER_SCOPE');
  requireFact(pr.commits?.nodes?.length === 1 && commit?.oid === observedHead && checkStates.has(checksState)
    && reviewDecisions.has(reviewDecision) && mergeStates.has(pr.mergeable), 'UNKNOWN_STATUS');
  const snapshot = { repo: repo.nameWithOwner, number: pr.number, head_sha: pr.headRefOid, state: pr.state,
    checks_state: checksState, review_decision: reviewDecision, mergeable: pr.mergeable };
  const pages = {};
  for (const field of fields.filter(name => cursors[name] !== undefined)) {
    const page = pr[field];
    requireFact(Array.isArray(page?.nodes) && page.nodes.length <= 50 && Number.isSafeInteger(page.totalCount)
      && page.totalCount >= 0 && typeof page.pageInfo?.hasNextPage === 'boolean', 'INCOMPLETE_PAGE');
    const next = page.pageInfo.hasNextPage ? page.pageInfo.endCursor : undefined;
    requireFact(!page.pageInfo.hasNextPage || (identifier(next) && next.length <= 2048 && page.nodes.length),
      'INCOMPLETE_PAGE');
    pages[field] = { records: page.nodes.map(node => legacyEvent(node, field, pr.url)),
      total: page.totalCount, next };
  }
  return { reference, snapshot, pages };
}

function graphqlAdapter(endpoint, binding, sourceKind) {
  return Object.freeze({
    platform: 'github',
    sourceKind,
    capabilities: Object.freeze([READ_PULL_REQUEST]),
    async readPage(ref, cursors, { maxBytes, signal }) {
      const token = binding.tokenEnv && process.env[binding.tokenEnv];
      if (binding.tokenEnv && !identifier(token)) return { reason: 'MISSING_CREDENTIAL' };
      const [owner, name] = ref.repo.split('/');
      const variables = { owner, name, number: ref.number, comments: cursors.comments !== undefined,
        reviews: cursors.reviews !== undefined, afterComment: cursors.comments, afterReview: cursors.reviews };
      const { reason, data, attempt } = await postJson(endpoint, {
        ...binding, token, maxBytes, signal, payload: { query, variables },
      });
      if (reason) return { reason, attempt };
      try {
        return { ...githubPage(data, ref, cursors, attempt), attempt };
      } catch (error) {
        return { reason: error instanceof Incomplete ? error.message : 'INVALID_PAGE', attempt };
      }
    },
  });
}

// Bindings come from trusted deployment configuration, outside platform data and policy decisions.
export function githubPullRequests({ tokenEnv, proxy = 'environment', ca } = {}) {
  if (typeof tokenEnv !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(tokenEnv)
      || !['environment', 'direct'].includes(proxy)) {
    throw new TypeError('Invalid GitHub binding');
  }
  return graphqlAdapter(new URL(GITHUB_GRAPHQL), { tokenEnv, proxy, ca }, 'configured');
}

// Synthetic upstream uses the same wire shape and transport, with an explicit loopback boundary.
export function loopbackPullRequests(origin, { proxy = 'direct', ca } = {}) {
  const url = new URL('/graphql', origin);
  if (url.hostname !== '127.0.0.1' || !['http:', 'https:'].includes(url.protocol)
      || url.username || url.password || !['environment', 'direct'].includes(proxy)
      || (url.protocol === 'http:' && proxy !== 'direct')) throw new TypeError('Invalid synthetic binding');
  return graphqlAdapter(url, { proxy, ca }, 'synthetic');
}

function newPageState() {
  return { records: new Map(), platformIds: new Map(), cursors: new Set(), total: null, next: null };
}

function addPage(state, page) {
  requireFact(state.total === null || state.total === page.total, 'PAGINATION_CHANGED');
  state.total = page.total;
  for (const event of page.records) {
    const { id, platformId } = event.reference;
    const existing = state.records.get(id);
    if (existing) {
      requireFact(JSON.stringify(existing) === JSON.stringify(event), 'EVENT_CONFLICT');
      continue;
    }
    requireFact(!state.platformIds.has(platformId), 'EVENT_CONFLICT');
    state.records.set(id, event);
    state.platformIds.set(platformId, id);
  }
  requireFact(state.records.size <= state.total, 'PAGINATION_CHANGED');
  if (page.next === undefined) {
    requireFact(state.records.size === state.total, 'INCOMPLETE_PAGE');
  } else {
    requireFact(!state.cursors.has(page.next), 'PAGINATION_LOOP');
    state.cursors.add(page.next);
  }
  state.next = page.next;
}

// grant is an existing caller authorization. Scope, operation and adapter support are independent.
export async function collectPullRequest(adapter, { grant, ref, signal, timeoutMs = 10_000,
    maxBytes = 1024 * 1024, maxPages = 32 }) {
  const attempts = [];
  const history = { comments: newPageState(), reviews: newPageState() };
  const pages = () => Object.fromEntries(fields.map(field => [field, [...history[field].records.values()]]));
  let requested = null;
  let reference = null;
  const result = (status, reason, snapshot = null) => ({
    external: { status, reason, sourceKind: adapter?.sourceKind ?? 'unknown', attempts, requested, reference,
      events: Object.fromEntries(fields.map(field =>
      [field, pages()[field].map(event => event.reference)])) },
    // A null list makes the existing reader surface UNKNOWN. An empty list could imply idle.
    readerInput: { prs: snapshot ? [snapshot] : null },
  });
  if (!ref || !adapter || grant?.operation !== READ_PULL_REQUEST || !identifier(grant.platform)
      || grant.platform !== adapter.platform
      || grant.repo !== ref?.repo || grant.repositoryId !== ref?.repositoryId || grant.number !== ref?.number
      || grant.head !== ref?.head) return result('DENIED', 'NOT_AUTHORIZED');
  if (!identifier(ref.repositoryId) || typeof ref.repo !== 'string' || !repositoryName.test(ref.repo)
      || !Number.isInteger(ref.number) || ref.number < 1 || ref.number > 2_147_483_647
      || typeof ref.head !== 'string' || !sha.test(ref.head)) return result('UNKNOWN', 'UNBOUND_REFERENCE');
  requested = Object.freeze({ repo: ref.repo, repositoryId: ref.repositoryId, number: ref.number, head: ref.head });
  if (!Array.isArray(adapter.capabilities) || !adapter.capabilities.includes(READ_PULL_REQUEST)
      || typeof adapter.readPage !== 'function') return result('UNKNOWN', 'CAPABILITY_UNKNOWN');
  if (![[timeoutMs, 60_000], [maxBytes, 1024 * 1024], [maxPages, 32]]
      .every(([value, limit]) => Number.isInteger(value) && value > 0 && value <= limit)) {
    throw new RangeError('Invalid platform observation limits');
  }
  const deadline = new AbortController();
  const expires = performance.now() + timeoutMs;
  const timer = setTimeout(() => deadline.abort(new DOMException('deadline', 'TimeoutError')), timeoutMs);
  const combined = signal ? AbortSignal.any([signal, deadline.signal]) : deadline.signal;
  const interrupted = () => signal?.aborted ? 'CANCELLED' : 'TIMEOUT';
  let snapshot;
  try {
    for (let i = 0; i < maxPages; i++) {
      if (combined.aborted || performance.now() >= expires) return result('UNKNOWN', interrupted());
      const cursors = Object.fromEntries(fields.map(field => [field, history[field].next]));
      const page = await adapter.readPage(requested, cursors, { maxBytes, signal: combined });
      if (page.attempt) attempts.push(page.attempt);
      if (combined.aborted || performance.now() >= expires) return result('UNKNOWN', interrupted());
      if (page.reason) return result(page.reason === 'HTTP_PERMISSION' || page.reason === 'MISSING_CREDENTIAL'
        ? 'DENIED' : 'UNKNOWN', page.reason);
      if (reference) {
        requireFact(JSON.stringify(reference) === JSON.stringify(page.reference)
          && JSON.stringify(snapshot) === JSON.stringify(page.snapshot), 'SNAPSHOT_CHANGED');
      }
      reference = page.reference;
      snapshot = page.snapshot;
      for (const field of fields.filter(name => cursors[name] !== undefined)) addPage(history[field], page.pages[field]);
      if (fields.every(field => history[field].next === undefined)) {
        for (const [field, records] of Object.entries(pages())) {
          // The legacy reader uses review order. Stable sort preserves provider order for equal timestamps.
          snapshot[field] = records.sort((a, b) => (a.reference.occurredAt === null ? Infinity : Date.parse(a.reference.occurredAt))
            - (b.reference.occurredAt === null ? Infinity : Date.parse(b.reference.occurredAt))).map(event => event.row);
        }
        if (combined.aborted || performance.now() >= expires) return result('UNKNOWN', interrupted());
        return result('COMPLETE', null, snapshot);
      }
    }
    return result('UNKNOWN', 'PAGE_LIMIT');
  } catch (error) {
    return result('UNKNOWN', error instanceof Incomplete ? error.message : 'ADAPTER_ERROR');
  } finally {
    clearTimeout(timer);
  }
}
