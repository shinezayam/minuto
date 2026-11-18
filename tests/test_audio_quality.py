"""Upload хийсэн аудионы чанарын шинжилгээ (дуу сонсогдохгүй / сул / хагарсан / шуугиантай)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services.audio_utils import analyze_audio_quality, classify_audio_levels, ffmpeg_exe
from tests.conftest import register
from tests.test_providers import needs_ffmpeg


def _speech(n: int, loud: float = -25.0, gap: float = -60.0) -> list[float]:
    return [gap if i % 3 == 0 else loud for i in range(n)]


def test_classify_levels():
    assert classify_audio_levels([], []) is None
    assert classify_audio_levels(_speech(100), [-10.0] * 100) is None
    assert classify_audio_levels([-80.0] * 100, [-70.0] * 100) == "silent"
    assert classify_audio_levels(_speech(100, -48, -70), [-40.0] * 100) == "quiet"
    assert classify_audio_levels(_speech(100), [0.0] * 10 + [-10.0] * 90) == "clipping"
    assert classify_audio_levels([-30.0 - (i % 4) for i in range(100)], [-20.0] * 100) == "noisy"


def _lavfi(path: Path, source: str, af: str | None = None) -> None:
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source, "-t", "5"]
    if af:
        cmd += ["-af", af]
    subprocess.run([*cmd, str(path)], check=True)


@needs_ffmpeg
@pytest.mark.parametrize(
    "source, af, expected",
    [
        ("anullsrc=r=16000:cl=mono", None, "silent"),
        # 0.5 секунд дуу / 0.5 секунд нам гүм — ярианы хэв маяг
        ("sine=f=300:d=5", "volume='if(lt(mod(t,1),0.5),4,0)':eval=frame", None),
        ("sine=f=300:d=5", "volume='if(lt(mod(t,1),0.5),0.05,0)':eval=frame", "quiet"),
        ("sine=f=300:d=5", "volume=40", "clipping"),
    ],
)
def test_analyze_generated_audio(tmp_path, source, af, expected):
    path = tmp_path / "a.wav"
    _lavfi(path, source, af)
    assert analyze_audio_quality(path) == expected


@needs_ffmpeg
async def test_upload_reports_audio_issue(anon_client, tmp_path):
    token = (await register(anon_client))["token"]
    headers = {"Authorization": f"Bearer {token}"}
    path = tmp_path / "silence.wav"
    _lavfi(path, "anullsrc=r=16000:cl=mono")
    mid = (await anon_client.post("/api/meetings", json={}, headers=headers)).json()["id"]
    with path.open("rb") as f:
        r = await anon_client.post(
            f"/api/meetings/{mid}/upload", files={"file": ("silence.wav", f, "audio/wav")}, headers=headers
        )
    assert r.status_code == 200, r.text
    assert r.json()["audio_issue"] == "silent"
