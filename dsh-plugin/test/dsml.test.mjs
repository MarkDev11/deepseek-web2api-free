import assert from 'node:assert/strict';
import { DsmlStreamSieve, parseDsmlToolCalls, normalizeDsml, DSML_BLOCK_START } from '../src/dsml.js';
const D = '｜';

function run(chunks) {
  const sieve = new DsmlStreamSieve();
  let text = ''; const tools = [];
  for (const c of chunks) { const r = sieve.feed(c); text += r.text; tools.push(...r.toolCalls); }
  const r = sieve.flush(); text += r.text; tools.push(...r.toolCalls);
  return { text, tools };
}

// 1) canonical wrapped block
const canonical = '<' + D + 'DSML' + D + 'function_calls>\n<' + D + 'DSML' + D + 'invoke name="get_weather">\n<' + D + 'DSML' + D + 'parameter name="city" string="true">Jakarta</' + D + 'DSML' + D + 'parameter>\n</' + D + 'DSML' + D + 'invoke>\n</' + D + 'DSML' + D + 'function_calls>';
let r = run(['Halo! ', canonical]);
assert.equal(r.text, 'Halo! ');
assert.equal(r.tools.length, 1);
assert.deepEqual(r.tools[0].parsedArguments, { city: 'Jakarta' });

// 2) char-by-char feeding must survive marker splits
r = run((('Ok ' + canonical).split('')));
assert.equal(r.text, 'Ok ');
assert.equal(r.tools.length, 1);

// 3) plain text untouched, marker-like tail emitted at flush
r = run(['satu dua', '<' + D + 'DSML']);
assert.equal(r.text, 'satu dua<' + D + 'DSML');
assert.equal(r.tools.length, 0);

// 4) parse-only, malformed closing tags (user sample)
const calls = parseDsmlToolCalls('<' + D + 'DSML' + D + 'invoke name="pwsh">\n<' + D + 'DSML' + D + 'parameter name="description" string="true">List files</' + D + 'DSML' + D + 'parameter>\n</' + D + 'DSML' + D + 'invoke>');
assert.equal(calls.length, 1);

// 5) EXACT user sample: unwrapped invoke, mangled closings, stray </invoke>, nasty splits
const sample = "I'll take a look at the current workspace for you.\n\n" +
  '<' + D + 'DSML' + D + 'invoke name="pwsh">\n' +
  '<' + D + 'DSML' + D + 'parameter name="description" string="true">List files and folders in workspace</' + '｜｜DSML｜｜ parameter>\n' +
  '<' + D + 'DSML' + D + 'parameter name="command" string="true">Get-ChildItem -Force</' + '｜｜DSML｜｜ parameter>\n' +
  '</' + '｜｜DSML｜｜ invoke>\n</invoke>';
const chunks = [];
for (let i = 0; i < sample.length; i += 7) chunks.push(sample.slice(i, i + 7));
r = run(chunks);
assert.equal(r.tools.length, 1);
assert.equal(r.tools[0].name, 'pwsh');
assert.equal(r.tools[0].parsedArguments.command, 'Get-ChildItem -Force');
assert.ok(!r.text.includes('DSML'), 'no DSML leak in text, got: ' + r.text);

// 6) stray loose closings arriving as plain text after the block (transcript defect)
r = run(['Baik. ', '</' + '｜DSML｜｜function_calls>\n', 'Saya coba lagi. ', '</｜｜DSML｜｜function_calls>']);
assert.ok(!r.text.includes('DSML'), 'no stray closings, got: ' + JSON.stringify(r.text));
assert.equal(r.text.replace(/\s+/g, ' ').trim(), 'Baik. Saya coba lagi.');
assert.equal(r.tools.length, 0);

// 7) loose double-pipe opening invoke still parses
const looseBlock = '<' + '｜｜DSML｜｜invoke name=\"pwsh\">\n<' + D + 'DSML' + D + 'parameter name=\"command\" string=\"true\">dir</' + D + 'DSML' + D + 'parameter>\n</' + D + 'DSML' + D + 'invoke>';
r = run([looseBlock]);
assert.equal(r.tools.length, 1);
assert.equal(r.text, '');

// 8) wrapped block then stray closing in a LATER chunk (exact transcript shape)
r = run([canonical, '\n</' + '｜DSML｜｜function_calls>\n', 'Lanjut.']);
assert.equal(r.tools.length, 1);
assert.ok(!r.text.includes('DSML'), 'no stray closings, got: ' + JSON.stringify(r.text));
assert.equal(r.text.trim(), 'Lanjut.');

// v3 varian model: penutup polos, pipe nol, tag terpotong, pipe ASCII
{
  const bare = '<｜DSML｜invoke name="grep"> <｜DSML｜parameter name="path" string="true">a.md</｜DSML｜parameter> </invoke> </invoke>';
  const s = new DsmlStreamSieve();
  const r = [s.feed(bare), s.flush()];
  const calls = r[0].toolCalls.concat(r[1].toolCalls);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].name, 'grep');
  assert.equal(calls[0].parsedArguments.path, 'a.md');
  assert.ok(!r[0].text.includes('DSML'));
}
{
  const s = new DsmlStreamSieve();
  const r = [s.feed('Baik. </DSML｜function_calls> lanjut'), s.flush()];
  const txt = r[0].text + r[1].text;
  assert.ok(!txt.includes('DSML'), 'pipe-nol close bocor: ' + txt);
  assert.ok(txt.includes('Baik.') && txt.includes('lanjut'));
}
{
  const s = new DsmlStreamSieve();
  const r = [s.feed('Jawaban akhir </｜DSML｜invoke'), s.flush()];
  const txt = r[0].text + r[1].text;
  assert.ok(!txt.includes('DSML'), 'truncated close bocor: ' + txt);
  assert.ok(txt.includes('Jawaban akhir'));
}
{
  const ascii = '<|DSML|function_calls><|DSML|invoke name="t"><|DSML|parameter name="x" string="true">1</|DSML|parameter></|DSML|invoke></|DSML|function_calls>';
  const s = new DsmlStreamSieve();
  const r = [s.feed(ascii), s.flush()];
  const calls = r[0].toolCalls.concat(r[1].toolCalls);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].name, 't');
  assert.equal(r[0].text + r[1].text, '');
}
console.log('dsml.test OK');
