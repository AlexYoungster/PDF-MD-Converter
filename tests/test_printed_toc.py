"""Contents must follow printed rows/columns even with adversarial PDF blocks."""
from pathlib import Path
import json
import random

import pymupdf
import pytest

from document_converter.body_script_layout import body_html
from document_converter.converter import convert_document
from document_converter.fidelity import Inspector, canonical, parse_markdown
from document_converter.pdf_tables import body_blocks, characters
from document_converter.volumes import printed_contents, volume_pages


class GlyphPage:
    def __init__(self):
        self.chars = []
        self.block = 0

    def add(self, text, x, y, block=None, line=0):
        for char in text:
            self.chars.append({'id': len(self.chars), 'text': char, 'bbox': [x, y-8, x+5, y+2],
                               'origin': [x, y], 'size': 10, 'direction': (1, 0),
                               'block': self.block if block is None else block, 'line': line})
            x += 5
        self.block += 1

    def entry(self, title, number, y, x=20, right=230, block=None):
        self.add(title, x, y, block, int(y)*3)
        self.add('.'*8, right-max(55, 5*len(str(number))+50), y, block, int(y)*3+1)
        self.add(str(number), right-5*len(str(number)), y, block, int(y)*3+2)

    def model(self):
        blocks = body_blocks(self.chars, set())
        assert len(blocks) == 1 and blocks[0].get('toc')
        return blocks[0]['toc']


def test_all_entries_in_one_native_block_are_separate_rows():
    p = GlyphPage()
    for i in reversed(range(1, 6)):
        p.entry(f'{i} Entry title', i*10, 40+i*15, block=0)
    entries = p.model()['entries']
    assert [(e['title'], e['page']) for e in entries] == [(f'{i} Entry title', str(i*10)) for i in range(1, 6)]


def test_two_columns_ignore_scrambled_text_blocks_and_keep_page_pairs():
    p = GlyphPage()
    rows = [(f'Left entry {i}', i, 60+i*16, 20, 230) for i in range(4)]
    rows += [(f'Right entry {i}', i+30, 60+i*16, 280, 490) for i in range(4)]
    random.Random(4).shuffle(rows)
    for title, page, y, x, right in rows:
        # Insert page, then leader, then title: deliberately wrong native order.
        p.add(str(page), right-5*len(str(page)), y)
        p.add('.'*8, right-55, y)
        p.add(title, x, y)
    model = p.model()
    assert model['columns'] == 2
    assert [(e['title'], e['page']) for e in model['entries']] == [
        (f'Left entry {i}', str(i)) for i in range(4)] + [(f'Right entry {i}', str(i+30)) for i in range(4)]


def test_wrapped_title_joins_words_and_keeps_final_row_page():
    p = GlyphPage()
    p.add('Contents', 20, 20)
    p.add('1 A long title', 20, 50)
    p.entry('continued here', 17, 65, x=30)
    p.entry('2 Another title', 28, 85)
    model = p.model()
    assert model['entries'][0]['title'] == '1 A long title continued here'
    assert model['entries'][0]['page'] == '17'
    assert not model['reviews']


def test_missing_indent_band_does_not_flatten_continuation_hierarchy():
    p = GlyphPage()
    p.add('Contents', 20, 20)
    p.entry('Chapter 2 Bearings', 10, 50, x=20)
    p.entry('2.1 Bearing types', 10, 65, x=30)
    # Level 2 is absent on this page; the indent is still three physical steps.
    p.entry('Example 1', 12, 80, x=50)
    p.entry('Example 2', 15, 95, x=50)
    assert [e['level'] for e in p.model()['entries']] == [0, 1, 3, 3]


@pytest.mark.parametrize('label', ['12', 'IV', 'Ⅷ', 'A-12', '12–14', '１２'])
def test_printed_page_label_forms_are_preserved(label):
    p = GlyphPage()
    p.add('Contents', 20, 20)
    p.entry('First topic', label, 50)
    p.entry('Second topic', label, 70)
    assert [e['page'] for e in p.model()['entries']] == [label, label]


def test_clear_heading_allows_leaderless_contents_but_numeric_rows_do_not():
    p = GlyphPage()
    p.add('Contents', 20, 20)
    for i in range(3):
        p.add(f'{i+1} Topic', 20, 50+i*20)
        p.add(str(i+10), 220, 50+i*20)
    assert len(p.model()['entries']) == 3
    q = GlyphPage()
    for i in range(5):
        q.entry(f'{i}.25', i+10, 50+i*20)
    assert not any(b.get('toc') for b in body_blocks(q.chars, set()))


def test_heading_alone_and_prose_are_not_restructured():
    p = GlyphPage()
    p.add('Contents', 20, 20)
    p.add('This paragraph describes how a book is organized.', 20, 55)
    assert not any(b.get('toc') for b in body_blocks(p.chars, set()))


def test_unpaginated_frontmatter_and_unpaired_text_are_preserved():
    p = GlyphPage()
    p.add('Contents', 20, 20)
    p.add('Preface', 20, 45)
    p.entry('Chapter 1 Basics', 1, 60)
    p.entry('1.1 Main topic', 2, 80, x=30)
    p.add('Uncertain tail', 20, 100)
    model = p.model()
    assert [(e['title'], e['page']) for e in model['entries']][:2] == [('Preface', ''), ('Chapter 1 Basics', '1')]
    assert model['reviews'][0]['reason'] == 'unpaired_contents_fragment'
    assert 'Uncertain tail' in model['text']
    assert len(model['character_ids']) == len(set(model['character_ids']))


