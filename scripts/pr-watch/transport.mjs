import http from 'node:http';
import https from 'node:https';
import { performance } from 'node:perf_hooks';
import { getCACertificates } from 'node:tls';

const proxyVariables = ['HTTPS_PROXY', 'https_proxy', 'NO_PROXY', 'no_proxy'];

// The caller supplies a verified binding. One fresh HTTP/1.1 request is made per page.
export function postJson(endpoint, { token, proxy, ca, maxBytes, signal, payload }) {
  const tls = endpoint.protocol === 'https:';
  if (tls && proxy === 'environment') {
    const [major, minor] = process.versions.node.split('.').map(Number);
    if (major < 24 || (major === 24 && minor < 5)) throw new Error('Proxy-capable Node runtime required');
  }
  const proxyEnv = Object.fromEntries(proxyVariables.map(name => [name, process.env[name]]));
  const agent = tls ? new https.Agent({
    keepAlive: false,
    ...(proxy === 'environment' && { proxyEnv }),
    ...(ca && { ca: [...getCACertificates('default'), ca] }),
    rejectUnauthorized: true,
  }) : new http.Agent({ keepAlive: false });
  const body = Buffer.from(JSON.stringify(payload));
  const started = performance.now();
  const elapsed = () => Math.round(performance.now() - started);
  const attempt = { requestFlushedMs: null, firstResponse: null, finalStatus: null, bodyBytes: 0 };

  return new Promise(resolve => {
    function finish(reason, data) {
      if (done) return;
      done = true;
      signal.removeEventListener('abort', onAbort);
      agent.destroy();
      resolve({ reason, data, attempt: { ...attempt } });
    }

    function firstResponse(status) {
      attempt.firstResponse ??= { status, elapsedMs: elapsed() };
    }

    function onAbort() {
      request.destroy();
      finish(signal.reason?.name === 'TimeoutError' ? 'TIMEOUT' : 'CANCELLED');
    }

    let done = false;
    const request = (tls ? https : http).request(endpoint, {
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
  });
}
