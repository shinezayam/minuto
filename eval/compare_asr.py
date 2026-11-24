"""ASR загваруудыг ижил бичлэг дээр харьцуулна: Qwen-Omni (production-ийн яг одоогийн код) ба ElevenLabs Scribe.
ТӨЛБӨРТЭЙ — QWEN_API_KEY болон ELEVENLABS_API_KEY хэрэгтэй (.env эсвэл орчны хувьсагч). CI-д хэзээ ч ажиллуулахгүй.

    pip install -r requirements.txt jiwer
    python -m eval.compare_asr <аудио_хавтас> <гаралтын_хавтас> [--probe] [--hints hints.txt]
        [--providers qwen,scribe] [--scribe-model scribe_v3] [--language mn]

<аудио_хавтас>: .wav/.mp3/.m4a/.webm/.ogg/.flac файлууд. Файл бүрийн хажууд ижил нэртэй `.ref.txt` (гараар засаж
бичсэн зөв бичвэр) байвал WER/CER болон НЭМЭГДСЭН үг (= зохиомол үг) тоологдоно. Байхгүй бол зөвхөн бичвэрүүдийг
хадгална — нүдээр харьцуулна.
--probe: 30с чимээгүй, 30с намуухан шуугиан, 30с хүчтэй шуугиан үүсгээд загвар бүрт өгнө. Яриа огт байхгүй тул
гарсан үг бүр зохиомол үг.
--hints: мөр бүрт нэг нэр/нэр томьёо. Qwen-д prompt-ийн "гарч болох үгс", Scribe-д `keyterms` болж очно.

Гаралт: <гаралтын_хавтас>/<файл>/<загвар>.txt, report.json, report.md.
Яригчийн шошго, [...] тэмдэглэгээ, (инээд) мэт ярианы бус дууг WER тооцохоос өмнө хасна.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List

import httpx

from app.config import settings
from app.services import asr as asr_mod
from app.services.audio_utils import ffmpeg_exe

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".webm", ".ogg", ".flac", ".mp4", ".aac"}
SCRIBE_URL = "https://api.elevenlabs.io/v1/speech-to-text"
SCRIBE_USD_PER_HOUR = 0.20  # elevenlabs.io/pricing/api, 2026-11; keyterms +0.05
SCRIBE_KEYTERM_USD_PER_HOUR = 0.05


# ---------- тохиргоо ----------

def env_value(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name].strip()
    env = Path(".env")
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return ""


# ---------- аудио ----------

def duration_sec(path: Path) -> float:
    out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path), "-f", "null", "-"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    times = re.findall(r"time=(\d+):(\d+):(\d+\.\d+)", out.stderr)
    if not times:
        return 0.0
    h, m, s = times[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


def make_probes(out_dir: Path) -> List[Path]:
    """Яриагүй туршилтын файлууд: энд гарсан бүх үг зохиомол."""
    out_dir.mkdir(parents=True, exist_ok=True)
    specs = {
        "probe_silence": ["-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono"],
        "probe_noise_quiet": ["-f", "lavfi", "-i", "anoisesrc=r=16000:a=0.01:c=pink"],
        "probe_noise_loud": ["-f", "lavfi", "-i", "anoisesrc=r=16000:a=0.15:c=brown"],
    }
    paths = []
    for name, src in specs.items():
        p = out_dir / f"{name}.wav"
        subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *src, "-t", "30",
                        "-ac", "1", "-ar", "16000", str(p)], check=True)
        paths.append(p)
    return paths


# ---------- загварууд ----------

async def run_qwen(path: Path, hints: List[str], args) -> str:
    """Production-ийн QwenOmniASRProvider-ийг яг байгаагаар нь (prompt, 30с хэсэг, яригч) ажиллуулна."""
    prov = asr_mod.QwenOmniASRProvider()
    segs = await prov.transcribe(str(path), context=asr_mod.ASRContext(hints=hints))
    return "\n".join(s.text for s in segs)


async def run_scribe(path: Path, hints: List[str], args) -> str:
    key = env_value("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY тохируулаагүй байна")
    data: Dict[str, object] = {
        "model_id": args.scribe_model,
        "diarize": "true",
        "tag_audio_events": "true",
        "timestamps_granularity": "word",
    }
    if args.language:
        data["language_code"] = args.language
    terms = [re.sub(r"[<>{}\[\]\\]", "", h)[:50].strip() for h in hints]
    terms = [t for t in terms if t and len(t.split()) <= 5][:1000]
    if terms:
        data["keyterms"] = terms
    async with httpx.AsyncClient(timeout=1800) as client:
        with path.open("rb") as fh:
            res = await client.post(SCRIBE_URL, headers={"xi-api-key": key}, data=data,
                                    files={"file": (path.name, fh, "application/octet-stream")})
    if res.status_code != 200:
        raise RuntimeError(f"Scribe HTTP {res.status_code}: {res.text[:300]}")
    body = res.json()
    # Нэг яригчийн дараалсан үгсийг "Яригч N: ..." мөр болгоно (production-ийн хэлбэртэй адил)
    lines: List[str] = []
    speakers: Dict[str, int] = {}
    cur, buf = None, []
    for w in body.get("words") or []:
        kind = w.get("type")
        if kind == "audio_event":
            buf.append(f"({w.get('text', '').strip('()')})")
            continue
        if kind == "spacing":
            buf.append(w.get("text", " "))
            continue
        spk = w.get("speaker_id") or "speaker_0"
        if spk != cur:
            if buf and "".join(buf).strip():
                lines.append(f"Яригч {speakers[cur]}: " + "".join(buf).strip() if cur else "".join(buf).strip())
            speakers.setdefault(spk, len(speakers) + 1)
            cur, buf = spk, []
        buf.append(w.get("text", ""))
    if buf and "".join(buf).strip():
        lines.append(f"Яригч {speakers.get(cur, 1)}: " + "".join(buf).strip())
    return "\n".join(lines) if lines else (body.get("text") or "").strip()


PROVIDERS: Dict[str, Callable] = {"qwen": run_qwen, "scribe": run_scribe}


# ---------- хэмжилт ----------

_SPEAKER_RE = re.compile(r"(?m)^\s*\**\s*(?:Яригч|Speaker|speaker)[\s_]*\d+\s*\**\s*[:：]\s*")


def normalize_words(text: str) -> List[str]:
    text = _SPEAKER_RE.sub(" ", text)
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text)  # [...], (инээд)
    return re.findall(r"\w+", text.lower().replace("ё", "е"))


def score(ref: str, hyp: str) -> dict:
    import jiwer

    r, h = normalize_words(ref), normalize_words(hyp)
    if not r:
        return {"ref_words": 0, "hyp_words": len(h)}
    out = jiwer.process_words(" ".join(r), " ".join(h) or "<хоосон>")
    runs = []  # 3+ үг дараалан нэмэгдсэн хэсэг = зохиосон хэллэг байх магадлал өндөр
    for chunk in out.alignments[0]:
        if chunk.type == "insert" and chunk.hyp_end_idx - chunk.hyp_start_idx >= 3:
            runs.append(" ".join(h[chunk.hyp_start_idx:chunk.hyp_end_idx]))
    return {
        "ref_words": len(r),
        "hyp_words": len(h),
        "wer": round(out.wer, 4),
        "cer": round(jiwer.cer(" ".join(r), " ".join(h) or "<хоосон>"), 4),
        "substitutions": out.substitutions,
        "deletions": out.deletions,
        "insertions": out.insertions,
        "insertion_rate": round(out.insertions / len(r), 4),
        "inserted_runs_3plus": len(runs),
        "inserted_runs_examples": runs[:10],
    }


# ---------- үндсэн ----------

async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):  # Windows console (cp1252) кирилл хэвлэж чаддаггүй
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio_dir", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--providers", default="qwen,scribe")
    ap.add_argument("--scribe-model", default="scribe_v3")
    ap.add_argument("--language", default="mn", help="Scribe-ийн language_code; хоосон бол автоматаар таних")
    ap.add_argument("--hints", type=Path)
    ap.add_argument("--probe", action="store_true")
    args = ap.parse_args()

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    for p in providers:
        if p not in PROVIDERS:
            sys.exit(f"Тодорхойгүй загвар: {p}")
    hints = [l.strip() for l in args.hints.read_text(encoding="utf-8").splitlines() if l.strip()] if args.hints else []

    files = sorted(p for p in args.audio_dir.iterdir() if p.suffix.lower() in AUDIO_EXT) if args.audio_dir.is_dir() else []
    if args.probe:
        files += make_probes(args.out_dir / "_probes")
    if not files:
        sys.exit("Аудио файл олдсонгүй (--probe ашиглаж болно)")

    report: Dict[str, dict] = {}
    for f in files:
        dur = duration_sec(f)
        ref_path = f.with_name(f.stem + ".ref.txt")
        ref = ref_path.read_text(encoding="utf-8") if ref_path.is_file() else None
        is_probe = f.stem.startswith("probe_")
        entry: dict = {"duration_sec": round(dur, 1), "has_ref": ref is not None, "probe": is_probe, "results": {}}
        (args.out_dir / f.stem).mkdir(parents=True, exist_ok=True)
        for p in providers:
            print(f"[{p}] {f.name} ({dur / 60:.1f} мин) ...", flush=True)
            t0 = time.time()
            try:
                text = await PROVIDERS[p](f, hints, args)
                err = None
            except Exception as e:  # noqa: BLE001 — нэг файл унасан ч бусдыг үргэлжлүүлнэ
                text, err = "", str(e)
                print(f"   АЛДАА: {e}", flush=True)
            elapsed = time.time() - t0
            (args.out_dir / f.stem / f"{p}.txt").write_text(text, encoding="utf-8")
            res: dict = {"seconds": round(elapsed, 1), "error": err, "words": len(normalize_words(text)),
                         "speakers": len(set(re.findall(r"(?m)^\s*Яригч\s*(\d+)", text)))}
            if is_probe:
                res["hallucinated_words"] = res["words"]  # яриагүй файл: гарсан бүх үг зохиомол
            elif ref is not None and not err:
                res.update(score(ref, text))
            if p == "scribe":
                res["est_cost_usd"] = round(dur / 3600 * (SCRIBE_USD_PER_HOUR + (SCRIBE_KEYTERM_USD_PER_HOUR if hints else 0)), 4)
            entry["results"][p] = res
        report[f.name] = entry

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = write_markdown(report, providers, args)
    (args.out_dir / "report.md").write_text(md, encoding="utf-8")
    print("\n" + md)


def write_markdown(report: Dict[str, dict], providers: List[str], args) -> str:
    lines = [f"# ASR харьцуулалт ({', '.join(providers)}; Scribe: {args.scribe_model})", ""]
    lines += ["| Файл | Урт | Загвар | WER | CER | Нэмэгдсэн үг | 3+ үгийн зохиомол хэсэг | Яригч | Хугацаа | Алдаа |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    totals: Dict[str, Dict[str, float]] = {p: {"ref": 0, "s": 0, "d": 0, "i": 0, "runs": 0, "probe": 0} for p in providers}
    for name, e in report.items():
        for p, r in e["results"].items():
            ins = r.get("hallucinated_words", r.get("insertions", "—"))
            lines.append(f"| {name} | {e['duration_sec'] / 60:.1f} мин | {p} | {r.get('wer', '—')} | {r.get('cer', '—')} | "
                         f"{ins} | {r.get('inserted_runs_3plus', '—')} | {r.get('speakers', '—')} | {r['seconds']}с | "
                         f"{(r['error'] or '')[:60]} |")
            t = totals[p]
            if "wer" in r:
                t["ref"] += r["ref_words"]; t["s"] += r["substitutions"]; t["d"] += r["deletions"]
                t["i"] += r["insertions"]; t["runs"] += r["inserted_runs_3plus"]
            t["probe"] += r.get("hallucinated_words", 0)
    lines += ["", "## Нийт (бүх лавлах бичвэртэй файл)", "", "| Загвар | WER | Нэмэгдсэн үг / 100 үг | 3+ үгийн зохиомол хэсэг | Яриагүй файлд гарсан үг |",
              "|---|---|---|---|---|"]
    for p, t in totals.items():
        wer = f"{(t['s'] + t['d'] + t['i']) / t['ref']:.3f}" if t["ref"] else "—"
        ir = f"{100 * t['i'] / t['ref']:.1f}" if t["ref"] else "—"
        lines.append(f"| {p} | {wer} | {ir} | {int(t['runs'])} | {int(t['probe'])} |")
    lines += ["", "Оноо нь үгийн түвшний тааруулалт: гаралтыг (`<файл>/<загвар>.txt`) заавал нүдээр уншиж харьцуулна."]
    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(main())
