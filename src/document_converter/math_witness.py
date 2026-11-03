"""Read derived fraction notation and independently verify its PDF evidence.

This module does not import the converter's fraction recognition or layout.
Only explicitly validated punctuation is excluded from source glyph counts.
"""
from __future__ import annotations

import json
import math
import re


def read_cell(node):
    display = node.get_text()
    fractions, errors = [], []
    for span in node.select('span.numeric-fraction'):
        children = [c for c in span.contents if not isinstance(c, str) or c.strip()]
        mixed = span.get('data-mixed-integer')
        classes = ['math-generated'] + (['mixed-integer','math-generated'] if mixed is not None else []) + ['fraction-numerator', 'math-generated',
                   'fraction-denominator', 'math-generated']
        valid = (len(children) == len(classes) and all(getattr(c, 'name', None) == 'span'
                 and c.get('class') == [kind] and not c.find(True)
                 for c, kind in zip(children, classes)))
        if valid:
            offset=2 if mixed is not None else 0
            a, b = children[1+offset].get_text(), children[3+offset].get_text()
            valid = ([children[i].get_text() for i in range(0,len(children),2)] ==
                     (['(', '+', '/', ')'] if mixed is not None else ['(', '/', ')'])
                     and bool(re.fullmatch(r'[+−-]?\d+(?:\.\d+)?', a))
                     and bool(re.fullmatch(r'[+−-]?\d+(?:\.\d+)?', b))
                     and span.get('data-fraction') == a+'/'+b)
            if mixed is not None:
                valid = (valid and bool(re.fullmatch(r'\d+',mixed)) and children[1].get_text()==mixed
                         and a.isdigit() and b.isdigit() and 0<int(a)<int(b))
        if not valid:
            errors.append('invalid fraction children or declared ratio')
            continue
        try:
            geometry = json.loads(span['data-source-geometry'])
            if set(geometry) != {'bar', 'numerator_bbox', 'denominator_bbox'} | ({'integer_bbox'} if mixed is not None else set()):
                raise ValueError('geometry keys')
            if any(not isinstance(v, list) or len(v) != 4 or
                   any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in v)
                   for v in geometry.values()):
                raise ValueError('geometry coordinates')
            if float(b.replace('−', '-')) == 0:
                raise ValueError('zero denominator')
        except (ValueError, TypeError, KeyError):
            errors.append('invalid fraction geometry or denominator')
            continue
        fractions.append({'numerator': a, 'denominator': b, **geometry,
                          **({'whole':mixed} if mixed is not None else {})})
        for i in range(0,len(children),2):
            children[i].decompose()
    if node.select('.math-generated'):
        errors.append('unaccounted generated mathematical punctuation')
    return node.get_text(), display, fractions, errors


