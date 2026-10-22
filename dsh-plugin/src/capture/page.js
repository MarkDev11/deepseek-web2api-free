/**
 * src/capture/page.js - the loopback helper page HTML.
 *
 * Hard constraint this page works around: a page served from
 * http://127.0.0.1:<port> is a *different origin* from https://chat.deepseek.com,
 * so it can never read that site's localStorage (where userToken lives) or its
 * cookies. Any capture design has to cross that boundary deliberately; the three
 * routes this page offers, in descending order of reliability, are:
 *
 *  1. one-paste (default): the user runs our one-line copy command in the
 *     DeepSeek tab's console, pastes the clipboard here, submits. Same-origin
 *     POST to us - no CORS, no browser flags, no extension, and it works in the
 *     profile the user is already logged into. This mirrors the "Quick import"
 *     box the Python WebUI already ships, so the muscle memory is shared.
 *  2. bookmarklet: a javascript: link dragged to the bookmarks bar. Clicked on
 *     the DeepSeek tab it reads localStorage + document.cookie itself and POSTs
 *     to us with Content-Type: text/plain, which keeps the request "simple" (no
 *     preflight); a loopback http origin is exempt from mixed-content blocking,
 *     and the server sends permissive CORS headers anyway.
 *  3. explicit fields: token and cookies in their own inputs, for the case where
 *     even the console is inconvenient - ds_session_id is HttpOnly and therefore
 *     only visible in the Network tab, so that field is how it gets supplied.
 *
 * The form is a real HTML form: it posts urlencoded with no JavaScript at all and
 * the server answers with an HTML result page, so capture also works in a
 * locked-down browser. With JavaScript available the submit upgrades to fetch
 * (inline feedback) and the tab polls /status and announces success by itself.
 */

/** The one-line console command that puts token + cookies on the clipboard. */
export const COPY_SNIPPET = "copy(JSON.parse(localStorage.userToken).value + '||' + document.cookie)";

