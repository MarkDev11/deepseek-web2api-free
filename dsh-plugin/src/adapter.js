import { AccountStore, launchOAuthPopupCapture } from './capture/index.js';
import { WebClient } from './upstream/index.js';
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { buildToolSystemPrompt, DsmlStreamSieve } from './dsml.js';
const DBG_PATH = process.env.DSWEB_DEBUG !== undefined ? process.env.DSWEB_DEBUG : 'E:\\TESTING\\deepseekapi\\dsweb-debug.log';
const DBG = DBG_PATH && DBG_PATH !== '0' ? fs.createWriteStream(DBG_PATH, { flags: 'a' }) : null;

export function coerceToolArgs(parsed, schema) {
  if (!parsed || !schema || !schema.properties) return parsed;
  const out = {};
  for (const [k, v] of Object.entries(parsed)) {
    const prop = schema.properties[k];
    const rawType = prop && prop.type;
    const type = Array.isArray(rawType) ? rawType[0] : rawType;
    if (typeof v === 'string') {
      if (type === 'number' || type === 'integer') {
        const n = Number(v.trim());
        if (v.trim() !== '' && Number.isFinite(n)) { out[k] = v.trim(); out[k] = type === 'integer' ? Math.round(n) : n; continue; }
      } else if (type === 'boolean') {
        if (v === 'true') { out[k] = true; continue; }
        if (v === 'false') { out[k] = false; continue; }
      } else if (type === 'object' || type === 'array') {
        try { out[k] = JSON.parse(v); continue; } catch {}
      }
    }
    out[k] = v;
  }
  return out;
}

export function formatMessagesToPrompt(messages, tools) {
  const parts = [];
  const toolSys = buildToolSystemPrompt(tools || []);
  if (toolSys) parts.push('[System]\n' + toolSys);
  for (const msg of messages || []) {
    let text = '';
    if (typeof msg.content === 'string') text = msg.content;
    else if (Array.isArray(msg.content)) text = msg.content.map((c) => (typeof c === 'string' ? c : (c && (c.text || c.content)) || JSON.stringify(c))).join('\n');
    else if (msg.content != null) text = JSON.stringify(msg.content);
    if (msg.role === 'system') parts.push('[System]\n' + text);
    else if (msg.role === 'assistant') {
      let t = text;
      if (Array.isArray(msg.tool_calls) && msg.tool_calls.length) {
        const calls = msg.tool_calls.map((tc) => {
          const fn = tc.function || {};
          let args = {};
          try { args = typeof fn.arguments === 'string' ? JSON.parse(fn.arguments) : (fn.arguments || {}); } catch { args = {}; }
          const params = Object.keys(args).map((k) => {
            const v = args[k];
            return '<｜DSML｜parameter name="' + k + '" string="true">' + (typeof v === 'string' ? v : JSON.stringify(v)) + '</｜DSML｜parameter>';
          }).join('');
          return '<｜DSML｜invoke name="' + (fn.name || 'unknown') + '">' + params + '</｜DSML｜invoke>';
        }).join('');
        t = (text ? text + '\n' : '') + '<｜DSML｜function_calls>' + calls + '</｜DSML｜function_calls>';
      }
      parts.push('[Assistant]\n' + t);
    }
    else if (msg.role === 'tool') parts.push('[Tool Result' + (msg.name ? ' of tool "' + msg.name + '"' : '') + ']\n' + text);
    else parts.push('[User]\n' + text);
  }
  return parts.join('\n\n');
}

