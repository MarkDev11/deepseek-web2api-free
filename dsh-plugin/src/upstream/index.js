import { buildPowHeader } from '../pow.js';
import { mergeCookieStrings } from '../capture/index.js';

const BASE_URL = 'https://chat.deepseek.com';

export function buildFingerprintHeaders(token, cookies, extra) {
  const headers = {
    'accept': '*/*',
    'accept-language': 'en-US,en;q=0.9',
    'content-type': 'application/json',
    'origin': BASE_URL,
    'referer': BASE_URL + '/',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
    'sec-ch-ua': '"Chromium";v="150", "Not:A-Brand";v="24", "Google Chrome";v="150"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'x-app-version': '20241129.1',
    'x-client-bundle-id': 'com.deepseek.chat',
    'x-client-locale': 'en_US',
    'x-client-platform': 'web',
    'x-client-version': '2.5.0',
    'x-client-timezone-offset': String(-new Date().getTimezoneOffset() * 60)
  };
  if (token) headers['authorization'] = 'Bearer ' + token;
  if (cookies) headers['cookie'] = cookies;
  return Object.assign(headers, extra || {});
}

export class WebClient {
  constructor(account, options) {
    const o = options || {};
    this.accountId = account.id;
    this.token = account.token || '';
    this.cookies = account.cookies || '';
    this.baseUrl = o.baseUrl || BASE_URL;
    this.fetchImpl = o.fetchImpl || globalThis.fetch;
    this.onCookies = o.onCookies || null;
  }
  _captureCookies(res) {
    try {
      const list = typeof res.headers.getSetCookie === 'function' ? res.headers.getSetCookie() : [];
      if (list && list.length) {
        const kv = list.map((sc) => sc.split(';')[0]).join('; ');
        const merged = mergeCookieStrings(this.cookies, kv);
        if (merged !== this.cookies) { this.cookies = merged; if (this.onCookies) this.onCookies(merged); }
      }
    } catch {}
  }
  async _post(path, body, extra, fetchOpts) {
    const res = await this.fetchImpl(this.baseUrl + path, {
      method: 'POST',
      headers: buildFingerprintHeaders(this.token, this.cookies, extra),
      body: JSON.stringify(body),
      signal: (fetchOpts && fetchOpts.signal) || undefined
    });
    this._captureCookies(res);
    return res;
  }
  async deleteSession(sessionId) {
    if (!sessionId) return false;
    try { await this._post('/api/v0/chat_session/delete', { chat_session_id: sessionId }); return true; } catch { return false; }
  }
  static _authFail(data) {
    if (data && (data.code === 40003 || String(data.msg || '').toLowerCase().indexOf('authorization failed') !== -1)) {
      const e = new Error((data.msg || 'Authorization Failed') + ' [40003]');
      e.code = 40003;
      throw e;
    }
  }
  async createPowChallenge() {
    const res = await this._post('/api/v0/chat/create_pow_challenge', { target_path: '/api/v0/chat/completion' });
    const j = await res.json().catch(() => null);
    WebClient._authFail(j);
    const ch = j && j.data && j.data.biz_data && j.data.biz_data.challenge;
    if (!ch) throw new Error('create_pow_challenge gagal: ' + JSON.stringify(j || {}).slice(0, 200));
    return ch;
  }
  async createChatSession() {
    const ch = await this.createPowChallenge();
    const pow = await buildPowHeader(ch);
    const res = await this._post('/api/v0/chat_session/create', { target_path: '/api/v0/chat/completion' }, { 'x-ds-pow-response': pow });
    const j = await res.json().catch(() => null);
    WebClient._authFail(j);
    const bd = j && j.data && j.data.biz_data;
    const id = bd && (bd.id || (bd.chat_session && bd.chat_session.id));
    if (!id) throw new Error('create_session gagal: ' + JSON.stringify(j || {}).slice(0, 200));
    return id;
  }
  async *streamCompletion(opts) {
    const ch = await this.createPowChallenge();
    const pow = await buildPowHeader(ch);
    let res;
    try {
      res = await this._post('/api/v0/chat/completion', {
        chat_session_id: opts.sessionId,
        parent_message_id: opts.parentMessageId || null,
        prompt: opts.prompt,
        ref_file_ids: [],
        thinking_enabled: Boolean(opts.thinkingEnabled),
        search_enabled: Boolean(opts.searchEnabled),
        preempt: false
      }, { 'x-ds-pow-response': pow, 'accept': 'text/event-stream' }, { signal: AbortSignal.timeout(600000) });
    } catch (e) {
      if (e && (e.name === 'TimeoutError' || e.name === 'AbortError')) { const te = new Error('upstream request timed out'); te.code = 'UPSTREAM_TIMEOUT'; throw te; }
      throw e;
    }
    this._captureCookies(res);

    const ct = res.headers.get('content-type') || '';
    if (ct.indexOf('text/event-stream') === -1) {
      const j = await res.json().catch(() => null);
      WebClient._authFail(j);
      const d = j && j.data;
      if (d && (d.biz_code === 5 || String(d.biz_msg || '').indexOf('muted') !== -1)) {
        const e = new Error('user is muted');
        e.code = 'USER_MUTED';
        e.muteUntil = d.mute_until ? d.mute_until * 1000 : Date.now() + 300000;
        throw e;
      }
      if (d && d.biz_code === 4 && String(d.biz_msg || '').indexOf('频繁') !== -1) {
        const e = new Error('rate limited (hint)');
        e.code = 'RATE_LIMIT';
        throw e;
      }
      throw new Error('upstream non-SSE: ' + JSON.stringify(j || {}).slice(0, 200));
    }

    yield* parseCompletionStream(res.body);
  }
}

