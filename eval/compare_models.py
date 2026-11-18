"""Qwen загваруудыг ижил хурлууд дээр харьцуулна: rubric оноо, хугацаа, токен (ТӨЛБӨРТЭЙ — QWEN_API_KEY хэрэгтэй).

    python -m eval.compare_models eval/fixtures qwen3.7-plus,qwen3.8-max 2 out_dir
    # eval/fixtures_models/ — санал/шийдвэр холилдсон, шийдвэр өөрчлөгдсөн, ойлгомжгүй яриатай нэмэлт хурлууд

out_dir-д report.json болон загвар бүрийн гаралт (outputs-<model>.json) хадгалагдана: оноо нь үгийн давхцлаар
тааруулдаг тул гаралтыг заавал нүдээр уншиж харьцуулна.
"""
import asyncio, json, sys, time
from pathlib import Path
import httpx
from app.config import settings
from app.services import llm as llm_mod
from app.models.minutes import TranscriptSegment
from eval.rubric import score_case, aggregate

fixtures, models, runs, outdir = Path(sys.argv[1]), sys.argv[2].split(","), int(sys.argv[3]), Path(sys.argv[4])
outdir.mkdir(parents=True, exist_ok=True)
usage = []

async def _complete(self, client, messages):
    url = settings.qwen_base_url.rstrip("/") + "/chat/completions"
    res = await client.post(url, headers={"Authorization": f"Bearer {settings.qwen_api_key}"}, json={
        "model": settings.qwen_model, "temperature": 0.2, "enable_thinking": settings.qwen_enable_thinking,
        "response_format": {"type": "json_object"}, "messages": messages})
    if res.status_code != 200:
        raise llm_mod.ProviderError(f"HTTP {res.status_code}: {res.text[:300]}")
    body = res.json(); usage.append(body.get("usage") or {})
    return body["choices"][0]["message"]["content"]
llm_mod.QwenLLMProvider._complete = _complete

async def main():
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(fixtures.glob("*.json"))]
    report = {}
    for model in models:
        settings.qwen_model = model
        prov = llm_mod.QwenLLMProvider()
        rows, outs, times, toks_in, toks_out, errors = [], {}, [], 0, 0, 0
        for r in range(runs):
            for c in cases:
                usage.clear(); t0 = time.time()
                try:
                    m = await prov.generate_minutes([TranscriptSegment(**s) for s in c["transcript"]], c.get("participants"))
                except Exception as e:
                    errors += 1; print(model, c["name"], "ERROR", e); continue
                times.append(time.time() - t0)
                toks_in += sum(u.get("prompt_tokens", 0) for u in usage); toks_out += sum(u.get("completion_tokens", 0) for u in usage)
                out = m.model_dump(exclude={"prepared_by", "speaker_map"})
                outs[f"{c['name']}#{r}"] = out
                rows.append(score_case(c, out))
        n = max(1, len(times))
        report[model] = {"avg": aggregate(rows), "avg_sec": round(sum(times)/n, 1), "max_sec": round(max(times or [0]), 1),
                         "tokens_in_per_meeting": round(toks_in/n), "tokens_out_per_meeting": round(toks_out/n), "errors": errors,
                         "per_case": rows}
        (outdir / f"outputs-{model}.json").write_text(json.dumps(outs, ensure_ascii=False, indent=1), encoding="utf-8")
        print(model, json.dumps({k: v for k, v in report[model].items() if k != "per_case"}, ensure_ascii=False))
    (outdir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

asyncio.run(main())
