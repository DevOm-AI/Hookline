from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

# The page holds no data and needs no key to load: its script asks for the API key and calls
# the API with it as a Bearer header. A header, unlike a cookie, isn't sent automatically, so
# no other site can make a browser replay deliveries (no CSRF).
router = APIRouter(include_in_schema=False)

# Receivers' error bodies end up on this page. Rendering uses textContent only, and the CSP
# is the second line: no inline or third-party script could run even if some markup got in.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

static_files = StaticFiles(directory=DASHBOARD_DIR)


@router.get("/dashboard")
def dashboard() -> FileResponse:
    return FileResponse(DASHBOARD_DIR / "index.html", headers=SECURITY_HEADERS)
