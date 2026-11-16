"""Үнэлгээний rubric өөрөө зөв оноодог эсэхийг бэлэн (canned) гаралтаар шалгана — Qwen дуудахгүй, зардалгүй."""
from __future__ import annotations

import json
from pathlib import Path

from eval.rubric import aggregate, score_case, similar
from eval.run_eval import compare, load_cases

CANNED = Path(__file__).resolve().parent.parent / "eval" / "canned"


def _run(name: str) -> dict:
    outputs = json.loads((CANNED / name).read_text(encoding="utf-8"))
    rows = [score_case(c, outputs[c["name"]]) for c in load_cases()]
    return {"rows": rows, "average": aggregate(rows)}


def test_fixtures_are_marked_synthetic_and_valid():
    cases = load_cases()
    assert len(cases) >= 2 and all(c.get("synthetic") for c in cases)  # жинхэнэ хурал биш (зөвшөөрөлгүйгээр оруулахгүй)
    for c in cases:
        ids = {s["id"] for s in c["transcript"]}
        assert ids and "gold" in c


def test_stem_matching_handles_mongolian_suffixes():
    assert similar("Үнийг бууруулна", "Үнийг бууруулах")
    assert not similar("Тайлан бэлдэх", "Үнийг бууруулах")


def test_good_model_scores_perfectly():
    avg = _run("good_model_outputs.json")["average"]
    assert avg["decision_precision"] == avg["decision_recall"] == 1.0
    assert avg["false_confirmed"] == avg["hallucinations"] == 0
    assert avg["action_owner_acc"] == avg["action_due_acc"] == avg["evidence_found_rate"] == 1.0


def test_bad_model_is_penalised_on_every_failure_mode():
    avg = _run("bad_model_outputs.json")["average"]
    assert avg["false_confirmed"] > 0  # санал байсан үнийн бууралтыг "confirmed" гэж зарласан
    assert avg["hallucinations"] > 0  # байхгүй даалгавар/шийдвэр, олдохгүй ишлэл
    assert avg["decision_precision"] < 1.0 and avg["action_owner_acc"] < 1.0 and avg["evidence_found_rate"] < 1.0


def test_baseline_comparison_marks_regressions():
    good, bad = _run("good_model_outputs.json"), _run("bad_model_outputs.json")
    lines = compare(bad, good)
    assert any("hallucinations" in l and "WORSE" in l for l in lines)
    assert not any("WORSE" in l for l in compare(good, bad))  # сайжирсан бол ямар ч муудалт үгүй
