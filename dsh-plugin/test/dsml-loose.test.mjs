import assert from 'node:assert/strict';
import { parseDsmlToolCalls, normalizeDsml, DsmlStreamSieve } from '../src/dsml.js';
import { checkToolNonce } from '../src/adapter.js';
const Q = '\uff5c';

// kasus tangkapan layar: penutup parameter pertama TANPA '<'
{
  const raw = '<' + Q + 'DSML' + Q + 'invoke name="Pwsh"><' + Q + 'DSML' + Q + 'parameter name="command" string="true">Search OpenMuse UI for dark mode support' + Q + 'DSML' + Q + 'parameter> <' + Q + 'DSML' + Q + 'parameter name="_nonce" string="true">zcpny08s</' + Q + 'DSML' + Q + 'parameter></' + Q + 'DSML' + Q + 'invoke>';
  const calls = parseDsmlToolCalls(raw);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].name, 'Pwsh');
  assert.equal(calls[0].parsedArguments.command, 'Search OpenMuse UI for dark mode support');
  assert.equal(calls[0].parsedArguments._nonce, 'zcpny08s');
  const chk = checkToolNonce(calls[0].parsedArguments, 'zcpny08s');
  assert.equal(chk.ok, true);
  assert.deepEqual(chk.parsed, { command: 'Search OpenMuse UI for dark mode support' });
}

// regresi: bentuk kanonik tidak boleh rusak oleh aturan penutup telanjang
{
  const kanonik = '<' + Q + 'DSML' + Q + 'invoke name="a"><' + Q + 'DSML' + Q + 'parameter name="p" string="true">v</' + Q + 'DSML' + Q + 'parameter></' + Q + 'DSML' + Q + 'invoke>';
  assert.equal(normalizeDsml(kanonik), kanonik);
  const c = parseDsmlToolCalls(kanonik);
  assert.deepEqual(c[0].parsedArguments, { p: 'v' });
}

// sieve utuh-utuh: blok cacat dipecah antar-delta tetap terparse bersih
{
  const s = new DsmlStreamSieve();
  const raw = 'Baik<' + Q + 'DSML' + Q + 'invoke name="cek"><' + Q + 'DSML' + Q + 'parameter name="q" string="true">halo dunia' + Q + 'DSML' + Q + 'parameter></' + Q + 'DSML' + Q + 'invoke>';
  let text = '', calls = [];
  for (let i = 0; i < raw.length; i += 7) { const o = s.feed(raw.slice(i, i + 7)); text += o.text; calls = calls.concat(o.toolCalls); }
  const f = s.flush(); text += f.text; calls = calls.concat(f.toolCalls);
  assert.equal(text, 'Baik');
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].parsedArguments, { q: 'halo dunia' });
}
console.log('OK dsml-loose');
// regresi (laporan user 2026-10-27): wrapper alias 'calls' + pipa ganda + spasi + CDATA
{
  const Q2 = Q + Q;
  const tag = (n) => '<' + Q2 + 'DSML' + Q2 + ' ' + n + '>';
  const end = (n) => '</' + Q2 + 'DSML' + Q2 + ' ' + n + '>';
  const raw = 'Tahu. ' + tag('calls') + ' ' + tag('invoke name="read"')
    + tag('parameter name="file_path"') + '<![CDATA[src/cli.js]]>' + end('parameter')
    + end('invoke') + end('calls') + ' selesai';
  const calls = parseDsmlToolCalls(raw);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].name, 'read');
  assert.equal(calls[0].parsedArguments.file_path, 'src/cli.js'); // CDATA terbuka
  const s = new DsmlStreamSieve();
  let text = '';
  let n = 0;
  for (let i = 0; i < raw.length; i += 9) {
    const o = s.feed(raw.slice(i, i + 9));
    text += o.text; n += o.toolCalls.length;
  }
  const f = s.flush();
  text += f.text; n += f.toolCalls.length;
  assert.equal(n, 1);
  assert.equal(text.replace(/\s+/g, ' ').trim(), 'Tahu. selesai');
  console.log('OK loose calls-alias + double pipes + CDATA never leaks');
}

// regresi: atribut string="false" tidak memecah parameter
{
  const Q2 = Q + Q;
  const raw = '<' + Q2 + 'DSML' + Q2 + ' invoke name="read">'
    + '<' + Q2 + 'DSML' + Q2 + ' parameter name="offset" string="false">19</' + Q2 + 'DSML' + Q2 + ' parameter>'
    + '</' + Q2 + 'DSML' + Q2 + ' invoke>';
  const calls = parseDsmlToolCalls(raw);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].parsedArguments.offset, '19');
  console.log('OK string="false" attribute tolerated');
}
