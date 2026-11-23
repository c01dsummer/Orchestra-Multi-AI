import http from 'node:http';
import https from 'node:https';
import { isIPv4 } from 'node:net';
import { performance } from 'node:perf_hooks';
import { connect as connectTls, getCACertificates } from 'node:tls';

const hostname = url => url.hostname.replace(/^\[|\]$/g, '');
const ipv4Number = address => address.split('.').reduce((value, octet) => value * 256 + Number(octet), 0);

function bypassProxy(endpoint) {
  const host = hostname(endpoint).toLowerCase();
  const authority = `${endpoint.hostname}:${endpoint.port || 443}`;
  const exclusions = process.env.no_proxy || process.env.NO_PROXY || '';
  return exclusions.toLowerCase().split(',').some(item => {
    const entry = item.trim();
    if (entry === '*' || entry === host || entry === authority || entry === endpoint.hostname) return true;
    if (entry.startsWith('*.')) return host.endsWith(entry.slice(1));
    if (entry.startsWith('.')) return host === entry.slice(1) || host.endsWith(entry);
    if (!isIPv4(host) || !entry.includes('-')) return false;
    const [start, end, extra] = entry.split('-').map(value => value.trim());
    if (extra !== undefined || !start || !end || !isIPv4(start) || !isIPv4(end)) return false;
    const address = ipv4Number(host);
    return address >= ipv4Number(start) && address <= ipv4Number(end);
  });
}

function proxyFor(endpoint) {
  const value = process.env.https_proxy || process.env.HTTPS_PROXY;
  if (!value || bypassProxy(endpoint)) return null;
  const url = new URL(value);
  const authenticated = url.username || url.password;
  if (!['http:', 'https:'].includes(url.protocol) || (url.protocol === 'http:' && authenticated)
      || /[\r\n]/.test(value)) throw new Error('Invalid HTTPS proxy configuration');
  const credentials = authenticated && `${decodeURIComponent(url.username)}:${decodeURIComponent(url.password)}`;
  return { url, authorization: credentials && `Basic ${Buffer.from(credentials).toString('base64')}` };
}

function connectTunnel(proxy, endpoint, signal) {
  const authority = `${endpoint.hostname}:${endpoint.port || 443}`;
  const transport = proxy.url.protocol === 'https:' ? https : http;
  return new Promise((resolve, reject) => {
    const request = transport.request({
      hostname: hostname(proxy.url),
      port: proxy.url.port,
      method: 'CONNECT',
      path: authority,
      agent: false,
      signal,
      rejectUnauthorized: true,
      maxHeaderSize: 16 * 1024,
      headers: {
        Host: authority,
        ...(proxy.authorization && { 'Proxy-Authorization': proxy.authorization }),
      },
    });
    request.once('error', reject);
    request.once('connect', (response, socket, head) => {
      if (signal.aborted || response.statusCode !== 200 || head.length) {
        socket.destroy();
        reject(new Error('Proxy tunnel unavailable'));
        return;
      }
      resolve(socket);
    });
    request.end();
  });
}

// The caller supplies a verified binding. One fresh HTTP/1.1 request is made per page.
export async function postJson(endpoint, { token, proxy, ca, maxBytes, signal, payload }) {
  const tls = endpoint.protocol === 'https:';
  const transport = tls ? https : http;
  const started = performance.now();
  const elapsed = () => Math.round(performance.now() - started);
  const attempt = { requestFlushedMs: null, firstResponse: null, finalStatus: null, bodyBytes: 0 };
  const interrupted = () => signal.reason?.name === 'TimeoutError' ? 'TIMEOUT' : 'CANCELLED';
  if (signal.aborted) return { reason: interrupted(), attempt };
  let proxyConfig;
  if (tls && proxy === 'environment') {
    try {
      proxyConfig = proxyFor(endpoint);
    } catch {
      return { reason: 'PROXY_CONFIGURATION', attempt };
    }
  }
  const body = Buffer.from(JSON.stringify(payload));
  const agent = new transport.Agent({
    keepAlive: false,
    ...(tls && { rejectUnauthorized: true }),
    ...(tls && ca && { ca: [...getCACertificates('default'), ca] }),
  });
  let tunnel;
  if (proxyConfig) {
    try {
      tunnel = await connectTunnel(proxyConfig, endpoint, signal);
    } catch {
      return { reason: signal.aborted ? interrupted() : 'TRANSPORT_ERROR', attempt };
    }
    if (signal.aborted) {
      tunnel.destroy();
      return { reason: interrupted(), attempt };
    }
  }
  if (tunnel) agent.createConnection = options => connectTls({ ...options, socket: tunnel });

  return new Promise(resolve => {
    function finish(reason, data) {
      if (done) return;
      done = true;
      signal.removeEventListener('abort', onAbort);
      resolve({ reason, data, attempt: { ...attempt } });
    }

    function firstResponse(status) {
      attempt.firstResponse ??= { status, elapsedMs: elapsed() };
    }

    function onAbort() {
      request.destroy();
      finish(interrupted());
    }

    let done = false;
    const request = transport.request(endpoint, {
      agent,
      method: 'POST',
      maxHeaderSize: 16 * 1024,
      headers: {
        ...(token && { Authorization: `Bearer ${token}` }),
        'User-Agent': 'orchestra-pr-watch',
        Accept: 'application/json',
        'Accept-Encoding': 'identity',
        'Content-Type': 'application/json',
        'Content-Length': body.length,
        Connection: 'close',
      },
    });
    request.once('finish', () => { attempt.requestFlushedMs = elapsed(); });
    request.on('information', response => firstResponse(response.statusCode));
    request.once('error', () => finish('TRANSPORT_ERROR'));
    request.once('upgrade', (response, socket) => {
      firstResponse(response.statusCode);
      attempt.finalStatus = response.statusCode;
      socket.destroy();
      finish('HTTP_STATUS');
    });
    request.once('response', response => {
      firstResponse(response.statusCode);
      attempt.finalStatus = response.statusCode;
      response.once('error', () => finish('PARTIAL_BODY'));
      response.once('close', () => {
        if (!response.complete) finish('PARTIAL_BODY');
      });
      if (response.statusCode !== 200) {
        finish([401, 403].includes(response.statusCode) ? 'HTTP_PERMISSION' : 'HTTP_STATUS');
        return;
      }
      if (response.headers['content-encoding'] && response.headers['content-encoding'] !== 'identity') {
        finish('CONTENT_ENCODING');
        return;
      }
      if (Number(response.headers['content-length']) > maxBytes) {
        finish('BODY_LIMIT');
        return;
      }
      const chunks = [];
      response.on('data', chunk => {
        attempt.bodyBytes += chunk.length;
        if (attempt.bodyBytes > maxBytes) finish('BODY_LIMIT');
        else chunks.push(chunk);
      });
      response.once('end', () => {
        if (!response.complete) return finish('PARTIAL_BODY');
        try {
          const decoded = new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks));
          finish(null, JSON.parse(decoded));
        } catch {
          finish('INVALID_JSON');
        }
      });
    });
    signal.addEventListener('abort', onAbort, { once: true });
    if (signal.aborted) onAbort();
    else request.end(body);
  }).finally(() => {
    agent.destroy();
    tunnel?.destroy();
  });
}
