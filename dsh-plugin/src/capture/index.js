import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import net from 'node:net';
import { spawn } from 'node:child_process';

export const DEFAULT_STORE_PATH = path.join(os.homedir(), '.dsh', 'dsweb-accounts.json');

export function findSystemBrowser() {
  const bases = [
    process.env['PROGRAMFILES'],
    process.env['ProgramFiles(x86)'],
    path.join(os.homedir(), 'AppData', 'Local')
  ].filter(Boolean);
  const candidates = [];
  for (const b of bases) {
    candidates.push(path.join(b, 'Google', 'Chrome', 'Application', 'chrome.exe'));
    candidates.push(path.join(b, 'Microsoft', 'Edge', 'Application', 'msedge.exe'));
    candidates.push(path.join(b, 'BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'));
  }
  for (const c of candidates) if (fs.existsSync(c)) return c;
  throw new Error('Chrome / Edge / Brave tidak ditemukan di sistem.');
}

function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => { const p = srv.address().port; srv.close(() => resolve(p)); });
    srv.on('error', reject);
  });
}

export function extractTokenValue(raw) {
  if (!raw) return '';
  const t = String(raw).trim();
  if (t.startsWith('{')) {
    try { const o = JSON.parse(t); if (o && typeof o.value === 'string' && o.value.trim()) return o.value.trim(); if (o && o.value === null) return ''; } catch {}
  }
  return t;
}

export function serializeCookieMap(map) {
  return Object.entries(map).filter(([k, v]) => k && v !== undefined && v !== '').map(([k, v]) => k + '=' + v).join('; ');
}

export function mergeCookieStrings(existing, incoming) {
  const map = {};
  for (const part of [existing, incoming]) {
    if (!part) continue;
    for (const seg of String(part).split(';')) {
      const idx = seg.indexOf('=');
      if (idx > 0) { const k = seg.slice(0, idx).trim(); const v = seg.slice(idx + 1).trim(); if (k) map[k] = v; }
    }
  }
  return serializeCookieMap(map);
}

export class AccountStore {
  constructor(storePath = DEFAULT_STORE_PATH) { this.storePath = storePath; this.rr = 0; }
  load() {
    try {
      if (!fs.existsSync(this.storePath)) return { version: 1, accounts: [] };
      const d = JSON.parse(fs.readFileSync(this.storePath, 'utf8'));
      if (!Array.isArray(d.accounts)) d.accounts = [];
      return d;
    } catch { return { version: 1, accounts: [] }; }
  }
  save(d) { fs.mkdirSync(path.dirname(this.storePath), { recursive: true }); fs.writeFileSync(this.storePath, JSON.stringify(d, null, 2), 'utf8'); }
  upsertAccount({ id, email, token, cookies }) {
    const clean = extractTokenValue(token);
    if (!clean) throw new Error('Token kosong / tidak valid');
    const d = this.load();
    const acctId = id || email || ('acct_' + clean.slice(0, 8));
    const now = Date.now();
    let acct = d.accounts.find((a) => a.id === acctId || (email && a.email === email));
    if (acct) {
      acct.token = clean;
      acct.cookies = mergeCookieStrings(acct.cookies, cookies);
      if (email) acct.email = email;
      acct.state = 'idle'; acct.mutedUntil = 0; acct.lastError = ''; acct.updatedAt = now;
    } else {
      acct = { id: acctId, email: email || acctId, token: clean, cookies: mergeCookieStrings('', cookies), state: 'idle', mutedUntil: 0, createdAt: now, updatedAt: now };
      d.accounts.push(acct);
    }
    this.save(d);
    return acct;
  }
  acquireNext() {
    const d = this.load();
    const now = Date.now();
    const usable = d.accounts.filter((a) => a.token && a.state !== 'quarantined' && !(a.mutedUntil && a.mutedUntil > now));
    if (!usable.length) return null;
    const pick = usable[this.rr % usable.length];
    this.rr = (this.rr + 1) % usable.length;
    return pick;
  }
  removeAccount(acctId) {
    const d = this.load();
    const before = d.accounts.length;
    d.accounts = d.accounts.filter((a) => a.id !== acctId);
    if (d.accounts.length === before) return false;
    this.save(d);
    return true;
  }
  markQuarantined(acctId, reason) {
    const d = this.load();
    const a = d.accounts.find((x) => x.id === acctId);
    if (a) { a.state = 'quarantined'; a.lastError = reason || ''; a.updatedAt = Date.now(); this.save(d); }
  }
  markMuted(acctId, untilMs) {
    const d = this.load();
    const a = d.accounts.find((x) => x.id === acctId);
    if (a) { a.mutedUntil = untilMs || Date.now() + 300000; a.updatedAt = Date.now(); this.save(d); }
  }
  updateCookies(acctId, newCookies) {
    if (!newCookies) return;
    const d = this.load();
    const a = d.accounts.find((x) => x.id === acctId);
    if (a) { a.cookies = mergeCookieStrings(a.cookies, newCookies); this.save(d); }
  }
}