export async function* parseCompletionStream(body) {
  const reader = body.getReader();
  const dec = new TextDecoder('utf-8');
  let buf = '';
  let curEvent = null;
  let fragment = 'RESPONSE';
  const emitted = { text: 0, thinking: 0 };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf('\n')) !== -1) {
      let line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (line.endsWith('\r')) line = line.slice(0, -1);
      if (!line) { curEvent = null; continue; }
      if (line.startsWith('event:')) { curEvent = line.slice(6).trim(); continue; }
      if (!line.startsWith('data:')) continue;
      const ds = line.slice(5).trim();
      if (!ds || ds === '[DONE]') continue;
      let pkt;
      try { pkt = JSON.parse(ds); } catch { continue; }
      // hint/toast dalam SSE: rate limit (freq) & mute — jangan biarkan lolos jadi 'kosong'
      const hintObj = (pkt && typeof pkt.v === 'object' && pkt.v && !Array.isArray(pkt.v)) ? Object.assign({}, pkt, pkt.v) : pkt;
      const bcode = hintObj.biz_code;
      const bmsg = String(hintObj.biz_msg || hintObj.msg || '');
      const fr = hintObj.finish_reason;
      if (curEvent === 'hint' || bcode != null || (fr && fr !== 'stop') || hintObj.type === 'error') {
        if (bcode === 5 || /muted|suspend/i.test(bmsg)) {
          const e = new Error('user is muted');
          e.code = 'USER_MUTED';
          const mu = hintObj.mute_until;
          e.muteUntil = mu ? mu * 1000 : Date.now() + 300000;
          throw e;
        }
        if (bcode === 4 || fr === 'rate_limit_reached' || /频繁|frequent|too many/i.test(bmsg)) {
          const e = new Error('rate limited (hint): ' + (bmsg || fr));
          e.code = 'RATE_LIMIT';
          throw e;
        }
        if (hintObj.type === 'error' && fr && fr !== 'stop') {
          const e = new Error('upstream hint error: ' + (bmsg || fr));
          e.code = 'UPSTREAM_HINT';
          throw e;
        }
      }
      if (curEvent === 'ready' && pkt && pkt.response_message_id) {
        yield { type: 'meta', responseMessageId: pkt.response_message_id };
        continue;
      }
      if (pkt.p === 'response/fragments/-1/type' && typeof pkt.v === 'string') {
        if (pkt.v === 'THINK') fragment = 'THINK';
        else if (pkt.v === 'RESPONSE') fragment = 'RESPONSE';
        continue;
      }
      if (pkt.p === 'response/fragments' && Array.isArray(pkt.v)) {
        for (const frag of pkt.v) {
          if (!frag || typeof frag.content !== 'string' || !frag.content.length) continue;
          if (frag.type === 'THINK') fragment = 'THINK';
          else if (frag.type === 'RESPONSE') fragment = 'RESPONSE';
          const kind = fragment === 'THINK' ? 'thinking' : 'text';
          if (frag.content.length > emitted[kind]) {
            const delta = frag.content.slice(emitted[kind]);
            emitted[kind] = frag.content.length;
            yield { type: kind, delta };
          }
        }
        continue;
      }
      if (pkt.p === 'response/status') continue;

      if (typeof pkt.v === 'string' && pkt.v.length) {
        const kind = fragment === 'THINK' ? 'thinking' : 'text';
        emitted[kind] += pkt.v.length;
        yield { type: kind, delta: pkt.v };
        continue;
      }
      if (pkt.v && typeof pkt.v === 'object') {
        if (Array.isArray(pkt.v)) {
          for (const item of pkt.v) {
            if (item && typeof item.p === 'string' && typeof item.v === 'string' && (item.p === 'content' || item.p.endsWith('/content')) && item.v.length) {
              const kind = fragment === 'THINK' ? 'thinking' : 'text';
              emitted[kind] += item.v.length;
              yield { type: kind, delta: item.v };
            }
          }
          continue;
        }
        const resp = pkt.v.response;
        if (resp && Array.isArray(resp.fragments)) {
          for (const frag of resp.fragments) {
            if (!frag || typeof frag.content !== 'string' || !frag.content.length) continue;
            const kind = frag.type === 'THINK' ? 'thinking' : 'text';
            if (frag.type === 'THINK') fragment = 'THINK';
            else if (frag.type === 'RESPONSE') fragment = 'RESPONSE';
            if (frag.content.length > emitted[kind]) {
              const delta = frag.content.slice(emitted[kind]);
              emitted[kind] = frag.content.length;
              yield { type: kind, delta };
            }
          }
          continue;
        }
      }
    }
  }
}
