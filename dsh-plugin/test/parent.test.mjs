import assert from 'node:assert/strict';
import { DsWebAdapter } from '../src/adapter.js';
import * as U from '../src/upstream/index.js';
const asli = U.WebClient.prototype.createChatSession;
const delAsli = U.WebClient.prototype.deleteSession;
const asliS = U.WebClient.prototype.streamCompletion;
function mk() {
  const a = new DsWebAdapter({ storePath: process.env.TEMP + '/dsweb-test-parent.json' });
  a.ensureAccount = async () => ({ id: 'acct_sim', token: 'x'.repeat(24), cookies: '' });
  return a;
}
U.WebClient.prototype.deleteSession = async function () {};

// UPSTREAM_PARENT -> sesi baru, parent null, prompt penuh, sukses
{
  const reqs = [];
  let sid = 0;
  U.WebClient.prototype.createChatSession = async function () { return 's' + (++sid); };
  U.WebClient.prototype.streamCompletion = async function* (o) {
    reqs.push({ parent: o.parentMessageId, prompt: o.prompt, session: o.sessionId });
    if (reqs.length === 1) { const e = new Error('upstream rejected parent message id (session tip lost)'); e.code = 'UPSTREAM_PARENT'; throw e; }
    yield { type: 'meta', responseMessageId: 40 };
    yield { type: 'text', delta: 'Jawaban berhasil.' };
  };
  let out = '';
  for await (const ev of mk().stream({ messages: [{ role: 'user', content: 'kerja penuh ya' }], model: 'deepseek-chat', thinking: false, search: false })) {
    if (ev.type === 'text-delta') out += ev.text;
  }
  assert.equal(reqs.length, 2, 'harus retry sekali');
  assert.equal(reqs[1].parent, null, 'retry tanpa parent: ' + reqs[1].parent);
  assert.notEqual(reqs[1].session, reqs[0].session, 'sesi harus baru');
  assert.ok(reqs[1].prompt.includes('kerja penuh'), reqs[1].prompt);
  assert.equal(out, 'Jawaban berhasil.');
}

U.WebClient.prototype.createChatSession = asli;
U.WebClient.prototype.deleteSession = delAsli;
U.WebClient.prototype.streamCompletion = asliS;
console.log('OK parent');