def test_ambiguous_fragment_stays_between_its_neighboring_entries():
    p = GlyphPage()
    p.add('Contents', 20, 20)
    p.entry('Chapter 1 First', 1, 50)
    p.add('Unpaired note', 20, 65)
    p.entry('Chapter 2 Next', 2, 80)
    block = body_blocks(p.chars, set())[0]
    text = body_html(block, lambda x:x)
    assert text.index('Chapter 1') < text.index('Unpaired note') < text.index('Chapter 2')
    pages, _ = parse_markdown('<!-- PDF_PAGE:1 -->\n' + text)
    assert 'Unpaired note' in ''.join(r.text for r in pages[0].regions)


def make_real_contents(tmp_path):
    source = tmp_path / 'contents.pdf'
    with pymupdf.open() as doc:
        page = doc.new_page(width=320, height=260)
        page.insert_text((20, 25), 'Contents')
        for i in reversed(range(1, 5)):
            y = 40+i*28
            page.insert_text((280, y), str(i*10))
            page.insert_text((150, y), '.'*25)
            page.insert_text((20, y), f'Chapter {i} Long topic')
        doc.set_toc([[1, 'Unprinted bookmark', 1]])
        doc.save(source)
    result = convert_document(source, tmp_path / 'converted.md')
    return source, Path(result['markdown'])


def inspect(source, md, tmp_path):
    check = Inspector(source, md, tmp_path / 'check')
    result = check.run(ocr='none', verify_images=False, evidence_limit=0)
    return check, result


def test_real_conversion_retains_leader_provenance_and_passes_inspection(tmp_path):
    source, md = make_real_contents(tmp_path)
    text = md.read_text(encoding='utf-8')
    assert 'Unprinted bookmark' not in text
    assert '<!-- PRINTED_TOC -->' in text and 'data-source-text=' in text
    pages, _ = parse_markdown(text)
    assert [r.toc_fields['page'] for r in pages[0].regions if r.toc_id is not None] == ['10', '20', '30', '40']
    check, summary = inspect(source, md, tmp_path)
    assert not summary['severity_counts'].get('error'), check.findings
    assert not summary['severity_counts'].get('review'), check.findings
    assert summary['contents_entry_checks'] == 4
    assert summary['source_characters'] == summary['converted_characters']
    assert printed_contents(text.splitlines())
    saved = json.loads((tmp_path / 'converted_assets/structure.jsonl').read_text(encoding='utf-8'))
    assert saved['printed_contents'][0]['entries'][0]['page'] == '10'


def test_swapping_printed_page_labels_is_detected_without_character_loss(tmp_path):
    source, md = make_real_contents(tmp_path)
    text = md.read_text(encoding='utf-8').replace('>10</span>', '>TEMP</span>').replace('>20</span>', '>10</span>').replace('>TEMP</span>', '>20</span>')
    md.write_text(text, encoding='utf-8')
    check, summary = inspect(source, md, tmp_path)
    assert not summary['pages_with_character_differences']
    assert len([f for f in check.findings if f['kind'] == 'contents_entry_difference']) == 2


def test_reordering_contents_rows_is_detected(tmp_path):
    source, md = make_real_contents(tmp_path)
    text = md.read_text(encoding='utf-8')
    lines = text.splitlines()
    row_lines = [i for i, line in enumerate(lines) if line.startswith('<tr data-toc-id=')]
    a, b = row_lines[:2]
    lines[a], lines[b] = lines[b], lines[a]
    md.write_text('\n'.join(lines)+'\n', encoding='utf-8')
    check, summary = inspect(source, md, tmp_path)
    assert not summary['pages_with_character_differences']
    assert any(f['kind'] == 'contents_order_difference' for f in check.findings)


def test_printed_volume_entries_never_become_volume_boundaries():
    md = '<a id="pdf-page-1"></a>\n<!-- PDF_PAGE:1 -->\n<!-- PRINTED_TOC -->\n<table><tr><td>第一卷</td></tr></table>\n<!-- /PRINTED_TOC -->\n---\n'
    assert volume_pages(md, 1) == []


@pytest.mark.private_source
def test_user_book_all_five_contents_pages(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'origin/典型精密零件机械加工工艺分析及实例_第2版.pdf'
    if not source.exists():
        pytest.skip('Optional user book not installed')
    with pymupdf.open(source) as doc:
        models = [body_blocks(characters(doc[i]), set())[0]['toc'] for i in range(4, 9)]
    assert [len(m['entries']) for m in models] == [28, 38, 38, 37, 12]
    assert all(not m['reviews'] for m in models)
    assert models[0]['entries'][7]['level'] == 3
    assert models[1]['entries'][0]['level'] == 3
    appendix = next(e for e in models[-1]['entries'] if '附录I' in e['title'])
    assert 'α₀' in appendix['title']
    assert 'data-script-role="sub"' in appendix['title_html']
