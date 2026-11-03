from __future__ import annotations

import io
from datetime import datetime
from typing import List, Optional

from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT

from app.models.minutes import MeetingMinutes


def generate_meeting_docx(
    meeting_id: str,
    title: str,
    created_at: str,
    minutes: MeetingMinutes,
    transcript: Optional[List[dict]] = None,
) -> bytes:
    """Generate a DOCX file from meeting minutes."""
    doc = Document()

    # Set default font
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Arial"
    font.size = Pt(11)

    # Title
    heading = doc.add_heading(title, level=0)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Meeting metadata
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        date_str = dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        date_str = created_at

    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = meta.add_run(f"Огноо: {date_str}")
    run.font.size = Pt(10)
    run.font.color.rgb = RGBColor(100, 100, 100)

    doc.add_paragraph()  # Spacer

    # Summary
    doc.add_heading("Товч агуулга", level=1)
    doc.add_paragraph(minutes.summary)

    # Topics
    if minutes.topics:
        doc.add_heading("Хэлэлцсэн асуудлууд", level=1)
        for topic in minutes.topics:
            p = doc.add_paragraph()
            run = p.add_run(f"• {topic.title}")
            run.bold = True
            if topic.description:
                p.add_run(f"\n{topic.description}")

    # Decisions
    if minutes.decisions:
        doc.add_heading("Шийдвэрүүд", level=1)
        for i, decision in enumerate(minutes.decisions, 1):
            p = doc.add_paragraph()
            run = p.add_run(f"{i}. {decision.text}")
            run.bold = True
            if decision.rationale:
                p.add_run(f"\n   Шалтгаан: {decision.rationale}")

    # Action Items
    if minutes.action_items:
        doc.add_heading("Үүрэг даалгавар", level=1)

        # Create table
        table = doc.add_table(rows=1, cols=4)
        table.style = "Light Grid Accent 1"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER

        # Header row
        hdr_cells = table.rows[0].cells
        hdr_cells[0].text = "№"
        hdr_cells[1].text = "Даалгавар"
        hdr_cells[2].text = "Хариуцагч"
        hdr_cells[3].text = "Хугацаа"

        # Make header bold
        for cell in hdr_cells:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.bold = True

        # Data rows
        for i, item in enumerate(minutes.action_items, 1):
            row_cells = table.add_row().cells
            row_cells[0].text = str(i)
            row_cells[1].text = item.task
            row_cells[2].text = item.owner or "-"
            row_cells[3].text = item.due_date or "-"

    # Open questions
    if minutes.open_questions:
        doc.add_heading("Нээлттэй асуудлууд", level=1)
        for q in minutes.open_questions:
            doc.add_paragraph(q.text, style="List Bullet")

    # Transcript appendix
    if transcript:
        doc.add_page_break()
        doc.add_heading("Бичвэр (хавсралт)", level=1)

        for segment in transcript:
            start_ms = segment.get("start_ms", 0)
            minutes_val = start_ms // 60000
            seconds_val = (start_ms % 60000) // 1000
            timestamp = f"[{minutes_val:02d}:{seconds_val:02d}]"

            p = doc.add_paragraph()
            run = p.add_run(f"{timestamp} ")
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(100, 100, 100)
            p.add_run(segment.get("text", ""))

    # Save to bytes
    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer.read()
