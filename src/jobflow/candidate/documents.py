"""Writing a CV as .docx or .tex (RenderCV makes the PDF), a cover letter as .docx, and a
.docx copy of each review.

Everything is laid out in code from the fact-checked RenderCV YAML, never by the LLM, so
each file says exactly what passed the fact check. Like RenderCV, an entry is never split
across pages.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


# --- The CV as a layout every format shares ------------------------------------------------

def _date(value: object) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})(?:-\d{2})?", text)
    if match and 1 <= int(match[2]) <= 12:
        return f"{MONTHS[int(match[2]) - 1]} {match[1]}"
    return text


def _dates(entry: dict) -> str:
    if entry.get("date"):
        return _date(entry["date"])
    start, end = _date(entry.get("start_date")), _date(entry.get("end_date"))
    return f"{start} – {end}" if start and end else start or end


def _text(value: object) -> str:
    """Markdown links become their text; **bold** is kept for the writers."""
    return LINK.sub(r"\1", str(value or "")).strip()


def _entry(entry: object) -> dict:
    """One section item as a block: title (with **bold**), detail, right-hand dates and
    location, summary, and bullets. Nothing in the entry is dropped."""
    if not isinstance(entry, dict):
        return {"kind": "text", "text": _text(entry)}
    if "bullet" in entry:
        return {"kind": "bullet", "text": _text(entry["bullet"])}
    if "label" in entry:
        return {"kind": "text", "text": f"**{_text(entry['label'])}:** {_text(entry.get('details'))}"}
    if "number" in entry or "reversed_number" in entry:
        return {"kind": "text", "text": _text(entry.get("number") or entry.get("reversed_number"))}
    title, detail = "", ""
    if entry.get("company") or entry.get("position"):
        title = f"**{_text(entry.get('company'))}**" if entry.get("company") else ""
        if entry.get("position"):
            title += f", {_text(entry['position'])}" if title else f"**{_text(entry['position'])}**"
    elif entry.get("institution"):
        title = f"**{_text(entry['institution'])}**"
        degree, area = _text(entry.get("degree")), _text(entry.get("area"))
        detail = f"{degree} in {area}" if degree and area else degree or area
    elif entry.get("title"):
        title = f"**{_text(entry['title'])}**"
        authors = entry.get("authors") or []
        detail = ", ".join(_text(a) for a in authors) if isinstance(authors, list) else _text(authors)
        if entry.get("journal"):
            detail = f"{detail}. {_text(entry['journal'])}" if detail else _text(entry["journal"])
    elif entry.get("name"):
        title = f"**{_text(entry['name'])}**"
    highlights = entry.get("highlights") or []
    return {"kind": "entry", "title": title, "detail": detail, "dates": _dates(entry),
            "location": _text(entry.get("location")), "summary": _text(entry.get("summary")),
            "bullets": [_text(h) for h in highlights if str(h).strip()]}


def layout(cv: dict) -> tuple[str, list[str], list[tuple[str, list[dict]]]]:
    """The name, contact details, and (section title, blocks) pairs."""
    contact = [_text(cv.get(key)).removeprefix("tel:").removeprefix("mailto:")
               for key in ("location", "email", "phone", "website") if cv.get(key)]
    for network in cv.get("social_networks") or []:
        if isinstance(network, dict) and network.get("username"):
            contact.append(f"{network.get('network')}: {network['username']}")
    sections = []
    for key, items in (cv.get("sections") or {}).items():
        title = key.replace("_", " ").strip()
        title = title[:1].upper() + title[1:]
        items = items if isinstance(items, list) else [items]
        sections.append((title, [_entry(item) for item in items]))
    return _text(cv.get("name")), contact, sections


def _runs(text: str) -> list[tuple[str, bool]]:
    """Split **bold** markup into (text, bold) runs."""
    return [(part, i % 2 == 1) for i, part in enumerate(text.split("**")) if part]


# --- .docx ---------------------------------------------------------------------------------

def _docx_base(margin_cm: float, size: float):
    doc = Document()
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Cm(margin_cm)
        section.left_margin = section.right_margin = Cm(margin_cm)
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = "Calibri", Pt(size)
    normal.paragraph_format.space_after = Pt(0)
    return doc


def _add_runs(paragraph, text: str, italic: bool = False) -> None:
    for part, bold in _runs(text):
        run = paragraph.add_run(part)
        run.bold, run.italic = bold, italic


def write_docx(cv: dict, path: Path) -> None:
    name, contact, sections = layout(cv)
    doc = _docx_base(1.8, 10.5)
    width = doc.sections[0].page_width - doc.sections[0].left_margin - doc.sections[0].right_margin

    def paragraph(space_before: float = 0, keep: bool = True):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(space_before)
        p.paragraph_format.keep_with_next = keep   # keeps an entry on one page
        return p

    def row(left: str, right: str, italic: bool = False, space_before: float = 0, keep: bool = True):
        p = paragraph(space_before, keep)
        p.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
        _add_runs(p, left, italic)
        if right:
            p.add_run("\t" + right).italic = italic

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(name)
    run.bold, run.font.size = True, Pt(20)
    if contact:
        line = doc.add_paragraph(" | ".join(contact))
        line.alignment = WD_ALIGN_PARAGRAPH.CENTER
        line.runs[0].font.size = Pt(9.5)
    for section_title, blocks in sections:
        heading = paragraph(10)
        run = heading.add_run(section_title)
        run.bold, run.font.size = True, Pt(12)
        border = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        for attribute, value in (("val", "single"), ("sz", "6"), ("space", "1"), ("color", "808080")):
            bottom.set(qn(f"w:{attribute}"), value)
        border.append(bottom)
        heading.paragraph_format.element.get_or_add_pPr().append(border)
        heading.paragraph_format.space_after = Pt(3)
        for block in blocks:
            if block["kind"] == "bullet":
                _add_runs(doc.add_paragraph(style="List Bullet"), block["text"])
                continue
            if block["kind"] == "text":
                _add_runs(paragraph(2, keep=False), block["text"])
                continue
            lines = ([(block["detail"], block["location"])] if block["detail"] or block["location"] else [])
            last = not lines and not block["summary"] and not block["bullets"]
            row(block["title"], block["dates"], space_before=5, keep=not last)
            for detail, location in lines:
                row(detail, location, italic=True,
                    keep=bool(block["summary"] or block["bullets"]))
            if block["summary"]:
                _add_runs(paragraph(keep=bool(block["bullets"])), block["summary"])
            for i, bullet in enumerate(block["bullets"]):
                p = doc.add_paragraph(style="List Bullet")
                p.paragraph_format.keep_with_next = i < len(block["bullets"]) - 1
                _add_runs(p, bullet)
    doc.save(path)


def write_letter_docx(letter: str, cv: dict, job: dict, path: Path, today: date | None = None) -> None:
    """A standard business letter: sender, date, recipient, subject line, then the
    letter's own text (which opens with its greeting and ends with its sign-off)."""
    doc = _docx_base(2.5, 11)
    sender = [_text(cv.get("name"))] + [
        _text(cv.get(key)).removeprefix("tel:").removeprefix("mailto:")
        for key in ("location", "email", "phone") if cv.get(key)]
    greeting = re.match(r"\s*Dear\s+([^,\n]+),", letter)
    addressee = greeting[1].strip() if greeting else "Hiring Manager"
    recipient = [addressee] + [part for part in (job.get("company"), job.get("location")) if part]
    today = today or date.today()

    def block(lines: list[str], bold_first: bool = False, space_after: float = 12) -> None:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(space_after)
        for i, line in enumerate(lines):
            run = p.add_run(line)
            run.bold = bold_first and i == 0
            if i < len(lines) - 1:
                run.add_break()

    block(sender, bold_first=True)
    block([f"{today.day} {today:%B %Y}"])
    block(recipient)
    block([f"Re: {job.get('title') or 'Application'}"], bold_first=True)
    paragraphs = [[line.strip() for line in p.splitlines() if line.strip()]
                  for p in re.split(r"\n\s*\n", letter.strip())]
    if len(paragraphs) == 1:
        # No blank lines: each line is a paragraph, but the sign-off stays with the name.
        lines = paragraphs[0]
        paragraphs = [[line] for line in lines]
        if len(lines) > 2 and lines[-2].endswith(","):
            paragraphs[-2:] = [lines[-2:]]
    for lines in paragraphs:
        block(lines, space_after=10)
    doc.save(path)


