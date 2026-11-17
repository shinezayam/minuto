"""Хурлын тэмдэглэлийн PDF — албан бичгийн A4 хэлбэр.

Бүтэц: гарчиг → огноо → хурал эхэлсэн цаг → оролцсон бүрэлдэхүүн → хэлэлцсэн асуудал → хурлын тэмдэглэл →
дүгнэлт, шийдвэр → үүрэг даалгавар → нээлттэй асуудал → хөтөлсөн хүний гарын үсэг.
Бичвэр (transcript) PDF-д ОРОХГҮЙ — зөвхөн DOCX-д.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

from app.models.minutes import MeetingMinutes
from app.services.export_common import DRAFT_NOTICE_MN, STATUS_MN, split_decisions, status_line

_FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# Албан бичгийн A4 талбар (мм): зүүн 30, баруун 15, дээд/доод 20
MARGIN_LEFT, MARGIN_RIGHT, MARGIN_TOP, MARGIN_BOTTOM = 30, 15, 20, 20
BODY = 11  # үндсэн фонтын хэмжээ
LINE = 6  # мөрийн өндөр (мм)
INDENT = 10  # догол мөр (мм)
ULAANBAATAR = timezone(timedelta(hours=8))  # UTC+8, зуны цаг байхгүй


class MeetingPDF(FPDF):
    """Монгол кирилл дэмждэг PDF: DejaVu Sans (Unicode TTF) шигтгэнэ."""

    def __init__(self) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.add_font("DejaVu", "", str(_FONTS / "DejaVuSans.ttf"))
        self.add_font("DejaVu", "B", str(_FONTS / "DejaVuSans-Bold.ttf"))
        self.add_font("DejaVu", "I", str(_FONTS / "DejaVuSans-Oblique.ttf"))
        self.set_margins(MARGIN_LEFT, MARGIN_TOP, MARGIN_RIGHT)
        self.set_auto_page_break(auto=True, margin=MARGIN_BOTTOM)

    def footer(self):
        self.set_y(-12)
        self.set_font("DejaVu", "", 8)
        self.set_text_color(130, 130, 130)
        self.cell(0, 5, f"{self.page_no()}/{{nb}}", align="C")


# ---------------------------------------------------------------- огноо, цаг


def _local_dt(created_at: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:  # DB-д UTC-ээр хадгалагдсан
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ULAANBAATAR)


def _genitive(n: int) -> str:
    """Тооны харьяалахын нөхцөл: 1, 4, 9-өөр төгссөн (11, 14, 19...) → -ний, бусад → -ны (17-ны, 20-ны)."""
    return f"{n}-ний" if n % 10 in (1, 4, 9) else f"{n}-ны"


def _date_mn(dt: datetime) -> str:
    return f"{dt.year} оны {dt.month} сарын {_genitive(dt.day)} өдөр"


def _duration_mn(ms: int) -> str:
    minutes = max(1, round(ms / 60000))
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} цаг {m} минут"
    return f"{h} цаг" if h else f"{m} минут"


# ---------------------------------------------------------------- бичих туслахууд


def _heading(pdf: MeetingPDF, text: str, keep_with: float = 20) -> None:
    """keep_with: гарчгийн доор дор хаяж ийм зай (мм) байхгүй бол шинэ хуудаснаас эхэлнэ (ганцаараа үлдэхгүй)."""
    pdf.ln(3)
    if pdf.get_y() > pdf.h - MARGIN_BOTTOM - keep_with:
        pdf.add_page()
    pdf.set_font("DejaVu", "B", BODY)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, LINE + 1, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)


def _paragraph(pdf: MeetingPDF, text: str, indent: bool = True, style: str = "", size: int = BODY) -> None:
    """Догол мөртэй, хоёр талдаа тэгшилсэн догол."""
    pdf.set_font("DejaVu", style, size)
    for para in [p.strip() for p in (text or "").split("\n") if p.strip()]:
        if indent:
            _first_line_indent(pdf, para)
        else:
            pdf.set_x(MARGIN_LEFT)
            pdf.multi_cell(0, LINE, para, align="J", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _subheading(pdf: MeetingPDF, text: str) -> None:
    pdf.ln(1)
    pdf.set_font("DejaVu", "B", BODY)
    pdf.set_x(MARGIN_LEFT + INDENT)
    pdf.multi_cell(0, LINE, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _first_line_indent(pdf: MeetingPDF, text: str) -> None:
    """Эхний мөр нь INDENT-ээр догол, бүх мөр хоёр талдаа тэгшилсэн (fpdf2 text_columns)."""
    with pdf.text_columns(text_align="J", line_height=1.25) as cols:
        with cols.paragraph(first_line_indent=INDENT) as par:
            par.write(text)


def _numbered(pdf: MeetingPDF, items: List[str], style: str = "") -> None:
    pdf.set_font("DejaVu", style, BODY)
    for n, text in enumerate(items, 1):
        pdf.set_x(MARGIN_LEFT + INDENT)
        num = f"{n}."
        pdf.cell(8, LINE, num)
        pdf.multi_cell(0, LINE, text, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


# ---------------------------------------------------------------- PDF


def generate_meeting_pdf(
    meeting_id: str,
    title: str,
    created_at: str,
    minutes: MeetingMinutes,
    transcript: Optional[List[dict]] = None,  # noqa: ARG001 — PDF-д бичвэр орохгүй (зөвхөн DOCX)
    participants: Optional[List[str]] = None,
    duration_ms: int = 0,
    notes: Optional[str] = None,
) -> bytes:
    """Хурлын тэмдэглэлийг албан бичгийн A4 хэлбэрээр PDF болгоно."""
    pdf = MeetingPDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_text_color(0, 0, 0)

    # Гарчиг
    pdf.set_font("DejaVu", "B", 14)
    pdf.multi_cell(0, 8, (title or minutes.title or "Хурал").upper(), align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("DejaVu", "", BODY)
    pdf.cell(0, LINE, "ХУРЛЫН ТЭМДЭГЛЭЛ", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(6)

    # Огноо (зүүн) — хурал эхэлсэн цаг
    dt = _local_dt(created_at)
    pdf.set_font("DejaVu", "", BODY)
    if dt:
        pdf.cell(0, LINE, _date_mn(dt), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(2)
        intro = f"Хурал {dt.year} оны {dt.month} сарын {_genitive(dt.day)} өдрийн {dt.hour:02d} цаг {dt.minute:02d} минутад эхэлсэн"
        intro += f" бөгөөд {_duration_mn(duration_ms)} үргэлжилсэн." if duration_ms > 0 else "."
        _paragraph(pdf, intro)

    # Оролцсон бүрэлдэхүүн
    attendees = [a for a in (participants or minutes.participants or []) if a and a.strip()]
    _heading(pdf, "ХУРАЛД ОРОЛЦСОН БҮРЭЛДЭХҮҮН:")
    if attendees:
        _numbered(pdf, attendees)
        pdf.ln(1)
        _paragraph(pdf, f"Хурлын ирц: нийт {len(attendees)} хүн оролцов.")
    else:
        _paragraph(pdf, "Оролцогчдыг бүртгээгүй.", style="I")

    # Хэлэлцсэн асуудал
    if minutes.topics:
        _heading(pdf, "ХЭЛЭЛЦСЭН АСУУДАЛ:")
        _numbered(pdf, [t.title for t in minutes.topics])

    # Хурлын тэмдэглэл: "Товч агуулга" (бүх хурлын) → дээрх ХЭЛЭЛЦСЭН АСУУДАЛ-ын дугаараар тус бүрийн яриа
    _heading(pdf, "ХУРЛЫН ТЭМДЭГЛЭЛ:")
    if minutes.summary:
        _subheading(pdf, "Товч агуулга")
        _paragraph(pdf, minutes.summary)
    for n, topic in enumerate(minutes.topics, 1):
        if not topic.description:
            continue
        _subheading(pdf, f"{n}. {topic.title}")
        _paragraph(pdf, topic.description)
    if not minutes.summary and not any(t.description for t in minutes.topics) and not minutes.qa:
        _paragraph(pdf, "Тэмдэглэл үүсээгүй.", style="I")
    if minutes.qa:
        _subheading(pdf, "Асуулт, хариулт")
        for qa in minutes.qa:
            who = f" ({qa.asked_by})" if qa.asked_by else ""
            _paragraph(pdf, f"Асуулт{who}: {qa.question}")
            ans_who = f" ({qa.answered_by})" if qa.answered_by else ""
            _paragraph(pdf, f"Хариулт{ans_who}: {qa.answer or 'хариулаагүй'}")

    # Дүгнэлт, шийдвэр
    approved = status_line(minutes)[1]
    confirmed, other = split_decisions(minutes)
    # Хөтлөгчийн гар тэмдэглэл (хүн бичсэн)
    if notes and notes.strip():
        _heading(pdf, "ХӨТЛӨГЧИЙН ТЭМДЭГЛЭЛ:")
        pdf.set_font("DejaVu", "", BODY)
        for line in notes.splitlines():
            stripped = line.lstrip()
            depth = (len(line) - len(stripped)) // 2
            pdf.set_x(MARGIN_LEFT + INDENT + depth * 6)
            pdf.multi_cell(0, LINE, stripped, align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    _heading(pdf, "ХУРЛЫН ДҮГНЭЛТ, ШИЙДВЭР:")
    if confirmed:
        pdf.set_font("DejaVu", "", BODY)
        for n, d in enumerate(confirmed, 1):
            pdf.set_x(MARGIN_LEFT + INDENT)
            pdf.cell(8, LINE, f"{n}.")
            mark = "  (шалгах)" if d.needs_review and not approved else ""
            pdf.multi_cell(0, LINE, d.text + mark, align="J", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            if d.rationale:
                pdf.set_font("DejaVu", "I", BODY - 1)
                pdf.set_text_color(70, 70, 70)
                pdf.set_x(MARGIN_LEFT + INDENT + 8)
                pdf.multi_cell(0, LINE - 1, f"Үндэслэл: {d.rationale}", align="J", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_text_color(0, 0, 0)
                pdf.set_font("DejaVu", "", BODY)
    else:
        _paragraph(pdf, "Шийдвэр гараагүй.", style="I")
    if other:
        pdf.ln(2)
        pdf.set_font("DejaVu", "B", BODY - 1)
        pdf.set_x(MARGIN_LEFT + INDENT)
        pdf.cell(0, LINE, "Санал, тодорхойгүй зүйлс (шийдвэр биш):", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("DejaVu", "", BODY - 1)
        for d in other:
            pdf.set_x(MARGIN_LEFT + INDENT)
            pdf.multi_cell(0, LINE - 1, f"– [{STATUS_MN.get(d.status, d.status)}] {d.text}", align="L",
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # Үүрэг даалгавар (хүснэгт: мөр бүр өөрийн өндрөөр)
    if minutes.action_items:
        _heading(pdf, "ҮҮРЭГ ДААЛГАВАР:", keep_with=35)  # хүснэгтийн толгой + 1 мөртэй хамт
        pdf.set_font("DejaVu", "", BODY - 1)
        pdf.set_draw_color(150, 150, 150)
        with pdf.table(
            col_widths=(8, 92, 35, 30),
            width=pdf.w - MARGIN_LEFT - MARGIN_RIGHT,
            align="LEFT",
            text_align=("CENTER", "LEFT", "LEFT", "CENTER"),
            line_height=LINE - 0.5,
            headings_style=FontFace(emphasis="BOLD", fill_color=(238, 238, 238)),
            padding=1.5,
        ) as table:
            head = table.row()
            for h in ("№", "Даалгавар", "Хариуцагч", "Хугацаа"):
                head.cell(h)
            for n, item in enumerate(minutes.action_items, 1):
                row = table.row()
                row.cell(str(n))
                row.cell(item.task)
                row.cell(item.owner or "—")
                row.cell(item.due_date or "—")

    # Нээлттэй асуудал
    if minutes.open_questions:
        _heading(pdf, "ШИЙДВЭРЛЭГДЭЭГҮЙ АСУУДАЛ:")
        _numbered(pdf, [q.text for q in minutes.open_questions])

    # Гарын үсэг
    pdf.ln(12)
    if pdf.get_y() > pdf.h - MARGIN_BOTTOM - 25:
        pdf.add_page()
    pdf.set_font("DejaVu", "", BODY)
    name = (minutes.prepared_by or "").split("/")[-1].strip()
    pdf.cell(0, LINE, f"Хурлын тэмдэглэл хөтөлсөн: ............................  /{name or '                    '}/",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if minutes.minutes_status == "approved" and minutes.approved_by:
        pdf.ln(4)
        pdf.cell(0, LINE, f"Баталсан: ............................  /{minutes.approved_by}/", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # Батлагдаагүй бол анхааруулга (жижиг, саарал)
    if not approved:
        pdf.ln(8)
        pdf.set_font("DejaVu", "I", 8)
        pdf.set_text_color(150, 90, 0)
        pdf.multi_cell(0, 4, f"{status_line(minutes)[0]}. {DRAFT_NOTICE_MN}", align="L", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(0, 0, 0)

    return bytes(pdf.output())
