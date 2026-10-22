/**
 * src/capture/store.js - multi-account credential store for the DSWeb plugin.
 *
 * Location: <home>/.dsh/dsweb-accounts.json (the plugin's own file). The
 * harness credential vault (.credentials.yaml) is deliberately untouched - this
 * store holds DeepSeek *upstream* credentials, which are a different thing from
 * the harness API key, and the plugin must be able to rewrite them freely.
 * Override for tests or custom layouts with DSWEB_ACCOUNTS_PATH (a file path) or
 * DSWEB_HOME (the directory that receives dsweb-accounts.json).
 *
 * Shape: a JSON array, one object per account, in the field set the task
 * specifies - id, email, token, cookies, state (idle | error | muted),
 * muted_until, last_ok_at - plus the bookkeeping the pool needs (created_at,
 * updated_at, last_error, source, token_expires_at). Unknown keys on read are
 * preserved so a future field is never destroyed by a rewrite.
 *
 * Writes are atomic: serialise to a sibling temp file, then rename over the
 * target. A reader therefore never observes a half-written account file, and a
 * crash leaves the previous document in place. On Windows the rename can still
 * lose to an antivirus/indexer lock, so it retries once after unlinking the
 * target; chmod is best-effort only (see AGENTS.md: a protected chmod is what
 * used to make every account save fail on Vercel).
 *
 * No in-process cache: every operation reads the file. The document is a few
 * hundred bytes, and re-reading keeps the CLI, the plugin process and a test
 * run from clobbering each other.
 */

import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import { createHash, randomBytes, randomUUID } from 'node:crypto';

import { parseCredBlob, tokenExpiresAt } from './parse.js';

/** File name inside the plugin directory. */
export const ACCOUNTS_FILENAME = 'dsweb-accounts.json';
/** The only lifecycle states an account may hold (mirrors account_pool.py). */
export const ACCOUNT_STATES = ['idle', 'error', 'muted'];

/** Epoch seconds - the unit every timestamp in this file uses. */
export function nowSec() {
  return Math.floor(Date.now() / 1000);
}

function positiveInt(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.trunc(n) : 0;
}

function positiveIntOrNull(value) {
  const n = positiveInt(value);
  return n || null;
}

/** chmod is a bonus, never a precondition: some filesystems refuse it outright. */
function quietChmod(path, mode = 0o600) {
  try {
    chmodSync(path, mode);
  } catch {
    /* Windows ACLs / read-only mounts: permission bits are advisory here. */
  }
}

/** Resolve the home directory the plugin owns its state under. */
export function resolveHomeDir(env = process.env) {
  const home =
    env.DSWEB_HOME ||
    env.HOME ||
    env.USERPROFILE ||
    (env.HOMEDRIVE && env.HOMEPATH ? env.HOMEDRIVE + env.HOMEPATH : '');
  return home ? String(home) : '';
}

/** Expand a leading ~ against the same environment the store resolves from. */
function expandHome(rawPath, env = process.env) {
  const p = String(rawPath || '');
  if (p === '~' || p.startsWith('~/') || p.startsWith('~\\')) {
    const home = resolveHomeDir(env);
    if (home) return join(home, p.slice(1));
  }
  return p;
}

/**
 * Default store path: <home>/.dsh/dsweb-accounts.json, honouring
 * DSWEB_ACCOUNTS_PATH (exact file) and DSWEB_HOME (directory).
 */
export function resolveAccountsPath(env = process.env) {
  const explicit = env.DSWEB_ACCOUNTS_PATH;
  if (explicit) return resolve(expandHome(explicit, env));
  const home = resolveHomeDir(env);
  if (!home) {
    throw new Error(
      'cannot resolve the DSWeb account store: no HOME/USERPROFILE in the environment, ' +
        'set DSWEB_ACCOUNTS_PATH',
    );
  }
  return join(home, '.dsh', ACCOUNTS_FILENAME);
}

