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
// kasus 2: stall terjadi di RONDE NUDGE -> dianggap ronde kosong, retry, call terselamatkan
{
  let n = 0;
  U.WebClient.prototype.streamCompletion = async function* () {
    n++;
    yield { type: 'meta', responseMessageId: 50 + n };
    if (n === 1) { yield { type: 'text', delta: 'Saya cek dulu ya.' }; return; }
    if (n === 2) { await new Promise((r) => setTimeout(r, 1500)); return; }
    yield { type: 'text', delta: '<｜DSML｜function_calls><｜DSML｜invoke name="cek"><｜DSML｜parameter name="q" string="true">halo</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜function_calls>' };
  };
  const t1 = Date.now();
  let tool = 0;
  for await (const ev of mk().stream({ messages: [{ role: 'user', content: 'cek' }], model: 'deepseek-chat', thinking: false, search: false, tools: [{ type: 'function', function: { name: 'cek', description: 'cek', parameters: { type: 'object', properties: { q: { type: 'string' } } } } }] })) {
    if (/tool/.test(ev.type)) tool++;
  }
  assert.ok(Date.now() - t1 < 4000, 'stall nud harus dipotong: ' + (Date.now() - t1) + 'ms');
  assert.ok(tool > 0, 'call dari ronde setelah stall harus diterima');
}

U.WebClient.prototype.createChatSession = asli;
console.log('OK stall');