/** Same payload as the snippet, POSTed straight back to the loopback server. */
export function bookmarkletCode(nonce) {
  return [
    '(function(){try{',
    'var t=JSON.parse(localStorage.userToken).value;',
    "var raw=t+'||'+document.cookie;",
    "var u=new URL(location.href);u.pathname='/capture';u.search='?nonce='+encodeURIComponent(",
    JSON.stringify(String(nonce)),
    '));',
    'fetch(u,{method:"POST",headers:{"Content-Type":"text/plain"},body:raw})',
    '.then(function(r){return r.json()})',
    '.then(function(j){alert(j.ok?"DSWeb: captured account "+j.account.id:"DSWeb: "+(j.error||"failed"))})',
    '.catch(function(e){alert("DSWeb capture failed: "+e)});',
    '}catch(e){alert("DSWeb: not logged in on this tab ("+e+")")} })();',
  ].join('');
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const STYLE = [
  ':root{color-scheme:dark light;--fg:#e8e8ea;--muted:#9a9aa3;--bg:#141417;--card:#1d1d21;--line:#2e2e35;--ok:#39b36b;--warn:#e0a33a}',
  '@media (prefers-color-scheme:light){:root{--fg:#1b1b1f;--muted:#5f5f68;--bg:#f6f6f8;--card:#fff;--line:#dcdce2}}',
  'body{margin:0;padding:32px 16px;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,Segoe UI,Roboto,sans-serif}',
  'main{max-width:720px;margin:0 auto;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px 28px}',
  'h1{font-size:19px;margin:0 0 4px}p.lede{color:var(--muted);margin:0 0 20px}',
  'ol{padding-left:20px;margin:0 0 18px}li{margin:0 0 14px}',
  'code,pre,input,textarea{font-family:ui-monospace,Consolas,monospace;font-size:13px}',
  'pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px 12px;overflow-x:auto;white-space:pre-wrap;word-break:break-all;margin:8px 0 0}',
  'textarea{width:100%;box-sizing:border-box;min-height:96px;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:10px}',
  'input[type=text]{width:100%;box-sizing:border-box;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-top:4px}',
  'button,a.btn{display:inline-block;margin-top:10px;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer;text-decoration:none;font:inherit}',
  'button.primary{background:var(--ok);border-color:var(--ok);color:#fff;font-weight:600}',
  '#status{margin:0 0 18px;padding:10px 12px;border-radius:8px;border:1px solid var(--line);color:var(--muted)}',
  '#status.ok{border-color:var(--ok);color:var(--ok)}#status.warn{border-color:var(--warn);color:var(--warn)}',
  '.fields{display:grid;gap:10px;margin-top:10px}small{color:var(--muted);display:block;margin-top:6px}',
].join('\n');

/** Inline script, written so that it needs no quotes beyond double quotes. */
const PAGE_SCRIPT = [
  '<script>',
  'var NONCE = __NONCE__;',
  'var copyBtn = document.getElementById("copy");',
  'if (copyBtn) copyBtn.addEventListener("click", function () {',
  '  var text = document.getElementById("snippet").textContent;',
  '  if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(text); }',
  '  else { var ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select();',
  '         document.execCommand("copy"); document.body.removeChild(ta); }',
  '  copyBtn.textContent = "Copied";',
  '});',
  'function setStatus(cls, text) {',
  '  var s = document.getElementById("status");',
  '  if (!s) return;',
  '  s.className = cls; s.textContent = text;',
  '}',
  'var form = document.getElementById("form");',
  'if (form) form.addEventListener("submit", function (ev) {',
  '  if (!window.fetch) return;',
  '  ev.preventDefault();',
  '  var raw = document.getElementById("raw").value;',
  '  var extra = document.getElementById("cookies").value.trim();',
  '  if (extra) raw = raw ? raw + "||Cookie: " + extra : "Cookie: " + extra;',
  '  var email = document.getElementById("email").value.trim();',
  '  setStatus("", "Sending...");',
  '  fetch("/capture", { method: "POST",',
  '    headers: { "Content-Type": "application/json", Accept: "application/json" },',
  '    body: JSON.stringify({ nonce: NONCE, raw: raw, email: email }) })',
  '    .then(function (r) { return r.json(); })',
  '    .then(function (j) {',
  '      if (j.ok) setStatus("ok", "Saved account " + j.account.id + ". You can close this tab.");',
  '      else setStatus("warn", "Not saved: " + (j.error || "unknown error"));',
  '    })',
  '    .catch(function (e) { setStatus("warn", "Send failed: " + e); form.submit(); });',
  '});',
  'setInterval(function () {',
  '  fetch("/status?nonce=" + encodeURIComponent(NONCE), { headers: { Accept: "application/json" } })',
  '    .then(function (r) { return r.json(); })',
  '    .then(function (j) {',
  '      if (j && j.captured) {',
  '        setStatus("ok", "Captured account " + (j.account && j.account.id ? j.account.id : "") + ". You can close this tab.");',
  '      }',
  '    }).catch(function () {});',
  '}, 2000);',
  '</script>',
].join('\n');

/**
 * @param {{nonce?: string, chatUrl?: string, captured?: null|{id?: string}}} [options]
 */
export function renderWaitPage(options = {}) {
  const nonce = String(options.nonce || '');
  const chatUrl = String(options.chatUrl || 'https://chat.deepseek.com/');
  const bookmarklet = 'javascript:' + encodeURIComponent(bookmarkletCode(nonce));
  const captured = options.captured || null;
  const banner = captured
    ? '<div id="status" class="ok">Captured account ' + escapeHtml(captured.id || '') +
      '. You can close this tab.</div>'
    : '<div id="status">Waiting for credentials from chat.deepseek.com ...</div>';

  return [
    '<!doctype html>',
    '<html lang="en"><head><meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width,initial-scale=1">',
    '<meta name="robots" content="noindex">',
    '<title>DSWeb login capture</title>',
    '<style>' + STYLE + '</style></head><body><main>',
    '<h1>DSWeb login capture</h1>',
    '<p class="lede">Local helper for the DeepSeek Harness plugin. It listens on 127.0.0.1 only, and it cannot',
    'read DeepSeek&#39;s own storage &mdash; that is exactly why it asks you for one paste.</p>',
    banner,
    '<ol>',
    '<li>Log in at <a href="' + escapeHtml(chatUrl) + '" target="_blank" rel="noopener noreferrer">' +
      escapeHtml(chatUrl) + '</a> and send one chat message (that is what rotates the session cookies).',
    '<br><button class="btn" type="button" onclick="window.open(&quot;' + escapeHtml(chatUrl) +
      '&quot;,&quot;_blank&quot;)">Open in a new tab</button></li>',
    '<li>In that tab press F12, open the Console, and run this single line (the button copies it):',
    '<pre id="snippet">' + escapeHtml(COPY_SNIPPET) + '</pre>',
    '<button class="btn" type="button" id="copy">Copy command</button>',
    '<small>It reads <code>localStorage.userToken</code> (the bearer token) plus <code>document.cookie</code>,',
    'joined by <code>||</code>. <code>ds_session_id</code> is HttpOnly and invisible here, so anything already',
    'stored for this account is kept name-by-name, and the Cookie field below is how you can supply it.</small></li>',
    '<li>Paste the clipboard here and save:',
    '<form method="post" action="/capture" id="form" enctype="application/x-www-form-urlencoded">',
    '<input type="hidden" name="nonce" value="' + escapeHtml(nonce) + '">',
    '<textarea name="raw" id="raw" spellcheck="false" placeholder="paste token, or token||cookies, or a Cookie: header"></textarea>',
    '<div class="fields">',
    '<label>Cookie (optional - only if you copied it out of the Network tab)',
    '<input type="text" name="cookies" id="cookies" spellcheck="false" placeholder="ds_session_id=...; aws-waf-token=..."></label>',
    '<label>Email (optional - labels this account)',
    '<input type="text" name="email" id="email" spellcheck="false" placeholder="you@example.com"></label>',
    '</div>',
    '<button class="primary" type="submit">Save credentials</button>',
    '</form></li>',
    '</ol>',
    '<p><a class="btn" href="' + escapeHtml(bookmarklet) + '">DSWeb&nbsp;Capture</a>',
    '<small style="display:inline;margin-left:8px">Optional: drag this to your bookmarks bar, then click it on a',
    'logged-in DeepSeek tab - it posts the credentials straight back, no pasting.</small></p>',
    PAGE_SCRIPT.replace('__NONCE__', JSON.stringify(nonce)),
    '</main></body></html>',
    '',
  ].join('\n');
}

/** Result page for a no-JavaScript capture (plain form post or GET /submit). */
export function renderResultPage(result = {}) {
  const ok = Boolean(result.ok);
  const heading = ok ? 'Credentials saved' : 'Not saved';
  const detail = ok
    ? 'Account <code>' + escapeHtml(result.account && result.account.id) + '</code> (' +
      (result.created ? 'added' : result.deduped ? 'already present' : 'updated') + '). You can close this tab.'
    : escapeHtml(result.error || 'unknown error');
  return [
    '<!doctype html>',
    '<html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex">',
    '<title>' + heading + '</title><style>' + STYLE + '</style></head><body><main>',
    '<h1>' + heading + '</h1>',
    '<div id="status" class="' + (ok ? 'ok' : 'warn') + '">' + detail + '</div>',
    '<p><a class="btn" href="/">Capture another account</a></p>',
    '</main></body></html>',
    '',
  ].join('\n');
}

export { escapeHtml };
