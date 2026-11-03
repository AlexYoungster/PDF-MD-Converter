from collections import Counter
from pathlib import Path
import json

from bs4 import BeautifulSoup
import pymupdf
import pytest

from document_converter.glyph_layout import table_text_lines
from document_converter.pdf_tables import characters, inside, editable_table
from document_converter.units import cell_unit, parse_unit


def glyph(value, x, y, size, ident, width=None):
    return {'text': value, 'origin': [x, y], 'size': size, 'id': ident,
            'bbox': [x, y-size, x+(width if width is not None else size*.55), y],
            'direction': (1, 0), 'block': ident, 'line': 0}


def test_mixed_font_baseline_before_script_classification():
    chars = [glyph('牛', 0, 20, 16, 0, 7.76), glyph('顿', 8, 20, 16, 1, 7.76),
             glyph('/', 17, 19.61, 7.76, 2), glyph('米', 20, 20, 16, 3, 7.76),
             glyph('2', 28, 16.03, 5.14, 4)]
    assert table_text_lines(chars) == ['牛顿/米²']
    # The denominator may contain another script without moving its brackets.
    chars += [glyph('（', 0, 35, 16, 5, 8), glyph('N', 5, 35, 7.76, 6),
              glyph('/', 11, 35, 7.76, 7), glyph('m', 14, 35, 7.76, 8),
              glyph('2', 20, 31.4, 5.14, 9), glyph('）', 24, 35, 16, 10, 8)]
    assert table_text_lines(chars) == ['牛顿/米²', '（N/m²）']


def test_fraction_numerator_and_denominator_do_not_become_exponents():
    chars = [glyph('5', 0, 20, 8, 0), glyph('9', 0, 30, 8, 1)]
    assert table_text_lines(chars) == ['5', '9']


def test_same_baseline_small_latin_is_not_superscript():
    chars = [glyph('瓦', 0, 20, 16, 0, 8), glyph('/', 9, 19.6, 8, 1),
             glyph('（', 12, 19.6, 16, 2, 8), glyph('米', 17, 20, 16, 3, 8),
             glyph('·', 25, 19.6, 16, 4, 8), glyph('开', 33, 20, 16, 5, 8),
             glyph('）', 41, 19.6, 16, 6, 8)]
    assert table_text_lines(chars) == ['瓦/（米·开）']


def test_unit_tree_encodes_entire_denominator_and_powers():
    unit = parse_unit('W/（m·K）')
    assert unit['ascii_expression'] == 'W/(m*K)'
    assert unit['ast'] == {'op': 'divide', 'left': {'op': 'unit', 'symbol': 'W'},
                           'right': {'op': 'multiply', 'left': {'op': 'unit', 'symbol': 'm'},
                                     'right': {'op': 'unit', 'symbol': 'K'}}}
    assert parse_unit('N/m²')['ast']['right']['exponent'] == 2
    assert cell_unit('牛顿/米²\n（N/ m²）')['ascii_expression'] == 'N/m^2'
    assert cell_unit('W/(m*K)')['ascii_expression'] == 'W/(m*K)'
    for broken in ['W/（m·K', '/瓦 （米·开）', 'W/ m K', 'W/(m*UNKNOWN)', 'm/s/sx']:
        assert parse_unit(broken) is None
    assert parse_unit('('*1000+'W'+')'*1000) is None
    for label in ['C', 'S', 'F', '碳（C）', '硫（S）', '太拉（T）']:
        assert cell_unit(label) is None
    assert cell_unit('压力（MPa）')['ascii_expression'] == 'MPa'


def test_html_unit_visible_text_and_machine_syntax_agree():
    text = '瓦/（米·开）\n[W/（m·K）]'
    table = {'rows': 1, 'columns': 1, 'geometry_gaps': 0,
             'cells': [{'row': 0, 'column': 0, 'rowspan': 1, 'colspan': 1, 'text': text}]}
    soup = BeautifulSoup(editable_table(table), 'html.parser')
    span = soup.select_one('.unit-expression')
    assert span['data-unit'] == 'W/(m*K)'
    assert json.loads(span['data-unit-ast'])['op'] == 'divide'
    assert soup.td.get_text() == text.replace('\n', '')
    assert 'white-space:nowrap' in span['style']


def test_signed_decimal_tolerance_is_not_fragmented_into_scripts():
    chars = [glyph('4', 0, 20, 8, 0), glyph('5', 4, 20, 8, 1),
             glyph('+', 9, 15.6, 5.14, 2), glyph('0', 12, 15.9, 5.14, 3),
             glyph('.', 15, 15.9, 5.14, 4), glyph('0', 18, 15.9, 5.14, 5),
             glyph('2', 21, 15.9, 5.14, 6), glyph('5', 24, 15.9, 5.14, 7),
             glyph('0', 12, 23.1, 5.14, 8)]
    assert table_text_lines(chars) == ['+0.025', '45', '0']


@pytest.mark.private_source
def test_original_heat_conductivity_units_are_complete():
    root = Path(__file__).resolve().parents[1]
    sources = list((root/'origin').glob('实用机械加工工艺手册 第5版*.pdf'))
    structures = root/'output/document_converter/实用机械加工工艺手册_全文_assets/structure.jsonl'
    if not sources or not structures.exists():
        pytest.skip('Original handbook not installed')
    with structures.open(encoding='utf-8') as stream:
        record = next(json.loads(line) for line in stream if json.loads(line)['pdf_page'] == 27)
    table = next(t for t in record['tables'] if t['id'] == 'p00027-t02')
    expected = ['W/(m*K)', 'kcal/(m*h*℃)', 'cal/(cm*s*℃)', 'J/(cm*s*℃)', 'Btu/(ft*h*°F)']
    with pymupdf.open(sources[0]) as doc:
        chars = characters(doc[26])
        for cell in table['cells']:
            if cell['row']:
                continue
            source = [c for c in chars if inside(c, cell['bbox'])]
            value = '\n'.join(table_text_lines(source))
            assert cell_unit(value)['ascii_expression'] == expected[cell['column']]
            # Width/line normalization must never drop/duplicate source glyphs.
            normalize = lambda s: ''.join(c for c in s if not c.isspace())
            assert Counter(normalize(value)) == Counter(normalize(''.join(c['text'] for c in source)))
