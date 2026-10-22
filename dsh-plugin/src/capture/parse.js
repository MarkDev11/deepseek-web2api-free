/**
 * src/capture/parse.js - pure credential parsing for the DSWeb plugin.
 *
 * Port of the root repo's credentials.py (the authoritative reference):
 * _strip_bearer / _looks_like_token / _unwrap_value / _search_keys /
 * extract_refreshed_token / merge_cookie_blobs / _looks_like_cookie_header /
 * _strip_cookie_prefix / parse_credential_blob, plus the JWT-expiry and
 * auth-failure helpers so the plugin has exactly one home for credential
 * semantics.
 *
 * Everything here is pure: no fs, no network, no I/O - only the ECMAScript
 * standard library plus Buffer. That purity is what makes the offline suite in
 * test/capture.test.mjs meaningful.
 *
 * Why the parsing is this tolerant: DeepSeek hands credentials out in several
 * shapes and a human copies them out of DevTools by hand. A paste may be a bare
 * token, the localStorage.userToken wrapper ({"value": ...}), a raw "Cookie:"
 * header, or any of those joined by "||" / newlines. Whatever the paste does not
 * contain must fall back to what the account already has - a partial paste may
 * never blank out working credentials, because ds_session_id is HttpOnly and
 * therefore invisible to document.cookie.
 */

/** Upstream business codes that mean "these credentials are dead". */
export const AUTH_ERROR_CODES = new Set([40003]);

/** Message markers with the same meaning (matched case-insensitively). */
export const AUTH_ERROR_MARKERS = [
  'authorization failed',
  'invalid token',
  'token expired',
  'unauthorized',
  'not logged in',
  'credentials rejected',
];

/** Key names a token may hide behind inside a response payload. */
const TOKEN_KEYS = ['token', 'access_token', 'accessToken', 'jwt'];
/** Container keys worth descending into before anything else. */
const CONTAINER_KEYS = ['data', 'biz_data', 'bizData', 'result'];
/** localStorage stores its token as {"value": ..., "__version": "0"}. */
const WRAPPER_KEYS = ['value'];

const COOKIE_PREFIX_RE = /^\s*cookie\s*:\s*/i;

/** Minimum length of a plausible DeepSeek token (real ones are 64 chars). */
export const MIN_TOKEN_LENGTH = 20;

/** Strip an "Authorization: Bearer " prefix; port of _strip_bearer. */
export function stripBearer(value) {
  let s = typeof value === 'string' ? value.trim() : '';
  if (s.toLowerCase().startsWith('bearer ')) s = s.slice('bearer '.length).trim();
  return s;
}

/**
 * Port of _looks_like_token: non-empty, no space, at least MIN_TOKEN_LENGTH
 * characters once any Bearer prefix is gone.
 */
export function looksLikeToken(value) {
  if (typeof value !== 'string') return false;
  const s = stripBearer(value);
  return s.length >= MIN_TOKEN_LENGTH && !s.includes(' ');
}

/**
 * JWT claims without signature verification (port of decode_jwt_claims).
 * null when the value is not a JWT - DeepSeek's userToken is an opaque
 * 64-character string, so null is the normal answer, not an error.
 */
export function decodeJwtClaims(token) {
  const s = stripBearer(token);
  if (!s) return null;
  const parts = s.split('.');
  if (parts.length !== 3 || !parts[1]) return null;
  try {
    const claims = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'));
    if (claims && typeof claims === 'object' && !Array.isArray(claims)) return claims;
  } catch {
    /* not a JWT */
  }
  return null;
}

/** Unix seconds when the token expires; 0 when unknown (port of token_expires_at). */
export function tokenExpiresAt(token) {
  const claims = decodeJwtClaims(token);
  if (!claims) return 0;
  const exp = Number(claims.exp);
  return Number.isFinite(exp) ? Math.trunc(exp) : 0;
}

/** Seconds of validity left; null when the token carries no exp claim. */
export function tokenExpiresIn(token, now = Math.floor(Date.now() / 1000)) {
  const exp = tokenExpiresAt(token);
  if (!exp) return null;
  return exp - Math.trunc(now);
}

/** Port of should_refresh; an unknown expiry never triggers a proactive refresh. */
export function shouldRefresh(expiresAt, aheadSecs, now = Math.floor(Date.now() / 1000)) {
  const exp = Number(expiresAt) || 0;
  const ahead = Number(aheadSecs) || 0;
  if (exp <= 0 || ahead <= 0) return false;
  return Math.trunc(now) >= exp - ahead;
}

/** Port of is_auth_failure (HTTP 200 + code 40003 is the real signal, not 401). */
export function isAuthFailure(code, msg = '') {
  if (code !== null && code !== undefined && code !== '') {
    const n = Number(code);
    if (Number.isFinite(n) && AUTH_ERROR_CODES.has(n)) return true;
  }
  const low = String(msg || '').toLowerCase();
  return AUTH_ERROR_MARKERS.some((marker) => low.includes(marker));
}

/**
 * Port of _unwrap_value: DeepSeek stores its token as
 * {"value": "...", "__version": "0"}.
 */
export function unwrapValue(value) {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const inner = value.value;
    if (typeof inner === 'string' && inner) return inner;
  }
  return value;
}

