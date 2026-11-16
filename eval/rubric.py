"""Минутын чанарын үнэлгээний rubric. Бүх хэмжүүр тодорхой (LLM-гүй) тул ижил оролтод ижил оноо гарна.

Хэмжүүрүүд (case бүрт):
  decision_precision / decision_recall — "confirmed" гэж гаргасан шийдвэр алтан (gold) шийдвэртэй таарах хувь
  false_confirmed      — санал/эргэлзээтэй байсан зүйлийг "confirmed" гэж зарласан тоо (хамгийн муу алдаа)
  action_owner_acc / action_due_acc — таарсан даалгаврын хариуцагч, хугацаа зөв эсэх
  hallucinations       — алтан жагсаалтад огт байхгүй шийдвэр/даалгавар + нотолгоо нь бичвэрт олдоогүй зүйлийн нийлбэр
"""
from __future__ import annotations

import re
from typing import Dict, List

from app.models.minutes import MeetingMinutes, TranscriptSegment
from app.services.minutes_verify import normalize, verify_minutes

MATCH_THRESHOLD = 0.5  # үгийн Jaccard ижилсэл энэнээс дээш бол "ижил зүйл"


def _tokens(text: str) -> set:
    """Монгол хэлний нөхцөл дагавар олон (бууруулна/бууруулах) тул үгийн эхний 5 үсгээр (stem) харьцуулна."""
    return {w[:5] for w in normalize(text).split() if len(w) > 2}


def similar(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= MATCH_THRESHOLD


def _match(items: List[str], gold: List[str]) -> Dict[int, int]:
    """items[i] → gold[j] (нэг gold нэг л удаа таардаг)."""
    used: set = set()
    out: Dict[int, int] = {}
    for i, text in enumerate(items):
        for j, g in enumerate(gold):
            if j not in used and similar(text, g):
                out[i] = j
                used.add(j)
                break
    return out


def score_case(case: dict, output: dict) -> dict:
    transcript = [TranscriptSegment(**s) for s in case["transcript"]]
    # Загварын ГАРАЛТЫГ өөрийг нь (verify-ийн бууруулалтгүйгээр) оноож, нотолгоог тусад нь бодитоор шалгана:
    # ингэснээр загвар/prompt-ийн чанар (санал → "confirmed" гэж зарлах) харагдана, хамгаалалт нь түүнийг халхалж чадахгүй.
    minutes = MeetingMinutes(**output)
    checked = verify_minutes(MeetingMinutes(**output), transcript)
    for raw, chk in zip((*minutes.decisions, *minutes.action_items), (*checked.decisions, *checked.action_items)):
        raw.verification = chk.verification
    gold_dec = case["gold"]["decisions"]  # [{"text","status"}]
    gold_confirmed = [d["text"] for d in gold_dec if d["status"] == "confirmed"]
    gold_not_confirmed = [d["text"] for d in gold_dec if d["status"] != "confirmed"]

    reported_confirmed = [d for d in minutes.decisions if d.status == "confirmed"]
    m = _match([d.text for d in reported_confirmed], gold_confirmed)
    tp = len(m)
    precision = tp / len(reported_confirmed) if reported_confirmed else (1.0 if not gold_confirmed else 0.0)
    recall = tp / len(gold_confirmed) if gold_confirmed else 1.0
    false_confirmed = sum(1 for d in reported_confirmed if any(similar(d.text, g) for g in gold_not_confirmed))

    gold_actions = case["gold"]["action_items"]  # [{"task","owner","due_date"}]
    am = _match([a.task for a in minutes.action_items], [g["task"] for g in gold_actions])
    if not gold_actions:
        owner_acc = due_acc = 1.0 if not minutes.action_items else 0.0  # gold-д даалгавар байхгүй: зохиосон даалгавар л алдаа
    else:
        owner_acc = due_acc = 0.0
    owner_ok = sum(1 for i, j in am.items() if (minutes.action_items[i].owner or "").strip().casefold() == (gold_actions[j].get("owner") or "").strip().casefold())
    due_ok = sum(1 for i, j in am.items() if (minutes.action_items[i].due_date or "") == (gold_actions[j].get("due_date") or ""))

    # "unclear/proposed" нь баталгаа биш тул зохиомол гэж тооцохгүй; "confirmed" (баттай мэдэгдсэн) л хариуцлагатай
    unmatched_decisions = sum(
        1 for d in minutes.decisions if d.status == "confirmed" and not any(similar(d.text, g["text"]) for g in gold_dec)
    )
    unmatched_actions = len(minutes.action_items) - len(am)
    asserted = [*reported_confirmed, *minutes.action_items]
    evidence_rate = round(sum(1 for it in asserted if it.verification == "quote_found") / len(asserted), 3) if asserted else 1.0
    bad_evidence = sum(1 for it in (*minutes.decisions, *minutes.action_items) if it.verification == "quote_not_found")

    return {
        "case": case["name"],
        "decision_precision": round(precision, 3),
        "decision_recall": round(recall, 3),
        "false_confirmed": false_confirmed,
        "action_owner_acc": round(owner_ok / len(am), 3) if am else owner_acc,
        "action_due_acc": round(due_ok / len(am), 3) if am else due_acc,
        "hallucinations": unmatched_decisions + unmatched_actions + bad_evidence,
        "evidence_found_rate": evidence_rate,
    }


def aggregate(rows: List[dict]) -> dict:
    keys = [k for k in rows[0] if k != "case"] if rows else []
    return {k: round(sum(r[k] for r in rows) / len(rows), 3) for k in keys} | {"cases": len(rows)}