function inspectTargetViaCdp(wsUrl) {
  return new Promise((resolve) => {
    let ws;
    try { ws = new WebSocket(wsUrl); } catch { return resolve(null); }
    let id = 1;
    const pending = new Map();
    const timer = setTimeout(() => { try { ws.close(); } catch {} resolve(null); }, 5000);
    const send = (method, params) => new Promise((res, rej) => {
      const mid = id++;
      pending.set(mid, { res, rej });
      ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
    });
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(String(ev.data));
        if (msg.id && pending.has(msg.id)) {
          const { res, rej } = pending.get(msg.id);
          pending.delete(msg.id);
          if (msg.error) rej(new Error(msg.error.message)); else res(msg.result);
        }
      } catch {}
    };
    ws.onerror = () => { clearTimeout(timer); resolve(null); };
    ws.onopen = async () => {
      try {
        const ev = await send('Runtime.evaluate', {
          expression: '(function(){try{var raw=localStorage.getItem("userToken");if(!raw)return null;var t=null;try{var o=JSON.parse(raw);if(o&&typeof o.value==="string")t=o.value}catch(e){t=raw}if(!t||t.trim().length<16)return null;return JSON.stringify({token:t.trim(),href:location.href})}catch(e){return null}})()',
          returnByValue: true
        });
        const valStr = ev && ev.result && ev.result.value;
        if (!valStr) { clearTimeout(timer); try { ws.close(); } catch {} return resolve(null); }
        const info = JSON.parse(valStr);
        if (!info.token || info.token.length < 16) { clearTimeout(timer); try { ws.close(); } catch {} return resolve(null); }
        const ck = await send('Network.getCookies', { urls: ['https://chat.deepseek.com', 'https://deepseek.com'] });
        const map = {};
        for (const c of (ck.cookies || [])) if (c && c.name && c.value) map[c.name] = c.value;
        clearTimeout(timer);
        try { ws.close(); } catch {}
        resolve({ token: info.token, cookies: serializeCookieMap(map), email: null });
      } catch { clearTimeout(timer); try { ws.close(); } catch {} resolve(null); }
    };
  });
}

export function classifyProbeError(e) {
  const msg = String((e && e.message) || e || '');
  if (e && e.code === 40003) return 'dead';
  if (/invalid token|authorization failed/i.test(msg)) return 'dead';
  if (e && e.code === 'USER_MUTED') return 'muted';
  if (/muted|suspend/i.test(msg)) return 'muted';
  return 'network';
}

export async function probeAccountHealth(acct, timeoutMs) {
  const limit = timeoutMs || 45000;
  const run = (async () => {
    const { WebClient } = await import('../upstream/index.js');
    const client = new WebClient(acct, {});
    const sessionId = await client.createChatSession();
    let got = false;
    for await (const ev of client.streamCompletion({ sessionId, prompt: 'OK', thinkingEnabled: false })) {
      if ((ev.type === 'text' || ev.type === 'thinking') && ev.delta) got = true;
    }
    return got ? 'ok' : 'ok';
  })();
  const timer = new Promise((_, rej) => setTimeout(() => rej(new Error('probe timeout')), limit));
  try {
    await Promise.race([run, timer]);
    return { ok: true, reason: 'ok' };
  } catch (e) {
    const kind = classifyProbeError(e);
    if (kind === 'network') return { ok: true, reason: 'network, dianggap ok' };
    return { ok: false, reason: kind, muteUntil: (e && e.muteUntil) || undefined };
  }
}

export async function launchOAuthPopupCapture(options) {
  const opts = options || {};
  const storePath = opts.storePath || DEFAULT_STORE_PATH;
  const timeoutMs = opts.timeoutMs || 600000;
  const browserExe = opts.browserExe || findSystemBrowser();
  const port = await getFreePort();
  const profileDir = path.join(os.homedir(), '.dsh', 'dsweb-browser-profile');
  fs.mkdirSync(profileDir, { recursive: true });
  const args = [
    '--remote-debugging-port=' + port,
    '--user-data-dir=' + profileDir,
    '--no-first-run',
    '--no-default-browser-check',
    '--new-window',
    'https://chat.deepseek.com/'
  ];
  const proc = spawn(browserExe, args, { stdio: 'ignore', detached: false });
  const badTokens = new Set();
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (proc.exitCode !== null && Date.now() - started > 15000) throw new Error('Browser ditutup sebelum login selesai.');
    try {
      const res = await fetch('http://127.0.0.1:' + port + '/json/list');
      if (res.ok) {
        const targets = await res.json();
        const page = targets.find((t) => t.type === 'page' && String(t.url || '').indexOf('deepseek.com') !== -1 && t.webSocketDebuggerUrl);
        if (page) {
          const cred = await inspectTargetViaCdp(page.webSocketDebuggerUrl);
          if (cred && cred.token && !badTokens.has(cred.token)) {
            const store = new AccountStore(storePath);
            const saved = store.upsertAccount(cred);
            const health = await probeAccountHealth(saved);
            if (health.ok) {
              try { proc.kill(); } catch {}
              return saved;
            }
            badTokens.add(cred.token);
            if (health.reason === 'muted') store.markMuted(saved.id, health.muteUntil || Date.now() + 3600000);
            else store.markQuarantined(saved.id, 'login probe: ' + health.reason);
            // jendela dibiarkan terbuka: silakan logout & login akun lain, token baru akan dicoba lagi
          }
        }
      }
    } catch {}
    await new Promise((r) => setTimeout(r, 1500));
  }
  try { proc.kill(); } catch {}
  throw new Error('Waktu login habis (timeout ' + Math.round(timeoutMs / 60000) + ' menit).');
}
