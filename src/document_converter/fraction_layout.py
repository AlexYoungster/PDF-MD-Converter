"""Numeric fractions grounded in PDF vector bars and original glyphs.

Detection precedes grid repair: a fraction is an indivisible layout object.
Added linear notation is recorded separately from original text characters.
"""
from __future__ import annotations

import html
import json
import re
from statistics import median

from .glyph_layout import table_layout, script_value, script_html, _script_content

NUMBER = re.compile(r'[+−-]?\d+(?:\.\d+)?')


def horizontal_segments(drawings):
    for pi, path in enumerate(drawings):
        for ii, item in enumerate(path['items']):
            if item[0] == 'l':
                a, b = item[1:]
                if abs(a.y-b.y) <= .25 and abs(a.x-b.x) >= 1:
                    yield pi, ii, [min(a.x, b.x), (a.y+b.y)/2, max(a.x, b.x), (a.y+b.y)/2]
            elif item[0] == 're':
                r = item[1]
                if r.height <= .7 and r.width >= 1:
                    yield pi, ii, [r.x0, (r.y0+r.y1)/2, r.x1, (r.y0+r.y1)/2]


def _bounds(chars):
    return [min(c['bbox'][0] for c in chars), min(c['bbox'][1] for c in chars),
            max(c['bbox'][2] for c in chars), max(c['bbox'][3] for c in chars)]


def horizontal_runs(drawings):
    """Join collinear touching path fragments before assigning line roles."""
    bands=[]
    for pi,ii,bar in sorted(horizontal_segments(drawings),key=lambda v:(v[2][1],v[2][0])):
        group=next((g for g in reversed(bands) if abs(g['y']-bar[1])<=.25),None)
        if group is None:
            group={'y':bar[1],'segments':[]}; bands.append(group)
        group['segments'].append((pi,ii,bar))
    for band in bands:
        runs=[]
        for pi,ii,bar in sorted(band['segments'],key=lambda v:v[2][0]):
            if runs and bar[0]<=runs[-1]['bar'][2]+.5:
                runs[-1]['bar'][2]=max(runs[-1]['bar'][2],bar[2])
                runs[-1]['items'].append([pi,ii])
            else:
                runs.append({'bar':[bar[0],band['y'],bar[2],band['y']],'items':[[pi,ii]]})
        yield from runs


