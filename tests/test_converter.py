from pathlib import Path
import json
from unittest.mock import patch

import pymupdf
import pytest

from document_converter.converter import ConversionError, convert_document
from document_converter.volumes import VolumeError, volume_pages, write_volumes
from document_converter.pdf_tables import characters, editable_table, extract_tables, text_lines


def make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((30, 40), "Chapter One", fontsize=14)
    page.insert_textbox((30, 70, 270, 180), "Paragraph one.\nParagraph two keeps its line break.\n\nParagraph three.")
    doc.set_toc([[1, "Chapter One", 1], [2, "Unresolved bookmark", 99]])
    doc.save(path)
    doc.close()


def test_pdf_bundle_ignores_bookmarks_and_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "handbook.pdf"
    make_pdf(source)
    result = convert_document(source, tmp_path / "out.md")
    markdown = Path(result["markdown"]).read_text(encoding="utf-8")
    audit = Path(result["audit"]).read_text(encoding="utf-8")
    assert "# handbook" in markdown
    assert "Chapter One" in markdown
    assert "Paragraph two keeps its line break" in markdown
    assert "Unresolved bookmark" not in markdown
    assert "PDF 书签不参与转换" in markdown
    assert "pdf-page-1" in markdown
    assert (Path(result["assets"]) / ("source_" + source.name)).exists()
    assert '"pdf_pages": 1' in audit
    assert '"bookmarks_extracted": false' in audit


def test_output_is_refused_without_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("hello\n\nworld", encoding="utf-8")
    convert_document(source, tmp_path / "out.md")
    with pytest.raises(ConversionError, match="overwrite"):
        convert_document(source, tmp_path / "out.md")
    convert_document(source, tmp_path / "out.md", overwrite=True)


def test_html_headings_lists_and_table(tmp_path: Path) -> None:
    source = tmp_path / "sample.html"
    source.write_text("<h1>标题</h1><p>正文</p><ul><li>一</li><li>二</li></ul><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>", encoding="utf-8")
    result = convert_document(source, tmp_path / "html.md")
    markdown = Path(result["markdown"]).read_text(encoding="utf-8")
    assert "# 标题" in markdown
    assert "- 一" in markdown and "- 二" in markdown
    assert "| A | B |" in markdown and "| 1 | 2 |" in markdown


def test_unsupported_type_is_explicit(tmp_path: Path) -> None:
    source = tmp_path / "sample.rtf"
    source.write_text("x", encoding="utf-8")
    with pytest.raises(ConversionError, match="不支持"):
        convert_document(source, tmp_path / "out.md")


def test_pdf_reconstructs_unruled_rows_and_keeps_merges(tmp_path: Path) -> None:
    page = pymupdf.open()
    p = page.new_page(width=240, height=180)
    # Three columns with segmented vertical rules but no horizontal rules.
    for x in (20, 80, 140, 220):
        p.draw_line((x, 20), (x, 35))
        for top in (35, 51, 67, 83):
            p.draw_line((x, top), (x, top + 16))
    p.draw_line((20, 20), (220, 20)); p.draw_line((20, 35), (220, 35)); p.draw_line((20, 99), (220, 99))
    p.insert_text((28, 30), "Code"); p.insert_text((148, 30), "Meaning")
    for y, left, right in [(48, "A", "one"), (64, "B", "two"), (80, "C", "three"), (96, "D", "four")]:
        p.insert_text((28, y), left); p.insert_text((148, y), right)
    source = tmp_path / "grid.pdf"; page.save(source); page.close()
    doc = pymupdf.open(source); extracted, issues = extract_tables(doc[0], characters(doc[0]), tmp_path, "grid_assets", 1)
    assert not issues and extracted
    table = extracted[0]
    assert table["rows"] == 5 and table["columns"] == 3
    assert table["cells"][0]["rowspan"] == 1
    assert len(table["inferred_rules"]) == 9
    assert "<table border=" in editable_table(table)
    doc.close()


