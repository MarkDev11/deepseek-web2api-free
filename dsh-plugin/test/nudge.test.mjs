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
    yield { type: 'text', delta: 'Sudah selesai, dark mode live di link yang sama. [done]' };
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
  assert.equal(r5.reqs.length, 6, 'main + 5 nud (cap attempts)');
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

// 9) ronde nud membalas nonce telanjang -> diabaikan; ronde 2 (thinking OFF) harus jawab
{
  const fixed = (0.777).toString(36).slice(2, 10);
  const asliRandom = Math.random;
  Math.random = () => 0.777;
  let r9;
  try {
    r9 = await run(async function* (n) {
      yield { type: 'meta', responseMessageId: 17 };
      if (n === 1) { yield { type: 'text', delta: 'Sekarang saya ganti UI daftar token.' }; return; }
      if (n === 2) { yield { type: 'text', delta: fixed }; return; }
      yield { type: 'text', delta: 'Sudah saya ganti dan rebuild.' };
    });
  } finally { Math.random = asliRandom; }
  assert.equal(r9.reqs.length, 3, 'nonce telanjang bukan jawaban: ' + r9.reqs.length);
  assert.equal(r9.thinks[2], false, 'ronde 3 thinking OFF');
  assert.ok(r9.out.includes('Sudah saya ganti'), r9.out);
  assert.ok(!r9.out.includes(fixed), 'nonce tak boleh bocor: ' + r9.out);
}

// 10) nud membalas kalimat rencana lagi -> bukan jawaban; ronde 2 (thinking OFF) menghasilkan call
{
  const r10 = await run(async function* (n) {
    yield { type: 'meta', responseMessageId: 18 };
    if (n === 1) { yield { type: 'text', delta: 'Sekarang saya ganti UI daftar token.' }; return; }
    if (n === 2) { yield { type: 'text', delta: 'Sedang saya terapkan perbaikannya ke tiga file.' }; return; }
    yield { type: 'text', delta: blk };
  });
  assert.equal(r10.reqs.length, 3, 'plan-prose bukan jawaban: ' + r10.reqs.length);
  assert.equal(r10.thinks[2], false, 'ronde 3 thinking OFF');
  assert.equal(r10.tool, JSON.stringify({ q: 'halo' }), r10.tool);
  assert.ok(!r10.out.includes('Sedang saya terapkan'), 'plan-prose tak boleh keluar: ' + r10.out);
}

// 11) rencana terus sampai ronde 3 -> DONE menghentikan; hanya teks asli yang keluar
{
  const r11 = await run(async function* (n) {
    yield { type: 'meta', responseMessageId: 19 };
    if (n === 1) { yield { type: 'text', delta: 'Baik, sedang saya kerjakan sekarang.' }; return; }
    if (n <= 3) { yield { type: 'text', delta: 'Akan saya lanjutkan segera.' }; return; }
    yield { type: 'text', delta: 'DONE' };
  });
  assert.equal(r11.reqs.length, 4, 'main + 3 nud: ' + r11.reqs.length);
  assert.ok(r11.reqs[3].includes('Do NOT write another plan sentence'), r11.reqs[3].slice(0, 120));
  assert.ok(!r11.out.includes('Akan saya lanjutkan'), r11.out);
}

// 12) ronde nud benar-benar kosong -> retry seketika, lalu call diterima
{
  const r12 = await run(async function* (n) {
    yield { type: 'meta', responseMessageId: 21 };
    if (n === 1) { yield { type: 'text', delta: 'Saya cek dulu kondisinya ya.' }; return; }
    if (n === 2) return;
    yield { type: 'text', delta: blk };
  });
  assert.equal(r12.reqs.length, 3, 'harus retry setelah kosong: ' + r12.reqs.length);
  assert.equal(r12.tool, JSON.stringify({ q: 'halo' }), r12.tool);
}

U.WebClient.prototype.createChatSession = asli;

// 12) jawaban pendek tanpa rencana sama sekali -> tetap ditagih (shortOpenish)
{
  const r = await run(async function* (n) {
    yield { type: 'meta', responseMessageId: 10 + n };
    if (n === 1) { yield { type: 'text', delta: 'Baik ya.' }; return; }
    yield { type: 'text', delta: blk };
  });
  assert.ok(r.tool, 'jawaban pendek harus ditagih, dapat: ' + r.out);
  assert.ok(r.reqs.length >= 2, 'harus ada ronde tagih kedua request');
}

// 13) blok tanpa nama tool (invoke hilang) -> buangan bersuara + tagih ulang
{
  const blkBad = '<' + Q + 'DSML' + Q + 'function_calls>\n<' + Q + 'DSML' + Q + 'parameter name="q" string="true">x</' + Q + 'DSML' + Q + 'parameter>\n</' + Q + 'DSML' + Q + 'function_calls>';
  const r = await run(async function* (n) {
    yield { type: 'meta', responseMessageId: 30 + n };
    if (n === 1) { yield { type: 'text', delta: 'Ambil datanya.' + blkBad }; return; }
    yield { type: 'text', delta: blk };
  });
  assert.ok(r.tool, 'call tanpa nama harus ditagih ulang');
}


// 14) protokol [done]: tanpa [done] -> ditagih + reminder; dengan [done] -> diam
{
  const r = await run(async function* (n) {
    yield { type: 'meta', responseMessageId: 60 + n };
    if (n === 1) { yield { type: 'text', delta: 'Sekarang jelas: turn setelah tool result tidak pernah menghasilkan teks. Saya instrumentasi adapter untuk melihat apa yang diterima.' }; return; }
    yield { type: 'text', delta: 'Sudah kuberes kan. [done]' };
  });
  assert.ok(r.reqs[0].includes('[done]'), 'prompt harus memuat STOP PROTOCOL [done]');
  assert.equal(r.reqs.length, 2, 'tanpa [done] harus ditagih sekali');
  assert.ok(/Reminder/.test(r.reqs[1]), 'tagihan menyertakan reminder');
  assert.ok(!/\[done\]/.test(r.out), 'token [done] tidak membocori jawaban: ' + r.out);
}

console.log('OK nudge');