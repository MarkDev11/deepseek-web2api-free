// dsweb login helper - OAuth popup capture for the HOSTED WebUI.
//
// The panel on Vercel cannot open browsers itself; this tiny server runs on
// the same machine as the browser and does exactly what the dsh-plugin
// popup does: launch a small Chrome window on chat.deepseek.com, wait for
// the login, pull token + cookies via CDP, and hand them back over HTTP.
//
//   node dsh-plugin/scripts/login-helper.mjs   (keep it running while adding)
//
// Zero dependencies: node:http + this repo's capture module only.
import http from 'node:http';
import { launchOAuthPopupCapture, DEFAULT_STORE_PATH } from '../src/capture/index.js';

const PORT = Number(process.env.DSWEB_HELPER_PORT || 55343);
let busy = false;

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'content-type',
};

const server = http.createServer(async (req, res) => {
  const send = (code, obj) => {
    res.writeHead(code, { ...CORS, 'Content-Type': 'application/json' });
    res.end(JSON.stringify(obj));
  };
  if (req.method === 'OPTIONS') { res.writeHead(204, CORS); res.end(); return; }
  const url = (req.url || '').split('?')[0];
  if (req.method === 'GET' && url === '/health') {
    send(200, { ok: true, service: 'dsweb-login-helper', store: DEFAULT_STORE_PATH });
    return;
  }
  if (req.method === 'POST' && url === '/login') {
    if (busy) { send(409, { ok: false, error: 'login is already in progress' }); return; }
    busy = true;
    console.log('Membuka jendela login kecil (480x760)...');
    try {
      const acct = await launchOAuthPopupCapture({
        // popup-sized window, like an OAuth dialog: small, fixed on screen
        extraArgs: ['--window-size=480,760', '--window-position=140,90'],
      });
      console.log('LOGIN OK ->', acct.email || acct.id);
      send(200, { ok: true, account: acct });
    } catch (e) {
      console.error('LOGIN GAGAL:', e && e.message || e);
      send(500, { ok: false, error: String((e && e.message) || e) });
    } finally {
      busy = false;
    }
    return;
  }
  send(404, { ok: false, error: 'not found' });
});

server.listen(PORT, '127.0.0.1', () => {
  console.log('dsweb-login-helper: http://127.0.0.1:' + PORT + '  (Ctrl+C untuk berhenti)');
});
