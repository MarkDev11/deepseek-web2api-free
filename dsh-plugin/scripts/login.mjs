import { launchOAuthPopupCapture, DEFAULT_STORE_PATH } from '../src/capture/index.js';

console.log('Membuka browser untuk login DeepSeek (OAuth-like capture)...');
console.log('Login seperti biasa di jendela yang terbuka.');
console.log('Jendela tertutup sendiri begitu token + cookie tertangkap.');
try {
  const acct = await launchOAuthPopupCapture();
  console.log('LOGIN OK ->', acct.email || acct.id);
  console.log('Tersimpan di:', DEFAULT_STORE_PATH);
} catch (e) {
  console.error('LOGIN GAGAL:', e.message);
  process.exit(1);
}
