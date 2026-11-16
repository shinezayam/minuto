"""Экспортын нийтлэг логик: батлалтын төлөв, шийдвэрийн ангилал."""
from __future__ import annotations

from typing import List, Tuple

from app.models.minutes import Decision, MeetingMinutes

DRAFT_NOTICE_MN = "Анхаар: Энэ тэмдэглэлийг AI автоматаар үүсгэсэн бөгөөд хүн баталгаажуулаагүй байна. Ашиглахын өмнө шалгана уу."
DRAFT_NOTICE_LATIN = "Anhaar: Ene temdeglel AI avtomataar vvsgesen bogood hvn batalgaajuulaagvi baina. Ashiglahiin omno shalgana uu."


def status_line(m: MeetingMinutes, latin: bool = False) -> Tuple[str, bool]:
    """(текст, батлагдсан эсэх)."""
    if m.minutes_status == "approved":
        who = m.approved_by or "-"
        when = (m.approved_at or "")[:10]
        return (f"Batlagdsan: {who}, {when}", True) if latin else (f"Батлагдсан: {who}, {when}", True)
    if m.minutes_status == "in_review":
        return ("Shalgaj bui (batlagdaagvi)", False) if latin else ("Шалгаж байна (батлагдаагүй)", False)
    return ("Noorog (AI vvsgesen)", False) if latin else ("Ноорог (AI үүсгэсэн)", False)


def split_decisions(m: MeetingMinutes) -> Tuple[List[Decision], List[Decision]]:
    """(батлагдсан шийдвэрүүд, санал/эргэлзээтэй/татгалзсан). Санал нь шийдвэр БИШ тул тусад нь харуулна.
    Хүн хүлээн авсан/засварласан бол шийдвэр; татгалзсан бол хоёр дахь жагсаалтад."""
    confirmed, other = [], []
    for d in m.decisions:
        if d.review in ("accepted", "edited") or (d.review is None and d.status == "confirmed"):
            confirmed.append(d)
        else:
            other.append(d)
    return confirmed, other


STATUS_MN = {"proposed": "Санал", "unclear": "Тодорхойгүй", "rejected": "Татгалзсан", "confirmed": "Шийдвэр"}
STATUS_LATIN = {"proposed": "Sanal", "unclear": "Todorhoigvi", "rejected": "Tatgalzsan", "confirmed": "Shiidver"}
