from __future__ import annotations

import io
from datetime import datetime
from typing import List, Optional

from fpdf import FPDF

from app.models.minutes import MeetingMinutes


class MeetingPDF(FPDF):
    """Custom PDF with Mongolian Cyrillic support."""
    
    def header(self):
        pass
    
    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Minuto - Page {self.page_no()}/{{nb}}", align="C")


def generate_meeting_pdf(
    meeting_id: str,
    title: str,
    created_at: str,
    minutes: MeetingMinutes,
    transcript: Optional[List[dict]] = None,
    participants: Optional[List[str]] = None,
) -> bytes:
    """Generate a PDF file from meeting minutes."""
    pdf = MeetingPDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    
    # Use Helvetica (built-in, supports Cyrillic via Unicode)
    # Title
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(0, 0, 0)
    pdf.multi_cell(0, 10, title, align="C")
    pdf.ln(5)
    
    # Meeting metadata
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        date_str = dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        date_str = created_at
    
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, f"Ognoo: {date_str}", align="C", new_x="LMARGIN", new_y="NEXT")
    
    # Participants
    attendees = participants or minutes.participants
    if attendees:
        pdf.cell(0, 6, f"Oroltsogchid: {', '.join(attendees)}", align="C", new_x="LMARGIN", new_y="NEXT")
    
    if minutes.prepared_by:
        pdf.cell(0, 6, f"Hurl Temdeglel hotolsun: {minutes.prepared_by}", align="C", new_x="LMARGIN", new_y="NEXT")
    
    pdf.ln(10)
    
    # Summary
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 8, "Tovch aguulga", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(0, 6, minutes.summary)
    pdf.ln(5)
    
    # Topics
    if minutes.topics:
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 8, "Kheltsegsen asuudluud", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 11)
        for topic in minutes.topics:
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 6, f"  {topic.title}", new_x="LMARGIN", new_y="NEXT")
            if topic.description:
                pdf.set_font("Helvetica", "", 11)
                pdf.multi_cell(0, 6, f"    {topic.description}")
        pdf.ln(3)
    
    # Decisions
    if minutes.decisions:
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 8, "Shiidvervvud", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 11)
        for i, decision in enumerate(minutes.decisions, 1):
            pdf.multi_cell(0, 6, f"  {i}. {decision.text}")
            if decision.rationale:
                pdf.set_font("Helvetica", "I", 10)
                pdf.set_text_color(80, 80, 80)
                pdf.multi_cell(0, 5, f"      Shaltgaan: {decision.rationale}")
                pdf.set_text_color(0, 0, 0)
        pdf.ln(3)
    
    # Action Items
    if minutes.action_items:
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 8, "Vvreg daalgavar", new_x="LMARGIN", new_y="NEXT")
        
        # Table header
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_fill_color(240, 240, 240)
        pdf.cell(10, 7, "#", border=1, fill=True, align="C")
        pdf.cell(90, 7, "Daalgavar", border=1, fill=True)
        pdf.cell(45, 7, "Hariutsagch", border=1, fill=True)
        pdf.cell(35, 7, "Khugaatsaa", border=1, fill=True, align="C")
        pdf.ln()
        
        # Table rows
        pdf.set_font("Helvetica", "", 10)
        for i, item in enumerate(minutes.action_items, 1):
            # Calculate row height based on content
            task_lines = len(item.task) // 40 + 1
            row_height = max(7, task_lines * 6)
            
            y_start = pdf.get_y()
            x_start = pdf.get_x()
            
            # Check if we need a new page
            if y_start + row_height > 280:
                pdf.add_page()
                y_start = pdf.get_y()
            
            pdf.cell(10, row_height, str(i), border=1, align="C")
            
            # Task (may wrap)
            pdf.multi_cell(90, 6, item.task, border=1)
            y_after_task = pdf.get_y()
            
            # Owner and due date
            pdf.set_xy(x_start + 100, y_start)
            pdf.cell(45, row_height, item.owner or "-", border=1)
            pdf.cell(35, row_height, item.due_date or "-", border=1, align="C")
            
            pdf.set_xy(x_start, y_after_task)
        
        pdf.ln(5)
    
    # Open Questions
    if minutes.open_questions:
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 8, "Neelttei asuudluud", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 11)
        for q in minutes.open_questions:
            pdf.multi_cell(0, 6, f"  - {q.text}")
        pdf.ln(3)
    
    # Transcript (optional)
    if transcript:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 8, "Bichver", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        
        for seg in transcript:
            start_sec = seg.get("start_ms", 0) // 1000
            mins, secs = divmod(start_sec, 60)
            timestamp = f"[{mins:02d}:{secs:02d}]"
            
            speaker = seg.get("speaker") or ""
            text = seg.get("text", "")
            
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(80, 80, 80)
            pdf.cell(0, 5, f"{timestamp} {speaker}", new_x="LMARGIN", new_y="NEXT")
            
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(0, 0, 0)
            pdf.multi_cell(0, 5, f"  {text}")
            pdf.ln(2)
    
    # Output
    return pdf.output()
