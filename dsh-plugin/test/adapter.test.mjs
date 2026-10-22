import assert from 'node:assert/strict';
import { coerceToolArgs } from '../src/adapter.js';
const schema = { type: 'object', properties: { file_path: { type: 'string' }, offset: { type: 'number' }, limit: { type: 'number' }, stream: { type: 'boolean' }, files: { type: 'array' } } };
const out = coerceToolArgs({ file_path: 'AGENTS.md', offset: '1', limit: '2000', stream: 'false', files: '["a.ts"]', other: 'keep' }, schema);
assert.deepEqual(out, { file_path: 'AGENTS.md', offset: 1, limit: 2000, stream: false, files: ['a.ts'], other: 'keep' });
assert.deepEqual(coerceToolArgs({ limit: 'abc' }, schema), { limit: 'abc' });
assert.deepEqual(coerceToolArgs({ a: '1' }, null), { a: '1' });
console.log('adapter.test OK');
