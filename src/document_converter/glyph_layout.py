"""Local baseline layout independent of PDF font-size scale conventions."""
from __future__ import annotations

from collections import defaultdict
from statistics import median
import re
import unicodedata

SUPER = str.maketrans('0123456789+-−=()', '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾')
SUB = str.maketrans('0123456789+-−=()', '₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎')
FOOTNOTES = set('①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳')


def effective_em(char: dict) -> float:
    """CJK PDFs can report twice the em of adjacent Latin at one baseline.

    Use a CJK glyph's advance-sized box only when its PDF size disagrees.
    Latin width is deliberately not used: i and W are the same font size.
    """
    size = max(.1, char['size'])
    width = char['bbox'][2] - char['bbox'][0]
    if (char['text'] and unicodedata.east_asian_width(char['text'][0]) in {'W', 'F'}
            and 0 < width < size * .75):
        return max(size * .5, width)
    return size


def table_text_lines(chars: list[dict]) -> list[str]:
    return table_layout(chars)['text'].split('\n') if chars else []


def table_layout(chars: list[dict], *, infer_scripts=True) -> dict:
    """Keep script attachment evidence as well as searchable Unicode text.

    Baseline grouping precedes size comparisons. Runs include letters and
    internal spaces (e.g. ``e min``); a suffix on the same baseline is never
    promoted to a script. All decisions use original glyphs, not token names.
    """
    if not chars:
        return {'text': ''}
    if sum(abs(c['direction'][0]) > .9 for c in chars) < len(chars) * .7:
        groups = defaultdict(list)
        for c in chars:
            groups[(c['block'], c['line'])].append(c)
        return {'text': '\n'.join(_render(g, {}) for g in groups.values())}

    # First cluster ALL fonts by baseline. Small Latin is not a script merely
    # because the Chinese font's numeric size happens to be twice as large.
    groups = []
    for c in sorted(chars, key=lambda c: (c['origin'][1], c['bbox'][0], c['id'])):
        em = effective_em(c)
        eligible = [(abs(c['origin'][1] - g['baseline']), i) for i, g in enumerate(groups)
                    if abs(c['origin'][1] - g['baseline']) <= max(.5, min(1.0, min(em, g['em']) * .12))]
        if eligible:
            group = groups[min(eligible)[1]]
            group['chars'].append(c)
            group['baseline'] = median(v['origin'][1] for v in group['chars'])
            group['em'] = median(effective_em(v) for v in group['chars'])
        else:
            groups.append({'chars': [c], 'baseline': c['origin'][1], 'em': em})

    roles = {}
    scripts, reviews = [], []
    # Decimal tolerances are two-dimensional annotations, not fragments of an
    # integer exponent. Keep the complete signed decimal and its aligned lower
    # bound together on their original physical lines.
    protected = set()
    for gi, group in enumerate(groups):
        raw = ''.join(c['text'] for c in sorted(group['chars'], key=lambda c: c['bbox'][0]))
        if not re.search(r'\d\s*\.\s*\d', raw):
            continue
        protected.add(gi)
        left = min(c['bbox'][0] for c in group['chars'])
        for hi, other in enumerate(groups):
            value = ''.join(c['text'] for c in other['chars']).strip()
            other_left = min(c['bbox'][0] for c in other['chars'])
            if (re.fullmatch(r'[+−-]?\s*\d+', value) and other['em'] <= group['em'] * 1.25
                    and abs(other_left - left) <= max(5, group['em'])
                    and abs(other['baseline'] - group['baseline']) <= group['em'] * 2):
                protected.add(hi)
    # Script runs must be locally smaller, vertically displaced and immediately
    # to the right of a base glyph. Equal-size fraction rows stay separate.
    for gi, group in enumerate(groups):
        if not infer_scripts or gi in protected:
            continue
        ordered = sorted(group['chars'], key=lambda c: (c['bbox'][0], c['id']))
        runs = []
        for c in ordered:
            if (not runs or c['bbox'][0] - runs[-1][-1]['bbox'][2] > max(2.5, effective_em(c) * .55)
                    or max(effective_em(c), effective_em(runs[-1][-1])) >
                       min(effective_em(c), effective_em(runs[-1][-1])) * 1.25
                    or (c['text'] in FOOTNOTES) != (runs[-1][-1]['text'] in FOOTNOTES)):
                runs.append([c])
            else:
                runs[-1].append(c)
        for run in runs:
            nonspace = [c for c in run if c['text'].strip()]
            annotation = all(c['text'] in FOOTNOTES for c in nonspace)
            if not nonspace or any('\ue000' in c['text'] for c in nonspace):
                continue
            first = nonspace[0]
            run_em = max(effective_em(c) for c in nonspace)
            candidates = []
            for hi, host in enumerate(groups):
                if hi == gi:
                    continue
                for base in host['chars']:
                    if (not base['text'].strip() or base['text'] in FOOTNOTES
                            or not (base['text'][-1].isalnum() or base['text'] in ')]）］'
                                    or unicodedata.category(base['text'][-1]).startswith('S'))):
                        continue
                    base_em = effective_em(base)
                    shift = median(c['origin'][1] for c in nonspace) - base['origin'][1]
                    if run_em >= base_em * .84 or not max(.65, base_em * .12) <= abs(shift) <= base_em * .65:
                        continue
                    side = 'right' if first['bbox'][0] >= base['bbox'][0] else 'left'
                    # Operators can be geometrically adjacent to a left script
                    # while belonging to the expression around the true base.
                    # Keep those cases for the independent recall review.
                    if side == 'left' and not base['text'][-1].isalnum():
                        continue
                    boundary_gap = (first['bbox'][0]-base['bbox'][2] if side == 'right'
                                    else base['bbox'][0]-nonspace[-1]['bbox'][2])
                    gap = max(0, boundary_gap)
                    if (boundary_gap >= -base_em*.25 and gap <= max(3.0, base_em*.65)
                            and nonspace[-1]['bbox'][2]-first['bbox'][0] <= max(12, base_em*5)):
                        candidates.append((gap, abs(shift) / base_em, hi, shift, base, side))
            if candidates:
                # Prefer a proven postscript attachment to interpreting an
                # inter-token script as a prescript of the following token.
                if any(v[5] == 'right' for v in candidates):
                    candidates = [v for v in candidates if v[5] == 'right']
                candidates.sort(key=lambda v: (v[0], v[1], v[2], v[4]['id']))
                _, _, host_index, shift, base, side = candidates[0]
                # A nearly tied base on a different physical line is ambiguous.
                if any(v[2] != host_index and abs(v[0]-candidates[0][0]) < .3
                       and abs(v[1]-candidates[0][1]) < .08 for v in candidates[1:]):
                    reviews.append({'character_ids': [c['id'] for c in nonspace],
                                    'text': ''.join(c['text'] for c in nonspace),
                                    'reason': 'competing_local_baselines'})
                    continue
                if annotation and side == 'left':
                    continue
                for c in run:
                    roles[c['id']] = 'annotation' if annotation else 'sup' if shift < 0 else 'sub'
                # Capture the contiguous base token, not only its final glyph.
                bases = [base]
                for previous in sorted((c for c in groups[host_index]['chars']
                                        if side == 'right' and c['bbox'][0] < base['bbox'][0]),
                                       key=lambda c: c['bbox'][0], reverse=True):
                    if (not previous['text'].isalnum() or
                            abs(previous['origin'][1]-base['origin'][1]) > .65 or
                            effective_em(previous) < effective_em(base)*.84 or
                            (base['text'].isalpha() and previous['text'].isdigit()) or
                            (base['text'].isalpha() and previous['text'].isalpha() and
                             base['text'].isascii() != previous['text'].isascii()) or
                            bases[0]['bbox'][0]-previous['bbox'][2] > max(1, effective_em(base)*.22)):
                        break
                    bases.insert(0, previous)
                if side == 'left':
                    for following in sorted((c for c in groups[host_index]['chars']
                                             if c['bbox'][0] > base['bbox'][0]),
                                            key=lambda c: c['bbox'][0]):
                        previous = bases[-1]
                        if (not following['text'].isalnum() or
                                abs(following['origin'][1]-base['origin'][1]) > .65 or
                                effective_em(following) < effective_em(base)*.84 or
                                (base['text'].isalpha() and following['text'].isdigit()) or
                                (base['text'].isalpha() and following['text'].isalpha() and
                                 base['text'].isascii() != following['text'].isascii()) or
                                following['bbox'][0]-previous['bbox'][2] > max(1, effective_em(base)*.22)):
                            break
                        bases.append(following)
                value = ''.join(c['text'] for c in run).strip()
                role = roles[first['id']]
                decorated = ('^{' if role in ('sup', 'annotation') else '_{') + value + '}'
                base_text = ''.join(c['text'] for c in bases)
                scripts.append({'id': f's{first["id"]}', 'role': role,
                                'base': base_text, 'value': value, 'side': side,
                                'base_character_ids': [c['id'] for c in bases],
                                'character_ids': [c['id'] for c in run],
                                'base_bbox': _bounds(bases), 'bbox': _bounds(run),
                                'base_em': effective_em(base), 'script_em': run_em,
                                'baseline_shift': shift,
                                'linear': base_text + decorated if side == 'right' else decorated + base_text,
                                'evidence': f'local_smaller_run_displaced_{side}_of_base'})
    by_id = {s['id']: s for s in scripts}
    owners = {i: s['id'] for s in scripts for i in s['character_ids']}
    glyphs = {c['id']: c for c in chars}
    id_group = {c['id']: gi for gi, g in enumerate(groups) for c in g['chars']}
    children = defaultdict(list)
    for s in scripts:
        parent = owners.get(s['base_character_ids'][-1])
        if parent:
            s['parent_id'] = parent
            children[parent].append(s)
    # Size strictly decreases along each edge, preventing attachment cycles.
    # Build nested content before the physical line representation is flattened.
    for s in sorted(scripts, key=lambda v: v['script_em']):
        if not children[s['id']]:
            continue
        local = [glyphs[i] for i in s['character_ids']]
        local += [glyphs[c['character_ids'][0]] for c in children[s['id']]]
        firsts = {c['character_ids'][0]: c for c in children[s['id']]}
        raw = _render(local, roles, firsts, set(firsts))
        s['content_parts'] = _parts(raw)
    firsts = {s['character_ids'][0]: s for s in scripts if 'parent_id' not in s}
    render_groups = [dict(g, chars=[c for c in g['chars'] if c['id'] not in owners]) for g in groups]
    for s in firsts.values():
        render_groups[id_group[s['base_character_ids'][-1]]]['chars'].append(glyphs[s['character_ids'][0]])
    lines = [_render(g['chars'], roles, firsts, set(firsts))
             for g in sorted(render_groups, key=lambda g: g['baseline']) if g['chars']]
    rendered = '\n'.join(_join_unit_wraps([line for line in lines if line.strip()]))
    if not scripts:
        return {'text': rendered, **({'script_reviews': reviews} if reviews else {})}
    parts = _parts(rendered)
    text = ''.join(p['text'] if 'text' in p else script_value(by_id[p['script']], by_id) for p in parts)
    linear = ''.join(p['text'] if 'text' in p else
                     ('_{' if by_id[p['script']]['role'] == 'sub' else '^{') +
                     _script_content(by_id[p['script']], by_id, linear=True) + '}' for p in parts)
    return {'text': text, 'linear_text': linear, 'scripts': scripts, 'script_parts': parts,
            **({'script_reviews': reviews} if reviews else {})}


