import { AccountStore, launchOAuthPopupCapture } from './capture/index.js';
import { WebClient } from './upstream/index.js';
import fs from 'node:fs';
import { buildToolSystemPrompt, DsmlStreamSieve } from './dsml.js';
const DBG = process.env.DSWEB_DEBUG ? fs.createWriteStream(process.env.DSWEB_DEBUG, { flags: 'a' }) : null;

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

export class DsWebAdapter {
  constructor(options) {
    const o = options || {};
    this.store = new AccountStore(o.storePath);
    this.autoLogin = o.autoLogin !== false;
  }
  async listModels() {
    return [
      { id: 'deepseek-chat', name: 'DeepSeek Chat (Local Native)', contextWindow: 200000, maxTokens: 65536, input: ['text'] },
      { id: 'deepseek-reasoner', name: 'DeepSeek Reasoner (Local Native)', contextWindow: 200000, maxTokens: 65536, input: ['text'] }
    ];
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
    let prompt = formatMessagesToPrompt(opts.messages || [], opts.tools || []);
    if ((opts.tools || []).length) prompt += '\n\nREMINDER: To take an action, reply with ONLY the \uff5cDSML\uff5cfunction_calls block as your entire reply - never code, never prose. The tags are XML-like with a single |DSML| prefix on each tag.';
    const toolSchemas = new Map((opts.tools || []).map((tt) => { const f = tt.function || tt; return [f.name, f.parameters]; }));
    const emitArgs = (tc) => {
      let parsed = tc.parsedArguments;
      if (!parsed) { try { parsed = JSON.parse(tc.arguments || '{}'); } catch { parsed = null; } }
      const coerced = coerceToolArgs(parsed, toolSchemas.get(tc.name));
      return JSON.stringify(coerced ?? {});
    };
    yield { type: 'block-start' };
    let lastErr;
    for (let attempt = 0; attempt < 2; attempt++) {
      const acct = await this.ensureAccount();
      const client = new WebClient(acct, { onCookies: (c) => this.store.updateCookies(acct.id, c) });
      const sieve = new DsmlStreamSieve();
      const thinkSieve = new DsmlStreamSieve();
      const seenCalls = new Set();
      const callKey = (tc) => (tc.name || '') + '|' + JSON.stringify(tc.arguments || tc.raw || '');
      const thinkingOn = Boolean(opts.thinking) || opts.model === 'deepseek-reasoner';
      let outChars = 0;
      let sawTools = false;
      let yielded = false;
      try {
        const sessionId = await client.createChatSession();
        for await (const ev of client.streamCompletion({ sessionId, prompt, thinkingEnabled: thinkingOn, searchEnabled: opts.search !== false })) {
          if (ev.type === 'thinking' && ev.delta) { if (DBG) DBG.write('T' + JSON.stringify(ev.delta) + '\n'); outChars += ev.delta.length; yielded = true;
            const tr = thinkSieve.feed(ev.delta);
            if (tr.text) yield { type: 'reasoning-delta', text: tr.text };
            for (const tc of tr.toolCalls) { const k = callKey(tc); if (!seenCalls.has(k)) { seenCalls.add(k); sawTools = true; yield { type: 'tool-call-delta', id: tc.id, name: tc.name, argumentsTextDelta: emitArgs(tc) }; } }
          }
          else if (ev.type === 'text' && ev.delta) {
            if (DBG) DBG.write('X' + JSON.stringify(ev.delta) + '\n'); outChars += ev.delta.length; yielded = true;
            const r = sieve.feed(ev.delta);
            if (r.text) yield { type: 'text-delta', text: r.text };
            for (const tc of r.toolCalls) { const k = callKey(tc); if (!seenCalls.has(k)) { seenCalls.add(k); sawTools = true; yield { type: 'tool-call-delta', id: tc.id, name: tc.name, argumentsTextDelta: emitArgs(tc) }; } }
          }
        }
        const tail = sieve.flush();
        if (tail.text) { yielded = true; yield { type: 'text-delta', text: tail.text }; }
        for (const tc of tail.toolCalls) { const k = callKey(tc); if (!seenCalls.has(k)) { seenCalls.add(k); sawTools = true; yield { type: 'tool-call-delta', id: tc.id, name: tc.name, argumentsTextDelta: emitArgs(tc) }; } }
        const tailT = thinkSieve.flush();
        if (tailT.text) { yielded = true; yield { type: 'reasoning-delta', text: tailT.text }; }
        for (const tc of tailT.toolCalls) { const k = callKey(tc); if (!seenCalls.has(k)) { seenCalls.add(k); sawTools = true; yield { type: 'tool-call-delta', id: tc.id, name: tc.name, argumentsTextDelta: emitArgs(tc) }; } }
        if (!yielded) { const ee = new Error('upstream returned an empty completion (no fragments; likely rate-limited)'); ee.code = 'RATE_LIMIT'; throw ee; }
        yield { type: 'block-end' };
        const inputTokens = Math.max(1, Math.ceil(prompt.length / 4));
        const outputTokens = Math.max(1, Math.ceil(outChars / 4));
        yield { type: 'usage', inputTokens, outputTokens, totalTokens: inputTokens + outputTokens };
        yield { type: 'finish', reason: { kind: sawTools ? 'tool-calls' : 'stop' } };
        return;
      } catch (e) {
        const isMuted = e && (e.code === 'USER_MUTED' || /muted|suspend/i.test(String(e && e.message || '')));
        const isAuth = e && (e.code === 40003 || /invalid token|authorization failed/i.test(String(e && e.message || '')));
        if (isMuted) this.store.markMuted(acct.id, e && e.muteUntil);
        if (isAuth) this.store.markQuarantined(acct.id, String(e && e.message || e));
        // akun mati/mute dan belum ada output -> coba akun lain (atau popup login) sekali
        if ((isMuted || isAuth || e.code === 'RATE_LIMIT' || e.code === 'UPSTREAM_HINT') && !yielded && attempt === 0) { if (e.code === 'RATE_LIMIT') { await new Promise((rr) => setTimeout(rr, 6000)); } lastErr = e; continue; }
        throw e;
      }
    }
    throw lastErr;
  }
}