def test_pdf_complex_table_geometry_retains_colspan_and_rowspan(tmp_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    for x in (20, 80, 200, 280):
        page.draw_line((x, 20), (x, 140))
    page.draw_line((140, 50), (140, 140))
    for y in (20, 50, 80, 110, 140):
        page.draw_line((20 if y in (20, 140) else 80, y), (280, y))
    # Native grid demonstrates a vertical header merge and a horizontal merge.
    page.insert_text((30, 40), "merged"); page.insert_text((95, 40), "wide")
    page.insert_text((95, 70), "value")
    doc.save(tmp_path / "merged.pdf"); doc.close()
    doc = pymupdf.open(tmp_path / "merged.pdf")
    tables, issues = extract_tables(doc[0], characters(doc[0]), tmp_path / "assets", "assets", 1)
    assert not issues
    assert tables
    assert any(c["rowspan"] > 1 or c["colspan"] > 1 for c in tables[0]["cells"])
    html = editable_table(tables[0])
    assert 'rowspan="4"' in html and 'colspan="2"' in html
    doc.close()


def test_wrapped_rows_do_not_get_split_at_stroke_endpoints(tmp_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    for y in (20, 60, 100, 140):
        page.draw_line((20, y), (280, y))
    for x in (20, 150, 280):
        for y in range(20, 140, 20):
            page.draw_line((x, y), (x, y + 20))
    for y in (35, 55, 75, 95, 115, 135):
        page.insert_text((30, y), f"wrapped {y}")
        page.insert_text((160, y), f"other {y}")
    tables, issues = extract_tables(page, characters(page), tmp_path, "assets", 1)
    assert not issues and len(tables) == 1
    assert tables[0]["rows"] == 3
    assert not tables[0]["inferred_rules"]
    doc.close()


def test_pdf_failure_does_not_replace_previous_bundle(tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    make_pdf(source)
    result = convert_document(source, tmp_path / "out.md")
    before = {key: Path(result[key]).read_bytes() for key in ("markdown", "audit")}
    with patch("document_converter.pdf_document.extract_tables", side_effect=RuntimeError("interrupted")):
        with pytest.raises(RuntimeError, match="interrupted"):
            convert_document(source, tmp_path / "out.md", overwrite=True)
    for key, original in before.items():
        assert Path(result[key]).read_bytes() == original


def test_detection_failure_retains_source_text_and_audit(tmp_path: Path) -> None:
    source = tmp_path / "sample.pdf"
    make_pdf(source)
    with patch.object(pymupdf.Page, "find_tables", side_effect=ValueError("bad geometry")):
        result = convert_document(source, tmp_path / "out.md")
    assert "Paragraph one." in Path(result["markdown"]).read_text(encoding="utf-8")
    audit = json.loads(Path(result["audit"]).read_text(encoding="utf-8"))
    assert "table detection failed" in audit["issues"][0]
    assert all(p["missing_characters"] == p["duplicated_characters"] == 0 for p in audit["character_conservation"])


def test_table_model_failure_keeps_original_crop_and_searchable_text(tmp_path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=240, height=140)
    for x in (20, 100, 220):
        page.draw_line((x, 20), (x, 100))
    for y in (20, 60, 100):
        page.draw_line((20, y), (220, y))
    page.insert_text((30, 45), "Code"); page.insert_text((110, 45), "Meaning")
    page.insert_text((30, 85), "A1"); page.insert_text((110, 85), "Sample")
    with patch("document_converter.pdf_tables._model", side_effect=ValueError("ambiguous grid")):
        tables, issues = extract_tables(page, characters(page), tmp_path, "assets", 1)
    assert tables[0]["fallback"]
    assert tables[0]["cells"][0]["text"].count("\n") == 1
    assert "Code Meaning" in tables[0]["cells"][0]["text"]
    assert Path(tmp_path / "tables" / "p00001-t01.png").is_file()
    assert any("retained as source text" in issue for issue in issues)
    doc.close()


def test_superscript_subscript_and_scientific_values_keep_semantics() -> None:
    def glyph(char: str, x: float, y: float, size: float, ident: int) -> dict:
        return {"id": ident, "text": char, "bbox": [x, y - size, x + size * .55, y],
                "origin": [x, y], "size": size, "direction": (1.0, 0.0),
                "block": ident, "line": ident}

    chars = [glyph(c, x, 20, 10, i) for i, (c, x) in enumerate(
        [("6", 0), (".", 5), ("1", 9), ("0", 14), ("2", 19), ("×", 27), ("1", 36), ("0", 41)])]
    chars.append(glyph("4", 46, 15, 6, 8))
    assert text_lines(chars) == ["6.102×10⁴"]

    unit = [glyph(char, index * 5, 20 if char != "3" else 14,
                  10 if char != "3" else 6, index)
            for index, char in enumerate("m3")]
    assert text_lines(unit) == ["m³"]

    subscript = [glyph("x", 0, 20, 10, 0), glyph("1", 5, 25, 6, 1)]
    assert text_lines(subscript) == ["x₁"]

    paragraph = [glyph(c, x, 20, 10, index) for index, (c, x) in enumerate(
        [("6", 0), (".", 5), ("1", 9), ("0", 14), ("2", 19), ("×", 27), ("1", 36), ("0", 41)])]
    paragraph.append(glyph("4", 46, 15, 6, 8))
    paragraph.extend([glyph("x", 0, 40, 10, 9), glyph("1", 5, 45, 6, 10)])
    for char in paragraph:
        char["block"] = 1
        char["line"] = 0 if char["origin"][1] < 30 else 1
    from document_converter.pdf_tables import body_blocks
    assert [block["text"] for block in body_blocks(paragraph, set())] == ["6.102×10⁴", "x₁"]

    from document_converter.pdf_tables import _html_cell
    rendered = _html_cell("6.102×10⁴ m³; 9.842×10⁻⁴ kg·m⁻³")
    assert '<data value="6.102e4">6.102×10⁴</data>' in rendered
    assert '<data value="9.842e-4">9.842×10⁻⁴</data>' in rendered
    assert "m³" in rendered and "kg·m⁻³" in rendered
    assert text_lines([glyph(char, index * 5, 20, 10, index) for index, char in enumerate("1000")]) == ["1000"]


def test_body_rebuilds_paragraphs_without_reordering_mixed_glyph_lines() -> None:
    from document_converter.pdf_tables import body_blocks

    def line(text: str, x: float, y: float, block: int, number: int,
             sizes: dict[int, float] | None = None) -> list[dict]:
        chars = []
        for offset, char in enumerate(text):
            size = (sizes or {}).get(offset, 10)
            left = x + offset * 6
            chars.append({"id": block * 10000 + number * 100 + offset, "text": char,
                          "bbox": [left, y - size, left + size * .55, y],
                          "origin": [left, y], "size": size, "direction": (1.0, 0.0),
                          "block": block, "line": number})
        return chars

    chars = []
    chars.extend(line("3.尺寸", 20, 20, 1, 0, {0: 6, 1: 6}))
    chars.extend(line("（1）公称尺寸 如图1-1-1所示，数值如32、", 20, 40, 1, 1))
    chars.extend(line("15、8.75、0.5……。", 0, 50, 2, 0))
    chars.extend(line("所示。（2）极限尺寸 允许值。", 0, 60, 2, 1))
    chars.extend(line("（3）上极限尺寸。", 20, 70, 3, 0))
    chars.extend(line("图1-1-1 公称尺寸图", 100, 55, 4, 0))

    result = body_blocks(chars, set())
    texts = [block["text"] for block in result]
    assert texts == [
        "3.尺寸",
        "（1） 公称尺寸 如图1-1-1所示，数值如32、15、8.75、0.5……。所示。",
        "图1-1-1 公称尺寸图",
        "（2） 极限尺寸 允许值。",
        "（3） 上极限尺寸。",
    ]
    assert len({ident for block in result for ident in block["character_ids"]}) == len(chars)


def test_numbered_body_headings_are_structural_without_promoting_running_headers() -> None:
    from document_converter.pdf_document import _heading

    assert _heading("3. 尺寸") == "#### 3. 尺寸"
    assert _heading("1.2.1.2 基本规定") == "#### 1.2.1.2 基本规定"
    assert _heading("第1章 常用技术资料 7") == "第1章 常用技术资料 7"


def test_body_preserves_physical_wraps_scripts_and_table_boundaries() -> None:
    from document_converter.pdf_tables import body_blocks

    def run(text, x, y, block, line, size=10):
        return [{"id": block * 1000 + line * 100 + i, "text": c,
                 "bbox": [x + i * 5, y - size, x + i * 5 + 5, y],
                 "origin": [x + i * 5, y], "size": size,
                 "direction": (1, 0), "block": block, "line": line}
                for i, c in enumerate(text)]

    assert body_blocks(run("  ", 0, 20, 1, 0), set()) == []

    chars = run("（1）尺寸如图1-1-1", 20, 20, 1, 0)
    # One PDF raw line contains the end of one item and start of another.
    combined = run("所示。", 0, 30, 2, 0) + run("（2）另一尺寸如图1-1-1", 20, 40, 2, 0)
    for i, char in enumerate(combined):
        char["id"] = 2000 + i
    chars += combined + run("所示。", 0, 50, 3, 0)
    result = body_blocks(chars, set())
    assert [b["text"] for b in result] == [
        "（1） 尺寸如图1-1-1所示。", "（2） 另一尺寸如图1-1-1所示。"]
    ids = [i for b in result for i in b["character_ids"]]
    assert len(ids) == len(set(ids)) == len(chars)

    # A later PDF object can supply a preceding exponent: geometry establishes
    # its location; source object order must not append it at the end.
    chars = run("10 m", 0, 20, 1, 0) + run("4", 9, 16, 2, 0, 6)
    assert [b["text"] for b in body_blocks(chars, set())] == ["10⁴ m"]

    before = run("表前说明", 0, 20, 1, 0)
    after = run("表后说明", 0, 36, 2, 0)
    assert [b["text"] for b in body_blocks(before + after, set(), [[0, 25, 150, 32]])] == [
        "表前说明", "表后说明"]


@pytest.mark.private_source
def test_original_handbook_paragraph_regression() -> None:
    """Run against the original source when present; portable tests stay above."""
    from document_converter.pdf_tables import body_blocks
    source_dir = Path(__file__).resolve().parents[1] / "origin"
    sources = sorted(source_dir.glob("实用机械加工工艺手册 第5版*.pdf"))
    if not sources:
        pytest.skip("Original handbook is not installed")
    with pymupdf.open(sources[0]) as doc:
        chars = characters(doc[27])
    result = body_blocks(chars, set())
    texts = [b["text"] for b in result]
    assert ("（1） 公称尺寸 由图样规范定义的理想形状要素的尺寸，如图1-1-1所示"
            "（公称尺寸可以是一个整数或一个小数值，如32、15、8.75、0.5……）。"
            "通过它应用上、下极限偏差可以计算出极限尺寸。") in texts
    assert "（3） 上极限尺寸 尺寸要素允许的最大尺寸，如图1-1-1所示。" in texts
    assert "（4） 下极限尺寸 尺寸要素允许的最小尺寸，如图1-1-1所示。" in texts
    assert "图1-1-1 公称尺寸、上极限尺寸和下极限尺寸" in texts
    assert not {"3.", "尺寸", "1-1-1 32", "15 8.75 0.5"}.intersection(texts)
    ids = [i for b in result for i in b["character_ids"]]
    assert len(ids) == len(set(ids)) == len(chars)
    with pymupdf.open(sources[0]) as doc:
        notes = [b["text"] for b in body_blocks(characters(doc[219]), set())]
        alloys = [b["text"] for b in body_blocks(characters(doc[1116]), set())]
        steps = [b["text"] for b in body_blocks(characters(doc[1819]), set())]
    assert ("3. 如无特殊说明，推荐公差带适用于涂镀前螺纹。涂镀后，螺纹实际牙型轮廓上的任何点"
            "不应超越按公差位置H或h所确定的最大实体牙型。") in notes
    assert any("Al₂O₃" in text for text in alloys)
    assert any(text.startswith("2）切削工具") for text in alloys)
    assert any(text.startswith("3）切削工具") for text in alloys)
    assert "b. 再用衬垫①弯成B角（见图3-5-82b）。" in steps
    assert "c. 最后用衬垫②弯成C角（见图3-5-82c）。" in steps


def test_volume_page_detection_uses_printed_titles_and_includes_front_matter() -> None:
    def page(number: int, marker: str = "", title: str = "") -> str:
        return f"<a id=\"pdf-page-{number}\"></a>\n<!-- PDF_PAGE:{number} -->\n{marker}\n{title}\n---\n"

    titles = {4: ("第1 卷", "金属切削常用技术标准及应用"),
              20: ("第2 卷", "机械加工工艺技术及管理"),
              40: ("第3 卷", "切削加工技术及技术测量")}
    md = "# Handbook\n" + "".join(page(n, *titles.get(n, ("正文", ""))) for n in range(1, 41))
    result = volume_pages(md, 40)
    assert [(v["volume"], v["start_page"]) for v in result] == [(1, 1), (2, 20), (3, 40)]


def test_volume_page_detection_refuses_incomplete_or_misordered_headings() -> None:
    md = "".join(f'<a id="pdf-page-{n}"></a>\n<!-- PDF_PAGE:{n} -->\n'
                 + {1: "第1 卷\n任意书名甲\n", 8: "第3 卷\n任意书名乙\n"}.get(n, "正文\n")
                 for n in range(1, 11))
    with pytest.raises(VolumeError, match="印刷卷号不连续或重复"):
        volume_pages(md, 10)


def test_pdf_conversion_writes_complete_and_volume_markdowns_atomically(tmp_path: Path) -> None:
    source = tmp_path / "volumes.pdf"
    doc = pymupdf.open()
    titles = [
        ("第1 卷", "金属切削常用技术标准及应用"),
        ("第2 卷", "机械加工工艺技术及管理"),
        ("第3 卷", "切削加工技术及技术测量"),
    ]
    for index in range(9):
        page = doc.new_page(width=300, height=400)
        page.insert_text((30, 50), f"PAGE_BODY_{index + 1}")
        if index in (1, 4, 7):
            marker, title = titles[(index - 1) // 3]
            page.insert_text((30, 100), marker, fontname="china-s")
            page.insert_text((30, 130), title, fontname="china-s")
    doc.set_toc([[1, "Bookmark should be ignored", 2]])
    doc.save(source)
    doc.close()

    result = convert_document(source, tmp_path / "complete.md")
    full = Path(result["markdown"]).read_text(encoding="utf-8")
    assert "PAGE_BODY_1" in full and "PAGE_BODY_9" in full
    assert "Bookmark should be ignored" not in full
    volume_dir = Path(result["volumes"])
    pieces = sorted(volume_dir.glob("*_第*卷.md"))
    assert len(pieces) == 3
    volumes = [path.read_text(encoding="utf-8") for path in pieces]
    assert all(f"PAGE_BODY_{n}" in volumes[0] for n in range(1, 5))
    assert all(f"PAGE_BODY_{n}" in volumes[1] for n in range(5, 8))
    assert all(f"PAGE_BODY_{n}" in volumes[2] for n in range(8, 10))
    assert all(f"PAGE_BODY_{n}" not in volumes[1] for n in range(1, 5))
    audit = json.loads(Path(result["audit"]).read_text(encoding="utf-8"))
    assert [(v["start_page"], v["end_page"]) for v in audit["volumes"]] == [(1, 4), (5, 7), (8, 9)]
    index = (volume_dir / "README.md").read_text(encoding="utf-8")
    from urllib.parse import unquote
    assert "完整全文" in index and all(path.name in unquote(index) for path in pieces)
