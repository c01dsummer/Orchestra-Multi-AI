import { isDeepStrictEqual } from 'node:util';

// Read facts from separately bound task, hosting and review providers. The
// caller supplies read grants and existing history from its own authority.
const operations = Object.freeze({ task: 'task:read', change: 'change:read', review: 'review:read' });
const taskStates = new Set(['todo', 'in_progress', 'blocked', 'done', 'canceled']);
const checkStates = new Set(['success', 'failed', 'pending', 'skipped']);
const reviewStates = new Set(['approved', 'changes_requested', 'commented', 'pending', 'dismissed']);
const severities = new Set(['blocker', 'major', 'minor', 'info']);
const key = value => typeof value === 'string' && /^[!-~]{1,512}$/.test(value);
const sha = value => typeof value === 'string' && /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(value);
const same = isDeepStrictEqual;

function isLink(value) {
  try {
    const url = new URL(value);
    return typeof value === 'string' && url.protocol === 'https:' && !url.username && !url.password;
  } catch {
    return false;
  }
}

const authority = input => ({ provider: input?.provider, installation: input?.installation, version: input?.version });
const reference = input => ({ id: input?.id, url: input?.url });
const eventKey = event => JSON.stringify([event.source.provider, event.source.installation, event.collectionId, event.id]);
const revisionKey = event => JSON.stringify([eventKey(event), event.revision]);

function sameRevision(a, b) {
  // The version identifies the adapter that observed a revision. The stable
  // event identity and the revision's facts survive a compatible upgrade.
  const facts = ({ source, findings, ...rest }) => ({
    ...rest, source: { provider: source.provider, installation: source.installation },
    findings: new Map(findings.map(finding => [finding.id, finding])),
  });
  return same(facts(a), facts(b));
}

class Refusal extends Error {
  constructor(reason, status = 'UNKNOWN') {
    super(reason);
    this.status = status;
  }
}

function requireFact(condition, reason) {
  if (!condition) throw new Refusal(reason);
}

function reviewFact(record, binding, changeId) {
  requireFact(key(record?.id) && isLink(record.url) && key(record.reviewer) && key(record.revision)
    && sha(record.head) && record.changeId === changeId && reviewStates.has(record.state)
    && Array.isArray(record.findings) && record.findings.every(finding => key(finding?.id)
      && severities.has(finding.severity) && isLink(finding.url)), 'UNKNOWN_REVIEW');
  requireFact(new Set(record.findings.map(f => f.id)).size === record.findings.length, 'CONFLICTING_FINDING');
  return { source: authority(binding.source), collectionId: binding.ref.id, id: record.id, url: record.url,
    revision: record.revision, reviewer: record.reviewer, changeId, head: record.head, state: record.state,
    findings: record.findings.map(({ id, severity, url }) => ({ id, severity, url })) };
}

function matchesGrant(grant, binding, operation, workId, head) {
  return grant?.operation === operation && grant.workId === workId && grant.head === head
    && same(authority(grant.source), binding.source) && same(reference(grant.ref), binding.ref);
}