def detect_fractions(chars, drawings):
    """Recognize simple numeric fractions, never infer a missing bar.

    Unknown symbolic or nested fractions remain source text. Candidate bars
    need centered numeric runs above/below, comparable em and compact height.
    Short grid lines, minus signs and underlines alone are insufficient.
    """
    if not chars:
        return []
    result, claimed = [], set()
    verticals = []
    for path in drawings:
        for item in path['items']:
            if item[0] == 'l':
                a, b = item[1:]
                if abs(a.x-b.x) < .3 and abs(a.y-b.y) >= 2:
                    verticals.append([(a.x+b.x)/2, min(a.y,b.y), max(a.y,b.y)])
            elif item[0]=='re' and item[1].width<=.7 and item[1].height>=2:
                r=item[1]; verticals.append([(r.x0+r.x1)/2,r.y0,r.y1])
    nonspace = [c for c in chars if c['text'].strip() and abs(c['direction'][0]) > .9]
    # Spatial buckets avoid rescanning a large table/page for each vector item.
    buckets = {}
    for c in nonspace:
        buckets.setdefault(int(c['origin'][0]//32), []).append(c)
    for horizontal in horizontal_runs(drawings):
        bar=horizontal['bar']
        x0, y, x1, _ = bar
        if not 2 <= x1-x0 <= 80:
            continue
        if any(min(abs(x-x0), abs(x-x1)) < .6 and lo-.6 <= y <= hi+.6
               for x,lo,hi in verticals):
            continue
        nearby = [c for k in range(int(x0//32)-1, int(x1//32)+2) for c in buckets.get(k, [])
                  if x0-.5 <= (c['bbox'][0]+c['bbox'][2])/2 <= x1+.5
                  and c['bbox'][0] >= x0-1 and c['bbox'][2] <= x1+1
                  and abs(c['origin'][1]-y) <= max(4, c['size']*1.8)]
        upper = [c for c in nearby if .12*c['size'] < y-c['origin'][1] < c['size']*1.2]
        lower = [c for c in nearby if .3*c['size'] < c['origin'][1]-y < c['size']*1.7]
        if not upper or not lower:
            continue
        ny, dy = max(c['origin'][1] for c in upper), min(c['origin'][1] for c in lower)
        num = sorted((c for c in upper if abs(c['origin'][1]-ny) <= .65), key=lambda c: c['bbox'][0])
        den = sorted((c for c in lower if abs(c['origin'][1]-dy) <= .65), key=lambda c: c['bbox'][0])
        a, b = ''.join(c['text'] for c in num), ''.join(c['text'] for c in den)
        if not NUMBER.fullmatch(a) or not NUMBER.fullmatch(b) or float(b.replace('−', '-')) == 0:
            continue
        ids = {c['id'] for c in num+den}
        if ids & claimed:
            continue
        nb, db = _bounds(num), _bounds(den)
        em = median(c['size'] for c in num+den)
        # Do not accept only the substring 6.0 from a nearby value 16.00.
        complete=True
        for run in (num,den):
            left,right=run[0]['bbox'][0],run[-1]['bbox'][2]
            base=median(c['origin'][1] for c in run)
            neighbors=[c for k in range(int(left//32)-1,int(right//32)+2) for c in buckets.get(k,[])
                       if c['id'] not in ids and abs(c['origin'][1]-base)<=.65]
            if any(c['text'] in '0123456789.+−-' and
                   (0<=left-c['bbox'][2]<=max(1,em*.35) or 0<=c['bbox'][0]-right<=max(1,em*.35))
                   for c in neighbors):
                complete=False; break
        if not complete:
            continue
        if (max(c['size'] for c in num+den) > min(c['size'] for c in num+den)*1.3
                or dy-ny > em*2.2 or x1-x0 > max(nb[2]-nb[0], db[2]-db[0])*3.2+1
                or abs((nb[0]+nb[2])/2-(x0+x1)/2) > max(1.5, (x1-x0)*.22)
                or abs((db[0]+db[2])/2-(x0+x1)/2) > max(1.5, (x1-x0)*.22)):
            continue
        # Require empty space at the bar itself, and exclude a text minus whose
        # glyph coincides with the vector. A numerator/denominator is a run.
        if any(c['id'] not in ids and x0 <= (c['bbox'][0]+c['bbox'][2])/2 <= x1
               and abs(c['origin'][1]-y) < em*.18 for c in nearby):
            continue
        claimed.update(ids)
        result.append({'id': f'f{min(ids)}', 'kind': 'numeric_fraction', 'numerator': a, 'denominator': b,
                       'bar': bar, 'numerator_bbox': nb, 'denominator_bbox': db,
                       'character_ids': sorted(ids), 'path_item': horizontal['items'][0],
                       'path_items': horizontal['items'],
                       'anchor': [x0, (ny+dy)/2], 'em': em,
                       'baseline_extent': [ny, dy],
                       'evidence': 'vector_bar_and_centered_numeric_runs'})
    return result


def atomic_glyphs(chars, fractions):
    consumed = {i for f in fractions for i in f['character_ids']}
    result = [c for c in chars if c['id'] not in consumed]
    by_id = {c['id']: c for c in chars}
    for f in fractions:
        base = by_id[f['character_ids'][0]]
        x, y = f['anchor']; em = f['em']
        result.append(base | {'text': f'\ue000{f["id"]}\ue001', 'origin': [x, y], 'size': em,
                              'bbox': [x, y-em*.75, f['bar'][2], y+em*.2]})
    return result


def cell_layout(chars, drawings):
    """Produce only independently supportable attachments; retain ambiguity."""
    from .script_witness import verify_script
    layout = _cell_layout(chars, drawings)
    used, errors = set(), []
    for s in layout.get('scripts', []):
        issue = verify_script(s, chars, used)
        if issue:
            errors.append({'character_ids': s['character_ids'], 'text': s['value'],
                           'reason': issue, 'rejected_claim': s})
    if errors:
        layout = _cell_layout(chars, drawings, infer_scripts=False)
        layout['script_reviews'] = errors
    return layout


def _cell_layout(chars, drawings, *, infer_scripts=True):
    fractions = detect_fractions(chars, drawings)
    if not fractions:
        return table_layout(chars, infer_scripts=infer_scripts)
    # A proper fraction immediately after a complete unsigned integer on its
    # midline is a mixed number only in a numeric context. Encode addition
    # explicitly instead of the ambiguous juxtaposition 1(1/2).
    claimed = {i for f in fractions for i in f['character_ids']}
    for f in fractions:
        if not (f['numerator'].isdigit() and f['denominator'].isdigit()
                and 0 < int(f['numerator']) < int(f['denominator'])):
            continue
        x,y = f['anchor']; em=f['em']
        left=sorted((c for c in chars if c['text'].strip() and c['id'] not in claimed
                     and abs(c['origin'][1]-y)<1 and c['bbox'][2]<=x+.1),
                    key=lambda c:c['bbox'][0])
        if not left or x-left[-1]['bbox'][2] > max(3, em*.8):
            continue
        run=[left[-1]]
        for c in reversed(left[:-1]):
            if run[0]['bbox'][0]-c['bbox'][2] > max(2,em*.35):
                break
            run.insert(0,c)
        whole=''.join(c['text'] for c in run)
        if not re.fullmatch(r'\d+',whole) or any(not .7*em<=c['size']<=1.3*em for c in run):
            continue
        f.update({'whole':whole,'integer_bbox':_bounds(run),
                  'integer_character_ids':[c['id'] for c in run]})
        f['character_ids'] += [c['id'] for c in run]
        claimed.update(c['id'] for c in run)
    layout = table_layout(atomic_glyphs(chars, fractions), infer_scripts=infer_scripts)
    # Compose both kinds of layout atoms before resolving their visible text.
    script_parts = layout.get('script_parts', [{'text': layout['text']}])
    rendered = ''.join(p['text'] if 'text' in p else '\ue002'+p['script']+'\ue003' for p in script_parts)
    by_id = {f['id']: f for f in fractions}
    scripts = {s['id']: s for s in layout.get('scripts', [])}
    parts, cursor = [], 0
    for match in re.finditer(r'\ue000(f\d+)\ue001|\ue002(s\d+)\ue003', rendered):
        if match.start() > cursor:
            parts.append({'text': rendered[cursor:match.start()]})
        parts.append({'fraction': match[1]} if match[1] else {'script': match[2]})
        cursor = match.end()
    if cursor < len(rendered):
        parts.append({'text': rendered[cursor:]})
    def value(part, notation):
        if 'text' in part:
            return part['text']
        if 'script' in part:
            return script_value(scripts[part['script']], scripts)
        f = by_id[part['fraction']]
        integer = f.get('whole','')
        return ('('+ (integer+'+' if integer else '') + f'{f["numerator"]}/{f["denominator"]})'
                if notation else integer+f['numerator']+f['denominator'])
    return {'text': ''.join(value(p, True) for p in parts),
            'source_text': ''.join(value(p, False) for p in parts),
            'linear_text': ''.join((('_{' if scripts[p['script']]['role'] == 'sub' else '^{') +
                                   _script_content(scripts[p['script']], scripts, linear=True) + '}') if 'script' in p else value(p, True) for p in parts),
            'math_parts': parts, 'fractions': fractions,
            **{k: layout[k] for k in ('scripts', 'script_reviews') if k in layout}}


def fraction_html(cell, render):
    if not cell.get('fractions') and not cell.get('scripts'):
        return render(cell['text'])
    by_id = {f['id']: f for f in cell.get('fractions', [])}
    scripts = {s['id']: s for s in cell.get('scripts', [])}
    output = []
    for part in cell.get('math_parts', cell.get('script_parts', [])):
        if 'text' in part:
            output.append(render(part['text']))
            continue
        if 'script' in part:
            output.append(script_html(scripts[part['script']], scripts))
            continue
        f = by_id[part['fraction']]
        geometry = {k: f[k] for k in ('bar', 'numerator_bbox', 'denominator_bbox')}
        if 'whole' in f:
            geometry['integer_bbox'] = f['integer_bbox']
        attrs = html.escape(json.dumps(geometry, separators=(',', ':')), quote=True)
        ratio = html.escape(f'{f["numerator"]}/{f["denominator"]}', quote=True)
        whole = f.get('whole')
        whole_attr = f' data-mixed-integer="{whole}"' if whole is not None else ''
        mixed = (f'<span class="mixed-integer">{whole}</span><span class="math-generated">+</span>'
                 if whole is not None else '')
        output.append(f'<span class="numeric-fraction" data-fraction="{ratio}"{whole_attr} data-source-geometry="{attrs}" '
                      'style="white-space:nowrap">'
                      '<span class="math-generated">(</span>'
                      + mixed +
                      f'<span class="fraction-numerator">{html.escape(f["numerator"])}</span>'
                      '<span class="math-generated">/</span>'
                      f'<span class="fraction-denominator">{html.escape(f["denominator"])}</span>'
                      '<span class="math-generated">)</span></span>')
    result = ''.join(output)
    # A script atom may split a unit token (m + superscript 2). Preserve the
    # complete unit AST, rather than parsing only the remaining text pieces.
    from .units import cell_unit
    unit = cell_unit(cell['text'])
    if unit:
        expression = html.escape(unit['ascii_expression'], quote=True)
        ast = html.escape(json.dumps(unit['ast'], ensure_ascii=False, separators=(',', ':')), quote=True)
        area = html.escape(json.dumps(unit['range']), quote=True)
        result = (f'<span class="unit-expression" data-unit="{expression}" data-unit-ast="{ast}" '
                  f'data-unit-range="{area}">{result}</span>')
    return result
