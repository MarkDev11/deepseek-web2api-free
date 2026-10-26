process.env.DSWEB_DEBUG = '0';
import assert from 'node:assert/strict';
const { plansMoreAction, DsWebAdapter } = await import('../src/adapter.js');
const U = await import('../src/upstream/index.js');
// H2: pendeteksi kalimat rencana
assert.ok(plansMoreAction('Saya lanjutkan membaca file-nya.'), 'lanjutkan=bukan selesai');
assert.ok(plansMoreAction('Saya baca dulu implementasinya.'), 'baca=rencana');
assert.ok(plansMoreAction("I'll check the server logs."), 'EN rencana');
assert.ok(!plansMoreAction('Sudah saya jalankan kok.'), 'lampau bukan rencana');
assert.ok(!plansMoreAction('Selesai, semuanya sudah beres.'), 'selesai bukan rencana');
assert.ok(!plansMoreAction('Fiturnya ada di file config.'), 'netral bukan rencana');
assert.ok(plansMoreAction('Interface sudah lengkap. Sekarang saya tulis provider host.'), 'klausa netral tak membunuh klausa rencana');
assert.ok(plansMoreAction('Sudah selesai bagian A. Saya cek bagian B dulu.'), 'rencana pasca-selesai tetap rencana');
import { shortOpenish } from '../src/adapter.js';
assert.ok(shortOpenish('Perbaikan masuk dan build bersih. Sekarang restart dan buktikan tidak ada proses yatim lagi.'), 'kalimat kerja pendek = terbuka');
assert.ok(!shortOpenish('Sudah selesai semua kok.'), 'penyangkal lampau = final');
assert.ok(!shortOpenish('X'.repeat(221)), 'panjang = jawaban utuh');

// H3: upstream hanya boleh 1-in-flight per adapter
function mk() {
  const a = new DsWebAdapter({ storePath: (process.env.TEMP || '.') + '/dsweb-test-harden.json' });
  a.ensureAccount = async () => ({ id: 'acct_sim', token: 'x'.repeat(24), cookies: '' });
  return a;
}
U.WebClient.prototype.createChatSession = async function () { return 's'; };
let infl = 0, peak = 0;
U.WebClient.prototype.streamCompletion = async function* () {
  infl++; peak = Math.max(peak, infl);
  yield { type: 'meta', responseMessageId: 1 };
  await new Promise((r) => setTimeout(r, 60));
  yield { type: 'text', delta: 'oke' };
  infl--;
};
const a = mk();
const one = async (msg) => { let s = ''; for await (const ev of a.stream({ messages: [{ role: 'user', content: msg }], model: 'deepseek-chat', thinking: false, search: false })) if (ev.type === 'text-delta') s += ev.text; return s; };
const [r1, r2] = await Promise.all([one('satu'), one('dua')]);
assert.equal(r1 + r2, 'okeoke', r1 + '|' + r2);
assert.equal(peak, 1, 'peak in-flight harus 1, dapat ' + peak);
console.log('OK harden');