import re

import pytest
from fastapi.testclient import TestClient

from app.api.dashboard import DASHBOARD_DIR


def test_page_loads_without_api_key(client: TestClient):
    del client.headers["Authorization"]

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Hookline</title>" in response.text


def test_page_has_strict_security_headers(client: TestClient):
    headers = client.get("/dashboard").headers

    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self';" in csp
    assert "frame-ancestors 'none'" in csp
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"


def test_page_has_no_inline_script_or_style(client: TestClient):
    # The CSP would block them, so they'd break the page silently.
    html = client.get("/dashboard").text

    assert re.search(r"<script(?![^>]*\bsrc=)", html) is None
    assert "<style" not in html
    assert "style=" not in html
    assert re.search(r"\son[a-z]+=", html) is None


@pytest.mark.parametrize(
    ("path", "content_type"),
    [("dashboard.js", "text/javascript"), ("dashboard.css", "text/css")],
)
def test_assets_are_served(client: TestClient, path: str, content_type: str):
    del client.headers["Authorization"]

    response = client.get(f"/dashboard/static/{path}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    assert response.text == (DASHBOARD_DIR / path).read_text()


def test_script_never_renders_api_data_as_html():
    # Error bodies come from receivers anyone can run, so they must stay text.
    script = (DASHBOARD_DIR / "dashboard.js").read_text()

    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script.replace("never innerHTML", "")


def test_dashboard_is_not_in_the_api_schema(client: TestClient):
    assert "/dashboard" not in client.get("/openapi.json").json()["paths"]


def test_script_keeps_the_api_key_out_of_browser_storage():
    # Storage outlives the page: another page of this origin in the same tab could read it.
    script = (DASHBOARD_DIR / "dashboard.js").read_text()

    for storage in ("sessionStorage", "localStorage", "indexedDB", "document.cookie"):
        assert storage not in script.replace("never in sessionStorage or localStorage", "")
