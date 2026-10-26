import assert from 'node:assert/strict';
import { DsWebAdapter } from '../src/adapter.js';
import * as U from '../src/upstream/index.js';

function mk() {
  const a = new DsWebAdapter({ storePath: process.env.TEMP + '/dsweb-test-mid.json' });
  a.ensureAccount = async () => ({ id: 'acct_sim', token: 'x'.repeat(24), cookies: '' });
  return a;
}
const asliCreate = U.WebClient.prototype.createChatSession;

// 1) error di tengah output -> auto-continue di sesi yang sama, parent = tip
{
  const reqs = []; let round = 0;
  U.WebClient.prototype.createChatSession = async function () { return 'sess-' + (++round); };
  U.WebClient.prototype.streamCompletion = async function* (o) {
    reqs.push({ parent: o.parentMessageId, prompt: o.prompt });
    yield { type: 'meta', responseMessageId: 77 };
    yield { type: 'text', delta: 'Halo ini jawaban yang ' };
    yield { type: 'text', delta: 'terpotong karena ' };
    if (reqs.length === 1) { const e = new Error('upstream hint error: generation_err'); e.code = 'UPSTREAM_HINT'; throw e; }
    yield { type: 'text', delta: 'kesalahan server, lalu berlanjut.' };
  };
  const a = mk();
  let out = '';
  for await (const ev of a.stream({ messages: [{ role: 'user', content: 'coba' }], model: 'deepseek-chat', thinking: false, search: false })) {
    if (ev.type === 'text-delta') out += ev.text;
  }
  assert.ok(out.includes('terpotong karena'), out);
  assert.ok(out.includes('berlanjut'), out);
  assert.equal(reqs.length, 2);
  assert.equal(reqs[1].parent, 77);
  assert.ok(reqs[1].prompt.includes('terpotong karena'), reqs[1].prompt);
}

// 2) error sebelum output apa pun -> retry penuh seperti dulu, BUKAN continue
{
  const reqs = [];
  U.WebClient.prototype.createChatSession = async function () { return 's2'; };
  U.WebClient.prototype.streamCompletion = async function* (o) {
    reqs.push(o.prompt);
    if (reqs.length === 1) { const e = new Error('upstream hint error: generation_err'); e.code = 'UPSTREAM_HINT'; throw e; }
    yield { type: 'meta', responseMessageId: 9 };
    yield { type: 'text', delta: 'jawaban bersih.' };
  };
  const a = mk();
  let out = '';
  for await (const ev of a.stream({ messages: [{ role: 'user', content: 'coba dua' }], model: 'deepseek-chat', thinking: false, search: false })) {
    if (ev.type === 'text-delta') out += ev.text;
  }
  assert.equal(out, 'jawaban bersih.');
  assert.equal(reqs.length, 2);
  assert.ok(!reqs[1].includes('cut short'), reqs[1].slice(0, 80));
}

U.WebClient.prototype.createChatSession = asliCreate;
console.log('OK mid-continue');