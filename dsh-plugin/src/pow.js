import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const WASM_PATH = path.resolve(here, '..', 'sha3_wasm_bg.wasm');

let cached = null;

async function getExports() {
  if (cached) return cached;
  const buf = fs.readFileSync(WASM_PATH);
  const mod = await WebAssembly.compile(buf);
  const imports = {};
  for (const im of WebAssembly.Module.imports(mod)) {
    if (!imports[im.module]) imports[im.module] = {};
    if (im.kind === 'function') imports[im.module][im.name] = function () {};
    else if (im.kind === 'global') imports[im.module][im.name] = new WebAssembly.Global({ value: 'i32', mutable: false }, 0);
    else if (im.kind === 'memory') imports[im.module][im.name] = new WebAssembly.Memory({ initial: 1 });
    else if (im.kind === 'table') imports[im.module][im.name] = new WebAssembly.Table({ initial: 1, element: 'anyfunc' });
  }
  const inst = await WebAssembly.instantiate(mod, imports);
  cached = inst.exports;
  return cached;
}

export async function solvePowAnswer(ch) {
  const exports = await getExports();
  const prefix = String(ch.salt) + '_' + String(ch.expire_at) + '_';
  const allocs = [];
  const enc = (s) => {
    const b = Buffer.from(String(s), 'utf8');
    const p = exports.__wbindgen_export_0(b.length, 1);
    new Uint8Array(exports.memory.buffer).set(b, p);
    allocs.push([p, b.length, 1]);
    return [p, b.length];
  };
  const sp = exports.__wbindgen_add_to_stack_pointer(-16);
  try {
    const [cp2, cl] = enc(ch.challenge);
    const [pp, pl] = enc(prefix);
    exports.wasm_solve(sp, cp2, cl, pp, pl, Number(ch.difficulty));
    const view = new DataView(exports.memory.buffer);
    const status = view.getInt32(sp, true);
    const nonce = view.getFloat64(sp + 8, true);
    if (status === 0) throw new Error('PoW solver returned no solution');
    return Math.trunc(nonce);
  } finally {
    exports.__wbindgen_add_to_stack_pointer(16);
    for (const [p, l, a] of allocs) {
      try { exports.__wbindgen_export_2(p, l, a); } catch {}
    }
  }
}

export function buildPowToken(ch, answer) {
  const payload = {
    algorithm: ch.algorithm || 'DeepSeekHashV1',
    challenge: ch.challenge,
    salt: ch.salt,
    answer,
    signature: ch.signature,
    target_path: ch.target_path || '/api/v0/chat/completion'
  };
  return Buffer.from(JSON.stringify(payload), 'utf8').toString('base64');
}

export async function buildPowHeader(ch) {
  const answer = await solvePowAnswer(ch);
  return buildPowToken(ch, answer);
}