def write_review_docx(markdown: str, path: Path) -> None:
    """A .docx copy of a review written as simple Markdown: # and ## headings, - bullets,
    and plain paragraphs."""
    doc = _docx_base(2, 11)
    for block in re.split(r"\n\s*\n", markdown.strip()):
        for line in block.splitlines():
            if line.startswith("## "):
                doc.add_heading(line[3:], level=2)
            elif line.startswith("# "):
                doc.add_heading(line[2:], level=1)
            elif line.startswith("- "):
                _add_runs(doc.add_paragraph(style="List Bullet"), line[2:])
            elif line.strip():
                _add_runs(doc.add_paragraph(), line)
        doc.paragraphs[-1].paragraph_format.space_after = Pt(8)
    doc.save(path)


# --- .tex ----------------------------------------------------------------------------------

LATEX_SPECIAL = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
                 "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
                 "^": r"\textasciicircum{}"}


def _latex(text: str) -> str:
    """Escape everything, so CV text can never run LaTeX commands; **bold** becomes
    \\textbf."""
    out = []
    for part, bold in _runs(text):
        escaped = "".join(LATEX_SPECIAL.get(c, c) for c in part)
        out.append(f"\\textbf{{{escaped}}}" if bold else escaped)
    return "".join(out)


def write_latex(cv: dict, path: Path) -> None:
    name, contact, sections = layout(cv)
    body = [r"\documentclass[10pt,a4paper]{article}",
            # lualatex or xelatex handle any script; pdflatex only Western European text.
            r"\usepackage{iftex}",
            r"\ifPDFTeX\usepackage[T1]{fontenc}\usepackage[utf8]{inputenc}\usepackage{lmodern}",
            r"\else\usepackage{fontspec}\fi",
            r"\usepackage[margin=1.8cm]{geometry}", r"\usepackage{enumitem}",
            r"\setlist[itemize]{leftmargin=1.2em,itemsep=0pt,topsep=2pt}",
            r"\pagestyle{empty}", r"\setlength{\parindent}{0pt}", "",
            r"\begin{document}", r"\begin{center}",
            rf"{{\LARGE\textbf{{{_latex(name)}}}}}\\[4pt]",
            r"{\small " + r" \textbar{} ".join(_latex(c) for c in contact) + "}",
            r"\end{center}"]
    for section_title, blocks in sections:
        body += ["", rf"\section*{{{_latex(section_title)}}}", r"\vspace{-0.8em}\hrule\vspace{0.4em}"]
        for block in blocks:
            if block["kind"] == "bullet":
                body.append(rf"\textbullet{{}} {_latex(block['text'])}\par")
                continue
            if block["kind"] == "text":
                body.append(_latex(block["text"]) + r"\par\smallskip")
                continue
            # A minipage is never split across pages, like a RenderCV entry.
            body.append(r"\noindent\begin{minipage}{\linewidth}")
            body.append(f"{_latex(block['title'])}\\hfill {_latex(block['dates'])}\\\\")
            if block["detail"] or block["location"]:
                body.append(f"\\textit{{{_latex(block['detail'])}}}\\hfill "
                            f"\\textit{{{_latex(block['location'])}}}\\\\")
            if block["summary"]:
                body.append(_latex(block["summary"]) + r"\\")
            if block["bullets"]:
                body.append(r"\begin{itemize}")
                body += [rf"  \item {_latex(b)}" for b in block["bullets"]]
                body.append(r"\end{itemize}")
            body.append(r"\end{minipage}\par\medskip")
    body += [r"\end{document}", ""]
    path.write_text("\n".join(body))
