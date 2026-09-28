"""Vercel serverless entrypoint — re-exports the FastAPI `app` from server.py.

Vercel's Python runtime loads `api/index.py` and serves the `app`
variable. All routes (including /webui static) are handled by FastAPI,
with `vercel.json` rewriting every path to this function.

Serverless notes (see vercel.json + README):
- Read-only filesystem except /tmp → ACCOUNT_STORE_PATH defaults to
  /tmp/accounts.json (override via Vercel env vars).
- No persistence across cold starts → configure DeepSeek credentials
  via env vars (DEEPSEEK_TOKEN_1 / DEEPSEEK_COOKIES_1, ...), not the
  panel JSON file.
- server.py skips the background stats-history sampler when VERCEL=1
  (set automatically by the platform).
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Writable path on Vercel (read-only FS except /tmp). setdefault so a
# user-configured ACCOUNT_STORE_PATH in Vercel env vars still wins.
os.environ.setdefault("ACCOUNT_STORE_PATH", "/tmp/accounts.json")
os.environ.setdefault("LOG_FORMAT", "json")

from server import app  # noqa: E402

# Vercel also accepts `handler`; keep both names valid.
handler = app
