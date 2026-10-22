import assert from 'node:assert/strict';
import { parseCompletionStream } from '../src/upstream/index.js';

function streamOf(chunks) {
  let i = 0;
  return new ReadableStream({
    pull(controller) {
      if (i < chunks.length) controller.enqueue(new TextEncoder().encode(chunks[i++]));
      else controller.close();
    }
  });
}

async function collect(chunks) {
  let text = '', thinking = '';
  let meta = null;
  for await (const ev of parseCompletionStream(streamOf(chunks))) {
    if (ev.type === 'text') text += ev.delta;
    else if (ev.type === 'thinking') thinking += ev.delta;
    else if (ev.type === 'meta') meta = ev;
  }
  return { text, thinking, meta };
}

// real capture shape: first token inside response snapshot, then APPEND deltas
const live = [
  'event: ready\ndata: {"request_message_id":1,"response_message_id":2,"model_type":"default"}\n\n',
  'data: {"v":{"response":{"status":"WIP","fragments":[{"id":2,"type":"RESPONSE","content":"K"}]}}}\n\n',
  'data: {"p":"response/fragments/-1/content","o":"APPEND","v":"ucing"}\n\ndata: {"v":"."}\n\n',
  'data: {"p":"response","o":"BATCH","v":[{"p":"accumulated_token_usage","v":51}]}\n\n',
  'data: {"p":"response/status","o":"SET","v":"FINISHED"}\n\nevent: title\ndata: {"content":"x"}\n\nevent: close\ndata: {}\n\n'
];
const r1 = await collect(live);
assert.equal(r1.text, 'Kucing.');
assert.equal(r1.meta.responseMessageId, 2);

// thinking snapshot + type switch
const think = [
  'data: {"v":{"response":{"status":"WIP","fragments":[{"id":1,"type":"THINK","content":"Hmm"}]}}}\n\n',
  'data: {"v":" pikir"}\n\n',
  'data: {"p":"response/fragments/-1/type","o":"SET","v":"RESPONSE"}\n\n',
  'data: {"p":"response/fragments/-1/content","o":"APPEND","v":"Jawaban"}\n\n'
];
const r2 = await collect(think);
assert.equal(r2.thinking, 'Hmm pikir');
assert.equal(r2.text, 'Jawaban');

// single-frame answer fully inside BATCH
const batch = [
  'data: {"p":"response","o":"BATCH","v":[{"p":"content","o":"APPEND","v":"OK"}]}\n\n'
];
const r3 = await collect(batch);
assert.equal(r3.text, 'OK');

// chunk split mid-line must not corrupt output
const split = ['data: {"v":"hal', 'o"}\n\n'];
const r4 = await collect(split);
assert.equal(r4.text, 'halo');

// 5) thinking lalu jawaban lewat frames response/fragments APPEND (bentuk wire thinking asli)
const thinkFrames = [
  'data: {"v":{"response":{"fragments":[{"id":2,"type":"THINK","content":"Hmm"}]}}}\n',
  'data: {"v":" pikir"}\n',
  'event: ready\ndata: {"response_message_id":2}\n\n',
  'data: {"p":"response/fragments","o":"APPEND","v":[{"id":3,"type":"RESPONSE","content":"K"}]}\n',
  'data: {"p":"response/fragments/-1/content","v":"ucing"}\n',
  'data: {"v":"."}\n'
];
const thinkBody = new Response(thinkFrames.join('')).body;
const out5 = [];
for await (const ev of parseCompletionStream(thinkBody)) out5.push(ev.type + ':' + (ev.delta || ''));
assert.deepEqual(out5, ['thinking:Hmm', 'thinking: pikir', 'meta:', 'text:K', 'text:ucing', 'text:.']);
console.log('upstream.test OK');
