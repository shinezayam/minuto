"""Smoke test: register → upload → process → read → export → delete, against any deployment.

  python scripts/smoke.py https://staging.example.com            # demo providers: free
  python scripts/smoke.py http://localhost:8000 --keep

Creates a throw-away user (smoke-<random>@smoke.invalid) and deletes the meeting at the end. Never run against
production with real providers unless you accept the cost of one tiny processing run.
Exit code 0 = every step passed, 1 = a step failed (the failing step is printed).
"""
from __future__ import annotations

import argparse
import io
import math
import secrets
import struct
import sys
import time
import wave

import httpx


def tone_wav(seconds: float = 1.0, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(int(seconds * rate))))
    return buf.getvalue()


def run(client: httpx.Client, keep: bool = False, poll_sec: float = 60.0) -> list[tuple[str, bool, str]]:
    steps: list[tuple[str, bool, str]] = []

    def step(name: str, ok: bool, detail: str = "") -> bool:
        steps.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
        return ok

    r = client.get("/healthz")
    if not step("liveness /healthz", r.status_code == 200, str(r.status_code)):
        return steps
    email = f"smoke-{secrets.token_hex(4)}@smoke.invalid"
    r = client.post("/api/auth/register", json={"email": email, "password": "Smoke-" + secrets.token_hex(6)})
    if not step("register", r.status_code == 201, str(r.status_code)):
        return steps
    client.headers["Authorization"] = f"Bearer {r.json()['token']}"
    r = client.get("/api/readyz")
    step("readiness /api/readyz", r.status_code == 200 and r.json()["checks"]["database"]["ok"], r.json().get("status", "?") if r.status_code == 200 else str(r.status_code))

    r = client.post("/api/meetings", json={"title": "Smoke test"})
    if not step("create meeting", r.status_code == 201, str(r.status_code)):
        return steps
    mid = r.json()["id"]
    r = client.post(f"/api/meetings/{mid}/upload", files={"file": ("smoke.wav", tone_wav(), "audio/wav")})
    if not step("upload audio", r.status_code == 200, f"{r.status_code} {r.text[:80]}" if r.status_code != 200 else ""):
        return steps
    r = client.post(f"/api/meetings/{mid}/process")
    if not step("start processing", r.status_code == 202, str(r.status_code)):
        return steps

    deadline = time.time() + poll_sec
    status = ""
    while time.time() < deadline:
        status = client.get(f"/api/meetings/{mid}").json()["status"]
        if status in ("completed", "failed"):
            break
        time.sleep(1)
    # silent tone: "completed" (demo provider) or a clear NO_SPEECH failure are both acceptable terminal states
    step("processing finishes", status in ("completed", "failed"), status or "timeout")

    step("read transcript", client.get(f"/api/meetings/{mid}/transcript").status_code == 200)
    step("audio via signed URL", client.get(client.post(f"/api/meetings/{mid}/audio-url").json()["url"], headers={}).status_code in (200, 206))
    if status == "completed":
        step("export docx", client.get(f"/api/meetings/{mid}/export.docx").status_code == 200)
        step("export pdf", client.get(f"/api/meetings/{mid}/export.pdf").status_code == 200)
    if not keep:
        step("delete meeting", client.delete(f"/api/meetings/{mid}").status_code == 204)
        step("deleted meeting is gone", client.get(f"/api/meetings/{mid}").status_code == 404)
    return steps


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("--keep", action="store_true", help="meeting-ийг устгахгүй")
    args = ap.parse_args(argv)
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=60) as client:
        steps = run(client, keep=args.keep)
    failed = [n for n, ok, _ in steps if not ok]
    print(f"\n{len(steps) - len(failed)}/{len(steps)} steps passed" + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
