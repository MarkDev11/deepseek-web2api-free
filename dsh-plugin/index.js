/**
 * dsh-plugin-deepseek-web2api
 *
 * DeepSeek Harness (dsh) plugin that makes the deepseek-web2api proxy a
 * first-class model source:
 *
 *   1. waits for / probes <baseURL>/health (optionally starting the local
 *      backend with "python server.py" when config.local.workdir is set),
 *   2. logs in through POST /admin/api/login with config.adminPassword,
 *   3. stores the returned session token in the harness credential store under
 *      config.keyName (the same name llm-pi-ai's apiKeyEnv resolves), and
 *   4. re-mints it every config.refreshMinutes so a Vercel cold start (which
 *      invalidates the in-memory admin token) heals itself.
 *
 * Intentionally dependency-free: no imports from @deepseek-ai/* and no npm
 * packages, so a profile only needs a junction to this directory.
 */

import { spawn } from "node:child_process";

export const name = "deepseek-web2api";

/** The credential service is required: that is where the token is published. */
export const inject = ["credentials"];

const DEFAULTS = {
  baseURL: "https://deepseek-web2api-free-chi.vercel.app",
  keyName: "DSWEB_API_KEY",
  adminPassword: "",
  refreshMinutes: 10,
  requestTimeoutMs: 20000,
};

function pick(raw, key) {
  const v = raw?.[key];
  return v === undefined || v === null || v === "" ? DEFAULTS[key] : v;
}

function log(ctx, level, message) {
  const logger = ctx?.logger;
  try {
    if (logger && typeof logger[level] === "function") logger[level](message);
    else if (typeof logger === "function") logger(message);
    else console.log(`[deepseek-web2api] ${message}`);
  } catch {
    /* logging must never break the plugin */
  }
}

async function requestJSON(url, init, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...init, signal: controller.signal });
    const text = await res.text();
    let body = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      body = null;
    }
    return { status: res.status, ok: res.ok, body, text };
  } finally {
    clearTimeout(timer);
  }
}

async function probeHealth(baseURL, timeoutMs) {
  try {
    const res = await requestJSON(baseURL.replace(/\/+$/, "") + "/health", {}, timeoutMs);
    return res.ok;
  } catch {
    return false;
  }
}

function startLocalBackend(ctx, local) {
  if (!local?.workdir) return null;
  const port = Number(local.port || 8080);
  const python = local.pythonExe || "python";
  let child;
  try {
    child = spawn(python, ["server.py"], {
      cwd: local.workdir,
      env: { ...process.env, PORT: String(port), HOST: "127.0.0.1" },
      stdio: "ignore",
      windowsHide: true,
    });
    child.on("error", (err) => log(ctx, "warn", `local backend failed to start: ${err?.message || err}`));
    log(ctx, "info", `local backend starting: ${python} server.py (port ${port})`);
  } catch (err) {
    log(ctx, "warn", `local backend spawn error: ${err?.message || err}`);
    return null;
  }
  return child;
}

function summarizeAccounts(body) {
  const accounts = Array.isArray(body?.accounts) ? body.accounts : [];
  if (!accounts.length) return "0 account(s)";
  const now = Date.now() / 1000;
  const usable = accounts.filter((a) => {
    if (!a) return false;
    // /admin/api/accounts reports state=idle|busy|error, plus a mute deadline.
    if (a.state === "error" || a.status === "error") return false;
    const mutedUntil = Number(a.muted_until ?? 0);
    if (mutedUntil > now) return false;
    return true;
  });
  return `${accounts.length} account(s), ${usable.length} usable`;
}

export function apply(ctx, rawConfig = {}) {
  const config = {
    baseURL: String(pick(rawConfig, "baseURL")).replace(/\/+$/, ""),
    keyName: pick(rawConfig, "keyName"),
    adminPassword: rawConfig?.adminPassword ?? DEFAULTS.adminPassword,
    refreshMinutes: Math.max(1, Number(pick(rawConfig, "refreshMinutes")) || DEFAULTS.refreshMinutes),
    requestTimeoutMs: Math.max(1000, Number(pick(rawConfig, "requestTimeoutMs")) || DEFAULTS.requestTimeoutMs),
    local: rawConfig?.local ?? null,
  };

  let child = null;
  let disposed = false;
  let busy = false;
  let timer = null;

  async function sync(reason) {
    if (disposed || busy) return;
    busy = true;
    try {
      let healthy = await probeHealth(config.baseURL, config.requestTimeoutMs);
      if (!healthy && config.local?.workdir && !child) {
        child = startLocalBackend(ctx, config.local);
        for (let i = 0; i < 30 && !healthy && !disposed; i += 1) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          healthy = await probeHealth(config.baseURL, config.requestTimeoutMs);
        }
      }
      if (!healthy) {
        log(ctx, "warn", `backend unreachable at ${config.baseURL} (${reason}); will retry`);
        return;
      }
      if (!config.adminPassword) {
        log(ctx, "warn", "adminPassword is empty: cannot mint a session token");
        return;
      }

      const login = await requestJSON(
        config.baseURL + "/admin/api/login",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ password: config.adminPassword }),
        },
        config.requestTimeoutMs,
      );
      const token = login?.body?.token;
      if (!login.ok || !token) {
        log(ctx, "warn", `admin login failed (HTTP ${login.status}) (${reason})`);
        return;
      }

      await ctx.credentials.set(config.keyName, token);

      let summary = "";
      try {
        const accounts = await requestJSON(
          config.baseURL + "/admin/api/accounts",
          { headers: { authorization: `Bearer ${token}` } },
          config.requestTimeoutMs,
        );
        if (accounts.ok) summary = `, ${summarizeAccounts(accounts.body)}`;
      } catch {
        /* summary is best effort */
      }

      log(ctx, "info", `credential ${config.keyName} refreshed (${reason})${summary}`);
    } catch (err) {
      log(ctx, "warn", `refresh failed (${reason}): ${err?.message || err}`);
    } finally {
      busy = false;
    }
  }

  sync("startup");
  timer = setInterval(() => {
    sync("timer");
  }, config.refreshMinutes * 60 * 1000);
  if (typeof timer.unref === "function") timer.unref();

  log(ctx, "info", `watching ${config.baseURL} every ${config.refreshMinutes}m -> credential ${config.keyName}`);

  return () => {
    disposed = true;
    if (timer) clearInterval(timer);
    if (child) {
      try {
        child.kill();
      } catch {
        /* ignore */
      }
      child = null;
    }
  };
}
