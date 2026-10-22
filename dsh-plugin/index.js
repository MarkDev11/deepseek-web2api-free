import http from 'node:http';
import crypto from 'node:crypto';
import fs2 from 'node:fs';
import { DsWebAdapter } from './src/adapter.js';

export const name = 'dsweb';

/** The credential service is required: the provider's apiKeyEnv resolves through it. */
export const inject = ['credentials'];

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (c) => { data += c; if (data.length > 8 * 1024 * 1024) { reject(new Error('body too large')); req.destroy(); } });
    req.on('end', () => resolve(data));
    req.on('error', reject);
  });
}

function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) });
  res.end(body);
}

export function apply(ctx, config) {
  const cfg = config || {};
  const port = Number(cfg.port || 55341);
  const keyName = String(cfg.keyName || 'DSWEB_NATIVE_KEY');
  let apiKey = String(cfg.apiKey || '');
  if (!apiKey) {
    try {
      const home = process.env.USERPROFILE || process.env.HOME || '';
      const keyFile = home + '/.dsh/dsweb-key.txt';
      if (home && fs2.existsSync(keyFile)) apiKey = fs2.readFileSync(keyFile, 'utf8').trim();
      if (!apiKey) {
        apiKey = 'dsweb-' + crypto.randomBytes(16).toString('hex');
        fs2.mkdirSync(home + '/.dsh', { recursive: true });
        fs2.writeFileSync(keyFile, apiKey, 'utf8');
      }
    } catch { apiKey = 'dsweb-' + crypto.randomBytes(16).toString('hex'); }
  }
  const adapter = new DsWebAdapter({ storePath: cfg.storePath, autoLogin: cfg.autoLogin !== false });

  try { ctx.credentials.set(keyName, apiKey); } catch (e) {
    if (ctx.log) ctx.log('warn', 'dsweb: credentials.set failed: ' + (e && e.message));
  }

  const server = http.createServer(async (req, res) => {
    try {
      const url = (req.url || '').split('?')[0];
      if (url === '/health') return json(res, 200, { status: 'ok', provider: 'dsweb' });
      const auth = String(req.headers.authorization || '');
      if (auth.replace(/^Bearer\s+/i, '') !== apiKey) return json(res, 401, { error: { message: 'invalid api key' } });

      if (url === '/v1/models') {
        return json(res, 200, { object: 'list', data: (await adapter.listModels()).map((m) => ({ id: m.id, object: 'model', owned_by: 'dsweb-local' })) });
      }
      if (url === '/v1/chat/completions' && req.method === 'POST') {
        const body = JSON.parse(await readBody(req) || '{}');
        const thinking = !(body.thinking === false || body.thinking_mode === false || body.reasoning === false || cfg.thinking === false);
        const search = !(body.search === false || body.search_enabled === false || cfg.search === false);
        const messages = body.messages || [];
        const tools = body.tools || [];
        const model = body.model || 'deepseek-chat';
        const id = 'chatcmpl-dsweb-' + crypto.randomBytes(8).toString('hex');
        const created = Math.floor(Date.now() / 1000);

        const sendChunk = (obj) => { res.write('data: ' + JSON.stringify(obj) + '\n\n'); };

        if (body.stream) {
          res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' });
          sendChunk({ id, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: { role: 'assistant' }, finish_reason: null }] });
          try {
            for await (const ev of adapter.stream({ messages, tools, thinking: thinking || model === 'deepseek-reasoner', search, model })) {
              if (ev.type === 'text-delta') sendChunk({ id, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: { content: ev.text }, finish_reason: null }] });
              else if (ev.type === 'reasoning-delta') sendChunk({ id, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: { reasoning_content: ev.text }, finish_reason: null }] });
              else if (ev.type === 'tool-call-delta') sendChunk({ id, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: ev.id, type: 'function', function: { name: ev.name, arguments: ev.argumentsTextDelta } }] }, finish_reason: null }] });
            }
            const finish = { id, object: 'chat.completion.chunk', created, model, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] };
            sendChunk(finish);
          } catch (e) {
            sendChunk({ error: { message: String(e && e.message || e) } });
          }
          res.write('data: [DONE]\n\n');
          return res.end();
        }

        let content = '';
        let reasoning = '';
        const toolCalls = [];
        let usage = null;
        for await (const ev of adapter.stream({ messages, tools, thinking: thinking || model === 'deepseek-reasoner', search, model })) {
          if (ev.type === 'text-delta') content += ev.text;
          else if (ev.type === 'reasoning-delta') reasoning += ev.text;
          else if (ev.type === 'tool-call-delta') toolCalls.push({ id: ev.id, type: 'function', function: { name: ev.name, arguments: ev.argumentsTextDelta } });
          else if (ev.type === 'usage') usage = ev;
        }
        const message = { role: 'assistant', content: content || (toolCalls.length ? '' : null) };
        if (reasoning) message.reasoning_content = reasoning;
        if (toolCalls.length) message.tool_calls = toolCalls;
        return json(res, 200, {
          id, object: 'chat.completion', created, model,
          choices: [{ index: 0, message, finish_reason: toolCalls.length ? 'tool_calls' : 'stop' }],
          usage: usage ? { prompt_tokens: usage.inputTokens, completion_tokens: usage.outputTokens, total_tokens: usage.totalTokens } : undefined
        });
      }
      return json(res, 404, { error: { message: 'not found: ' + url } });
    } catch (e) {
      const code = /40003|invalid token/.test(String(e && e.message)) ? 401 : /muted|rate/.test(String(e && e.message)) ? 429 : 502;
      try { json(res, code, { error: { message: String(e && e.message || e) } }); } catch {}
    }
  });

  let portTries = 0;
  const startListen = () => {
    const onError = (err) => {
      if (err && err.code === 'EADDRINUSE' && portTries < 12) {
        portTries++;
        try { if (ctx.log) ctx.log('warn', 'dsweb port busy, retry ' + portTries + '/12'); } catch {}
        setTimeout(startListen, 700);
      } else {
        try { if (ctx.log) ctx.log('error', 'dsweb listen failed: ' + (err && err.message)); } catch {}
      }
    };
    server.removeAllListeners('error');
    server.on('error', onError);
    try {
      server.listen(port, '127.0.0.1', () => {
        server.removeAllListeners('error');
        try { if (ctx.log) ctx.log('info', 'dsweb native provider listening on http://127.0.0.1:' + port); } catch {}
      });
    } catch (e) {
      if (portTries < 12) { portTries++; setTimeout(startListen, 700); }
      else { try { if (ctx.log) ctx.log('error', 'dsweb listen threw: ' + (e && e.message)); } catch {} }
    }
  };
  startListen();

  return () => {
    try { if (typeof server.closeIdleConnections === 'function') server.closeIdleConnections(); } catch {}
    try { server.close(); } catch {}
  };
}