/**
 * Coerce one stored object into the canonical account shape. Unknown keys are
 * kept; unusable values fall back to safe defaults instead of throwing, because
 * a broken account must not take the whole file down.
 */
export function normalizeAccount(raw = {}, now = nowSec()) {
  const src = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  const token = typeof src.token === 'string' ? src.token.trim() : '';
  const cookies = typeof src.cookies === 'string' ? src.cookies.trim() : '';
  const created = positiveInt(src.created_at) || now;
  const out = { ...src };
  out.id = typeof src.id === 'string' && src.id ? src.id : randomUUID();
  out.email = typeof src.email === 'string' ? src.email.trim() : '';
  out.token = token;
  out.cookies = cookies;
  out.state = ACCOUNT_STATES.includes(src.state) ? src.state : 'idle';
  out.muted_until = positiveIntOrNull(src.muted_until);
  out.last_ok_at = positiveIntOrNull(src.last_ok_at);
  const declaredExpiry = positiveInt(src.token_expires_at);
  out.token_expires_at = declaredExpiry || positiveIntOrNull(tokenExpiresAt(token));
  out.last_error = typeof src.last_error === 'string' ? src.last_error : '';
  out.source = typeof src.source === 'string' ? src.source : '';
  out.created_at = created;
  out.updated_at = positiveInt(src.updated_at) || created;
  return out;
}

/**
 * Stable identity of a credential pair (port of account_pool's fingerprint):
 * equal fingerprints are the same login, so pasting it twice must not create a
 * second account.
 */
export function credentialFingerprint(token, cookies = '') {
  return createHash('sha256')
    .update(String(token || '') + '\u0000' + String(cookies || ''))
    .digest('hex')
    .slice(0, 16);
}

/**
 * Write text atomically: sibling tmp file + rename. Returns the target path.
 * Throws only if even the retry after unlink fails; the temp file is always
 * cleaned up, so a failed save leaves no litter.
 */
export function atomicWriteText(filePath, text) {
  const target = resolve(String(filePath));
  const dir = dirname(target);
  try {
    mkdirSync(dir, { recursive: true });
  } catch {
    /* exists, or a permission problem that the write will report instead */
  }
  const tmp = join(
    dir,
    '.' + basename(target) + '.' + process.pid + '.' + randomBytes(4).toString('hex') + '.tmp',
  );
  let moved = false;
  try {
    writeFileSync(tmp, text, 'utf8');
    quietChmod(tmp);
    try {
      renameSync(tmp, target);
      moved = true;
    } catch (err) {
      const code = err && err.code;
      if (code !== 'EPERM' && code !== 'EACCES' && code !== 'EEXIST') throw err;
      // Windows: a locked or read-only target. Drop it and retry once, which is
      // still atomic enough (the window has no partial content in it).
      try {
        unlinkSync(target);
      } catch {
        /* nothing to drop */
      }
      renameSync(tmp, target);
      moved = true;
    }
  } finally {
    if (!moved) {
      try {
        unlinkSync(tmp);
      } catch {
        /* already gone */
      }
    }
  }
  quietChmod(target);
  return target;
}

export class AccountStore {
  /**
   * @param {{filePath?: string, env?: Record<string,string|undefined>, log?: (event: string, detail?: object) => void}} [options]
   */
  constructor(options = {}) {
    const env = options.env || process.env;
    this.env = env;
    this.log = typeof options.log === 'function' ? options.log : () => {};
    this.filePath = options.filePath
      ? resolve(expandHome(String(options.filePath), env))
      : resolveAccountsPath(env);
  }

  /** True when the store file exists on disk. */
  exists() {
    return existsSync(this.filePath);
  }

