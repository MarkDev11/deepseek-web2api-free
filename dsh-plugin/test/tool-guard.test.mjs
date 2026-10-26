import assert from 'node:assert/strict';
import { looksLikeToolIntent, checkToolNonce } from '../src/adapter.js';

assert.equal(checkToolNonce({ a: 1 }, 'x1').ok, true);
assert.deepEqual(checkToolNonce({ a: 1, _nonce: 'x1' }, 'x1').parsed, { a: 1 });
assert.equal(checkToolNonce({ a: 1, _nonce: 'lama' }, 'x1').ok, false);
assert.equal(checkToolNonce(null, 'x1').ok, true);

assert.equal(looksLikeToolIntent('{"name":"cek","arguments":{"a":1}}'), true);
assert.equal(looksLikeToolIntent('<tool>{"name":"a"}</tool>'), true);
assert.equal(looksLikeToolIntent('```json\n{"name":"a","arguments":{}}\n```'), true);
assert.equal(looksLikeToolIntent('[{"name":"a","arguments":{}}]'), true);
assert.equal(looksLikeToolIntent('{"name":"a","arg'), true);
assert.equal(looksLikeToolIntent('Halo dunia'), false);
assert.equal(looksLikeToolIntent('{"kota":"j"}'), false);
assert.equal(looksLikeToolIntent('function main() {'), false);

console.log('OK tool-guard');