export function looksLikeToolIntent(text) {
  let t = String(text || '').trim();
  if (/^```(?:json)?/i.test(t)) t = t.replace(/^```(?:json)?\s*/i, '').replace(/```\s*$/, '').trim();
  if (/<tool[\s>/]/i.test(t) || /tool_use/i.test(t)) return true;
  if (!/^[\[{]/.test(t)) return false;
  if (/^\{\s*"(?:name|function)"\s*:/i.test(t)) return true;
  try {
    const j = JSON.parse(t);
    const arr = Array.isArray(j) ? j : [j];
    return arr.some((x) => x && typeof x === 'object' && (('arguments' in x) && (x.name || (x.function && x.function.name))) || (x && x.function && 'arguments' in x.function));
  } catch { return false; }
}
export function plansMoreAction(text) {
  const t = String(text || '');
  return /\b(I'?ll|I will|let me|now I|going to|first,? I|next,? I|then I|I need to|I must|sekarang saya|sedang saya|akan saya|saya akan|akan ku[a-z]+|biar saya|perlu saya|saya\s+(?:akan\s+|sedang\s+)?(?:tambahkan|cek|lihat|periksa|patch|ubah|edit|cari|buka|tulis|buat|jalankan|coba|mau|terapkan|perbaiki)|aku\s+(?:akan\s+)?(?:tambahkan|cek|coba|mau))\b/i.test(t);
}

export function checkToolNonce(parsed, nonce) {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return { ok: true, parsed: parsed || {} };
  const v = parsed._nonce;
  if (v === undefined) return { ok: true, parsed };
  if (String(v) !== String(nonce)) return { ok: false, parsed };
  const rest = Object.assign({}, parsed); delete rest._nonce;
  return { ok: true, parsed: rest };
}

function normSearch(s) { return s.replace(/FINISHED/g, '').replace(/\[citation:(\d+)\]/g, '[$1]'); }
export class SearchStripper {
  constructor(enabled) { this.on = Boolean(enabled); this.head = ''; this.done = !this.on; }
  feed(text) {
    if (!this.on) return text || '';
    let out;
    if (!this.done) {
      this.head += text || '';
      if (this.head.length < 12) return '';
      out = this.head.replace(/^(?:WEB_SEARCH|SEARCHING|SEARCH)[ \t]*\r?\n?/i, '');
      this.head = ''; this.done = true;
    } else { out = text || ''; }
    return normSearch(out);
  }
  flush() {
    if (!this.on || this.done) return '';
    this.done = true;
    const out = this.head.replace(/^(?:WEB_SEARCH|SEARCHING|SEARCH)[ \t]*\r?\n?/i, '');
    this.head = '';
    return normSearch(out);
  }
}

export class DsWebAdapter {
  constructor(options) {
    const o = options || {};
    this.store = new AccountStore(o.storePath);
    this.sessions = new Map();
    this.keepAliveTtlMs = Number(o.keepAliveTtlMs || 300000);
    this.lastUsedAt = new Map();
    this.autoLogin = o.autoLogin !== false;
  }
  async listModels() {
    return [
      { id: 'deepseek-chat', name: 'DeepSeek Chat (Local Native)', contextWindow: 200000, maxTokens: 65536, input: ['text'] },
      { id: 'deepseek-reasoner', name: 'DeepSeek Reasoner (Local Native)', contextWindow: 200000, maxTokens: 65536, input: ['text'] }
    ];
  }
  keyFor(acct, opts) {
    const msgs = (opts && opts.messages) || [];
    let seed = '';
    for (const m of msgs.slice(0, 1)) {
      const c = typeof m.content === 'string' ? m.content : JSON.stringify(m.content || '');
      seed += (m.role || '') + ':' + c.slice(0, 200) + '\n';
    }
    const fp = createHash('sha256').update(seed).digest('hex').slice(0, 16);
    return acct.id + '|' + ((opts && opts.model) || 'deepseek-chat') + '|' + fp;
  }
  async sessionFor(acct, opts) {
    const k = this.keyFor(acct, opts);
    const s = this.sessions.get(k);
    if (s && s.sessionId) { this.lastUsedAt.delete(k); s.reused = true; return s; }
    this.lastUsedAt.delete(k);
    return { sessionId: null, parentId: null, reused: false, core: null, at: 0 };
  }
  async ensureAccount() {
    let acct = this.store.acquireNext();
    if (!acct) {
      if (!this.autoLogin) throw new Error('Belum ada akun DeepSeek. Jalankan: node scripts/login.mjs');
      acct = await launchOAuthPopupCapture({ storePath: this.store.storePath });
    }
    return acct;
  }
  async *stream(options) {
    const opts = options || {};
    const corePrompt = formatMessagesToPrompt(opts.messages || [], opts.tools || []);
    let promptBase = corePrompt;
    if ((opts.tools || []).length) promptBase += '\n\nREMINDER: To take an action, reply with ONLY the \uff5cDSML\uff5cfunction_calls block as your entire reply - never code, never prose. The tags are XML-like with a single |DSML| prefix on each tag.';
    const toolSchemas = new Map((opts.tools || []).map((tt) => { const f = tt.function || tt; return [f.name, f.parameters]; }));

    yield { type: 'block-start' };
    let lastErr;
    let sess = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      const acct = await this.ensureAccount();
      const client = new WebClient(acct, { onCookies: (c) => this.store.updateCookies(acct.id, c) });
      const sieve = new DsmlStreamSieve();
      const thinkSieve = new DsmlStreamSieve();
      const searchStrip = new SearchStripper(opts.search !== false);
      const seenCalls = new Set();
      const callKey = (tc) => (tc.name || '') + '|' + JSON.stringify(tc.arguments || tc.raw || '');
      const thinkingOn = Boolean(opts.thinking) || opts.model === 'deepseek-reasoner';
      let outChars = 0;
      let sawTools = false;
      let yielded = false;
      try {
        sess = await this.sessionFor(acct, opts);
        const nonce = Math.random().toString(36).slice(2, 10);
        const prompt = promptBase + ((opts.tools || []).length ? '\n\nTOOL BINDING: in every tool call include a parameter named "_nonce" with the exact value "' + nonce + '" (string="true"). A call with a different _nonce is stale and will be discarded.' : '');
        let sendPrompt = prompt;
        if (sess.sessionId && sess.core != null) {
          if (corePrompt === sess.core) {
            sendPrompt = prompt; // riwayat identik -> ulang pertanyaan yang sama
          } else if (corePrompt.startsWith(sess.core)) {
            sendPrompt = corePrompt.slice(sess.core.length) + prompt.slice(corePrompt.length); // riwayat memanjang -> kirim ekor saja
            sess.reused = true;
          } else {
            sendPrompt = prompt; // riwayat menyimpang (edit/regenerate) -> sesi sama, prompt penuh (semantik edit pesan)
          }
        }
        const sessionId = sess.sessionId || (await client.createChatSession());
        if (!sess.sessionId) { sess.sessionId = sessionId; if (this.sessions.size >= 100) { let oldestKey = null; let oldest = Infinity; for (const [kOld, vOld] of this.sessions) { const atOld = vOld && typeof vOld.at === 'number' ? vOld.at : 0; if (atOld < oldest) { oldest = atOld; oldestKey = kOld; } } if (oldestKey) this.sessions.delete(oldestKey); } this.sessions.set(this.keyFor(acct, opts), sess); }
        let curParent = sess.parentId || null;
        let nEvents = 0;
        const gate = { mode: 'watch', buf: '' };
        let partialAll = '';
        let continuationDone = false;
        let continued = false;
        let callsEmitted = 0;
        let sawTruncated = false;
        let nudged = false;
        let hadThinking = false;
        let nudgePass = false;
        function* emitCall(tc) {
          const k = callKey(tc);
          if (seenCalls.has(k) && !nudgePass) { if (DBG) DBG.write('DROP dup ' + String(k).slice(0, 80) + '\n'); return; }
          let parsed = tc.parsedArguments;
          if (!parsed) { try { parsed = JSON.parse(tc.arguments || '{}'); } catch { parsed = null; } }
          let chk = checkToolNonce(parsed, nonce);
          if (!chk.ok && nudgePass) { if (DBG) DBG.write('KEEP nonce-mismatch(nudge) ' + String(tc.name) + '\n'); const kept = Object.assign({}, chk.parsed); delete kept._nonce; chk = { ok: true, parsed: kept }; }
          if (!chk.ok) { if (DBG) DBG.write('DROP nonce ' + String(tc.name) + '\n'); return; }
          seenCalls.add(k); sawTools = true; callsEmitted++;
          const coerced = coerceToolArgs(chk.parsed, toolSchemas.get(tc.name));
          yield { type: 'tool-call-delta', id: tc.id, name: tc.name, argumentsTextDelta: JSON.stringify(coerced ?? {}) };
        }
        function* emitText(sTxt) {
          if (!sTxt) return;
          if (gate.mode === 'open') { yield { type: 'text-delta', text: sTxt }; return; }
          gate.buf += sTxt;
          if (gate.mode === 'hold') return;
          if (gate.buf.length >= 20) {
            if (looksLikeToolIntent(gate.buf)) gate.mode = 'hold';
            else { gate.mode = 'open'; yield { type: 'text-delta', text: gate.buf }; gate.buf = ''; }
          }
        }
        async function* consume(iter, gated) {
          for await (const ev of iter) {
            if (ev.type === 'meta' && ev.responseMessageId) { curParent = ev.responseMessageId; sess.parentId = curParent; sess.at = Date.now(); nEvents++; continue; }
            if (ev.type === 'thinking' && ev.delta) { if (DBG) DBG.write('T' + JSON.stringify(ev.delta) + '\n'); outChars += ev.delta.length; yielded = true; hadThinking = true;
              const tr = thinkSieve.feed(ev.delta);
              if (tr.text) yield { type: 'reasoning-delta', text: tr.text };
              for (const tc of tr.toolCalls) yield* emitCall(tc);
            }
            else if (ev.type === 'text' && ev.delta) {
              if (DBG) DBG.write('X' + JSON.stringify(ev.delta) + '\n'); outChars += ev.delta.length; yielded = true;
              const r = sieve.feed(ev.delta);
              partialAll += (r.text || '');
              const st = searchStrip.feed(r.text || '');
              if (gated) yield* emitText(st);
              else if (st) yield { type: 'text-delta', text: st };
              for (const tc of r.toolCalls) yield* emitCall(tc);
            }
          }
          const openBeforeFlush = sieve.inDsml;
          const tail = sieve.flush();
          const sTail = searchStrip.feed((tail && tail.text) || '') + searchStrip.flush();
          if (gated && openBeforeFlush && tail.toolCalls.length === 0) { if (sTail) sawTruncated = true; }
          else
          if (gated) yield* emitText(sTail);
          else if (sTail) yield { type: 'text-delta', text: sTail };
          for (const tc of tail.toolCalls) yield* emitCall(tc);
          const tailT = thinkSieve.flush();
          if (tailT.text) { yielded = true; yield { type: 'reasoning-delta', text: tailT.text }; }
          for (const tc of tailT.toolCalls) yield* emitCall(tc);
        }
        let midErr = null;
        try {
          if (DBG) DBG.write('REQ1 n=' + sendPrompt.length + ' parent=' + (curParent || 'null') + ' tools=' + ((opts.tools || []).length) + '\n');
          yield* consume(client.streamCompletion({ sessionId, prompt: sendPrompt, thinkingEnabled: thinkingOn, searchEnabled: opts.search !== false, parentMessageId: curParent }), true);
        } catch (eMid) {
          const contable = eMid && (eMid.code === 'UPSTREAM_HINT' || eMid.code === 'RATE_LIMIT' || eMid.code === 'UPSTREAM_TIMEOUT') && yielded && !sawTools && !continuationDone && curParent;
          if (!contable) throw eMid;
          midErr = eMid;
        }
        if (midErr) {
          continuationDone = true;
          await new Promise((rr) => setTimeout(rr, midErr.code === 'RATE_LIMIT' ? 4000 : 1500));
          const sofar = partialAll.slice(-500);
          const contPrompt = 'Your previous response was cut short by a server error after this text: \"\"\"' + sofar + '\"\"\". Continue from exactly that point, in the same language, without repeating any of it and without starting over.';
          const evB4C = nEvents;
          try {
            yield* consume(client.streamCompletion({ sessionId, prompt: contPrompt, thinkingEnabled: thinkingOn, searchEnabled: opts.search !== false, parentMessageId: curParent }), false);
          } catch (eCont) { throw midErr; }
          if (nEvents !== evB4C) continued = true;
          else { throw midErr; }
        }
        let held = gate.buf; gate.buf = '';
        if (held && !continued && !sawTools && curParent && looksLikeToolIntent(held)) {
          const evBefore = nEvents;
          gate.mode = 'open';
          const repairNote = 'Your previous response tried to call a tool but the envelope was invalid. Reply again with ONLY the \uff5cDSML\uff5cfunction_calls block for the next action, including the "_nonce" parameter set to "' + nonce + '". No prose, no JSON.';
          yield* consume(client.streamCompletion({ sessionId, prompt: repairNote, thinkingEnabled: thinkingOn, searchEnabled: opts.search !== false, parentMessageId: curParent }), false);
          if (nEvents === evBefore) { yielded = true; yield { type: 'text-delta', text: held }; }
        } else if (held && !continued) { yielded = true; yield { type: 'text-delta', text: held }; }
        const thinkOnly = !partialAll && hadThinking && !sawTools;
        const withTools = (opts.tools || []).length > 0;
        if (!sawTools && !nudged && curParent && (sawTruncated || thinkOnly || (withTools && plansMoreAction(partialAll)))) {
          nudged = true;
          const beforeN = callsEmitted;
          const maxRounds = 3;
          let forceNoThink = false;
          let nbuf = [];
          let ntxt = '';
          for (let nr = 0; nr < maxRounds; nr++) {
            if (DBG) DBG.write('NUDGE r' + (nr + 1) + ' thinkOnly=' + thinkOnly + ' trunc=' + sawTruncated + '\n');
            nudgePass = true;
            nbuf = [];
            const nPrompt = (thinkOnly
              ? ('Your previous response contained reasoning only, with no answer. Continue now: ' + (withTools ? 'emit ONLY the complete \uff5cDSML\uff5cfunction_calls block (with the "_nonce" parameter set to "' + nonce + '") or write your actual answer text.' : 'write your actual answer text now.') + ' Never reply with reasoning only. Never reply with just the _nonce value by itself, it is not an answer.')
              : ('You said you would take an action, but no tool block was emitted. If an action is still needed, reply with ' + (withTools ? 'ONLY the complete \uff5cDSML\uff5cfunction_calls block (with the "_nonce" parameter set to "' + nonce + '")' : 'your answer') + '. If everything is already done, reply DONE. Never reply with just the _nonce value by itself.'))
              + (forceNoThink ? ' Reply with final content only, no reasoning.' : '')
              + (nr >= 2 ? ' You replied with a plan already. Do NOT write another plan sentence: reply with ONLY the tool block now, or DONE.' : '');
            for await (const ev of consume(client.streamCompletion({ sessionId, prompt: nPrompt, thinkingEnabled: forceNoThink ? false : thinkingOn, searchEnabled: opts.search !== false, parentMessageId: curParent }), false)) nbuf.push(ev);
            ntxt = nbuf.filter((e) => e.type === 'text-delta').map((e) => e.text).join('').trim();
            if (ntxt && ntxt === String(nonce)) { if (DBG) DBG.write('DROP bare-nonce' + String.fromCharCode(10)); ntxt = ''; }
            else if (ntxt) ntxt = ntxt.replace(new RegExp('(?:' + String.fromCharCode(92) + 'r?' + String.fromCharCode(92) + 'n)+' + String.fromCharCode(92) + 's*' + nonce + String.fromCharCode(92) + 's*$'), '').trim();
            const planAgain = ntxt && plansMoreAction(ntxt);
            if (callsEmitted > beforeN || (ntxt && !/^DONE\b/i.test(ntxt) && !planAgain)) break;
            if (planAgain && DBG) DBG.write('NUDGE plan-prose diabaikan\n');
            if (/^DONE\b/i.test(ntxt)) break;
            forceNoThink = true;
          }
          nudgePass = false;
          if (DBG) DBG.write('NUDGE_END calls=' + (callsEmitted - beforeN) + ' txt=' + ntxt.length + '\n');
          if (callsEmitted > beforeN) { for (const ev of nbuf) yield ev; }
          else if (ntxt && !/^DONE\b/i.test(ntxt)) { yielded = true; yield { type: 'text-delta', text: ntxt }; }
        }
        if (!yielded) {
          if (sess.reused) {
            // sesi lama diabaikan upstream -> buka satu sesi baru (tetap satu sesi per percakapan)
            sess.reused = false;
            sess.parentId = null;
            if (sess.sessionId) { void client.deleteSession(sess.sessionId); }
            const sid2 = await client.createChatSession();
            let n2 = 0;
            for await (const e2 of client.streamCompletion({ sessionId: sid2, prompt, thinkingEnabled: thinkingOn, searchEnabled: opts.search !== false, parentMessageId: null })) {
              if (e2.type === 'meta' && e2.responseMessageId) { sess.parentId = e2.responseMessageId; }
              else if (e2.type === 'thinking' && e2.delta) { n2++; yield { type: 'reasoning-delta', text: e2.delta }; }
              else if (e2.type === 'text' && e2.delta) { n2++; yield { type: 'text-delta', text: e2.delta }; }
            }
            sess.sessionId = sid2;
            sess.core = corePrompt;
            sess.at = Date.now();
            if (!n2) { const ee2 = new Error('upstream returned an empty completion (no fragments; likely rate-limited)'); ee2.code = 'RATE_LIMIT'; throw ee2; }
          } else { const ee = new Error('upstream returned an empty completion (no fragments; likely rate-limited)'); ee.code = 'RATE_LIMIT'; throw ee; }
        }
        sess.core = corePrompt;
        sess.at = Date.now();
        yield { type: 'block-end' };
        const inputTokens = Math.max(1, Math.ceil(prompt.length / 4));
        const outputTokens = Math.max(1, Math.ceil(outChars / 4));
        yield { type: 'usage', inputTokens, outputTokens, totalTokens: inputTokens + outputTokens };
        yield { type: 'finish', reason: { kind: sawTools ? 'tool-calls' : 'stop' } };
        return;
      } catch (e) {
        if (DBG) DBG.write('ERR ' + String((e && (e.code || e.message)) || e) + '\n');
        const isMuted = e && (e.code === 'USER_MUTED' || /muted|suspend/i.test(String(e && e.message || '')));
        const isAuth = e && (e.code === 40003 || /invalid token|authorization failed/i.test(String(e && e.message || '')));
        if (isMuted) this.store.markMuted(acct.id, e && e.muteUntil);
        if (isAuth) this.store.removeAccount(acct.id); // token mati tak bisa pulih sendiri: hapus saja
        // akun mati/mute dan belum ada output -> coba akun lain (atau popup login) sekali
        if (!yielded && (isMuted || isAuth) && attempt === 0) { lastErr = e; continue; }
        if (sess && sess.sessionId && e.code === 'UPSTREAM_PARENT') { this.sessions.delete(this.keyFor(acct, opts)); this.lastUsedAt.set(this.keyFor(acct, opts), Date.now() + this.keepAliveTtlMs); void client.deleteSession(sess.sessionId); sess.parentId = null; sess.sessionId = null; } // parent ditolak = sesi beracun: buang sebelum retry
        if (!yielded && (e.code === 'RATE_LIMIT' || e.code === 'UPSTREAM_HINT' || e.code === 'UPSTREAM_TIMEOUT' || e.code === 'UPSTREAM_PARENT') && attempt < 2) { const waitMs = e.code === 'UPSTREAM_PARENT' ? 0 : (e.code === 'RATE_LIMIT' ? 6000 : (attempt === 0 ? 5000 : 15000)); await new Promise((rr) => setTimeout(rr, waitMs)); lastErr = e; continue; }
        if (sess && sess.sessionId && !yielded && (e.code === 'RATE_LIMIT' || e.code === 'UPSTREAM_HINT')) { this.sessions.delete(this.keyFor(acct, opts)); this.lastUsedAt.set(this.keyFor(acct, opts), Date.now() + this.keepAliveTtlMs); void client.deleteSession(sess.sessionId); }
        throw e;
      }
    }
    throw lastErr;
  }
}