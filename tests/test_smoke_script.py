"""Smoke script-ийг бодит ASGI app-ын эсрэг ажиллуулж, staging-д ажиллуулахаас өмнө өөрөө зөв эсэхийг батална."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import smoke  # noqa: E402


def test_smoke_script_passes_end_to_end_against_the_app():
    with TestClient(app) as tc:  # lifespan ажиллана → worker, migrations
        steps = smoke.run(tc, poll_sec=20)
    failed = [(n, d) for n, ok, d in steps if not ok]
    assert not failed, failed
    assert {n for n, _, _ in steps} >= {"register", "upload audio", "start processing", "processing finishes", "delete meeting"}
