"""Хөтлөгчийн гар тэмдэглэл (хурал явах үед хүн бичнэ).

Редактор (B / I / U / жагсаалт) HTML илгээнэ. Серверт зөвхөн аюулгүй цөөн tag-ийг attribute-гүйгээр үлдээнэ
(script, style, onclick, href гэх мэт бүгд хасагдана) — хурлын хуудсанд innerHTML-ээр харуулахад XSS гарахгүй.
AI тэмдэглэл гаргах, PDF/DOCX-д энгийн текст болгож ашиглана.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import List, Optional

MAX_NOTES_CHARS = 50_000

ALLOWED = {"p", "div", "br", "b", "strong", "i", "em", "u", "ul", "ol", "li"}
VOID = {"br"}
# Агуулгыг нь ч хамт хаях tag-ууд
DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "noscript", "svg", "math", "head", "title"}
# Нормчилно: strong → b, em → i (div-ийг хэвээр: <p> дотор <ul> орох боломжгүй)
CANON = {"strong": "b", "em": "i"}


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.stack: List[str] = []
        self.drop_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            self.drop_depth += 1
            return
        if self.drop_depth or tag not in ALLOWED:
            return
        tag = CANON.get(tag, tag)
        if tag in VOID:
            self.out.append("<br>")
            return
        self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in VOID and not self.drop_depth:
            self.out.append("<br>")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_CONTENT:
            self.drop_depth = max(0, self.drop_depth - 1)
            return
        if self.drop_depth or tag not in ALLOWED or tag in VOID:
            return
        tag = CANON.get(tag, tag)
        if tag in self.stack:  # зөв дарааллаар хаана (дутуу хаасан tag-уудыг ч)
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.drop_depth:
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_notes_html(raw: Optional[str]) -> Optional[str]:
    """Аюулгүй HTML; хоосон (зөвхөн хоосон мөр/зай) бол None."""
    if not raw:
        return None
    p = _Sanitizer()
    p.feed(raw[: MAX_NOTES_CHARS * 2])
    p.close()
    out = p.result().strip()
    return out if notes_to_text(out) else None


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: List[str] = []
        self.prefix = ""  # жагсаалтын "- " / "1. " (догол мөртэй)
        self.cur = ""
        self.lists: List[list] = []  # [kind, counter]

    def _flush(self):
        text = re.sub(r"\s+", " ", self.cur).strip()
        if text:
            self.lines.append(self.prefix + text)
        self.cur, self.prefix = "", ""

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "div", "br"):
            self._flush()
        elif tag in ("ul", "ol"):
            self._flush()
            self.lists.append([tag, 0])
        elif tag == "li":
            self._flush()
            depth = "  " * max(0, len(self.lists) - 1)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                self.prefix = f"{depth}{self.lists[-1][1]}. "
            else:
                self.prefix = f"{depth}- "

    def handle_endtag(self, tag):
        if tag in ("p", "div", "li"):
            self._flush()
        elif tag in ("ul", "ol"):
            self._flush()
            if self.lists:
                self.lists.pop()

    def handle_data(self, data):
        self.cur += data

    def text(self) -> str:
        self._flush()
        return "\n".join(self.lines)


def notes_to_text(notes_html: Optional[str]) -> str:
    """AI prompt, PDF/DOCX-д: жагсаалтыг "- " / "1. "-ээр, догол бүрийг шинэ мөрөөр."""
    if not notes_html:
        return ""
    p = _Text()
    p.feed(notes_html)
    p.close()
    return p.text()[:MAX_NOTES_CHARS]
