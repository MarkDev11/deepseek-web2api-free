# dsh-plugin-dsweb — module contracts (read before writing code)

Goal: DeepSeek Harness gets a **built-in style** model provider that talks to
`chat.deepseek.com` **directly** from the plugin (no Vercel, no local python
server, no OpenAI-compatible hop). Credentials are captured by opening a real
browser window (Edge/Chrome) through the DevTools protocol and harvesting
token + cookies + request headers after the human logs in and sends one chat.

Everything is ESM, Node 24 (Electron 44 utility process). `fetch` and
`WebSocket` are global. `@deepseek-ai/*` imports resolve inside the app.

## Files and owners

| file | owner | responsibility |
|---|---|---|
| `src/pow.js` | upstream | WASM PoW solver, port of `adapter.py::_WASMSolver` |
| `src/upstream.js` | upstream | DeepSeek web HTTP client: session create, completion SSE, headers/cookies, error taxonomy |
| `src/dsml.js` | dsml | DSML tool prompt builder + streaming sieve, port of `tool_dsml.py` / `tool_sieve.py` |
| `src/capture.js` | capture | Edge/Chrome CDP login capture → accounts.json |
| `src/adapter.js` | lead | `LlmAdapter` subclass, chunk emission, session reuse |
| `index.js` | lead | plugin entry, account loading, `ctx.llm.registerAdapter` |
| `scripts/login.mjs` | capture | CLI: open browser, capture, validate, save |

Reference implementation (Python, must be followed closely): `../adapter.py`,
`../tool_dsml.py`, `../tool_sieve.py`, `../server.py` (prompt building),
`../token_counter.py`. The Python code is authoritative for wire details.

## src/pow.js

```js
export async function loadSolver(wasmPath) -> Promise<{ solve(challenge: string, salt: string, expireAt: number, difficulty: number): number }>
```
- WASM has **no imports**; instantiate with an empty import object.
- Exports used: `memory`, `wasm_solve`, `__wbindgen_add_to_stack_pointer`,
  `__wbindgen_export_0` (malloc), `__wbindgen_export_2` (free).
- Algorithm must mirror `adapter.py::_WASMSolver.solve` exactly: prefix
  ```${salt}_${expireAt}_````, 16-byte stack for the result, UTF-8 encode
  challenge and prefix, call `wasm_solve(stack, chalPtr, chalLen, prefixPtr, prefixLen, difficulty)`,
  read int32 at stack (0 = failure → throw `PoWError`), read f64 at stack+8 as the
  answer, restore the stack, free allocations in `finally`.
- Accept `difficulty` as number; pass as f64.
- Export `class PoWError extends Error`.

## src/upstream.js

```js
export class WebClient {
  constructor({ token, cookies, wasmPath, clientVersion = "2.5.0", timezoneOffset = null, locale = "en-US" })
  get cookies(): string                      // live cookie jar as "a=1; b=2"
  setToken(token)
  async checkCredentials(): Promise<{ ok: boolean, status: number, code?: number, message?: string }>
  async createSession(): Promise<{ sessionId: string }>
  async *completion({ prompt, sessionId, parentMessageId, thinking, search }): AsyncGenerator<Event>
  async syncCookies(): Promise<boolean>       // merge Set-Cookie from responses
}
/** Event = { type: "ready", requestMessageId: number|string|null, responseMessageId: number|string|null, modelType?: string }
 *         | { type: "text", text: string }
 *         | { type: "thinking", text: string }
 *         | { type: "status", value: string }
 *         | { type: "toast", message: string }
 *         | { type: "hint", kind: "rate_limit" | "error", message: string, muteUntil?: number }
 */
export class UpstreamAuthError extends Error {}      // 40003 / "invalid token"
export class RateLimitError extends Error { muteUntil = null }  // user muted / rate_limit hint
export class UpstreamHintError extends Error {}
export class UpstreamEmptyError extends Error {}
```

Wire details to copy from `adapter.py`:
- Base URL `https://chat.deepseek.com`, headers `authorization: Bearer <token>`,
  browser headers `user-agent`, `x-client-version`, `x-client-platform: web`,
  `x-client-bundle-id: com.deepseek.chat`, `x-client-locale`,
  `x-client-timezone-offset` (minutes), `x-app-version` if Python sends it,
  plus `x-ds-pow-response` on session create and completion, plus optional
  `x-hif-leim`/`x-hif-dliq` if `_HifProvider` succeeds (best effort).
- PoW: `POST /api/v0/chat/create_pow_challenge` `{"target_path":"/api/v0/chat/completion"}`,
  then header value = base64(JSON({algorithm, challenge, salt, answer, signature, target_path})).
- `POST /api/v0/chat_session/create` (body `{"target_path":"/api/v0/chat/completion"}`) → session id at
  `data.biz_data.id` or `data.biz_data.chat_session.id`.
- `POST /api/v0/chat/completion` with `{ chat_session_id, parent_message_id, prompt, ref_file_ids: [], thinking_enabled, search_enabled, model_type: "default", preempt: false, stream: true }`.
- SSE parse: `event: ready` → ids; `data: {"p":"response/fragments/-1/content","o":"APPEND","v":"..."}`
  → text fragment; `data: {"v":"..."}` plain token; thinking fragments carry
  `{"p":"response/fragments/-1/content","o":"APPEND","v":"...","__type":"thinking"}` inside
  the response object (see Python for the exact shape);
  `data: {"p":"response/status","v":"FINISHED"}`; top-level toast/hint errors.
- Auth failures arrive as **HTTP 200 + `{"code":40003,"msg":"Authorization Failed (invalid token)"}`**.
- Muting arrives as HTTP 200 + `{"data":{"biz_code":5,"biz_msg":"user is muted","mute_until":...}}`.
- Rate limiting arrives as SSE `event: hint` with `finish_reason: rate_limit_reached`.
- Timeouts: 120 s overall, 300 s stream idle.

## src/dsml.js

```js
export function buildToolPrompt(tools, toolChoice) -> string   // tools: [{ name, description, parameters }]
export function stripMarkup(text) -> string
export function parseToolCalls(text, toolNames) -> { calls: [{ name, arguments: object }], text: string }
export function createSieve({ toolNames }) -> { feed(text) -> Event[], flush() -> Event[] }
/** Event = { type: "text", text } | { type: "call-start", name } | { type: "call", name, arguments: object } */
```
Port from `tool_dsml.py` (prompt text must be byte-identical to Python) and
`tool_sieve.py` (streaming hold-back logic, 1 MB capture cap, CDATA handling).

## src/adapter.js (lead-owned, contract for reference)

`LlmAdapter` subclass implementing `providerInfo`, `listModels`, `resolveModel`,
`stream(options)` and yielding chunks:
`block-start`, `text-delta`, `reasoning-delta`, `tool-call-delta`, `block-end`,
`usage` (`{ inputTokens, outputTokens, totalTokens }`), `finish` (`{ reason: { kind: "stop"|"tool-calls" } }`).
It consumes `src/upstream.js` and `src/dsml.js` only through the contracts above.

## Tests

`node test/<name>.test.mjs` style, zero dependencies, exit code 1 on failure.
No network in tests: use recorded SSE fixtures. The repo's Python tests are the
source of truth for expected behaviour; port the important cases.

## Ground rules

- No new npm dependencies (bundled Node only).
- Never import from the Python proxy at runtime; it is a reference only.
- Keep every file free of Vercel/local-server assumptions.
