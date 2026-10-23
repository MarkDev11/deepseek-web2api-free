import crypto from 'node:crypto';

export const DSML_BLOCK_START = '<\uff5cDSML\uff5cfunction_calls>';
export const DSML_BLOCK_END = '</\uff5cDSML\uff5cfunction_calls>';

// Pipe yang boleh muncul di tag model: fullwidth \uff5c, ASCII |, atau hilang sama sekali (varian model).
const P = '[|\uff5c]';
const LOOSE_OPEN_RE = new RegExp('<\\s*' + P + '{0,2}\\s*DSML\\s*' + P + '{0,2}\\s*(invoke|function_calls)');
const LOOSE_CLOSE_ANY_RE = new RegExp('<\\/\\s*' + P + '{0,2}\\s*DSML\\s*' + P + '{0,2}\\s*(?:invoke|parameter|function_calls)\\s*>', 'gi');
// Tag DSML apa pun (termasuk terpotong tanpa '>') untuk pembersihan teks:
const TRUNC_ANY_RE = new RegExp('<\\/?\\s*' + P + '{0,2}\\s*DSML\\s*' + P + '{0,2}\\s*(?:invoke|parameter|function_calls)[^>]*>?', 'gi');

export function buildToolSystemPrompt(tools) {
  if (!Array.isArray(tools) || tools.length === 0) return '';
  const defs = tools.map((t) => {
    const fn = t.function || t;
    return JSON.stringify({ name: fn.name, description: fn.description || '', parameters: fn.parameters || { type: 'object', properties: {} } });
  }).join('\n');
  return [
    'You can call tools by outputting a DSML block. STRICT RULES:',
    '1. When you want to perform an action (read, write, run code, search, edit a file), you MUST reply with ONLY the DSML block below - no prose, no code snippets, no pseudo-calls like "const r = await tools.x(...)".',
    '2. Never write JavaScript or tool syntax as plain text instead of a DSML block.',
    '3. Use DSML only for real tool calls; never mention these tags in normal answers.',
    'Available tools:', defs,
    DSML_BLOCK_START,
    '<\uff5cDSML\uff5cinvoke name="tool_name">',
    '<\uff5cDSML\uff5cparameter name="param" string="true">value</\uff5cDSML\uff5cparameter>',
    '</\uff5cDSML\uff5cinvoke>',
    DSML_BLOCK_END].join('\n');
}

export function normalizeDsml(text) {
  if (!text) return text;
  return text
    .replace(new RegExp('<\\/\\s*' + P + '{0,2}\\s*DSML\\s*' + P + '{0,2}\\s*(invoke|parameter|function_calls)\\s*>', 'gi'), '</\uff5cDSML\uff5c$1>')
    .replace(/<\/\s*(invoke|parameter)\s*>/gi, '</\uff5cDSML\uff5c$1>')
    .replace(new RegExp('<\\s*' + P + '{0,2}\\s*DSML\\s*' + P + '{0,2}\\s*(invoke|parameter|function_calls)', 'gi'), '<\uff5cDSML\uff5c$1')
    .replace(/<\/\uff5cDSML\uff5c\s+/g, '</\uff5cDSML\uff5c')
    .replace(/<\uff5cDSML\uff5c\s+/g, '<\uff5cDSML\uff5c');
}

export function parseDsmlToolCalls(text) {
  if (!text) return [];
  const norm = normalizeDsml(text);
  if (norm.indexOf('<\uff5cDSML\uff5c') === -1) return [];
  const calls = [];
  const invokeRe = /<\uff5cDSML\uff5cinvoke\s+name="([^"]+)"\s*>([\s\S]*?)<\/\uff5cDSML\uff5cinvoke>/g;
  const paramRe = /<\uff5cDSML\uff5cparameter\s+name="([^"]+)"(?:\s+string="(?:true|false)")?\s*>([\s\S]*?)<\/\uff5cDSML\uff5cparameter>/g;
  let m;
  while ((m = invokeRe.exec(norm)) !== null) {
    const args = {};
    let pm;
    paramRe.lastIndex = 0;
    while ((pm = paramRe.exec(m[2])) !== null) args[pm[1]] = pm[2];
    calls.push({ id: 'call_' + crypto.randomBytes(8).toString('hex'), name: m[1], arguments: JSON.stringify(args), parsedArguments: args });
  }
  return calls;
}

