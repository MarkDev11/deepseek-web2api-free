/**
 * src/capture/server.js - loopback capture server.
 *
 * A plain node:http server bound to 127.0.0.1 on a random port (listen(0)), so
 * nothing on the network can reach it and it never collides with another
 * instance. It serves the helper page and accepts credentials back over three
 * equivalent shapes, all funnelling into the same AccountStore call:
 *
 *   POST /capture   JSON {nonce, raw, email?, id?}      (fetch from the page)
 *                   text/plain body = the blob itself    (bookmarklet, no preflight)
 *                   application/x-www-form-urlencoded    (plain HTML form, no JS)
 *   GET  /submit    ?nonce=&raw=  or  ?nonce=&token=&cookies=   (navigation
 *                   fallback: no CORS, no fetch, works from a bookmarklet that
 *                   opens a popup or from a mailto-style link)
 *   GET  /status    ?nonce=  -> {captured, account}      (page polls this)
 *   GET  /healthz            -> {ok, port}               (readiness probe)
 *
 * Design notes
 * ------------
 * * Nonce. Every instance gets a random URL secret (crypto.randomBytes) that any
 *   write must echo (body.nonce, ?nonce=, or the x-dsh-capture-nonce header).
 *   Loopback alone is not a trust boundary - any local process or page can open a
 *   connection - so the nonce is what stops an unrelated local page from
 *   overwriting the account file while a capture is in progress. Compared with
 *   timingSafeEqual.
 * * No session state beyond the captured result: the store is the source of
 *   truth, so a second POST simply updates or adds another account (one server
 *   can capture several accounts before it closes).
 * * Never writes on a bad paste: parseCredBlob + AccountStore guarantee the
 *   existing credentials survive an unrecognisable blob; the caller gets
 *   {ok:false, error:"no_token_in_paste"} and the page shows it.
 * * Responses carry no-store + nosniff and permissive CORS, because the caller
 *   may be a bookmarklet running on https://chat.deepseek.com.
 */

import { createServer } from 'node:http';
import { randomBytes, timingSafeEqual } from 'node:crypto';

import { AccountStore } from './store.js';
import { renderWaitPage, renderResultPage } from './page.js';

export const DEFAULT_HOST = '127.0.0.1';
export const DEFAULT_CHAT_URL = 'https://chat.deepseek.com/';
/** A pasted blob is a token plus cookies; a quarter megabyte is generous. */
export const MAX_BODY_BYTES = 256 * 1024;

function newNonce() {
  return randomBytes(12).toString('base64url');
}

/** Timing-safe compare; unequal lengths are simply false. */
export function nonceMatches(expected, given) {
  const a = String(expected ?? '');
  const b = String(given ?? '');
  if (!a || !b) return false;
  const bufA = Buffer.from(a);
  const bufB = Buffer.from(b);
  if (bufA.length !== bufB.length) return false;
  return timingSafeEqual(bufA, bufB);
}

function parseFormUrlencoded(body) {
  const out = {};
  for (const pair of String(body ?? '').split('&')) {
    if (!pair) continue;
    const i = pair.indexOf('=');
    const rawKey = i === -1 ? pair : pair.slice(0, i);
    const rawVal = i === -1 ? '' : pair.slice(i + 1);
    let key = rawKey;
    let value = rawVal;
    try {
      key = decodeURIComponent(rawKey.replace(/\+/g, ' '));
      value = decodeURIComponent(rawVal.replace(/\+/g, ' '));
    } catch {
      /* leave the raw form when it is not percent-encoded correctly */
    }
    if (!(key in out)) out[key] = value;
  }
  return out;
}

/**
 * Normalise any accepted request body into {raw, email, id, nonce}.
 * Accepts JSON, urlencoded form fields, and a bare blob (text/plain).
 */
