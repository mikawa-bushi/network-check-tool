"""Tests for issue #33 (U1): dashboard serves Plotly locally (offline-capable).

The dashboard must not depend on the Plotly CDN: it should load the bundle from a
local application route so it works with no outbound network access.
"""

import pytest

from network_check_tool.app import create_app
from network_check_tool.config.config import Config, DevelopmentConfig


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(
        Config, "SQLALCHEMY_DATABASE_URI", f"sqlite:///{tmp_path / 'plotly.db'}"
    )
    monkeypatch.setattr(DevelopmentConfig, "SQLALCHEMY_ECHO", False)
    return create_app("development")


def test_plotly_bundle_served_locally(app):
    from plotly.offline import get_plotlyjs

    resp = app.test_client().get("/vendor/plotly.min.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["Content-Type"].lower()
    assert "max-age=86400" in resp.headers.get("Cache-Control", "")
    body = resp.get_data(as_text=True)
    # Served bytes are exactly the bundle shipped with the installed plotly
    # (version-pinned to the dependency) — proves it's the local package, not a
    # CDN fetch. (The bundle itself references cdn.plot.ly for default map tiles,
    # so a substring check would be misleading here; the index test covers that
    # the *page* no longer loads the script from the CDN.)
    assert body == get_plotlyjs()


def test_plotly_bundle_revalidates_with_etag(app):
    client = app.test_client()
    etag = client.get("/vendor/plotly.min.js").headers.get("ETag")
    assert etag
    # A conditional request with the same ETag gets a cheap 304, not 4.8MB again.
    resp = client.get("/vendor/plotly.min.js", headers={"If-None-Match": etag})
    assert resp.status_code == 304


def test_index_uses_local_plotly_not_cdn(app):
    body = app.test_client().get("/").get_data(as_text=True)
    assert "/vendor/plotly.min.js" in body
    assert "cdn.plot.ly" not in body
    assert "plotly-latest" not in body
