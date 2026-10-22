import assert from 'node:assert/strict';
import { apply } from '../index.js';

let setKey = null, setVal = null, adapterId = null;
const ctx = {
  log: (lvl, msg) => console.log('[ctx.log]', lvl, msg),
  credentials: { set: (k, v) => { setKey = k; setVal = v; } },
  llm: { registerAdapter: (id) => { adapterId = id; } }
};
const dispose = apply(ctx, { port: 55399, keyName: 'DSWEB_TEST_KEY' });
await new Promise((r) => setTimeout(r, 300));
assert.equal(setKey, 'DSWEB_TEST_KEY');
assert.ok(setVal && setVal.length > 10);
void adapterId;

const bad = await fetch('http://127.0.0.1:55399/v1/models', { headers: { authorization: 'Bearer wrong' } });
assert.equal(bad.status, 401);
const ok = await fetch('http://127.0.0.1:55399/v1/models', { headers: { authorization: 'Bearer ' + setVal } });
assert.equal(ok.status, 200);
const j = await ok.json();
assert.equal(j.data[0].id, 'deepseek-chat');
dispose();
console.log('dsweb.test OK (server + auth + models)');
