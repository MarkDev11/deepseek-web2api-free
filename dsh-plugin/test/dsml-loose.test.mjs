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