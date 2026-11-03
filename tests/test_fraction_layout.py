from collections import Counter
from pathlib import Path
import json

from bs4 import BeautifulSoup
import pymupdf
import pytest

from document_converter.converter import convert_document
from document_converter.fidelity import Inspector, canonical
from document_converter.fraction_layout import cell_layout, detect_fractions
from document_converter.pdf_tables import characters, editable_table, extract_tables, _repair_rows
from document_converter.math_witness import read_cell


def fraction(page, x=30, y=52, numerator='5', denominator='9', bar=True):
    page.insert_text((x, y-5.5), numerator, fontsize=8)
    page.insert_text((x, y+5.5), denominator, fontsize=8)
    if bar:
        page.draw_line((x-2, y-2.4), (x+7, y-2.4), width=.3)


def table_pdf(path):
    doc = pymupdf.open()
    p = doc.new_page(width=300, height=150)
    for y in (20,40,100):
        p.draw_line((20,y),(280,y))
    for x in (20,150,280):
        p.draw_line((x,20),(x,40))
        for y in (40,60,80):
            p.draw_line((x,y),(x,y+20))
    p.insert_text((30,32),'Fraction',fontsize=8)
    p.insert_text((160,32),'Formula',fontsize=8)
    for y in (52,72,92):
        fraction(p,30,y)
        p.insert_text((45,y),'(F-32)',fontsize=8)
        p.insert_text((160,y),'value',fontsize=8)
    doc.save(path)
    doc.close()


def test_numeric_fraction_requires_bar_and_centered_nonzero_numeric_runs():
    with pymupdf.open() as doc:
        p=doc.new_page()
        fraction(p)
        layout=cell_layout(characters(p),p.get_drawings())
        assert layout['text']=='(5/9)'
        assert layout['source_text']=='59'
        assert len(layout['fractions'])==1
    for kwargs in ({'bar':False},{'denominator':'0'},{'numerator':'x'}):
        with pymupdf.open() as doc:
            p=doc.new_page()
            fraction(p,**kwargs)
            assert not detect_fractions(characters(p),p.get_drawings())


def test_fraction_row_recovery_preserves_all_source_digits(tmp_path):
    path=tmp_path/'fraction.pdf'
    table_pdf(path)
    with pymupdf.open(path) as doc:
        p=doc[0]; cs=characters(p)
        tables, issues=extract_tables(p,cs,tmp_path/'assets','assets',1)
        assert not issues
        table=tables[0]
        assert (table['native_rows'],table['rows'],table['columns'])==(2,4,2)
        assert len(table['inferred_rules'])==4
        assert sum(len(c.get('fractions',[])) for c in table['cells'])==3
        for c in table['cells']:
            original=''.join(v['text'] for v in cs if v['id'] in c['character_ids'])
            assert Counter(canonical(original))==Counter(canonical(c.get('source_text',c['text'])))
        assert all(''.join(c['text'].split())=='(5/9)(F-32)' for c in table['cells'] if c['row'] and c['column']==0)
        soup=BeautifulSoup(editable_table(table),'html.parser')
        assert len(soup.select('.numeric-fraction'))==3
        assert soup.select_one('.numeric-fraction').get_text()=='(5/9)'


def test_row_cut_cannot_pass_between_numerator_and_denominator():
    with pymupdf.open() as doc:
        p=doc.new_page()
        fraction(p,30,52)
        p.insert_text((110,46),'above',fontsize=8)
        p.insert_text((110,62),'below',fontsize=8)
        # Independent vertical endpoints at 53 would otherwise suggest a row.
        for x in (20,100,180):
            p.draw_line((x,35),(x,53)); p.draw_line((x,53),(x,75))
        boxes=[(20,35,100,75),(100,35,180,75)]
        rebuilt, rules=_repair_rows(boxes,characters(p),p.get_drawings(),(20,35,180,75))
        assert rebuilt==boxes and not rules


def test_short_grid_rule_between_numbers_is_not_a_fraction():
    with pymupdf.open() as doc:
        p=doc.new_page()
        fraction(p)
        p.draw_line((28,35),(28,65))
        p.draw_line((37,35),(37,65))
        assert not detect_fractions(characters(p),p.get_drawings())


def test_touching_horizontal_grid_fragments_are_joined_before_classification():
    with pymupdf.open() as doc:
        p=doc.new_page()
        fraction(p)
        # Neither short fragment touches a vertical edge, but together these
        # fragments form a full table rule, not a numeric division bar.
        p.draw_line((10,49.6),(28,49.6))
        p.draw_line((37,49.6),(70,49.6))
        assert not detect_fractions(characters(p),p.get_drawings())


def test_partial_decimal_run_cannot_be_claimed_as_numerator():
    with pymupdf.open() as doc:
        p=doc.new_page()
        fraction(p)
        p.insert_text((25.55,46.5),'1',fontsize=8)
        # Would otherwise extract only 5 from the complete printed number 15.
        assert not detect_fractions(characters(p),p.get_drawings())