  /**
   * Read the account list plus any warnings. A missing file is an empty store,
   * not an error. An unparseable or misshapen file is copied aside to
   * dsweb-accounts.json.corrupt-<epoch> and reported, never silently overwritten
   * on the next save without leaving the evidence behind.
   */
  read() {
    const warnings = [];
    if (!this.exists()) return { accounts: [], warnings };
    let raw;
    try {
      raw = readFileSync(this.filePath, 'utf8');
    } catch (err) {
      warnings.push('unreadable: ' + (err && err.message));
      this.log('account_store_unreadable', { path: this.filePath, error: String(err && err.message) });
      return { accounts: [], warnings };
    }
    if (!raw.trim()) return { accounts: [], warnings };
    let data;
    try {
      data = JSON.parse(raw);
    } catch (err) {
      warnings.push('invalid json: ' + (err && err.message));
      this.log('account_store_corrupt', { path: this.filePath, error: String(err && err.message) });
      this._backupCorrupt();
      return { accounts: [], warnings };
    }
    const list = Array.isArray(data) ? data : data && Array.isArray(data.accounts) ? data.accounts : null;
    if (!list) {
      warnings.push('unexpected shape: expected a JSON array of accounts');
      this.log('account_store_bad_shape', { path: this.filePath });
      this._backupCorrupt();
      return { accounts: [], warnings };
    }
    const accounts = [];
    const seen = new Set();
    for (const item of list) {
      const acct = normalizeAccount(item);
      if (!acct.token) continue; // an account without a token cannot be used at all
      if (seen.has(acct.id)) {
        warnings.push('duplicate id ignored: ' + acct.id);
        continue;
      }
      seen.add(acct.id);
      accounts.push(acct);
    }
    return { accounts, warnings };
  }

  /** Account list only; warnings go through the constructor's log hook. */
  list() {
    const { accounts, warnings } = this.read();
    for (const w of warnings) this.log('account_store_warning', { path: this.filePath, warning: w });
    return accounts;
  }

  /** Normalise and atomically replace the whole document. */
  write(accounts, { now = nowSec() } = {}) {
    const list = (Array.isArray(accounts) ? accounts : []).map((a) => normalizeAccount(a, now));
    const text = JSON.stringify(list, null, 2) + '\n';
    atomicWriteText(this.filePath, text);
    return list;
  }

  /** Find by id, then by email (case-insensitive). */
  find(idOrEmail) {
    if (!idOrEmail) return null;
    const needle = String(idOrEmail).trim();
    if (!needle) return null;
    const accounts = this.list();
    return (
      accounts.find((a) => a.id === needle) ||
      accounts.find((a) => a.email && a.email.toLowerCase() === needle.toLowerCase()) ||
      null
    );
  }

  /** Full create/update from a raw one-paste blob (the browser-capture path). */
  upsertFromBlob(raw, { id = '', email = '', source = 'browser-paste', now = nowSec() } = {}) {
    const probe = parseCredBlob(raw);
    const existing = this._pickExisting({ id, email, token: probe.token });
    const parsed = parseCredBlob(raw, existing ? existing.token : '', existing ? existing.cookies : '');
    if (!parsed.token) {
      return { ok: false, created: false, error: 'no_token_in_paste', account: existing };
    }
    const accounts = this.list();
    const fingerprint = credentialFingerprint(parsed.token, parsed.cookies);
    const duplicate = accounts.find(
      (a) => (!existing || a.id !== existing.id) && credentialFingerprint(a.token, a.cookies) === fingerprint,
    );
    if (duplicate) {
      return { ok: true, created: false, deduped: true, account: duplicate };
    }
    const base = existing ? { ...existing } : { id: id || randomUUID(), created_at: now, source };
    const credentialsChanged =
      !existing || existing.token !== parsed.token || existing.cookies !== parsed.cookies;
    const account = normalizeAccount(
      {
        ...base,
        id: id || base.id,
        email: email || base.email || '',
        token: parsed.token,
        cookies: parsed.cookies,
        source: base.source || source,
        // Editing credentials is a new session: a mute or an error must not
        // follow it (same rule the Python pool applies on manual credential
        // edits), so a fresh paste always re-enters rotation.
        state: credentialsChanged ? 'idle' : base.state,
        muted_until: credentialsChanged ? null : base.muted_until,
        last_error: credentialsChanged ? '' : base.last_error,
        updated_at: now,
      },
      now,
    );
    const next = existing
      ? accounts.map((a) => (a.id === existing.id ? account : a))
      : [...accounts, account];
    this.write(next, { now });
    this.log('account_saved', { id: account.id, created: !existing, email: account.email });
    return { ok: true, created: !existing, account };
  }

