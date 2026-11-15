"""Хурлын загварын service."""
from __future__ import annotations

import logging
import threading
from typing import List, Optional

from sqlalchemy import select, update

from app.db import Meeting, MeetingTemplate, session_scope

logger = logging.getLogger(__name__)


# Урьдчилан бэлтгэсэн загварууд
DEFAULT_TEMPLATES = [
    {
        "name": "Албан хурал",
        "description": "Албан ёсны хурлын бичиг шийдвэр, даалгавар, оролцогчдын хамт",
        "icon": "📋",
        "sections": ["summary", "topics", "decisions", "action_items", "open_questions"],
        "custom_prompt": "Албан ёсны хурлын хэлбэрээр бичнэ үү. Шийдвэр, даалгаврыг тодорхой бичнэ.",
    },
    {
        "name": "Daily Standup",
        "description": "Өдөр тутмын богино уулзалт - юу хийсэн, юу хийх, саад",
        "icon": "⚡",
        "sections": ["summary", "action_items"],
        "custom_prompt": "Богино, тодорхой бичнэ. Хүн бүрийн хийсэн ажлыг тусгаж, дараагийн алхмуудыг жагсаана.",
    },
    {
        "name": "Brainstorming",
        "description": "Сэтгэлийн шуурга уулзалт - санаануудыг бүртгэх",
        "icon": "💡",
        "sections": ["summary", "topics"],
        "custom_prompt": "Санаануудыг бүлэглэж, гол сэдвүүдийг тодорхойлно. Шийдвэрээс илүү санаануудад анхаарна.",
    },
    {
        "name": "Ярилцлага",
        "description": "Хэрэглэгч эсвэл ажил горилогчтой хийсэн ярилцлага",
        "icon": "🎤",
        "sections": ["summary", "topics", "open_questions"],
        "custom_prompt": "Ярилцлагын гол асуулт, хариултыг бүртгэнэ. Чухал ойлголтуудыг тодорхойлно.",
    },
    {
        "name": "Сургалт/Лекц",
        "description": "Сургалт, лекцийн тэмдэглэл",
        "icon": "📚",
        "sections": ["summary", "topics"],
        "custom_prompt": "Сургалтын гол сэдвүүд, чухал ойлголтуудыг тодорхой бичнэ.",
    },
    {
        "name": "Төслийн төлөв",
        "description": "Төслийн явц, саад, дараагийн алхмууд",
        "icon": "📊",
        "sections": ["summary", "topics", "action_items", "open_questions"],
        "custom_prompt": "Төслийн одоогийн байдал, саад бэрхшээл, шийдвэрлэх асуудлуудыг тодорхой бичнэ.",
    },
]


# list_templates синхрон endpoint тул нэг дор хоёр хүсэлт зэрэг орвол хоёулаа "байхгүй" гэж үзээд давхар үүсгэдэг байсан
_seed_lock = threading.Lock()


def ensure_default_templates() -> None:
    """Default template-ууд байхгүй бол үүсгэж, давхардсаныг цэвэрлэнэ."""
    with _seed_lock, session_scope() as s:
        rows = s.scalars(
            select(MeetingTemplate)
            .where(MeetingTemplate.is_default == 1)
            .order_by(MeetingTemplate.created_at, MeetingTemplate.id)
        ).all()

        kept: dict[str, MeetingTemplate] = {}
        for t in rows:
            first = kept.setdefault(t.name, t)
            if first is not t:
                # Давхардсан загвар: хурлуудыг эхнийх рүү шилжүүлээд устгана
                s.execute(update(Meeting).where(Meeting.template_id == t.id).values(template_id=first.id))
                s.delete(t)
                logger.info("Removed duplicate default template: %s", t.name)

        for tmpl in DEFAULT_TEMPLATES:
            if tmpl["name"] not in kept:
                s.add(
                    MeetingTemplate(
                        name=tmpl["name"],
                        description=tmpl["description"],
                        icon=tmpl["icon"],
                        sections=tmpl["sections"],
                        custom_prompt=tmpl["custom_prompt"],
                        is_default=1,
                        owner_id=None,
                    )
                )
                logger.info("Created default template: %s", tmpl["name"])


def list_templates(owner_id: str) -> List[dict]:
    """Хэрэглэгчийн бүх template (default + өөрийн)."""
    ensure_default_templates()
    with session_scope() as s:
        rows = s.scalars(
            select(MeetingTemplate)
            .where((MeetingTemplate.owner_id == owner_id) | (MeetingTemplate.is_default == 1))
            .order_by(MeetingTemplate.is_default.desc(), MeetingTemplate.name)
        ).all()
        return [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "icon": t.icon,
                "sections": t.sections,
                "custom_prompt": t.custom_prompt,
                "is_default": bool(t.is_default),
                "owner_id": t.owner_id,
            }
            for t in rows
        ]


def get_template(template_id: str) -> Optional[dict]:
    """Template-г ID-р авах."""
    with session_scope() as s:
        t = s.get(MeetingTemplate, template_id)
        if not t:
            return None
        return {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "icon": t.icon,
            "sections": t.sections,
            "custom_prompt": t.custom_prompt,
            "is_default": bool(t.is_default),
            "owner_id": t.owner_id,
        }


def create_template(owner_id: str, name: str, description: str, icon: str, sections: list, custom_prompt: str = None) -> dict:
    """Шинэ template үүсгэх."""
    with session_scope() as s:
        t = MeetingTemplate(
            owner_id=owner_id,
            name=name,
            description=description,
            icon=icon,
            sections=sections,
            custom_prompt=custom_prompt,
            is_default=0,
        )
        s.add(t)
        s.flush()
        return {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "icon": t.icon,
            "sections": t.sections,
            "custom_prompt": t.custom_prompt,
            "is_default": False,
            "owner_id": t.owner_id,
        }


def delete_template(template_id: str, owner_id: str) -> bool:
    """Template устгах (зөвхөн өөрийнх)."""
    with session_scope() as s:
        t = s.get(MeetingTemplate, template_id)
        if not t or t.owner_id != owner_id:
            return False
        if t.is_default:
            return False  # Default template устгаж болохгүй
        s.delete(t)
        return True