/** Port of _search_keys: token keys first, then containers, then everything else. */
function searchKeys(node, depth = 0) {
  if (depth > 5) return '';
  if (Array.isArray(node)) {
    for (const item of node) {
      const found = searchKeys(item, depth + 1);
      if (found) return found;
    }
    return '';
  }
  if (!node || typeof node !== 'object') return '';
  for (const key of TOKEN_KEYS) {
    if (key in node) {
      const cand = unwrapValue(node[key]);
      if (looksLikeToken(cand)) return cand;
    }
  }
  for (const key of CONTAINER_KEYS) {
    if (key in node) {
      const found = searchKeys(node[key], depth + 1);
      if (found) return found;
    }
  }
  for (const key of WRAPPER_KEYS) {
    if (key in node && typeof node[key] === 'string') {
      const cand = stripBearer(node[key]);
      if (looksLikeToken(cand)) return cand;
    }
  }
  for (const [key, val] of Object.entries(node)) {
    if (TOKEN_KEYS.includes(key) || CONTAINER_KEYS.includes(key)) continue;
    const found = searchKeys(val, depth + 1);
    if (found) return found;
  }
  return '';
}

/**
 * Port of extract_refreshed_token. Tolerates {"data":{"biz_data":{"token":
 * {"value": ...}}}}, {"data":{"token": "..."}}, {"access_token": "..."}, a bare
 * localStorage wrapper, a JSON *string* holding one of those, and a plain
 * string. Returns "" when nothing token-shaped is present.
 */
export function extractRefreshedToken(payload) {
  if (typeof payload === 'string') {
    const s = payload.trim();
    if (s.startsWith('{')) {
      try {
        return extractRefreshedToken(JSON.parse(s));
      } catch {
        return '';
      }
    }
    return looksLikeToken(s) ? stripBearer(s) : '';
  }
  if (payload && typeof payload === 'object') {
    const found = searchKeys(payload);
    return found ? stripBearer(found) : '';
  }
  return '';
}

/** name=value; name2=value2 -> insertion-ordered Map (port of the inner parse). */
function cookieEntries(blob) {
  const out = new Map();
  for (let part of String(blob ?? '').split(';')) {
    part = part.trim();
    if (!part || !part.includes('=')) continue;
    const i = part.indexOf('=');
    const name = part.slice(0, i).trim();
    const value = part.slice(i + 1).trim();
    if (name) out.set(name, value);
  }
  return out;
}

/** Parsed cookie blob as a plain object. */
export function parseCookieHeader(blob) {
  return Object.fromEntries(cookieEntries(blob));
}

/**
 * Port of merge_cookie_blobs: two name=value blobs merged name-by-name with
 * newBlob winning. Keeping names we already had is deliberate - one response
 * rotates only a couple of cookies, and dropping a still-valid ds_session_id or
 * cf_clearance because it was absent from a paste would break the session.
 */
export function mergeCookieBlobs(oldBlob, newBlob) {
  const merged = cookieEntries(oldBlob);
  for (const [name, value] of cookieEntries(newBlob)) merged.set(name, value);
  const parts = [];
  for (const [name, value] of merged) parts.push(name + '=' + value);
  return parts.join('; ');
}

/** Port of _strip_cookie_prefix. */
export function stripCookiePrefix(value) {
  const text = String(value ?? '').replace(COOKIE_PREFIX_RE, '').trim();
  return text.replace(/;+$/, '').trim();
}

/** Port of _looks_like_cookie_header. */
export function looksLikeCookieHeader(value) {
  const text = String(value ?? '').trim();
  if (!text || !text.includes('=')) return false;
  if (COOKIE_PREFIX_RE.test(text)) return true;
  return text.includes(';');
}

/**
 * Port of parse_credential_blob - the one-paste import.
 *
 * raw may be a bare token, a localStorage wrapper, a raw "Cookie:" header, or
 * any combination joined by "||" / newlines. Absent parts fall back to the
 * account's current values and a pasted cookie header is merged name-by-name, so
 * an invisible HttpOnly ds_session_id survives a partial paste. When the blob
 * yields nothing recognisable the current credentials come back untouched: a bad
 * paste can never blank out a working account.
 *
 * @returns {{token: string, cookies: string, tokenFound: boolean, cookiesFound: boolean}}
 */
export function parseCredBlob(raw, currentToken = '', currentCookies = '') {
  const fallback = {
    token: currentToken || '',
    cookies: currentCookies || '',
    tokenFound: false,
    cookiesFound: false,
  };

  const text = String(raw ?? '').trim().replace(/^['"]+/, '').replace(/['"]+$/, '').trim();
  if (!text) return fallback;

  let token = '';
  let cookies = '';
  for (let chunk of text.split(/\|\||\n/)) {
    chunk = chunk.trim();
    if (!chunk) continue;
    if (chunk.startsWith('{') || chunk.startsWith('[')) {
      let found = '';
      try {
        found = extractRefreshedToken(JSON.parse(chunk));
      } catch {
        found = '';
      }
      if (found) token = found;
      continue;
    }
    if (looksLikeCookieHeader(chunk)) {
      cookies = stripCookiePrefix(chunk);
      continue;
    }
    const candidate = stripBearer(chunk);
    if (looksLikeToken(candidate)) token = candidate;
  }

  if (!token && !cookies) return fallback;
  return {
    token: token || currentToken || '',
    cookies: cookies ? mergeCookieBlobs(currentCookies, cookies) : currentCookies || '',
    tokenFound: Boolean(token),
    cookiesFound: Boolean(cookies),
  };
}
