"""Page-ordered Markdown composition and character-conservation audit."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
import re
import shutil
import sys
from typing import Callable

import pymupdf

from .pdf_tables import REVISION, body_blocks, characters, content_rotation, editable_table, extract_tables
from .body_script_layout import body_html


def _heading(text: str) -> str:
    """Promote only explicit, short headings printed on the page."""
    if len(text) > 100 or "\n" in text or re.search(r"[.…·]{3}|[。；;]", text):
        return text
    if re.match(r"^第\s*[一二三四五六七八九十百千零〇\d]+\s*章\s+.+\s+\d{1,4}$", text):
        return text
    match = re.match(r"^第\s*[一二三四五六七八九十百千零〇\d]+\s*([卷篇章节])\s*\S", text)
    if match:
        level = {"卷": 2, "篇": 2, "章": 3, "节": 4}[match[1]]
        return "#" * level + " " + text
    if re.match(r"^\d+(?:\.\d+)+\s+\S", text) or re.match(
            r"^\d+\.\s*[\u4e00-\u9fff][^。；;]{0,40}$", text):
        return "#### " + text
    return text


def convert_pdf(source: Path, stage: Path, asset_name: str, *, copy_source: bool,
                render_pages: bool, render_dpi: int, ocr_mode: str, ocr_dpi: int,
                slug: Callable, sha256: Callable, ocr: Callable) -> tuple[str, dict]:
    try:
        doc = pymupdf.open(source)
    except Exception as exc:
        raise RuntimeError(f"无法打开 PDF：{exc}") from exc
    with doc:
        if doc.is_encrypted and doc.needs_pass:
            raise RuntimeError("PDF 已加密；请先提供可访问的副本。")
        if not len(doc):
            raise RuntimeError("PDF 不包含页面。")
        assets = stage / asset_name
        assets.mkdir(parents=True, exist_ok=True)
        source_link = None
        if copy_source:
            copied = assets / ("source_" + slug(source.name))
            shutil.copy2(source, copied)
            source_link = f"{asset_name}/{copied.name}"
        output = [f"# {source.stem}", "",
                  "> 按原页顺序保留正文与印刷目录。表格显示完整原版式，缺失行线经几何验证后补绘；展开表格可检索、编辑单元格及合并结构。数字分数按原矢量分数线恢复为显式 (分子/分母)，派生符号单独记录。",
                  "> PDF 书签不参与转换。原文件与逐页结构记录随包保存。", ""]
        issues, low_text, ocr_pages, page_images, char_counts = [], [], [], [], []
        summaries, conservation, rotations, contents_pages = [], [], [], []
        structure_path = assets / "structure.jsonl"
        with structure_path.open("w", encoding="utf-8", newline="\n") as structure:
            for page_number, original in enumerate(doc, 1):
                chars = characters(original)
                rotation = content_rotation(chars)
                shadow = None
                page = original
                try:
                    if rotation or original.rotation:
                        # Remove metadata rotation first, then orient dominant
                        # sideways text. All following geometry shares one space.
                        shadow = pymupdf.open()
                        width, height = original.cropbox.width, original.cropbox.height
                        page = shadow.new_page(width=height if rotation % 180 else width,
                                               height=width if rotation % 180 else height)
                        saved_rotation = original.rotation
                        original.set_rotation(0)
                        try:
                            page.show_pdf_page(page.rect, doc, page_number - 1, rotate=rotation)
                        finally:
                            original.set_rotation(saved_rotation)
                        chars = characters(page)
                        rotations.append({"pdf_page": page_number, "content_rotation_ccw": rotation,
                                          "source_metadata_rotation": saved_rotation})
                    nonspace_ids = {c["id"] for c in chars if c["text"].strip()}
                    char_count = len(nonspace_ids)
                    char_counts.append(char_count)
                    if char_count < 40:
                        low_text.append(page_number)
                    output.append(f'<a id="pdf-page-{page_number}"></a>\n<!-- PDF_PAGE:{page_number} -->')
                    output.append(f"[原始 PDF 第 {page_number} 页]({source_link}#page={page_number})" if source_link
                                  else f"来源：{source.name}，PDF 第 {page_number} 页")
                    # Sparse/scan pages are always visualized, even in the
                    # default mode; their content cannot depend on an OCR guess.
                    if render_pages or char_count < 40:
                        folder = assets / "pages"
                        folder.mkdir(exist_ok=True)
                        image_file = folder / f"page-{page_number:05}.jpg"
                        pix = page.get_pixmap(dpi=render_dpi, alpha=False)
                        image_file.write_bytes(pix.tobytes("jpeg", jpg_quality=90))
                        relpath = f"{asset_name}/pages/{image_file.name}"
                        output.append(f"\n![PDF 第 {page_number} 页原页图]({relpath})\n")
                        page_images.append(relpath)
                    tables, table_issues = extract_tables(page, chars, assets, asset_name, page_number)
                    for table in tables:
                        table['numeric_fraction_count'] = sum(len(c.get('fractions', [])) for c in table['cells'])
                        table['mixed_number_count'] = sum('whole' in f for c in table['cells'] for f in c.get('fractions', []))
                    issues.extend(table_issues)
                    excluded = {i for table in tables for cell in table["cells"] for i in cell["character_ids"]}
                    blocks = body_blocks(chars, excluded, [table["bbox"] for table in tables])
                    contents = [b['toc'] for b in blocks if b.get('toc')]
                    if contents:
                        contents_pages.append({'pdf_page': page_number,
                                               'entries': sum(len(t['entries']) for t in contents),
                                               'columns': max(t['columns'] for t in contents)})
                        for toc in contents:
                            issues.extend(f'page {page_number}: contents review ({r["reason"]})'
                                          for r in toc['reviews'])
                    body_ids = {i for block in blocks for i in block["character_ids"]}
                    assignments = Counter(i for block in blocks for i in block["character_ids"])
                    assignments.update(i for table in tables for cell in table["cells"]
                                       for i in cell["character_ids"])
                    missing = nonspace_ids - body_ids - excluded
                    if missing:
                        # Preserve unusual control/whitespace-only blocks as
                        # explicit source text instead of silently discarding.
                        output.append("\n" + "".join(c["text"] for c in chars if c["id"] in missing) + "\n")
                        body_ids.update(missing)
                        issues.append(f"page {page_number}: {len(missing)} characters retained in fallback text")
                    record = {"pdf_page": page_number, "native_characters": char_count,
                              "body_characters": len(body_ids & nonspace_ids),
                              "table_characters": len(excluded & nonspace_ids),
                              "missing_characters": len(nonspace_ids - body_ids - excluded),
                              "duplicated_characters": sum(max(0, assignments[i] - 1) for i in nonspace_ids)}
                    if record["missing_characters"] or record["duplicated_characters"]:
                        raise RuntimeError(f"page {page_number}: character conservation failed")
                    conservation.append(record)
                    items = [(block["y"], block["x"], "body", block) for block in blocks]
                    items.extend((table["bbox"][1], table["bbox"][0], "table", table) for table in tables)
                    ordered = (items if blocks and not tables and all(block.get("printed_contents") for block in blocks)
                               else sorted(items, key=lambda item: (item[0], item[1])))
                    for _, _, kind, item in ordered:
                        if kind == "body":
                            text = item["text"].replace("\x00", "�")
                            output.append("\n" + body_html(item | {'text': text}, _heading) + "\n")
                        else:
                            summary = ("展开保留的原表文字（网格不确定，未推断行列）" if item.get("fallback") else
                                       f'展开表格文本（{item["rows"]} 行 × {item["columns"]} 列，保留合并单元格）')
                            output.extend([f'\n<!-- TABLE:{item["id"]} -->',
                                           f'![完整表格 · PDF 第 {page_number} 页]({item["image"]})', "",
                                           "<details>", f"<summary>{summary}</summary>",
                                           "", editable_table(item), "", "</details>", ""])
                            if item.get("original_image"):
                                output.append(f'[对照补线前原表]({item["original_image"]})\n')
                    if ocr_mode == "all" or ocr_mode == "auto" and char_count < 80:
                        tokens, error = ocr(doc, page_number, ocr_dpi)
                        if error:
                            issues.append(error)
                        if tokens:
                            ocr_pages.append(page_number)
                            # Explicitly show requested OCR results, including
                            # mixed native/scan pages, without replacing text.
                            output.extend(["<details>", "<summary>OCR 候选文本（需对照原页）</summary>", "",
                                           "\n\n".join(t["text"] for t in tokens), "", "</details>", ""])
                    output.append("\n---\n")
                    for table in tables:
                        summaries.append({key: value for key, value in table.items()
                                          if key not in {"cells", "x_boundaries", "y_boundaries", "inferred_rules"}} |
                                         {"inferred_rule_count": len(table["inferred_rules"])})
                    # Character identities are page-local and unnecessary in
                    # the persisted editable cell model after conservation.
                    serialized = [{key: value for key, value in table.items() if key != "cells"} |
                                  {"cells": [{key: value for key, value in cell.items() if key != "character_ids"}
                                             for cell in table["cells"]]} for table in tables]
                    structure.write(json.dumps(record | {"tables": serialized,
                                    'printed_contents': contents,
                                    'body_scripts': [{k: b[k] for k in ('body_id', 'character_ids', 'scripts', 'linear_text', 'text')}
                                                     for b in blocks if b.get('scripts')],
                                    'body_script_reviews': [b['script_reviews'] for b in blocks if b.get('script_reviews')],
                                    "coordinate_space": {"width": page.rect.width, "height": page.rect.height,
                                                         "content_rotation_ccw": rotation}}, ensure_ascii=False) + "\n")
                finally:
                    if shadow is not None:
                        shadow.close()
                if page_number % 50 == 0 or page_number == len(doc):
                    print(f"doc2md: {page_number}/{len(doc)} pages; {len(summaries)} tables", file=sys.stderr, flush=True)
        repaired = sum(t["inferred_rule_count"] > 0 for t in summaries)
        output.extend(["## 转换校验", "", f"- PDF 页数：{len(doc)}；表格：{len(summaries)} 个",
                       f"- 补绘缺失行线的表格：{repaired} 个；原表对照图同时保留",
                       f"- 非空白原生字符：{sum(char_counts)} 个；逐页分配至正文或表格，无遗漏、无重复分配",
                       f"- 原页图：{len(page_images)} 张；低文本页自动保留原页图",
                       f"- [逐页单元格结构与字符校验记录]({asset_name}/structure.jsonl)", ""])
        audit = {"source": source.name, "sha256": sha256(source), "format": "pdf", "pdf_pages": len(doc),
                 "extracted_chars_by_page": char_counts, "toc_entries": 0, "bookmarks_extracted": False,
                 "printed_contents_pages": contents_pages,
                 "printed_contents_entries": sum(t['entries'] for t in contents_pages),
                 "tables": len(summaries), "table_details": summaries, "repaired_tables": repaired,
                 "merged_cells": sum(t["merged_cells"] for t in summaries),
                 "rendered_page_images": len(page_images), "low_text_pages_lt_40_chars": low_text,
                 "ocr_pages": ocr_pages, "issues": issues, "source_asset": source_link,
                 "copy_source": copy_source, "ocr_mode": ocr_mode, "normalized_pages": rotations,
                 "character_conservation": conservation, "structure_asset": f"{asset_name}/structure.jsonl",
                 "extraction_model": f"PyMuPDF original glyphs + rectangular cells and spans; {REVISION}",
                 "body_reconstruction": {"physical_lines": True, "native_mixed_font_order": True,
                                         "paragraph_reflow": True, "table_barriers": True,
                                         "printed_contents_detection": True,
                                         "printed_contents_spatial_pairing": True,
                                         "printed_contents_hierarchy": True},
                 "fraction_reconstruction": {"numeric_fractions": sum(t.get('numeric_fraction_count', 0) for t in summaries),
                                             "mixed_numbers": sum(t.get('mixed_number_count', 0) for t in summaries),
                                             "requires_vector_bar": True, "atomic_row_boundaries": True,
                                             "notation": "explicit_parenthesized_ratio_with_source_geometry",
                                             "derived_punctuation_separate_from_source_glyphs": True},
                 "limitations": ["Character conservation covers the PDF text layer, not OCR correctness or reading order.",
                                 "Inferred row rules are conservative; visually ambiguous cells retain original geometry.",
                                 "Table visuals preserve formula/graphic layout; editable text may not encode it."]}
        markdown = "\n".join(output).rstrip() + "\n"
        return markdown, audit