export function parseCaptureBody(contentType, body) {
  const type = String(contentType || '').toLowerCase();
  const text = String(body ?? '');

  let fields = null;
  if (type.includes('json') || (!type.includes('urlencoded') && /^[{[]/.test(text.trim()))) {
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) fields = parsed;
    } catch {
      fields = null;
    }
  } else if (type.includes('urlencoded')) {
    fields = parseFormUrlencoded(text);
  }
  if (!fields) fields = { raw: text };

  const raw =
    typeof fields.raw === 'string' ? fields.raw : typeof fields.blob === 'string' ? fields.blob : '';
  const token = typeof fields.token === 'string' ? fields.token : '';
  const cookies = typeof fields.cookies === 'string' ? fields.cookies : '';
  // A {token, cookies} POST is the same information in two fields; compose the
  // blob so there is exactly one code path downstream.
  const combined = raw || (token && cookies ? token + '||Cookie: ' + cookies : token);
  return {
    raw: combined,
    email: typeof fields.email === 'string' ? fields.email : '',
    id: typeof fields.id === 'string' ? fields.id : '',
    nonce:
      typeof fields.nonce === 'string'
        ? fields.nonce
        : typeof fields._nonce === 'string'
          ? fields.nonce
          : '',
  };
}

function wantsJson(req, url) {
  const accept = String(req.headers.accept || '');
  if (url && url.searchParams.get('format') === 'json') return true;
  if (url && url.searchParams.get('format') === 'html') return false;
  if (accept.includes('application/json')) return true;
  if (req.method === 'POST' && String(req.headers['content-type'] || '').includes('json')) return true;
  if (req.method === 'POST') return false; // a form post wants a page back
  return accept.includes('text/html') ? false : true;
}

function send(res, status, body, headers = {}) {
  const buf = Buffer.isBuffer(body) ? body : Buffer.from(String(body));
  res.writeHead(status, {
    'Content-Length': buf.length,
    'Cache-Control': 'no-store, max-age=0, must-revalidate',
    'X-Content-Type-Options': 'nosniff',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'Content-Type, X-DSH-Capture-Nonce',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    ...headers,
  });
  res.end(buf);
}

function sendJson(res, status, payload) {
  send(res, status, JSON.stringify(payload) + '\n', { 'Content-Type': 'application/json; charset=utf-8' });
}

function sendHtml(res, status, html) {
  send(res, status, html, { 'Content-Type': 'text/html; charset=utf-8' });
}

function readBody(req, log) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        const err = new Error('payload_too_large');
        err.statusCode = 413;
        reject(err);
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', (err) => {
      log('capture_body_error', { error: String(err && err.message) });
      reject(err);
    });
  });
}

/**
 * @param {{store?: AccountStore, host?: string, port?: number, chatUrl?: string, nonce?: string,
 *          log?: (event: string, detail?: object) => void}} [options]
 */
