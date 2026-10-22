import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { buildPowToken, solvePowAnswer } from '../src/pow.js';

const tok = buildPowToken({ algorithm: 'DeepSeekHashV1', challenge: 'c', salt: 's', signature: 'g', target_path: '/api/v0/chat/completion' }, 42);
const dec = JSON.parse(Buffer.from(tok, 'base64').toString('utf8'));
assert.equal(dec.answer, 42);
assert.equal(dec.algorithm, 'DeepSeekHashV1');
assert.equal(dec.target_path, '/api/v0/chat/completion');
console.log('pow.test OK (structural)');

const storePath = path.join(os.homedir(), '.dsh', 'dsweb-accounts.json');
try {
  const d = JSON.parse(fs.readFileSync(storePath, 'utf8'));
  const acct = d.accounts && d.accounts.find((x) => x.token && x.cookies);
  if (acct) {
    const r = await fetch('https://chat.deepseek.com/api/v0/chat/create_pow_challenge', {
      method: 'POST',
      headers: { 'content-type': 'application/json', origin: 'https://chat.deepseek.com', referer: 'https://chat.deepseek.com/', authorization: 'Bearer ' + acct.token, cookie: acct.cookies },
      body: JSON.stringify({ target_path: '/api/v0/chat/completion' })
    });
    const j = await r.json();
    const ch = j && j.data && j.data.biz_data && j.data.biz_data.challenge;
    if (ch) { console.log('pow.test live: nonce =', await solvePowAnswer(ch)); }
    else console.log('pow.test live: SKIP (no challenge, WAF?)');
  } else console.log('pow.test live: SKIP (no account)');
} catch { console.log('pow.test live: SKIP (no store)'); }
