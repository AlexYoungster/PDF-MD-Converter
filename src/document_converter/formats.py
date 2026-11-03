from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _md_table(rows: list[list[Any]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [list(row) + [""] * (width - len(row)) for row in rows]
    def cell(value: Any) -> str:
        return ("" if value is None else str(value)).replace("&", "&amp;").replace("|", "&#124;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    return "\n".join(["| " + " | ".join(map(cell, rows[0])) + " |", "| " + " | ".join(["---"] * width) + " |", *["| " + " | ".join(map(cell, row)) + " |" for row in rows[1:]]])


def convert_html(source: Path) -> tuple[str, dict[str, Any]]:
    from bs4 import BeautifulSoup, NavigableString, Tag
    soup = BeautifulSoup(source.read_text(encoding="utf-8-sig", errors="replace"), "html.parser")
    lines: list[str] = []
    def visit(node: Any) -> None:
        if isinstance(node, NavigableString):
            if str(node).strip(): lines.append(str(node).strip())
            return
        if not isinstance(node, Tag) or node.name in {"script", "style", "noscript", "template"}: return
        name = node.name.lower()
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            lines.extend(["\n" + "#" * int(name[1]) + " " + node.get_text(" ", strip=True), ""]); return
        if name == "table":
            rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"], recursive=False)] for tr in node.find_all("tr")]
            rendered = _md_table(rows)
            if rendered: lines.extend(["", rendered, ""])
            return
        if name in {"ul", "ol"}:
            for i, li in enumerate(node.find_all("li", recursive=False), 1): lines.append(("- " if name == "ul" else f"{i}. ") + li.get_text(" ", strip=True))
            lines.append(""); return
        if name == "pre":
            fence = chr(96) * 3
            lines.extend(["", fence, node.get_text("", strip=False).rstrip(), fence, ""]); return
        if name == "blockquote": lines.extend(["", *["> " + x for x in node.get_text("\n", strip=True).splitlines()], ""]); return
        if name == "img":
            src, alt = node.get("src", ""), node.get("alt", "")
            lines.append(f"![{alt}]({src})" if src else f"[图片缺少来源: {alt}]"); return
        if name == "a":
            href, label = node.get("href", ""), node.get_text(" ", strip=True)
            lines.append(f"[{label or href}]({href})" if href else label); return
        if name == "br": lines.append("  \n"); return
        if name in {"p", "div", "section", "article", "main", "figure", "figcaption"}: lines.append("")
        for child in node.children: visit(child)
        if name in {"p", "div", "section", "article", "main", "figure", "figcaption"}: lines.append("")
    visit(soup.body or soup)
    return "\n".join(lines).strip() + "\n", {"format": source.suffix.lower().lstrip("."), "html_title": soup.title.get_text(strip=True) if soup.title else None,
        "html_elements": {tag: len(soup.find_all(tag)) for tag in ("h1", "h2", "h3", "p", "table", "ul", "ol", "img")}, "issues": [], "extraction_model": "HTML structural traversal; scripts and styles omitted"}


def convert_docx(source: Path) -> tuple[str, dict[str, Any]]:
    try:
        from docx import Document
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc: raise RuntimeError("DOCX support requires the office extra") from exc
    doc = Document(source); lines = ["# " + source.stem, ""]; paragraphs = tables = 0
    for child in doc.element.body.iterchildren():
        if isinstance(child, CT_P):
            para = Paragraph(child, doc); value = para.text
            if not value: continue
            paragraphs += 1; style = para.style.name if para.style else ""
            heading = re.fullmatch(r"Heading\s+(\d+)", style, re.I)
            if heading: lines.extend(["#" * min(int(heading.group(1)), 6) + " " + value, ""])
            elif style.lower() == "title": lines.extend(["# " + value, ""])
            elif style.lower() == "subtitle": lines.extend(["## " + value, ""])
            else: lines.extend([value.replace("\n", "  \n"), ""])
        elif isinstance(child, CT_Tbl):
            table = Table(child, doc); rendered = _md_table([[cell.text for cell in row.cells] for row in table.rows])
            if rendered: lines.extend([rendered, ""])
            tables += 1
    for index, section in enumerate(doc.sections, 1):
        for label, ps in (("页眉", section.header.paragraphs), ("页脚", section.footer.paragraphs)):
            values = [p.text for p in ps if p.text.strip()]
            if values: lines.extend([f"\n> [第 {index} 节{label}]", *["> " + v for v in values]])
    return "\n".join(lines).rstrip() + "\n", {"format": "docx", "paragraphs": paragraphs, "tables": tables, "sections": len(doc.sections), "issues": [], "extraction_model": "python-docx body order; source copy retains floating shapes, comments and tracked revisions"}


def convert_pptx(source: Path) -> tuple[str, dict[str, Any]]:
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
    except ImportError as exc: raise RuntimeError("PPTX support requires the office extra") from exc
    deck = Presentation(source); lines = ["# " + source.stem, ""]; tables = shapes = 0
    for n, slide in enumerate(deck.slides, 1):
        lines.extend([f"## 幻灯片 {n}", ""])
        for shape in sorted(slide.shapes, key=lambda s: (s.top, s.left)):
            if shape.has_text_frame and shape.text.strip():
                text = shape.text.strip(); is_title = getattr(shape, "is_placeholder", False) and shape.placeholder_format.type == 1
                lines.extend([("### " if is_title else "") + text, ""]); shapes += 1
            if shape.has_table:
                rendered = _md_table([[c.text for c in row.cells] for row in shape.table.rows])
                if rendered: lines.extend([rendered, ""])
                tables += 1
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE: lines.append(f"[图片保留于原始演示文稿；幻灯片 {n}]")
        if slide.has_notes_slide:
            note = slide.notes_slide.notes_text_frame.text.strip()
            if note: lines.extend(["\n> 演讲者备注", *["> " + x for x in note.splitlines()], ""])
    return "\n".join(lines).rstrip() + "\n", {"format": "pptx", "slides": len(deck.slides), "text_shapes": shapes, "tables": tables, "issues": [], "extraction_model": "python-pptx; visual shape order approximated by position; source copy retains images and layout"}


def convert_xlsx(source: Path) -> tuple[str, dict[str, Any]]:
    try: from openpyxl import load_workbook
    except ImportError as exc: raise RuntimeError("XLSX support requires the office extra") from exc
    book = load_workbook(source, data_only=False); lines = ["# " + source.stem, ""]; sheets = []
    for sheet in book.worksheets:
        lines.extend(["## 工作表：" + sheet.title, ""])
        for merged in sheet.merged_cells.ranges: lines.append(f"> 合并单元格：{merged}")
        rows = [["" if c.value is None else c.value for c in row] for row in sheet.iter_rows() if any(c.value is not None for c in row)]
        if rows: lines.extend([_md_table(rows), ""])
        if sheet.tables: lines.append("> Excel 表格区域：" + ", ".join(f"{t.name} ({t.ref})" for t in sheet.tables.values()))
        sheets.append({"sheet": sheet.title, "nonempty_rows": len(rows), "merged_ranges": len(sheet.merged_cells.ranges), "tables": len(sheet.tables)})
    book.close()
    return "\n".join(lines).rstrip() + "\n", {"format": "xlsx", "sheets": sheets, "issues": [], "extraction_model": "openpyxl formula-preserving cells; copied source retains formatting, charts and drawings"}
