"""Source-based regressions, including evidence that character bags miss."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from bs4 import BeautifulSoup
import pymupdf
import pytest

from document_converter.converter import convert_document
from document_converter.fidelity import Inspector, canonical
from document_converter.fraction_layout import cell_layout
from document_converter.glyph_layout import table_layout
from document_converter.pdf_tables import characters, editable_table, extract_tables
from document_converter.script_witness import read_scripts, verify_script, uncovered_candidates
from test_unit_layout import glyph


def table(layout):
    return {'rows': 1, 'columns': 1, 'geometry_gaps': 0,
            'cells': [{'row': 0, 'column': 0, 'rowspan': 1, 'colspan': 1, **layout}]}


@pytest.mark.parametrize('value', ['k', 'max', 'min', 'e min', 'i+1', 'αβ', '∗', 'a,b', '效率'])
@pytest.mark.parametrize('shift,role', [(2.4, 'sub'), (-4, 'sup')])
def test_complete_unicode_script_runs_have_semantic_markup(value, shift, role):
    cs = [glyph('F', 0, 20, 10, 0)]
    cs += [glyph(v, 6+i*3.3, 20+shift, 6, i+1) for i, v in enumerate(value)]
    layout = table_layout(cs)
    assert len(layout['scripts']) == 1
    s = layout['scripts'][0]
    assert (s['base'], s['value'], s['role']) == ('F', value, role)
    soup = BeautifulSoup(editable_table(table(layout)), 'html.parser')
    assert soup.find(role).get_text() == value
    claims, errors = read_scripts(soup.td)
    assert not errors and verify_script(claims[0], cs, set()) is None
    assert Counter(canonical(soup.td.get_text())) == Counter(canonical(''.join(c['text'] for c in cs)))


@pytest.mark.parametrize('suffix', ['h11', 'H13', 'js14', 'd12'])
def test_small_baseline_suffix_is_never_promoted(suffix):
    cs = [glyph(v, i*6, 20, 10 if i < len(suffix)-2 else 6, i) for i,v in enumerate(suffix)]
    layout = table_layout(cs)
    assert not layout.get('scripts')
    assert canonical(layout['text']) == suffix


def test_multichar_base_and_circled_note_remain_distinct():
    cs = [glyph('A', 0, 20, 10, 0), glyph('B', 5.5, 20, 10, 1), glyph('k', 11.3, 22.5, 6, 2)]
    s = table_layout(cs)['scripts'][0]
    assert s['base'] == 'AB' and s['linear'] == 'AB_{k}'
    cs = [glyph('h', 0, 20, 10, 0), glyph('1', 5.5, 20, 10, 1),
          glyph('1', 11, 20, 10, 2), glyph('①', 16.8, 16, 6, 3)]
    layout = table_layout(cs)
    assert layout['scripts'][0]['role'] == 'annotation'
    assert not BeautifulSoup(editable_table(table(layout)), 'html.parser').find('sup')


def test_binding_direction_box_and_duplicate_tampering_fail_independent_witness():
    cs = [glyph('F', 0, 20, 10, 0), glyph('k', 6, 22.5, 6, 1)]
    s = table_layout(cs)['scripts'][0]
    used = set()
    assert verify_script(s, cs, used) is None
    assert 'more than once' in verify_script(s, cs, used)
    for key, value in [('role', 'sup'), ('base_bbox', [0, 0, 1, 1]), ('base', 'G')]:
        modified = deepcopy(s); modified[key] = value
        assert verify_script(modified, cs, set())
    soup = BeautifulSoup(editable_table(table(table_layout(cs))), 'html.parser')
    soup.sub.unwrap()
    claims, errors = read_scripts(soup.td)
    assert errors and not claims
    # Removing the detector's output entirely must still produce a recall alert.
    assert uncovered_candidates(cs, set())[0]['character_id'] == 1


def test_unit_ast_survives_script_atoms():
    cs = [glyph(v, i*5.5, 20, 10, i) for i,v in enumerate('N/m')]
    cs.append(glyph('2', 17, 16, 6, 3))
    soup = BeautifulSoup(editable_table(table(table_layout(cs))), 'html.parser')
    assert soup.select_one('.unit-expression')['data-unit'] == 'N/m^2'
    assert soup.td.get_text() == 'N/m²'


def test_nested_script_tree_and_simultaneous_upper_lower_are_not_flattened():
    cs = [glyph('F', 0, 20, 10, 0), glyph('i', 6, 22.5, 6, 1),
          glyph('j', 9.5, 24, 3.6, 2), glyph('2', 6, 16, 6, 3)]
    layout = table_layout(cs)
    assert len(layout['scripts']) == 3
    assert 'F_{i_{j}}' in layout['linear_text']
    soup = BeautifulSoup(editable_table(table(layout)), 'html.parser')
    assert soup.select_one('sub sub').get_text() == 'j'
    assert '²' in soup.td.get_text()
    claims, errors = read_scripts(soup.td)
    assert not errors
    used = set()
    assert all(verify_script(s, cs, used) is None for s in claims)
    assert not uncovered_candidates(cs, used)
    assert Counter(canonical(soup.td.get_text())) == Counter(canonical(''.join(c['text'] for c in cs)))


def test_fraction_and_letter_script_share_a_cell_without_losing_evidence():
    with pymupdf.open() as doc:
        p = doc.new_page()
        p.insert_text((30, 46.5), '1', fontsize=8)
        p.insert_text((30, 57.5), '2', fontsize=8)
        p.draw_line((28, 49.6), (37, 49.6), width=.3)
        p.insert_text((40, 52), 'F', fontsize=8)
        p.insert_text((45, 54), 'max', fontsize=5)
        cs = characters(p)
        layout = cell_layout(cs, p.get_drawings())
        assert len(layout['fractions']) == len(layout['scripts']) == 1
        soup = BeautifulSoup(editable_table(table(layout)), 'html.parser')
        assert soup.sub.get_text() == 'max'
        claims, errors = read_scripts(soup.td)
        assert not errors and verify_script(claims[0], cs, set()) is None
        from document_converter.math_witness import read_cell
        accounting, display, fractions, errors = read_cell(soup.td)
        assert not errors and len(fractions) == 1
        assert display == layout['text']
        assert Counter(canonical(accounting)) == Counter(canonical(''.join(c['text'] for c in cs)))


@pytest.mark.private_source
def test_original_pin_table_letter_scripts_and_baseline_tolerances(tmp_path):
    sources = list((Path(__file__).resolve().parents[1]/'origin').glob('实用机械加工工艺手册 第5版*.pdf'))
    if not sources:
        pytest.skip('Original handbook not installed')
    with pymupdf.open(sources[0]) as doc:
        page = doc[361]; cs = characters(page)
        tables, issues = extract_tables(page, cs, tmp_path, 'assets', 362)
    assert not issues
    cells = {(c['row'], c['column']): c for c in tables[0]['cells']}
    expected = {1: ('d', 'k'), 2: ('d', '1'), 3: ('c', 'max'), 6: ('l', 'e min')}
    for row, (base, value) in expected.items():
        cell = cells[row, 0]
        s = next(s for s in cell['scripts'] if s['role'] == 'sub')
        assert (s['base'], s['value']) == (base, value)
        original = [c for c in cs if c['id'] in cell['character_ids']]
        assert verify_script(s, original, set()) is None
    for row, suffix in [(0, 'h11'), (1, 'h14'), (2, 'H13'), (5, 'js14')]:
        cell = cells[row, 0]
        assert suffix in cell['text']
        assert not any(s['role'] != 'annotation' and any(c.isdigit() for c in s['value']) and suffix.startswith(s['base'])
                       for s in cell.get('scripts', []))
    soup = BeautifulSoup(editable_table(tables[0]), 'html.parser')
    assert any(s.get_text() == 'max' for s in soup.find_all('sub'))


def test_full_converter_and_checker_reject_flattened_letter_script(tmp_path):
    source = tmp_path/'source.pdf'
    with pymupdf.open() as doc:
        p = doc.new_page(width=300, height=150)
        for y in (20, 60, 100):
            p.draw_line((20, y), (280, y))
        for x in (20, 150, 280):
            p.draw_line((x, 20), (x, 100))
        for x,y in ((30,40), (160,40), (30,80), (160,80)):
            p.insert_text((x,y), 'F', fontsize=10)
            p.insert_text((x+7,y+2.4), 'max', fontsize=6)
        doc.save(source)
    md = tmp_path/'converted.md'
    convert_document(source, md)
    checker = Inspector(source, md, tmp_path/'clean')
    summary = checker.run(ocr='none', verify_images=False, evidence_limit=0)
    assert summary['script_checks'] == 4 and not summary['severity_counts'].get('error')
    # Source text is unchanged: only the semantic tag is erased.
    md.write_text(md.read_text(encoding='utf-8').replace('<sub>max</sub>', 'max'), encoding='utf-8')
    checker = Inspector(source, md, tmp_path/'flattened')
    summary = checker.run(ocr='none', verify_images=False, evidence_limit=0)
    assert not summary['pages_with_character_differences']
    assert any(f['kind'] == 'script_evidence_difference' for f in checker.findings)


def test_body_letter_script_uses_the_same_semantics_and_checker(tmp_path):
    from document_converter.script_witness import linear_cell
    source = tmp_path/'body.pdf'
    with pymupdf.open() as doc:
        p = doc.new_page(width=300, height=150)
        p.insert_text((30,40), 'F', fontsize=10)
        p.insert_text((37,42.4), 'max', fontsize=6)
        p.insert_text((30,80), 'is the limit', fontsize=10)
        doc.save(source)
    md = tmp_path/'body.md'
    convert_document(source, md)
    text = md.read_text(encoding='utf-8')
    assert '<sub>max</sub>' in text
    soup = BeautifulSoup(text, 'html.parser')
    linear, errors = linear_cell(soup)
    assert not errors and 'F_{max}' in linear
    checker = Inspector(source, md, tmp_path/'body-clean')
    summary = checker.run(ocr='none', verify_images=False, evidence_limit=0)
    assert summary['body_script_checks'] == 1
    assert not summary['severity_counts'].get('error')
    md.write_text(text.replace('<sub>max</sub>', 'max'), encoding='utf-8')
    checker = Inspector(source, md, tmp_path/'body-flat')
    checker.run(ocr='none', verify_images=False, evidence_limit=0)
    assert any(f['kind'] == 'body_script_evidence_difference' for f in checker.findings)


def test_adjacent_footnotes_and_overbar_never_capture_subscript_base():
    cs = [glyph('C', 0, 20, 10, 0), glyph('Z', 5.5, 20, 10, 1),
          glyph('①', 11.2, 16, 6, 2), glyph('③', 14.5, 16, 6, 3)]
    s = cell_layout(cs, [])['scripts'][0]
    assert s['role'] == 'annotation' and s['value'] == '①③'
    # Actual PDF convention: the accent's wide box overlaps the base letter.
    cs = [glyph('ˉ', 0, 18.8, 10, 0, 10), glyph('h', .2, 20, 10, 1),
          glyph('a', 6, 22.4, 6, 2), glyph('1', 9.3, 22.4, 6, 3)]
    s = cell_layout(cs, [])['scripts'][0]
    assert s['base'] == 'h' and s['value'] == 'a1'


def test_body_refresh_is_idempotent_and_migrates_existing_prose(tmp_path):
    from document_converter.table_refresh import refresh_table_text
    source = tmp_path/'body.pdf'
    with pymupdf.open() as doc:
        p = doc.new_page(width=300, height=150)
        p.insert_text((30,40), 'F', fontsize=10)
        p.insert_text((37,42.4), 'max', fontsize=6)
        doc.save(source)
    md = tmp_path/'body.md'
    convert_document(source, md)
    original = md.read_text(encoding='utf-8')
    import re
    legacy = re.sub(r'(?ms)^<!-- BODY_SCRIPT:[^>]+ -->\n.*?\n<!-- /BODY_SCRIPT -->', 'Fmax', original)
    md.write_text(legacy, encoding='utf-8')
    first = refresh_table_text(source, md)
    assert first['changed_body_blocks'] == 1
    assert md.read_text(encoding='utf-8') == original
    structure = tmp_path/'body_assets'/'structure.jsonl'
    witness = structure.read_bytes()
    second = refresh_table_text(source, md)
    assert second['changed_body_blocks'] == 0
    assert md.read_text(encoding='utf-8') == original
    assert structure.read_bytes() == witness
    checker = Inspector(source, md, tmp_path/'body-refreshed')
    summary = checker.run(ocr='none', verify_images=False, evidence_limit=0)
    assert summary['body_script_checks'] == 1
    assert not summary['severity_counts'].get('error')


def test_body_refresh_assigns_repeated_identical_scripts_to_distinct_lines(tmp_path):
    from document_converter.table_refresh import refresh_table_text
    source = tmp_path/'repeated.pdf'
    with pymupdf.open() as doc:
        p = doc.new_page(width=300, height=150)
        for y in (40, 80):
            p.insert_text((30,y), 'R', fontsize=10)
            p.insert_text((36,y-4), '2', fontsize=6)
        doc.save(source)
    md = tmp_path/'repeated.md'
    convert_document(source, md)
    import re
    legacy = re.sub(r'(?ms)^<!-- BODY_SCRIPT:[^>]+ -->\n.*?\n<!-- /BODY_SCRIPT -->', 'R²',
                    md.read_text(encoding='utf-8'))
    md.write_text(legacy, encoding='utf-8')
    result = refresh_table_text(source, md)
    rendered = md.read_text(encoding='utf-8')
    assert result['changed_body_blocks'] == 2
    assert len(re.findall(r'(?m)^<!-- BODY_SCRIPT:b\d+ -->$', rendered)) == 2
    structure = [json.loads(line) for line in (tmp_path/'repeated_assets/structure.jsonl').read_text(encoding='utf-8').splitlines()]
    blocks = structure[0]['body_scripts']
    assert len(blocks) == 2 and [b['linear_text'] for b in blocks] == ['R^{2}', 'R^{2}']
    checker = Inspector(source, md, tmp_path/'repeated-check')
    summary = checker.run(ocr='none', verify_images=False, evidence_limit=0)
    assert summary['body_script_checks'] == 2
    assert not summary['severity_counts'].get('error')


@pytest.mark.parametrize('shift,role', [(2.4, 'sub'), (-4, 'sup')])
def test_prescripts_preserve_side_and_independent_source_evidence(shift, role):
    from document_converter.script_witness import linear_cell
    cs = [glyph('i', 0, 20+shift, 6, 0), glyph('A', 4, 20, 10, 1)]
    layout = cell_layout(cs, [])
    s = layout['scripts'][0]
    assert (s['side'], s['role'], s['base'], s['value']) == ('left', role, 'A', 'i')
    soup = BeautifulSoup(editable_table(table(layout)), 'html.parser')
    linear, errors = linear_cell(soup.td)
    assert not errors and linear == ('_{i}A' if role == 'sub' else '^{i}A')
    assert linear == layout['linear_text']
    assert verify_script(s, cs, set()) is None
    wrong = deepcopy(s); wrong['side'] = 'right'
    assert verify_script(wrong, cs, set())


def test_one_multichar_base_can_have_left_and_right_scripts_at_once():
    from document_converter.script_witness import linear_cell
    cs = [glyph('i', 0, 22.4, 6, 0), glyph('A', 5, 20, 10, 1),
          glyph('B', 10.5, 20, 10, 2), glyph('2', 16.5, 16, 6, 3)]
    layout = table_layout(cs)
    assert layout['linear_text'] == '_{i}AB^{2}'
    assert {(s['side'], s['role'], s['base']) for s in layout['scripts']} == {
        ('left', 'sub', 'AB'), ('right', 'sup', 'AB')}
    soup = BeautifulSoup(editable_table(table(layout)), 'html.parser')
    claims, errors = read_scripts(soup.td)
    used = set()
    assert not errors
    assert all(verify_script(s, cs, used) is None for s in claims)
    assert linear_cell(soup.td)[0] == layout['linear_text']
    assert Counter(canonical(soup.td.get_text())) == Counter(canonical(''.join(c['text'] for c in cs)))


@pytest.mark.parametrize('operator', ['+', '=', ')', '∑'])
def test_ambiguous_left_operator_is_not_guessed_as_script_base(operator):
    cs = [glyph('2', 0, 16, 6, 0), glyph(operator, 5, 20, 10, 1)]
    layout = table_layout(cs)
    assert not layout.get('scripts')
    assert uncovered_candidates(cs, set())
