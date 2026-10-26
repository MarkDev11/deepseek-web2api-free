process.env.DSWEB_DEBUG = '0';
process.env.DSWEB_STALL_FIRST_MS = '2000';
process.env.DSWEB_STALL_IDLE_MS = '400';
import assert from 'node:assert/strict';
const { DsWebAdapter } = await import('../src/adapter.js');
const U = await import('../src/upstream/index.js');
function mk() {
  const a = new DsWebAdapter({ storePath: (process.env.TEMP || '.') + '/dsweb-test-stall.json' });
  a.ensureAccount = async () => ({ id: 'acct_sim', token: 'x'.repeat(24), cookies: '' });
  return a;
}
const asli = U.WebClient.prototype.createChatSession;
U.WebClient.prototype.createChatSession = async function () { return 's1'; };
const reqs = [];
U.WebClient.prototype.streamCompletion = async function* (o) {
  reqs.push(o.prompt);
  yield { type: 'meta', responseMessageId: 30 + reqs.length };
  if (reqs.length === 1) {
    yield { type: 'text', delta: 'Halo sebagian.' };
    await new Promise((r) => setTimeout(r, 1500)); // stall: tidak pernah menyelesaikan blok
    yield { type: 'text', delta: 'ini tidak akan pernah dibaca tepat waktu' };
    return;
  }
  yield { type: 'text', delta: 'Lanjut selesai.' };
};
const t0 = Date.now();
let out = '';
for await (const ev of mk().stream({ messages: [{ role: 'user', content: 'kerja' }], model: 'deepseek-chat', thinking: false, search: false })) {
  if (ev.type === 'text-delta') out += ev.text;
}
const dt = Date.now() - t0;
assert.ok(dt < 3000, 'stall harus dipotong watchdog, bukan menunggu sleep: ' + dt + 'ms');
assert.ok(reqs.length >= 2, 'stall harus memicu pemulihan (nud/retry), reqs=' + reqs.length);
assert.ok(out.includes('Halo sebagian.'), out);
assert.ok(out.includes('Lanjut selesai.'), out);
U.WebClient.prototype.createChatSession = asli;
console.log('OK stall');