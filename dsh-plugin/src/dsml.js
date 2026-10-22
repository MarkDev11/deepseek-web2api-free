import crypto from 'node:crypto';

export const DSML_BLOCK_START = '<｜DSML｜function_calls>';
export const DSML_BLOCK_END = '</｜DSML｜function_calls>';

const LOOSE_OPEN_RE = /<\s*｜{1,2}\s*DSML\s*｜{1,2}\s*(invoke|function_calls)/;
const LOOSE_CLOSE_ANY_RE = /<\/\s*｜{1,2}\s*DSML\s*｜{1,2}\s*(?:invoke|parameter|function_calls)\s*>/gi;

export function buildToolSystemPrompt(tools) {
  if (!Array.isArray(tools) || tools.length === 0) return '';
  const defs = tools.map((t) => {
    const fn = t.function || t;
    return JSON.stringify({ name: fn.name, description: fn.description || '', parameters: fn.parameters || { type: 'object', properties: {} } });
  }).join('\n');
  return ['You can call tools by outputting ONLY a DSML block:', defs,
    DSML_BLOCK_START,
    '<｜DSML｜invoke name="tool_name">',
    '<｜DSML｜parameter name="param" string="true">value</｜DSML｜parameter>',
    '</｜DSML｜invoke>',
    DSML_BLOCK_END].join('\n');
}

export function normalizeDsml(text) {
  if (!text) return text;
  return text
    .replace(/<\/\s*｜+\s*DSML\s*｜+\s*/g, '</｜DSML｜')
    .replace(/<\s*｜+\s*DSML\s*｜+\s*/g, '<｜DSML｜')
    .replace(/<\/｜DSML｜\s+/g, '</｜DSML｜')
    .replace(/<｜DSML｜\s+/g, '<｜DSML｜');
}

export function parseDsmlToolCalls(text) {
  if (!text) return [];
  const norm = normalizeDsml(text);
  if (norm.indexOf('<｜DSML｜') === -1) return [];
  const calls = [];
  const invokeRe = /<｜DSML｜invoke\s+name="([^"]+)"\s*>([\s\S]*?)<\/｜DSML｜invoke>/g;
  const paramRe = /<｜DSML｜parameter\s+name="([^"]+)"(?:\s+string="(?:true|false)")?\s*>([\s\S]*?)<\/｜DSML｜parameter>/g;
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
          out.text += this.buffer.slice(0, this.buffer.length - hold).replace(LOOSE_CLOSE_ANY_RE, '');
          this.buffer = this.buffer.slice(this.buffer.length - hold);
          break;
        }
        out.text += this.buffer.slice(0, m.index).replace(LOOSE_CLOSE_ANY_RE, '');
        this.buffer = this.buffer.slice(m.index);
        if (m.index + m[0].length >= this.buffer.length) break;
        this.inDsml = true; this.dsmlBuffer = ''; this.expectFc = m[1] === 'function_calls';
        continue;
      }
      this.dsmlBuffer += this.buffer; this.buffer = '';
      if (this.dsmlBuffer.length > 1048576) { out.text += this.dsmlBuffer; this.dsmlBuffer = ''; this.inDsml = false; break; }
      const raw = this.dsmlBuffer;
      const opens = (raw.match(/<\s*｜+\s*DSML\s*｜+\s*invoke/g) || []).length;
      const closeRe = /<\/\s*｜+\s*DSML\s*｜+\s*invoke\s*>/g;
      let lastClose = -1, cc = 0, cm;
      while ((cm = closeRe.exec(raw)) !== null) { lastClose = cm.index + cm[0].length; cc++; }
      const fcRe = /<\/\s*｜+\s*DSML\s*｜+\s*function_calls\s*>/g;
      let fcEnd = -1, fm;
      while ((fm = fcRe.exec(raw)) !== null) fcEnd = fm.index + fm[0].length;
      const wrapped = this.expectFc || /<\s*｜+\s*DSML\s*｜+\s*function_calls\s*>/.test(raw);
      const complete = fcEnd !== -1 || (opens > 0 && cc >= opens && !wrapped);
      if (!complete) break;
      const cut = Math.max(lastClose, fcEnd);
      out.toolCalls.push(...parseDsmlToolCalls(raw.slice(0, cut)));
      let tail = raw.slice(cut);
      tail = tail.replace(/^(?:\s|<\/(?:｜{0,2}\s*DSML\s*｜{0,2}\s*)?(?:invoke|function_calls)\s*>)+/gi, '').replace(LOOSE_CLOSE_ANY_RE, '');
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
      else out.text += this.dsmlBuffer;
      this.dsmlBuffer = ''; this.inDsml = false; this.expectFc = false;
    }
    if (this.buffer) { out.text += this.buffer.replace(LOOSE_CLOSE_ANY_RE, ''); this.buffer = ''; }
    return out;
  }
}