def verify_fraction(fraction, chars, drawings, used):
    """Validate given claims against raw source glyphs and raw vector items."""
    bar = fraction['bar']
    x0, y, x1, y1 = bar
    if x0 >= x1 or abs(y-y1) > .01:
        return 'invalid horizontal bar'
    segments = []
    for path in drawings:
        for item in path['items']:
            if item[0] == 'l':
                a, b = item[1:]
                if (abs(a.x-b.x) < .3 and abs(a.y-b.y) >= 2
                        and min(abs(a.x-x0), abs(a.x-x1)) < .6
                        and min(a.y,b.y)-.6 <= y <= max(a.y,b.y)+.6):
                    return 'claimed fraction bar connects to a grid edge'
                if abs(a.y-b.y) <= .25:
                    segments.append([min(a.x,b.x), (a.y+b.y)/2, max(a.x,b.x), (a.y+b.y)/2])
            elif item[0] == 're':
                r = item[1]
                if (r.width<=.7 and r.height>=2
                        and min(abs((r.x0+r.x1)/2-x0),abs((r.x0+r.x1)/2-x1))<.6
                        and r.y0-.6<=y<=r.y1+.6):
                    return 'claimed fraction connects to rectangular grid edge'
                if r.height <= .7:
                    segments.append([r.x0, (r.y0+r.y1)/2, r.x1, (r.y0+r.y1)/2])
    connected=[s for s in segments if abs(s[1]-y)<=.25 and s[2]>=x0-.5 and s[0]<=x1+.5]
    if not connected:
        return 'fraction bar absent from source PDF'
    lo,hi=x0,x1
    while True:
        touching=[s for s in segments if abs(s[1]-y)<=.25 and s[2]>=lo-.5 and s[0]<=hi+.5]
        extent=(min([lo]+[s[0] for s in touching]),max([hi]+[s[2] for s in touching]))
        if extent==(lo,hi):break
        lo,hi=extent
    if abs(lo-x0)>.05 or abs(hi-x1)>.05:
        return 'claimed fraction is only a fragment of a longer collinear rule'
    intervals=sorted((s[0],s[2]) for s in connected)
    if abs(intervals[0][0]-x0)>.05 or abs(max(b for a,b in intervals)-x1)>.05:
        return 'source vector coverage does not match fraction bar'
    end=intervals[0][1]
    for a,b in intervals[1:]:
        if a>end+.5:return 'gap in claimed fraction vector'
        end=max(end,b)
    runs = []
    for label in ('numerator', 'denominator'):
        box = fraction[label+'_bbox']
        run = sorted((c for c in chars if c['text'].strip()
                      and box[0]-.02 <= (c['bbox'][0]+c['bbox'][2])/2 <= box[2]+.02
                      and box[1]-.02 <= (c['bbox'][1]+c['bbox'][3])/2 <= box[3]+.02),
                     key=lambda c:c['bbox'][0])
        if not run or ''.join(c['text'] for c in run) != fraction[label]:
            return label+' does not match source glyphs'
        if any(c['id'] in used for c in run):
            return 'source fraction glyphs claimed more than once'
        if any(c['bbox'][0] < x0-1 or c['bbox'][2] > x1+1 for c in run):
            return label+' outside vector bar projection'
        center = (min(c['bbox'][0] for c in run)+max(c['bbox'][2] for c in run))/2
        if abs(center-(x0+x1)/2) > max(1.6, (x1-x0)*.23):
            return label+' not centered on vector bar'
        runs.append(run)
        baseline=sum(c['origin'][1] for c in run)/len(run)
        em=sum(c['size'] for c in run)/len(run)
        left,right=run[0]['bbox'][0],run[-1]['bbox'][2]
        ids={c['id'] for c in run}
        if any(c['id'] not in ids and c['text'] in '0123456789.+−-'
               and abs(c['origin'][1]-baseline)<=.65
               and (0<=left-c['bbox'][2]<=max(1,em*.35) or 0<=c['bbox'][0]-right<=max(1,em*.35))
               for c in chars):
            return label+' is only part of a complete source number'
    if not max(c['origin'][1] for c in runs[0]) < y < min(c['origin'][1] for c in runs[1]):
        return 'fraction numerator/denominator vertical order invalid'
    if 'whole' in fraction:
        box=fraction['integer_bbox']
        integer=sorted((c for c in chars if c['text'].strip()
                        and box[0]-.02<=(c['bbox'][0]+c['bbox'][2])/2<=box[2]+.02
                        and box[1]-.02<=(c['bbox'][1]+c['bbox'][3])/2<=box[3]+.02),key=lambda c:c['bbox'][0])
        baseline=(sum(c['origin'][1] for c in runs[0])/len(runs[0])+
                  sum(c['origin'][1] for c in runs[1])/len(runs[1]))/2
        em=sum(c['size'] for run in runs for c in run)/sum(map(len,runs))
        if (''.join(c['text'] for c in integer)!=fraction['whole'] or not integer
                or not -.1<=x0-max(c['bbox'][2] for c in integer)<=max(3,em*.8)
                or any(abs(c['origin'][1]-baseline)>=1 or c['id'] in used for c in integer)):
            return 'mixed integer does not match adjacent source baseline/position'
        runs.append(integer)
    used.update(c['id'] for run in runs for c in run)
    return None
