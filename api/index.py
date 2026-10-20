"""Vercel serverless entrypoint — re-exports the FastAPI `app` from server.py.

Vercel's Python runtime loads `api/index.py` and serves the `app`
variable. All routes (including /webui static) are handled by FastAPI,
with `vercel.json` rewriting every path to this function.

Serverless notes (see vercel.json + README):
- Read-only filesystem except /tmp. Both writable stores therefore default to
  /tmp: ACCOUNT_STORE_PATH (accounts) and API_KEY_STORE_PATH (client API keys);
  a Vercel env var of the same name still wins.
- Nothing in /tmp survives a cold start. Bind a Vercel KV store
  (KV_REST_API_URL + KV_REST_API_TOKEN) and the account store - including
  refreshed tokens and cookies - becomes durable; /tmp then only serves as a
  local mirror. Client API keys are still instance-local.
- Credentials are added in the WebUI account pool, not via env vars
  (DEEPSEEK_TOKEN/DEEPSEEK_COOKIES are no longer read).
- server.py skips the background stats-history sampler when VERCEL=1
  (set automatically by the platform).
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Writable paths on Vercel (read-only FS except /tmp). setdefault so a
# user-configured value in Vercel env vars still wins.
os.environ.setdefault("ACCOUNT_STORE_PATH", "/tmp/accounts.json")
# Import-time requirement: server.py builds the API-key store while importing,
# so pointing it at the read-only project directory aborts the whole import and
# every route answers 500 (FUNCTION_INVOCATION_FAILED).
os.environ.setdefault("API_KEY_STORE_PATH", "/tmp/api_keys.sqlite3")
os.environ.setdefault("LOG_FORMAT", "json")

from server import app  # noqa: E402

# Vercel also accepts `handler`; keep both names valid.
handler = app