  /** Create or update from explicit token/cookies (non-paste callers). */
  upsert({ token, cookies = '', id = '', email = '', source = 'manual', now = nowSec() } = {}) {
    const raw = [token, cookies && ('Cookie: ' + cookies)].filter(Boolean).join(' || ');
    return this.upsertFromBlob(raw, { id, email, source, now });
  }

  /** Move an account between idle / error / muted. */
  setState(idOrEmail, state, { mutedUntil = null, error = '', now = nowSec() } = {}) {
    if (!ACCOUNT_STATES.includes(state)) {
      return { ok: false, error: 'invalid_state: ' + state + ' (expected ' + ACCOUNT_STATES.join('|') + ')' };
    }
    const accounts = this.list();
    const target = this._pickIn(accounts, idOrEmail);
    if (!target) return { ok: false, error: 'account_not_found' };
    const account = {
      ...target,
      state,
      muted_until: state === 'muted' ? positiveIntOrNull(mutedUntil ?? target.muted_until) : null,
      last_error: state === 'error' ? String(error || target.last_error || '') : '',
      updated_at: now,
    };
    this.write(accounts.map((a) => (a.id === target.id ? account : a)), { now });
    return { ok: true, account };
  }

  /** Record a successful upstream turn: back to idle, un-muted, timestamp kept. */
  markOk(idOrEmail, { now = nowSec() } = {}) {
    const accounts = this.list();
    const target = this._pickIn(accounts, idOrEmail);
    if (!target) return { ok: false, error: 'account_not_found' };
    const account = normalizeAccount(
      { ...target, state: 'idle', muted_until: null, last_error: '', last_ok_at: now, updated_at: now },
      now,
    );
    this.write(accounts.map((a) => (a.id === target.id ? account : a)), { now });
    return { ok: true, account };
  }

  /** Delete an account. Returns the removed id, or null when nothing matched. */
  remove(idOrEmail) {
    const accounts = this.list();
    const target = this._pickIn(accounts, idOrEmail);
    if (!target) return { ok: false, error: 'account_not_found' };
    this.write(accounts.filter((a) => a.id !== target.id));
    this.log('account_removed', { id: target.id });
    return { ok: true, removed: target.id };
  }

  _pickIn(accounts, idOrEmail) {
    if (!idOrEmail) return null;
    const needle = String(idOrEmail).trim();
    return (
      accounts.find((a) => a.id === needle) ||
      accounts.find((a) => a.email && a.email.toLowerCase() === needle.toLowerCase()) ||
      null
    );
  }

  _pickExisting({ id, email, token }) {
    const accounts = this.list();
    if (id) {
      const byId = accounts.find((a) => a.id === String(id).trim());
      if (byId) return byId;
    }
    if (email) {
      const byEmail = this._pickIn(accounts, email);
      if (byEmail) return byEmail;
    }
    // Same token already stored under another id: treat it as the same account
    // so re-capturing an existing login refreshes it instead of duplicating it.
    if (token) {
      const byToken = accounts.find((a) => a.token === token);
      if (byToken) return byToken;
    }
    return null;
  }

  _backupCorrupt() {
    const stamp = nowSec();
    const backup = this.filePath + '.corrupt-' + stamp;
    try {
      copyFileSync(this.filePath, backup);
      return backup;
    } catch {
      return null;
    }
  }
}

export default AccountStore;
