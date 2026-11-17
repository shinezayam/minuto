"""P0.1 regression: the app must import, and every 204 route must register.

FastAPI 0.115 raises ``AssertionError: Status code 204 must not have a
response body`` at import time when a ``status_code=204`` route has no
``response_model=None``. That crashed the whole app on startup.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi.routing import APIRoute

from app.main import app
from tests.routes_util import iter_routes

BACKEND_DIR = Path(__file__).resolve().parents[1]

# Routes that must exist and answer 204 (method, path template).
EXPECTED_204 = {
    ("DELETE", "/api/templates/{template_id}"),
    ("DELETE", "/api/workspaces/{workspace_id}/members/{user_id}"),
    ("DELETE", "/api/workspaces/{workspace_id}/projects/{project_id}"),
    ("DELETE", "/api/meetings/{meeting_id}"),
    ("DELETE", "/api/contacts/{contact_id}"),
    ("DELETE", "/api/vocabulary/{term_id}"),
    ("DELETE", "/api/workspaces/{workspace_id}"),
    ("POST", "/api/workspaces/{workspace_id}/transfer"),
}


def _routes_204() -> set[tuple[str, str]]:
    found = set()
    for r in iter_routes(app):
        if isinstance(r, APIRoute) and r.status_code == 204:
            for method in r.methods:
                found.add((method, r.path))
    return found


def test_app_module_imports_in_fresh_interpreter():
    """Import in a clean process so a cached module can't hide a crash."""
    proc = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


def test_all_expected_204_routes_registered():
    missing = EXPECTED_204 - _routes_204()
    assert not missing, f"204 routes not registered: {sorted(missing)}"


def test_no_unexpected_204_route_without_response_model_none():
    for r in iter_routes(app):
        if isinstance(r, APIRoute) and r.status_code == 204:
            assert r.response_model is None, f"{r.path} would crash on startup"


def test_new_204_routes_are_tracked():
    """If someone adds a 204 route, force them to extend EXPECTED_204."""
    unexpected = _routes_204() - EXPECTED_204
    assert not unexpected, f"add to EXPECTED_204: {sorted(unexpected)}"

