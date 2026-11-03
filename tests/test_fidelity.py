"""Adversarial regressions for omissions, swaps and evidence provenance."""
from pathlib import Path
import json

import pymupdf

from document_converter.converter import convert_document
from document_converter.fidelity import Inspector, canonical, parse_markdown, visible


def fixture(tmp_path: Path, table=False):
    source = tmp_path / 'source.pdf'
    with pymupdf.open() as doc:
        page = doc.new_page(width=320, height=400)
        page.insert_text((30, 35), 'Machining safety instructions')
        if table:
            for x in (30, 150, 270):
                page.draw_line((x, 60), (x, 140))
            for y in (60, 100, 140):
                page.draw_line((30, y), (270, y))
            page.insert_text((40, 85), 'Speed 120')
            page.insert_text((160, 85), 'Feed 240')
            page.insert_text((40, 125), 'Depth 1.5')
            page.insert_text((160, 125), 'Limit 2.5')
        else:
            page.insert_text((30, 75), 'Maximum speed is 120 rpm.')
            page.insert_text((30, 115), 'Minimum feed is 240 mm.')
        doc.save(source)
    result = convert_document(source, tmp_path / 'converted.md')
    return source, Path(result['markdown'])


def inspect(source, md, tmp_path, images=False):
    checker = Inspector(source, md, tmp_path / 'report')
    summary = checker.run(ocr='none', verify_images=images, evidence_limit=0)
    return checker, summary


def test_clean_conversion_and_real_jsonl(tmp_path):
    source, md = fixture(tmp_path)
    checker, summary = inspect(source, md, tmp_path)
    assert summary['complete_text_layer_check']
    assert not summary['severity_counts'].get('error')
    assert not summary['pages_with_character_differences']
    assert summary['source_characters'] == summary['converted_characters']
    for line in (checker.out / 'pages.jsonl').read_text(encoding='utf-8').splitlines():
        assert json.loads(line)['pdf_page'] == 1
    assert '\n## ' in (checker.out / '检查报告.md').read_text(encoding='utf-8')


def test_numeric_replacement_has_local_evidence(tmp_path):
    source, md = fixture(tmp_path)
    md.write_text(md.read_text(encoding='utf-8').replace('120 rpm', '129 rpm'), encoding='utf-8')
    checker, summary = inspect(source, md, tmp_path)
    assert summary['pages_with_character_differences'] == [1]
    finding = next(f for f in checker.findings if f['kind'] == 'body_content_difference')
    assert finding['character_delta'] == {'missing': {'0': 1}, 'extra': {'9': 1}}
    assert finding['md_lines'][0] > 1 and finding['bbox']
    assert finding['numeric_sensitive']


def test_same_page_cell_swap_defeats_global_character_bag(tmp_path):
    source, md = fixture(tmp_path, table=True)
    text = md.read_text(encoding='utf-8')
    assert '<td>' in text
    text = text.replace('Speed 120', 'TEMP_MARKER').replace('Feed 240', 'Speed 120').replace('TEMP_MARKER', 'Feed 240')
    md.write_text(text, encoding='utf-8')
    checker, summary = inspect(source, md, tmp_path)
    assert not summary['pages_with_character_differences']
    cells = [f for f in checker.findings if f['kind'] == 'cell_content_difference']
    assert len(cells) == 2
    assert all(f['table_id'] and f['bbox'] and f['md_lines'] for f in cells)


def test_missing_page_anchor_is_not_silent(tmp_path):
    source, md = fixture(tmp_path)
    md.write_text(md.read_text(encoding='utf-8').replace('<!-- PDF_PAGE:1 -->', ''), encoding='utf-8')
    checker, summary = inspect(source, md, tmp_path)
    assert not summary['complete_text_layer_check']
    assert any(f['kind'] == 'page_anchor_difference' for f in checker.findings)


def test_injected_image_corruption_detected(tmp_path):
    from PIL import Image
    source, md = fixture(tmp_path, table=True)
    path = next((tmp_path / 'converted_assets/tables').glob('*.png'))
    with Image.open(path) as stored:
        image = stored.convert('RGB')
    image.putpixel((image.width // 2, image.height // 2), (255, 0, 0))
    image.save(path)
    checker, _ = inspect(source, md, tmp_path, images=True)
    assert any(f['kind'] == 'table_image_difference' for f in checker.findings)


def test_normalization_does_not_erase_technical_differences():
    assert canonical('m² １２３ −5') == canonical('m2 123 -5')
    assert canonical('1.2') != canonical('12')
    assert canonical('≤') != canonical('<')
    assert canonical('μm') != canonical('mm')
    assert canonical('H7') != canonical('h7')
    assert visible('a<b and c>0 &amp; x') == 'a<b and c>0 & x'


def test_parser_does_not_count_navigation_or_scientific_attributes():
    text = '''<a id="pdf-page-1"></a>
<!-- PDF_PAGE:1 -->
[原始 PDF 第 1 页](source.pdf#page=1)
<!-- TABLE:p00001-t01 -->
![完整表格 · PDF 第 1 页](table.png)
<details><summary>展开表格文本</summary>
<table><tr><td><data value="1.2e3">1.2×10³</data></td></tr></table>
</details>
---
## 转换校验
- 自动生成统计
'''
    pages, numbers = parse_markdown(text)
    assert numbers == [1]
    assert ''.join(r.text for r in pages[0].regions).strip() == '1.2×10³'


def test_complete_body_lines_reordered_with_unchanged_character_counts(tmp_path):
    source, md = fixture(tmp_path)
    text = md.read_text(encoding='utf-8')
    text = text.replace('Maximum speed is 120 rpm.', 'TEMP_MARKER').replace(
        'Minimum feed is 240 mm.', 'Maximum speed is 120 rpm.').replace(
        'TEMP_MARKER', 'Minimum feed is 240 mm.')
    md.write_text(text, encoding='utf-8')
    checker, summary = inspect(source, md, tmp_path)
    assert not summary['pages_with_character_differences']
    assert any(f['kind'] == 'body_block_order_review' for f in checker.findings)


def test_stale_visual_review_is_rejected(tmp_path):
    import pytest
    source, md = fixture(tmp_path)
    checker, _ = inspect(source, md, tmp_path)
    path = tmp_path / 'reviews.json'
    path.write_text(json.dumps({'source_sha256': 'stale', 'markdown_sha256': checker.md_hash}), encoding='utf-8')
    with pytest.raises(ValueError, match='哈希'):
        checker.apply_reviews(path)


def test_table_refresh_preserves_document_and_asset_provenance(tmp_path):
    from document_converter.table_refresh import refresh_table_text
    source, md = fixture(tmp_path, table=True)
    before = md.read_text(encoding='utf-8')
    images = {p.name: p.read_bytes() for p in (tmp_path/'converted_assets/tables').glob('*.png')}
    result = refresh_table_text(source, md)
    assert result['tables'] == 1
    assert md.read_text(encoding='utf-8') == before
    assert {p.name:p.read_bytes() for p in (tmp_path/'converted_assets/tables').glob('*.png')} == images
    checker, summary = inspect(source, md, tmp_path)
    assert summary['complete_text_layer_check']
    assert not summary['severity_counts'].get('error')