export function createCaptureServer(options = {}) {
  const store = options.store || new AccountStore({ log: options.log });
  const host = options.host || DEFAULT_HOST;
  const requestedPort = Number.isFinite(options.port) ? options.port : 0;
  const chatUrl = options.chatUrl || DEFAULT_CHAT_URL;
  const nonce = options.nonce || newNonce();
  const log = typeof options.log === 'function' ? options.log : () => {};

  let captured = null;
  const waiters = [];
  let server = null;

  function settle(result) {
    captured = result;
    while (waiters.length) waiters.shift().resolve(result);
  }

  function respond(req, res, url, result) {
    if (wantsJson(req, url)) sendJson(res, result.ok ? 200 : 400, result);
    else sendHtml(res, result.ok ? 200 : 400, renderResultPage(result));
    if (result.ok) {
      log('credential_captured', { id: result.account && result.account.id, created: result.created });
      settle({ ok: true, account: result.account, created: result.created, source: req.method + ' ' + url.pathname });
    }
  }

  async function handle(req, res) {
    let url;
    try {
      url = new URL(req.url, 'http://' + host);
    } catch {
      sendJson(res, 400, { ok: false, error: 'bad_request_url' });
      return;
    }
    const path = url.pathname.replace(/\/+$/, '') || '/';
    const json = wantsJson(req, url);

    if (req.method === 'OPTIONS') {
      send(res, 204, '', { 'Access-Control-Max-Age': '600' });
      return;
    }
    if (path === '/healthz') {
      sendJson(res, 200, { ok: true, service: 'dsweb-capture', port: server ? server.address().port : null });
      return;
    }
    if (path === '/status') {
      if (!nonceMatches(nonce, url.searchParams.get('nonce'))) {
        sendJson(res, 401, { ok: false, error: 'bad_nonce' });
        return;
      }
      sendJson(res, 200, {
        ok: true,
        captured: Boolean(captured),
        account: captured && captured.account ? { id: captured.account.id, email: captured.account.email } : null,
      });
      return;
    }
    if (path === '/' || path === '/index.html' || path === '/capture' && req.method === 'GET') {
      const given = url.searchParams.get('nonce');
      const warn = given && !nonceMatches(nonce, given)
        ? '<div id="status" class="warn">This link carries an unknown nonce; the form below still works.</div>'
        : '';
      sendHtml(res, 200, renderWaitPage({ nonce, chatUrl, captured: captured && captured.account }) .replace('<ol>', warn + '<ol>'));
      return;
    }

    if (path === '/capture' && req.method === 'POST') {
      let body = '';
      try {
        body = await readBody(req, log);
      } catch (err) {
        sendJson(res, err.statusCode === 413 ? 413 : 400, {
          ok: false,
          error: err.statusCode === 413 ? 'payload_too_large' : 'unreadable_body',
        });
        return;
      }
      const fields = parseCaptureBody(req.headers['content-type'], body);
      const supplied = fields.nonce || url.searchParams.get('nonce') || req.headers['x-dsh-capture-nonce'];
      if (!nonceMatches(nonce, supplied)) {
        log('capture_rejected_bad_nonce', {});
        respond(req, res, url, { ok: false, error: 'bad_nonce' });
        return;
      }
      respond(req, res, url, store.upsertFromBlob(fields.raw, { id: fields.id, email: fields.email }));
      return;
    }

    if (path === '/submit' && (req.method === 'GET' || req.method === 'POST')) {
      const supplied = url.searchParams.get('nonce') || req.headers['x-dsh-capture-nonce'];
      if (!nonceMatches(nonce, supplied)) {
        if (json) sendJson(res, 401, { ok: false, error: 'bad_nonce' });
        else sendHtml(res, 401, renderResultPage({ ok: false, error: 'bad_nonce' }));
        return;
      }
      const token = url.searchParams.get('token') || '';
      const cookies = url.searchParams.get('cookies') || '';
      const raw = url.searchParams.get('raw') || (token ? token + (cookies ? '||Cookie: ' + cookies : '') : '');
      const result = store.upsertFromBlob(raw, {
        id: url.searchParams.get('id') || '',
        email: url.searchParams.get('email') || '',
      });
      respond(req, res, url, result);
      return;
    }

    if (json) sendJson(res, 404, { ok: false, error: 'not_found' });
    else sendHtml(res, 404, renderResultPage({ ok: false, error: 'not_found' }));
  }

  return {
    nonce,
    host,
    chatUrl,
    store,
    get port() {
      const addr = server && server.address();
      return addr && typeof addr === 'object' ? addr.port : null;
    },
    get url() {
      const p = this.port;
      return p ? 'http://' + host + ':' + p : null;
    },
    get captured() {
      return captured;
    },

    listen() {
      return new Promise((resolve, reject) => {
        server = createServer((req, res) => {
          handle(req, res).catch((err) => {
            log('capture_handler_error', { error: String(err && err.stack || err) });
            try {
              sendJson(res, 500, { ok: false, error: 'capture_handler_error' });
            } catch {
              /* already sent */
            }
          });
        });
        server.on('error', reject);
        server.listen(requestedPort, host, () => {
          server.removeListener('error', reject);
          log('capture_server_listening', { url: this.url });
          resolve(this);
        });
      });
    },

    /** Resolves on the first successful capture, on timeout, or on close. */
    waitFor({ timeoutMs = 300000 } = {}) {
      if (captured) return Promise.resolve(captured);
      return new Promise((resolve) => {
        const waiter = { resolve };
        waiters.push(waiter);
        const timer =
          timeoutMs > 0
            ? setTimeout(() => {
                const i = waiters.indexOf(waiter);
                if (i !== -1) waiters.splice(i, 1);
                resolve({ ok: false, error: 'timeout', timeoutMs });
              }, timeoutMs)
            : null;
        if (timer && timer.unref) timer.unref();
        waiter.resolve = (result) => {
          if (timer) clearTimeout(timer);
          resolve(result);
        };
      });
    },

    close() {
      return new Promise((resolve) => {
        while (waiters.length) waiters.shift().resolve({ ok: false, error: 'closed' });
        if (!server) {
          resolve();
          return;
        }
        const s = server;
        server = null;
        if (typeof s.closeAllConnections === 'function') s.closeAllConnections();
        s.close(() => {
          log('capture_server_closed', {});
          resolve();
        });
      });
    },
  };
}

export default createCaptureServer;
