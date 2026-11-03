"""Independent source witnesses for script roles and their base attachment.

No imports from the layout/recognition modules. Unicode folding alone cannot
detect a flattened or inverted script, so HTML and raw PDF geometry are checked.
"""
from __future__ import annotations

import json
import math
import unicodedata
from statistics import median

_SUPER = str.maketrans('0123456789+-−=()', '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾')
_SUB = str.maketrans('0123456789+-−=()', '₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎')
_NOTES = set('①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳')


def _em(c):
    size = max(.1, c['size'])
    width = c['bbox'][2]-c['bbox'][0]
    return max(size*.5, width) if (c['text'] and unicodedata.east_asian_width(c['text'][0]) in ('W', 'F')
                                         and 0 < width < size*.75) else size


def _box(chars):
    return [min(c['bbox'][0] for c in chars), min(c['bbox'][1] for c in chars),
            max(c['bbox'][2] for c in chars), max(c['bbox'][3] for c in chars)]


def read_scripts(node):
    claims, errors = [], []
    spans = node.select('.script-run')
    raw = {}
    for span in spans:
        try:
            s = json.loads(span['data-script-evidence'])
            if s['id'] in raw:
                raise ValueError('duplicate id')
            raw[s['id']] = s
        except (KeyError, TypeError, ValueError):
            errors.append('invalid or duplicate script evidence')
    def content(s, seen):
        if s['id'] in seen or len(seen) > 32:
            raise ValueError('script cycle or excessive depth')
        if not s.get('content_parts'):
            value = s['value']
            return (value if s['role'] == 'annotation' or not all(c in '0123456789+-−=() ' for c in value)
                    else value.translate(_SUPER if s['role'] == 'sup' else _SUB))
        result = ''
        for p in s['content_parts']:
            if set(p) == {'text'}:
                result += p['text']
            elif set(p) == {'script'}:
                child = raw[p['script']]
                if child.get('parent_id') != s['id']:
                    raise ValueError('child parent')
                result += content(child, seen | {s['id']})
            else:
                raise ValueError('content part')
        return result
    for span in spans:
        try:
            s = json.loads(span['data-script-evidence'])
            if s['role'] not in ('sub', 'sup', 'annotation') or span.get('data-script-role') != s['role']:
                raise ValueError('role')
            if s.get('side', 'right') not in ('left', 'right'):
                raise ValueError('side')
            for name in ('bbox', 'base_bbox'):
                if len(s[name]) != 4 or any(type(v) not in (float, int) or not math.isfinite(v) for v in s[name]):
                    raise ValueError('box')
            for name in ('character_ids', 'base_character_ids'):
                if not s[name] or any(type(v) is not int for v in s[name]) or len(set(s[name])) != len(s[name]):
                    raise ValueError('ids')
            for name in ('base_em', 'script_em', 'baseline_shift'):
                if type(s[name]) not in (float, int) or not math.isfinite(s[name]):
                    raise ValueError('metric')
            if not isinstance(s['value'], str) or not isinstance(s['base'], str):
                raise ValueError('text')
            value = s['value']
            expected = content(s, set())
            if span.get_text() != expected:
                raise ValueError('visible script text')
            wrapped = s['role'] in ('sub', 'sup') and (expected == value or bool(s.get('content_parts')))
            direct = [c for c in span.contents if not isinstance(c, str) or c]
            if wrapped and (len(direct) != 1 or getattr(direct[0], 'name', None) != s['role']):
                raise ValueError('script element')
            container = direct[0] if wrapped else span
            actual = []
            for c in container.contents:
                if isinstance(c, str):
                    actual.append({'text': str(c)})
                elif c.name == 'span' and c.get('class') == ['script-run']:
                    actual.append({'script': json.loads(c['data-script-evidence'])['id']})
                else:
                    raise ValueError('untracked script child')
            wanted = s.get('content_parts', [{'text': expected}])
            if actual != wanted:
                raise ValueError('nested script content')
            parent = span.find_parent(class_='script-run')
            parent_id = json.loads(parent['data-script-evidence'])['id'] if parent else None
            if s.get('parent_id') != parent_id:
                raise ValueError('HTML parent differs from evidence')
            decorated = ('_{' if s['role'] == 'sub' else '^{') + s['value'] + '}'
            linear = s['base'] + decorated if s.get('side', 'right') == 'right' else decorated + s['base']
            if s['linear'] != linear:
                raise ValueError('linear expression')
            claims.append(s)
        except (KeyError, TypeError, ValueError):
            errors.append('invalid script HTML, visible value or evidence')
    # Untracked tags are not accepted as evidence-backed script output.
    for tag in node.select('sub, sup'):
        if not tag.find_parent(class_='script-run'):
            errors.append('untracked script element')
    return claims, errors


