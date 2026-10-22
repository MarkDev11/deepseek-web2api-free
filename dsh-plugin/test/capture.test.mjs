import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { AccountStore, mergeCookieStrings, extractTokenValue } from '../src/capture/index.js';

const tmp = path.join(os.tmpdir(), 'dsweb-test-' + Date.now() + '.json');
const store = new AccountStore(tmp);

store.upsertAccount({ email: 'a@b.c', token: JSON.stringify({ value: 'tok_0123456789abcdef0123456789abcdef' }), cookies: 'ds_session_id=abc; aws-waf-token=xyz' });
const a = store.acquireNext();
assert.equal(a.email, 'a@b.c');
assert.equal(a.token, 'tok_0123456789abcdef0123456789abcdef');
assert.equal(extractTokenValue('{"value":"  xx  "}'), 'xx');
assert.equal(mergeCookieStrings('a=1; b=2', 'b=3; c=4'), 'a=1; b=3; c=4');
store.markQuarantined(a.id, '40003');
assert.equal(store.acquireNext(), null);
fs.rmSync(tmp, { force: true });
console.log('capture.test OK');
