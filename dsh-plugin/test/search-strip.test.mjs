import assert from 'node:assert/strict';
import { SearchStripper } from '../src/adapter.js';

const off = new SearchStripper(false);
assert.equal(off.feed('SEARCH\nhalo FINISHED'), 'SEARCH\nhalo FINISHED');

const s = new SearchStripper(true);
let out = '';
out += s.feed('SEARCH\n');
out += s.feed('Jakarta hujan.');
out += s.feed(' Selesai FINISHED[1].');
out += s.flush();
assert.ok(!out.includes('SEARCH'), out);
assert.ok(!out.includes('FINISHED'), out);
assert.ok(out.includes('Jakarta hujan.'), out);

const c = new SearchStripper(true);
let o2 = c.feed('WEB_SEARCH\nJawaban [citation:3] dan [citation:12].');
o2 += c.flush();
assert.equal(o2, 'Jawaban [3] dan [12].');

const t = new SearchStripper(true);
let o3 = t.feed('Halo.');
o3 += t.flush();
assert.equal(o3, 'Halo.');

console.log('OK search-strip');
