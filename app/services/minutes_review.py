"""Минутын хүний шалгалт, батлалт: draft → in_review → approved, ширхэг тус бүрийн accept / edit / reject."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

STATUSES = ("draft", "in_review", "approved")
REVIEWABLE = {"decisions", "action_items"}


class ReviewError(Exception):
    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code, self.status, self.message = code, status, message

    @property
    def detail(self) -> str:
        return f"{self.code}: {self.message}"


def invalidate_approval(minutes: dict) -> dict:
    """Агуулга өөрчлөгдвөл өмнөх батлалт хүчингүй: approved → in_review (хүн дахин шалгаж батлана)."""
    if minutes.get("minutes_status") == "approved":
        minutes["minutes_status"] = "in_review"
        minutes["approved_by"] = None
        minutes["approved_at"] = None
    return minutes


def set_status(minutes: dict, status: str, user_label: str, can_approve: bool) -> dict:
    if status not in STATUSES:
        raise ReviewError("BAD_STATUS", 422, "Төлөв draft, in_review, approved-ийн аль нэг байх ёстой")
    if status == "approved" and not can_approve:
        raise ReviewError("FORBIDDEN_APPROVE", 403, "Зөвхөн эзэн эсвэл админ минутыг батлана")
    minutes["minutes_status"] = status
    if status == "approved":
        minutes["approved_by"] = user_label
        minutes["approved_at"] = datetime.now(timezone.utc).isoformat()
    else:
        minutes["approved_by"], minutes["approved_at"] = None, None
    return minutes


def review_item(minutes: dict, kind: str, index: int, decision: Optional[str], new_text: Optional[str] = None) -> dict:
    """Нэг шийдвэр/даалгаварт хүний шийдвэр тэмдэглэнэ. "edited" үед текст (шийдвэр: text, даалгавар: task) шинэчилнэ."""
    if kind not in REVIEWABLE:
        raise ReviewError("BAD_KIND", 422, "Зөвхөн decisions эсвэл action_items шалгана")
    if decision not in (None, "accepted", "edited", "rejected"):
        raise ReviewError("BAD_DECISION", 422, "decision нь accepted, edited, rejected эсвэл null")
    items = minutes.get(kind) or []
    if not (0 <= index < len(items)):
        raise ReviewError("ITEM_NOT_FOUND", 404, "Элемент олдсонгүй")
    item = items[index]
    if new_text is not None:
        if not new_text.strip() or len(new_text) > 1000:
            raise ReviewError("BAD_TEXT", 422, "Текст хоосон биш, 1000 тэмдэгтээс бага байх ёстой")
        item["text" if kind == "decisions" else "task"] = new_text.strip()
        decision = "edited"
    item["review"] = decision
    # Хүн шалгаж хүлээн авсан/засварласан шийдвэр "confirmed" болж болно; татгалзсан нь "rejected"
    if kind == "decisions" and decision in ("accepted", "edited"):
        item["status"] = "confirmed"
    if kind == "decisions" and decision == "rejected":
        item["status"] = "rejected"
    invalidate_approval(minutes)  # агуулга өөрчлөгдсөн
    return minutes
