"""webui static serving: the SPA shell must never be cached.

A cached index.html outlives the deploy that produced it: every rebuild
renames the hashed bundles, so the stale shell asks for assets that no
longer exist and the panel paints a blank white page until a hard
refresh. Hashed assets, by contrast, should be cached forever.
"""

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import server  # noqa: E402

WEBUI_DIR = pathlib.Path(server._WEBUI_DIR)
HAS_DIST = (WEBUI_DIR / "index.html").is_file()

pytestmark = pytest.mark.skipif(
    not HAS_DIST, reason="webui dist not built (run npm run build in webui-new/)"
)


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app)


@pytest.mark.parametrize("path", ["/webui/", "/webui", "/webui/accounts"])
def test_spa_shell_is_never_cached(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store, max-age=0, must-revalidate"


def test_hashed_assets_are_immutable(client):
    assets = sorted((WEBUI_DIR / "assets").glob("*.js"))
    assert assets, "vite build produced no js assets"
    r = client.get(f"/webui/assets/{assets[0].name}")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_other_static_files_use_a_short_cache(client):
    r = client.get("/webui/favicon.svg")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=300"


def test_missing_bundle_is_404_not_html(client):
    """Regression: the SPA fallback used to answer missing bundles with HTML.

    A browser holding a shell from an older deploy then ran HTML as JS, the
    module graph aborted, and the panel rendered as a blank white page.
    """
    r = client.get("/webui/assets/index-stale0hash.js")
    assert r.status_code == 404
    assert not r.headers["content-type"].startswith("text/html")


def test_unknown_route_still_falls_back_to_shell(client):
    r = client.get("/webui/accounts/some-deep-link")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