def _bounds(chars):
    return [min(c['bbox'][0] for c in chars), min(c['bbox'][1] for c in chars),
            max(c['bbox'][2] for c in chars), max(c['bbox'][3] for c in chars)]


def _parts(rendered):
    parts, cursor = [], 0
    for match in re.finditer(r'\ue002(s\d+)\ue003', rendered):
        if match.start() > cursor:
            parts.append({'text': rendered[cursor:match.start()]})
        parts.append({'script': match[1]})
        cursor = match.end()
    if cursor < len(rendered):
        parts.append({'text': rendered[cursor:]})
    return parts


def _script_content(script, scripts, linear=False):
    if not script.get('content_parts'):
        return script['value']
    result = ''
    for p in script['content_parts']:
        if 'text' in p:
            result += p['text']
        else:
            child = scripts[p['script']]
            result += (('_{' if child['role'] == 'sub' else '^{') +
                       _script_content(child, scripts, linear=True) + '}' if linear
                       else script_value(child, scripts))
    return result


def script_value(script, scripts=None):
    if script.get('content_parts'):
        return _script_content(script, scripts)
    value = script['value']
    # Do not mix half-converted numerals with baseline letters/symbols.
    if script['role'] == 'annotation' or not all(c in '0123456789+-−=() ' for c in value):
        return value
    return value.translate(SUPER if script['role'] == 'sup' else SUB)


