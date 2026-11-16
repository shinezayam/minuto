"""Frontend↔backend шаардлагын (contract) хормын зураг: `python -m app.openapi_snapshot` нь docs/openapi.json-г шинэчилнэ."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"


def current() -> str:
    from app.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    SNAPSHOT.write_text(current(), encoding="utf-8")
    print("wrote", SNAPSHOT, file=sys.stderr)