def test_independent_witness_rejects_collinear_rule_fragment_and_partial_number():
    from document_converter.math_witness import verify_fraction
    for mutation in ('rule','number'):
        with pymupdf.open() as doc:
            p=doc.new_page(); fraction(p)
            claim=detect_fractions(characters(p),p.get_drawings())[0]
            if mutation=='rule':
                p.draw_line((10,49.6),(28,49.6))
                p.draw_line((37,49.6),(70,49.6))
            else:
                p.insert_text((25.55,46.5),'1',fontsize=8)
            assert verify_fraction(claim,characters(p),p.get_drawings(),set()) is not None


def test_fraction_evidence_is_checked_independently_and_tampering_fails(tmp_path):
    source=tmp_path/'fraction.pdf'; table_pdf(source)
    md=tmp_path/'converted.md'
    convert_document(source,md)
    summary=Inspector(source,md,tmp_path/'check').run(ocr='none')
    assert summary['fraction_checks']==3
    assert summary['derived_fraction_syntax_characters']==9
    assert not summary['severity_counts'].get('error')
    # Keep visible digits but forge the bar evidence: character bags alone
    # cannot detect this. Source geometry validation must reject it.
    soup=BeautifulSoup(md.read_text(encoding='utf-8'),'html.parser')
    span=soup.select_one('.numeric-fraction')
    geometry=json.loads(span['data-source-geometry'])
    geometry['bar'][1]+=1; geometry['bar'][3]+=1
    span['data-source-geometry']=json.dumps(geometry)
    md.write_text(str(soup),encoding='utf-8')
    checker=Inspector(source,md,tmp_path/'tampered')
    checker.run(ocr='none',verify_images=False)
    assert any(f['kind']=='fraction_evidence_difference' for f in checker.findings)


def test_unknown_generated_punctuation_is_never_silently_removed():
    soup=BeautifulSoup('<td>5<span class="math-generated">HIDDEN</span>9</td>','html.parser')
    source,display,fractions,errors=read_cell(soup.td)
    assert source==display=='5HIDDEN9'
    assert errors and not fractions


def test_mixed_number_uses_explicit_addition_and_preserves_source_digits():
    with pymupdf.open() as doc:
        p=doc.new_page()
        fraction(p,numerator='1',denominator='2')
        p.insert_text((22,52),'1',fontsize=8)
        layout=cell_layout(characters(p),p.get_drawings())
        assert layout['text']=='(1+1/2)'
        assert layout['source_text']=='112'
        assert layout['fractions'][0]['whole']=='1'
        table={'rows':1,'columns':1,'geometry_gaps':0,
               'cells':[{'row':0,'column':0,'rowspan':1,'colspan':1,**layout}]}
        soup=BeautifulSoup(editable_table(table),'html.parser')
        accounting,display,fs,errors=read_cell(soup.td)
        assert not errors and accounting=='112' and display=='(1+1/2)'
        from document_converter.math_witness import verify_fraction
        assert verify_fraction(fs[0],characters(p),p.get_drawings(),set()) is None


def test_improper_fraction_after_integer_is_not_guessed_as_mixed():
    with pymupdf.open() as doc:
        p=doc.new_page()
        fraction(p,numerator='5',denominator='2')
        p.insert_text((22,52),'1',fontsize=8)
        layout=cell_layout(characters(p),p.get_drawings())
        assert 'whole' not in layout['fractions'][0]


@pytest.mark.private_source
def test_original_temperature_table_has_eight_fractions_and_five_rows(tmp_path):
    root=Path(__file__).resolve().parents[1]
    sources=list((root/'origin').glob('实用机械加工工艺手册 第5版*.pdf'))
    if not sources:
        pytest.skip('Original handbook not installed')
    with pymupdf.open(sources[0]) as doc:
        p=doc[26]
        tables,issues=extract_tables(p,characters(p),tmp_path,'assets',27)
    assert not issues
    t=tables[0]
    assert (t['rows'],t['columns'])==(5,4)
    assert sum(len(c.get('fractions',[])) for c in t['cells'])==8
    cells={(c['row'],c['column']):c for c in t['cells']}
    assert cells[1,1]['text']=='(9/5)℃+32'
    assert cells[2,0]['text']=='(5/9)（℉-32）'
    assert cells[3,0]['text']=='(5/9)（°R-491.67）'
    assert cells[4,1]['text']=='(5/9)K-459.67'
    for c in t['cells']:
        for f in c.get('fractions',[]):
            assert not any(f['baseline_extent'][0]<rule['y']<f['baseline_extent'][1]
                           for rule in t['inferred_rules'])


@pytest.mark.private_source
def test_original_fragmented_grid_is_not_a_fraction_or_table_fallback(tmp_path):
    root=Path(__file__).resolve().parents[1]
    sources=list((root/'origin').glob('实用机械加工工艺手册 第5版*.pdf'))
    if not sources:
        pytest.skip('Original handbook not installed')
    with pymupdf.open(sources[0]) as doc:
        p=doc[1024]
        tables,issues=extract_tables(p,characters(p),tmp_path,'assets',1025)
    assert not issues
    t=next(t for t in tables if t['id']=='p01025-t02')
    assert (t['rows'],t['columns'])==(12,28)
    assert not t.get('fallback')
    assert not any(c.get('fractions') for c in t['cells'])
