"""Үнэлгээг ажиллуулж тайлан гаргана.

  python -m eval.run_eval --provider file:outputs.json          # бэлэн гаралтыг оноолно (зардалгүй)
  python -m eval.run_eval --provider qwen --out before.json     # Qwen-ийг ДУУДНА (төлбөртэй! QWEN_API_KEY хэрэгтэй)
  python -m eval.run_eval --provider qwen --baseline before.json --out after.json   # өмнөх/дараах харьцуулалт

Prompt, загвар өөрчилсөн бүрт өмнөх/дараах тоо заавал харуулна: --baseline-тай ажиллуулж, дундаж нь буурсан хэмжүүрийг (↓) харуул.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from eval.rubric import aggregate, score_case

FIXTURES = Path(__file__).parent / "fixtures"
HIGHER_IS_BETTER = {"decision_precision", "decision_recall", "action_owner_acc", "action_due_acc", "evidence_found_rate"}
LOWER_IS_BETTER = {"false_confirmed", "hallucinations"}


def load_cases(path: Path = FIXTURES) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(path.glob("*.json"))]


async def _qwen_outputs(cases: list[dict]) -> dict:
    from app.models.minutes import TranscriptSegment
    from app.services.llm import QwenLLMProvider

    provider = QwenLLMProvider()  # QWEN_API_KEY байхгүй бол RuntimeError
    out = {}
    for c in cases:
        m = await provider.generate_minutes([TranscriptSegment(**s) for s in c["transcript"]], c.get("participants"))
        out[c["name"]] = m.model_dump(exclude={"prepared_by", "speaker_map"})
    return out


def compare(current: dict, baseline: dict) -> list[str]:
    lines = []
    for k, v in current["average"].items():
        if k == "cases" or k not in baseline["average"]:
            continue
        b = baseline["average"][k]
        better = (v > b) if k in HIGHER_IS_BETTER else (v < b) if k in LOWER_IS_BETTER else None
        arrow = "=" if v == b else ("↑ better" if better else "↓ WORSE")
        lines.append(f"  {k:22s} {b:>6} → {v:>6}  {arrow}")
    return lines


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, help="qwen | file:<outputs.json>")
    ap.add_argument("--out")
    ap.add_argument("--baseline")
    ap.add_argument("--fixtures", default=str(FIXTURES))
    args = ap.parse_args(argv)

    cases = load_cases(Path(args.fixtures))
    if args.provider == "qwen":
        print("ANHAAR: Qwen-ийг бодитоор дуудаж байна (төлбөртэй).", file=sys.stderr)
        outputs = asyncio.run(_qwen_outputs(cases))
    elif args.provider.startswith("file:"):
        outputs = json.loads(Path(args.provider[5:]).read_text(encoding="utf-8"))
    else:
        ap.error("--provider qwen эсвэл file:<path>")

    rows = [score_case(c, outputs[c["name"]]) for c in cases]
    report = {"provider": args.provider, "rows": rows, "average": aggregate(rows)}
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["average"], ensure_ascii=False, indent=2))
    if args.baseline:
        print("\nBaseline-тай харьцуулалт:")
        print("\n".join(compare(report, json.loads(Path(args.baseline).read_text(encoding="utf-8")))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
