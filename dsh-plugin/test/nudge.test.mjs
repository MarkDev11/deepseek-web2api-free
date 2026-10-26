import assert from 'node:assert/strict';
import { DsWebAdapter } from '../src/adapter.js';
import * as U from '../src/upstream/index.js';
const Q = '\uff5c';
const TOOLS = [{ type: 'function', function: { name: 'cek', description: 'cek', parameters: { type: 'object', properties: { q: { type: 'string' } }, required: ['q'] } } }];
function mk() {
  const a = new DsWebAdapter({ storePath: process.env.TEMP + '/dsweb-test-nudge.json' });
  a.ensureAccount = async () => ({ id: 'acct_sim', token: 'x'.repeat(24), cookies: '' });
  return a;
}
const asli = U.WebClient.prototype.createChatSession;
const blk = '<' + Q + 'DSML' + Q + 'invoke name="cek"><' + Q + 'DSML' + Q + 'parameter name="q" string="true">halo</' + Q + 'DSML' + Q + 'parameter></' + Q + 'DSML' + Q + 'invoke>';
async function run(gen) {
  const reqs = [];
  const thinks = [];
  U.WebClient.prototype.createChatSession = async function () { return 's1'; };
  U.WebClient.prototype.streamCompletion = async function* (o) { reqs.push(o.prompt); thinks.push(o.thinkingEnabled); yield* gen(reqs.length, o); };
  let out = '', tool = null;
  for await (const ev of mk().stream({ messages: [{ role: 'user', content: 'kerja' }], model: 'deepseek-chat', thinking: false, search: false, tools: TOOLS })) {
    if (ev.type === 'text-delta') out += ev.text;
    if (ev.type === 'tool-call-delta') tool = (tool || '') + (ev.argumentsTextDelta || '');
  }
  return { reqs, out, tool, thinks };
}

// 1) janji tanpa call -> nud sekali, call dari nud diterima
{
  const r1 = await run(async function* (n) {
    yield { type: 'meta', responseMessageId: 10 };
    if (n === 1) { yield { type: 'text', delta: 'Saya cek dulu ya.' }; return; }
    yield { type: 'text', delta: blk };
  });
  assert.equal(r1.reqs.length, 2, 'harus ada nud');
  assert.ok(r1.reqs[1].includes('function_calls'), r1.reqs[1]);
  assert.ok(r1.out.includes('Saya cek dulu'), r1.out);
  assert.equal(r1.tool, JSON.stringify({ q: 'halo' }));
}

// 2) jawaban final wajar -> TIDAK ada nud
{
  const r2 = await run(async function* (n) {
    yield { type: 'meta', responseMessageId: 11 };
    yield { type: 'text', delta: 'Sudah selesai, dark mode live di link yang sama.' };
  });
  assert.equal(r2.reqs.length, 1, 'final answer jangan di-nud: ' + r2.reqs.length);
}

// 3) DSML terpotong di akhir -> sampah ditahan, nud menghasilkan call bersih
{
  const r3 = await run(async function* (n) {
    yield { type: 'meta', responseMessageId: 12 };
    if (n === 1) { yield { type: 'text', delta: 'Mengubah<' + Q + 'DSML' + Q + 'invoke name="cek"><' + Q + 'DSML' + Q + 'parameter name="q" string="true">par' }; return; }
    yield { type: 'text', delta: blk };
  });
  assert.equal(r3.reqs.length, 2, 'truncation harus memicu nud');
  assert.ok(!r3.out.includes('par'), 'sampah terpotong tidak boleh tampil: ' + r3.out);
  assert.equal(r3.tool, JSON.stringify({ q: 'halo' }));
}

// 4) thinking-only -> nud 'reasoning only', jawaban asli menyusul
{
  const r4 = await run(async function* (n) {
    yield { type: 'meta', responseMessageId: 13 };
    yield { type: 'thinking', delta: 'Hmm saya perlu menjawab...' };
    if (n === 1) return;
    yield { type: 'text', delta: 'Jawaban asli untukmu.' };
  });
  assert.equal(r4.reqs.length, 2, 'thinking-only harus di-nud');
  assert.ok(r4.reqs[1].includes('reasoning only'), r4.reqs[1]);
  assert.ok(r4.out.includes('Jawaban asli'), r4.out);
}

// 5) thinking-only dua kali -> tidak throw, tetap selesai
{
  const r5 = await run(async function* () {
    yield { type: 'meta', responseMessageId: 14 };
    yield { type: 'thinking', delta: 'Hmm...' };
  });
  assert.equal(r5.reqs.length, 3, 'main + 2 nud');
  assert.equal(r5.out, '');
}


// 6) reasoning-only dua kali -> ronde ke-2 packing thinkingEnabled:false
{
  const r6 = await run(async function* (n) {
    yield { type: 'meta', responseMessageId: 15 };
    if (n <= 2) { yield { type: 'thinking', delta: 'Hmm...' }; return; }
    yield { type: 'text', delta: 'Akhirnya jawab.' };
  });
  assert.equal(r6.reqs.length, 3, 'butuh 2 nud: ' + r6.reqs.length);
  assert.equal(r6.thinks[2], false, 'ronde 3 harus thinking OFF');
  assert.ok(r6.out.includes('Akhirnya jawab'), r6.out);
}

// 8) nud membalas blok dgn _nonce salah (echo lama) -> tetap diterima, _nonce dibuang
{
  const blkBad = '<' + Q + 'DSML' + Q + 'invoke name="cek"><' + Q + 'DSML' + Q + 'parameter name="q" string="true">halo</' + Q + 'DSML' + Q + 'parameter><' + Q + 'DSML' + Q + 'parameter name="_nonce" string="true">nonce_lama_999</' + Q + 'DSML' + Q + 'parameter></' + Q + 'DSML' + Q + 'invoke>';
  const r8 = await run(async function* (n) {
    yield { type: 'meta', responseMessageId: 16 };
    if (n === 1) { yield { type: 'text', delta: 'Saya akan cek dulu ya.' }; return; }
    yield { type: 'text', delta: blkBad };
  });
  assert.equal(r8.reqs.length, 2);
  assert.equal(r8.tool, JSON.stringify({ q: 'halo' }), 'nonce salah harus tetap diterima di nud: ' + r8.tool);
}

U.WebClient.prototype.createChatSession = asli;
console.log('OK nudge');