export class DsmlStreamSieve {
  constructor() { this.buffer = ''; this.inDsml = false; this.dsmlBuffer = ''; this.expectFc = false; }
  feed(chunk) {
    const out = { text: '', toolCalls: [] };
    this.buffer += chunk || '';
    for (;;) {
      if (!this.inDsml) {
        const m = this.buffer.match(LOOSE_OPEN_RE);
        if (!m) {
          let hold = 0;
          const lt = this.buffer.lastIndexOf('<');
          if (lt !== -1 && this.buffer.length - lt <= 40 && !this.buffer.slice(lt).includes('>')) hold = this.buffer.length - lt;
          out.text += this.buffer.slice(0, this.buffer.length - hold).replace(TRUNC_ANY_RE, '');
          this.buffer = this.buffer.slice(this.buffer.length - hold);
          break;
        }
        out.text += this.buffer.slice(0, m.index).replace(TRUNC_ANY_RE, '');
        this.buffer = this.buffer.slice(m.index);
        if (m.index + m[0].length >= this.buffer.length) break;
        this.inDsml = true; this.dsmlBuffer = ''; this.expectFc = m[1] === 'function_calls';
        continue;
      }
      this.dsmlBuffer += this.buffer; this.buffer = '';
      if (this.dsmlBuffer.length > 1048576) { out.text += this.dsmlBuffer; this.dsmlBuffer = ''; this.inDsml = false; break; }
      const raw = this.dsmlBuffer;
      const openRe = new RegExp('<\\s*' + P + '{0,2}\\s*DSML\\s*' + P + '{0,2}\\s*invoke', 'g');
      const opens = (raw.match(openRe) || []).length;
      // penutup invoke: varian DSML apa pun ATAU polos </invoke>
      const closeRe = /<\/\s*(?:(?:[|\uff5c]{0,2}\s*DSML\s*[|\uff5c]{0,2}\s*)?)invoke\s*>/g;
      let lastClose = -1, cc = 0, cm;
      while ((cm = closeRe.exec(raw)) !== null) { lastClose = cm.index + cm[0].length; cc++; }
      const fcRe = new RegExp('<\\/\\s*' + P + '{0,2}\\s*DSML\\s*' + P + '{0,2}\\s*function_calls\\s*>', 'g');
      let fcEnd = -1, fm;
      while ((fm = fcRe.exec(raw)) !== null) fcEnd = fm.index + fm[0].length;
      const wrapped = this.expectFc || new RegExp('<\\s*' + P + '{0,2}\\s*DSML\\s*' + P + '{0,2}\\s*function_calls').test(raw);
      const complete = fcEnd !== -1 || (opens > 0 && cc >= opens && !wrapped);
      if (!complete) break;
      const cut = Math.max(lastClose, fcEnd);
      out.toolCalls.push(...parseDsmlToolCalls(raw.slice(0, cut)));
      let tail = raw.slice(cut);
      tail = tail.replace(/^(?:\s|<\/[^>]*>)+/gi, '').replace(TRUNC_ANY_RE, '');
      out.text += tail;
      this.dsmlBuffer = ''; this.inDsml = false; this.expectFc = false;
    }
    return out;
  }
  flush() {
    const out = { text: '', toolCalls: [] };
    if (this.inDsml && this.dsmlBuffer) {
      const parsed = parseDsmlToolCalls(this.dsmlBuffer);
      if (parsed.length) out.toolCalls.push(...parsed);
      else out.text += this.dsmlBuffer.replace(TRUNC_ANY_RE, '');
      this.dsmlBuffer = ''; this.inDsml = false; this.expectFc = false;
    }
    if (this.buffer) { out.text += this.buffer.replace(TRUNC_ANY_RE, ''); this.buffer = ''; }
    return out;
  }
}
