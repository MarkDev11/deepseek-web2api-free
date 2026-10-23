import assert from 'node:assert/strict';
import { coerceToolArgs, formatMessagesToPrompt } from '../src/adapter.js';
const schema = { type: 'object', properties: { file_path: { type: 'string' }, offset: { type: 'number' }, limit: { type: 'number' }, stream: { type: 'boolean' }, files: { type: 'array' } } };
const out = coerceToolArgs({ file_path: 'AGENTS.md', offset: '1', limit: '2000', stream: 'false', files: '["a.ts"]', other: 'keep' }, schema);
assert.deepEqual(out, { file_path: 'AGENTS.md', offset: 1, limit: 2000, stream: false, files: ['a.ts'], other: 'keep' });
assert.deepEqual(coerceToolArgs({ limit: 'abc' }, schema), { limit: 'abc' });
assert.deepEqual(coerceToolArgs({ a: '1' }, null), { a: '1' });
// tool_calls assistant diflattening balik jadi DSML; tool result melabeli nama
{
  const p = formatMessagesToPrompt([
    { role: 'user', content: 'cek file a' },
    { role: 'assistant', content: null, tool_calls: [{ id: 'c1', function: { name: 'read', arguments: '{\"file_path\":\"a.txt\"}' } }] },
    { role: 'tool', tool_call_id: 'c1', name: 'read', content: 'ISI FILE' }
  ], [{ function: { name: 'read', parameters: { type: 'object', properties: { file_path: { type: 'string' } } } } }]);
  if (!p.includes('invoke name=\"read\"')) throw new Error('tool_calls tidak diserialisasi');
  if (!p.includes('a.txt')) throw new Error('argumen hilang');
  if (!p.includes('Tool Result of tool \"read\"')) throw new Error('label tool hilang');
  if (!p.includes('ISI FILE')) throw new Error('isi hasil hilang');
}
console.log('adapter.test OK');