def script_html(script, scripts=None):
    import html
    import json
    value = script_value(script, scripts)
    attrs = html.escape(json.dumps(script, ensure_ascii=False, separators=(',', ':')), quote=True)
    escaped = html.escape(value, quote=False)
    # Unicode numerals already carry the vertical position. Letters have no
    # complete Unicode script alphabet; use semantic HTML for the entire run.
    if script.get('content_parts'):
        escaped = ''.join(html.escape(p['text'], quote=False) if 'text' in p else
                          script_html(scripts[p['script']], scripts) for p in script['content_parts'])
    if script['role'] in ('sup', 'sub') and (value == script['value'] or script.get('content_parts')):
        escaped = f'<{script["role"]}>{escaped}</{script["role"]}>'
    return f'<span class="script-run" data-script-role="{script["role"]}" data-script-evidence="{attrs}">{escaped}</span>'


def _render(chars: list[dict], roles: dict[int, str], firsts=None, members=None) -> str:
    ordered = sorted(chars, key=lambda c: (c['bbox'][0], c['id']))
    widths = [c['bbox'][2] - c['bbox'][0] for c in ordered if c['text'].strip()]
    threshold = max(2.0, min(4.0, median(widths) * .4)) if widths else 2.0
    result = ''
    previous = None
    for c in ordered:
        if members and c['id'] in members and c['id'] not in firsts:
            continue
        role = roles.get(c['id'])
        if previous and not role and c['bbox'][0] - previous['bbox'][2] > threshold:
            result += ' '
        if firsts and c['id'] in firsts:
            script = firsts[c['id']]
            result += '\ue002' + script['id'] + '\ue003'
            previous = {'id': c['id'], 'bbox': script['bbox']}
            continue
        value = c['text']
        result += value.translate(SUPER if role == 'sup' else SUB) if role else value
        previous = c
    result = re.sub(r'(?<=\d)\s*\.\s*(?=\d)', '.', result.strip())
    # Normalize layout spaces, retaining the actual source operator. Replacing
    # a printed x/X with × changes source glyphs and violates strict fidelity.
    result = re.sub(r'(\d)\s*([×xX])\s*10(?=\ue002)',
                    lambda m: m[1] + m[2] + '10', result)
    return re.sub(r'(\d)\s*([×xX])\s*10([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)',
                  lambda m: m[1] + m[2] + '10' + m[3], result)


def _join_unit_wraps(lines: list[str]) -> list[str]:
    """Join only explicit division continuations, preserving every glyph.

    A trailing slash followed by a parenthesised denominator is unambiguous.
    Other physical wraps remain visible; do not guess implicit multiplication.
    """
    result = []
    for line in lines:
        if result and result[-1].rstrip().endswith('/') and line.lstrip().startswith(('(', '（')):
            result[-1] += line
        else:
            result.append(line)
    return result