def verify_script(s, chars, used):
    by_id = {c['id']: c for c in chars}
    try:
        base = [by_id[i] for i in s['base_character_ids']]
        run = [by_id[i] for i in s['character_ids']]
        if set(s['base_character_ids']) & set(s['character_ids']):
            return 'script consumes its own base'
        if any(c['id'] in used for c in run):
            return 'script glyph claimed more than once'
        if ''.join(c['text'] for c in base) != s['base'] or ''.join(c['text'] for c in run).strip() != s['value']:
            return 'script/base text differs from source glyphs'
        if any(abs(a-b) > .02 for name, group in (('base_bbox', base), ('bbox', run))
               for a, b in zip(_box(group), s[name])):
            return 'script/base box differs from source glyphs'
        side = s.get('side', 'right')
        if side not in ('left', 'right'):
            return 'invalid script side'
        last = base[-1] if side == 'right' else base[0]
        nonspace = [c for c in run if c['text'].strip()]
        em = _em(last)
        small = max(_em(c) for c in nonspace)
        shift = median(c['origin'][1] for c in nonspace)-median(c['origin'][1] for c in base)
        if small >= .84*em or not max(.65, .12*em)-.05 <= abs(shift) <= .65*em+.05:
            return 'source size/baseline does not support script role'
        if (s['role'] == 'sub' and shift <= 0) or (s['role'] in ('sup', 'annotation') and shift >= 0):
            return 'source baseline contradicts claimed script direction'
        if s['role'] == 'annotation' and not all(c in _NOTES for c in s['value']):
            return 'annotation is not a circled source note'
        if s['role'] != 'annotation' and any(v in _NOTES for v in s['value']):
            return 'circled note incorrectly treated as exponent/subscript'
        first = nonspace[0]
        near = nonspace[0] if side == 'right' else nonspace[-1]
        gap = first['bbox'][0]-last['bbox'][2] if side == 'right' else last['bbox'][0]-near['bbox'][2]
        misplaced = first['bbox'][0] <= last['bbox'][0] if side == 'right' else near['bbox'][2] >= last['bbox'][2]
        if misplaced or gap < -em*.25-.02 or max(0, gap) > max(3, em*.65)+.02:
            return 'script is not adjacent to its source base'
        if any(abs(c['origin'][1]-last['origin'][1]) > 1.05 for c in base):
            return 'claimed base token spans different baselines'
        # An intervening baseline glyph proves attachment to an earlier token
        # is wrong, even when all characters and counts remain unchanged.
        excluded = set(s['character_ids']) | set(s['base_character_ids'])
        def between(c):
            return (last['bbox'][2]+.02 < c['bbox'][0] < first['bbox'][0]-.02 if side == 'right' else
                    near['bbox'][2]+.02 < c['bbox'][0] < last['bbox'][0]-.02)
        if any(c['id'] not in excluded and c['text'].strip() and between(c) and
               abs(c['origin'][1]-last['origin'][1]) < .6 for c in chars):
            return 'script skips an intervening base glyph'
        if (abs(s['base_em']-em) > .02 or abs(s['script_em']-small) > .02 or
                abs(s['baseline_shift']-shift) > .55):
            return 'declared script metrics differ from source'
    except (KeyError, TypeError, ValueError, IndexError):
        return 'invalid script source claim'
    used.update(c['id'] for c in run)
    return None


def linear_cell(node):
    """Read semantic plain text BEFORE stripping HTML tags for LLM input.

    For example d<sub>k</sub> becomes d_{k}; nested roles recurse. This must
    follow evidence validation: ordinary get_text() necessarily loses roles.
    """
    claims, errors = read_scripts(node)
    if errors:
        return None, errors
    by_id = {s['id']: s for s in claims}
    def script(s):
        inside = ''.join(p['text'] if 'text' in p else script(by_id[p['script']])
                         for p in s.get('content_parts', [{'text': s['value']}]))
        return ('_{' if s['role'] == 'sub' else '^{') + inside + '}'
    def visit(tag):
        if isinstance(tag, str):
            return str(tag)
        if tag.get('class') == ['script-run']:
            return script(by_id[json.loads(tag['data-script-evidence'])['id']])
        if tag.name == 'br':
            return '\n'
        return ''.join(visit(c) for c in tag.contents)
    return visit(node), []


def uncovered_candidates(chars, claimed):
    """Independent recall audit. Ambiguous geometry is review, never a fix.

    Includes decimal tolerances and unsupported symbolic runs intentionally:
    they need specialized evidence instead of disappearing from the audit.
    """
    result = []
    bins = {}
    for base in chars:
        key = (int(base['bbox'][0]//24), int(base['origin'][1]//24))
        bins.setdefault(key, []).append(base)
    reach = math.ceil(max((_em(c) for c in chars), default=8)*2/24)+1
    for c in chars:
        if (c['id'] in claimed or not c['text'].strip() or abs(c['direction'][0]) < .9
                or not any(v.isalnum() or unicodedata.category(v).startswith('S') or v in _NOTES for v in c['text'])):
            continue
        choices = []
        bx, by = int(c['bbox'][0]//24), int(c['origin'][1]//24)
        nearby = (base for x in range(bx-reach, bx+reach+1) for y in range(by-reach, by+reach+1)
                  for base in bins.get((x, y), []))
        for base in nearby:
            if (not base['text'].strip() or not (base['text'][-1].isalnum() or
                                               base['text'][-1] in ')]）］}』」' or
                                               unicodedata.category(base['text'][-1]).startswith('S'))):
                continue
            em, small = _em(base), _em(c)
            shift = c['origin'][1]-base['origin'][1]
            gap = max(0, c['bbox'][0]-base['bbox'][2], base['bbox'][0]-c['bbox'][2])
            if (small < .84*em and max(.65, .12*em) <= abs(shift) <= em*1.25
                    and gap <= max(3, em*.65)):
                placement = ('after' if c['bbox'][0] >= base['bbox'][2]-.5 else
                             'before' if c['bbox'][2] <= base['bbox'][0]+.5 else 'overlapping')
                choices.append((gap, abs(shift)/em, base['id'], placement))
        if choices:
            result.append({'character_id': c['id'], 'text': c['text'], 'bbox': c['bbox'],
                           'possible_base_id': min(choices)[2], 'placement': min(choices)[3],
                           'reason': 'unclaimed_displaced_small_glyph'})
    return result