export function createPlatformObserver({ bindings, adapters, sourceKind, workId, historicalIds = [], states }) {
  if (sourceKind !== 'synthetic' && sourceKind !== 'configured') throw new TypeError('Invalid source kind');
  if (!key(workId) || !Array.isArray(historicalIds) || !historicalIds.every(key)
      || historicalIds.includes(workId) || new Set(historicalIds).size !== historicalIds.length
      || !states || !Object.values(states).every(state => taskStates.has(state))) {
    throw new TypeError('Invalid task identity or status mapping');
  }
  const tasks = new Set([workId, ...historicalIds]);
  const statusMap = new Map(Object.entries(states));
  const repositoryId = bindings?.change?.repositoryId;
  const reviewerIds = bindings?.review?.reviewerIds;
  if (!key(repositoryId) || !Array.isArray(reviewerIds) || !reviewerIds.every(key)) {
    throw new TypeError('Invalid repository or reviewer binding');
  }
  const reviewers = new Set(reviewerIds);
  const registry = {};
  for (const port of Object.keys(operations)) {
    const config = bindings?.[port];
    const source = authority(config?.source);
    const ref = reference(config?.ref);
    if (!Object.values(source).every(key) || !key(ref.id) || !isLink(ref.url)) {
      throw new TypeError('Invalid declared platform binding');
    }
    const adapter = adapters?.[port];
    const declared = authority(adapter?.source);
    registry[port] = { source: Object.freeze(source), ref: Object.freeze(ref), declared, sourceKind: adapter?.sourceKind,
      enabled: adapter?.enabled === true, capabilities: new Set(Array.isArray(adapter?.capabilities) ? adapter.capabilities : []),
      read: typeof adapter?.read === 'function' ? adapter.read.bind(adapter) : null };
  }

  return async function observe({ head, grants = [], history = [], seenReviews = [], signal, timeoutMs = 10_000 }) {
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60_000) throw new RangeError('Invalid workflow timeout');
    const approvedReads = structuredClone(grants);
    const reviewHistory = structuredClone(seenReviews);
    const retainedHistory = history.filter(row => tasks.has(row?.workId)).map(row => structuredClone(row));
    const observations = [];
    const newReviews = [];
    const result = (status, reason, facts = null) => ({
      external: { status, reason, sourceKind, observations }, identity: { workId, historicalIds: [...tasks].slice(1) },
      history: retainedHistory, facts, newReviews: status === 'COMPLETE' ? newReviews : [],
    });
    const deadline = new AbortController();
    const combined = signal ? AbortSignal.any([deadline.signal, signal]) : deadline.signal;
    const expires = performance.now() + timeoutMs;
    const timer = setTimeout(() => deadline.abort(), timeoutMs);
    const interrupted = () => new Refusal(signal?.aborted ? 'CANCELLED' : 'TIMEOUT');
    const stop = () => {
      if (combined.aborted || performance.now() >= expires) throw interrupted();
    };

    async function read(port, cursor = null) {
      stop();
      const binding = registry[port];
      const operation = operations[port];
      if (!approvedReads.some(grant => matchesGrant(grant, binding, operation, workId, head))) {
        throw new Refusal('NOT_AUTHORIZED', 'DENIED');
      }
      requireFact(binding.enabled, 'ADAPTER_DISABLED');
      requireFact(binding.sourceKind === sourceKind && same(binding.source, binding.declared), 'UNKNOWN_SOURCE_OR_VERSION');
      requireFact(binding.capabilities.has(operation) && typeof binding.read === 'function', 'CAPABILITY_UNKNOWN');
      let cancel;
      const cancelled = new Promise((_, reject) => {
        cancel = () => reject(interrupted());
        combined.addEventListener('abort', cancel, { once: true });
      });
      let reply;
      try {
        reply = await Promise.race([binding.read({ ref: binding.ref, head, cursor, signal: combined }), cancelled]);
      } finally {
        combined.removeEventListener('abort', cancel);
      }
      stop();
      if (reply?.status === 'DENIED') throw new Refusal('PLATFORM_DENIED', 'DENIED');
      requireFact(reply?.status === 'COMPLETE', 'PARTIAL_OBSERVATION');
      requireFact(same(authority(reply.source), binding.source)
        && same(reference(reply.ref), binding.ref), 'UNKNOWN_SOURCE_OR_VERSION');
      observations.push({ port, source: authority(reply.source), ref: reference(reply.ref), cursor });
      return reply.data;
    }

    try {
      requireFact(sha(head), 'UNKNOWN_HEAD');
      const task = await read('task');
      const taskState = statusMap.get(task?.state);
      requireFact(taskState && key(task.owner) && key(task.project) && (task.parent === null || key(task.parent)), 'UNKNOWN_TASK_STATE');
      const taskFact = { ref: registry.task.ref, state: taskState, platformState: task.state,
        owner: task.owner, project: task.project, parent: task.parent };
      const change = await read('change');
      requireFact(change?.repositoryId === repositoryId, 'UNKNOWN_REPOSITORY');
      requireFact(change.head === head, 'STALE_HEAD');
      requireFact(Array.isArray(change.checks) && change.checks.every(check => key(check?.id)
        && check.head === head && checkStates.has(check.state)), 'UNKNOWN_CHECKS');
      requireFact(new Set(change.checks.map(c => c.id)).size === change.checks.length, 'CONFLICTING_CHECK');
      const changeFact = { source: registry.change.source, ref: registry.change.ref, repositoryId, head,
        checks: change.checks.map(({ id, head, state }) => ({ id, head, state })) };

      const prior = new Map();
      for (const event of reviewHistory) {
        requireFact(Object.values(authority(event?.source)).every(key) && key(event.collectionId) && key(event.id)
          && key(event.revision) && Array.isArray(event.findings) && event.findings.every(finding => key(finding?.id))
          && new Set(event.findings.map(finding => finding.id)).size === event.findings.length, 'UNKNOWN_HISTORY');
        const revision = revisionKey(event);
        requireFact(!prior.has(revision) || sameRevision(prior.get(revision), event), 'CONFLICTING_HISTORY');
        prior.set(revision, event);
      }
      const reviews = new Map();
      const cursors = new Set();
      let cursor = null;
      let total;
      for (let index = 0; index < 32; index++) {
        const page = await read('review', cursor);
        requireFact(Array.isArray(page?.records) && page.records.length <= 50
          && Number.isSafeInteger(page.total) && page.total >= 0
          && (page.next === null || (key(page.next) && page.records.length > 0)), 'INCOMPLETE_REVIEW_PAGES');
        requireFact(total === undefined || total === page.total, 'INCOMPLETE_REVIEW_PAGES');
        total = page.total;
        for (const record of page.records) {
          const event = reviewFact(record, registry.review, registry.change.ref.id);
          const identity = eventKey(event);
          const revision = revisionKey(event);
          const observed = reviews.get(identity);
          const historical = prior.get(revision);
          requireFact(!historical || sameRevision(historical, event), 'CONFLICTING_REVIEW');
          requireFact(!observed || (observed.revision === event.revision
            && sameRevision(observed, event)), 'CONFLICTING_REVIEW');
          if (!observed) {
            reviews.set(identity, event);
            observations.push({ port: 'review-event', event: structuredClone(event) });
            if (!historical) newReviews.push(structuredClone(event));
          }
        }
        requireFact(reviews.size <= total, 'INCOMPLETE_REVIEW_PAGES');
        if (page.next === null) {
          requireFact(reviews.size === total, 'INCOMPLETE_REVIEW_PAGES');
          return result('COMPLETE', null, {
            task: taskFact,
            change: changeFact,
            reviews: [...reviews.values()].map(event => ({ ...event, atCurrentHead: event.head === head,
              trustedReviewer: reviewers.has(event.reviewer) })),
          });
        }
        requireFact(!cursors.has(page.next), 'REVIEW_CURSOR_LOOP');
        cursors.add(page.next);
        cursor = page.next;
      }
      return result('UNKNOWN', 'REVIEW_PAGE_LIMIT');
    } catch (error) {
      return error instanceof Refusal ? result(error.status, error.message) : result('UNKNOWN', 'ADAPTER_ERROR');
    } finally {
      clearTimeout(timer);
    }
  };
}

// Compatibility seam for the deployed Reader. A separate task or review
// provider never supplies or rewrites feedback, authors, checks or approvals
// in the legacy snapshot. An incomplete pair stays unknown to the Reader.
export function readerInputFor(workflow, pullRequestObservation) {
  const unknown = { prs: null };
  const change = workflow?.facts?.change;
  const bound = pullRequestObservation?.external?.reference;
  const snapshots = pullRequestObservation?.readerInput?.prs;
  if (workflow?.external?.status !== 'COMPLETE' || pullRequestObservation?.external?.status !== 'COMPLETE'
      || workflow.external.sourceKind !== pullRequestObservation.external.sourceKind
      || !change || !bound || !Array.isArray(snapshots) || snapshots.length !== 1) return unknown;
  const [snapshot] = snapshots;
  if (change.source?.provider !== bound.platform || change.ref?.id !== bound.platformId
      || change.ref?.url !== bound.url || change.repositoryId !== bound.repositoryId
      || change.head !== bound.head || snapshot?.head_sha !== bound.head || snapshot?.repo !== bound.repo
      || snapshot?.number !== bound.number) return unknown;
  return { prs: [structuredClone(snapshot)] };
}
