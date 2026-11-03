"""Add script semantics to prose without changing its established text order."""
from __future__ import annotations

import html
import unicodedata

from .glyph_layout import table_layout, script_html, script_value, effective_em
from .script_witness import verify_script

_FOLD = str.maketrans('⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎', '0123456789+-=()0123456789+-=()')


def key(text):
    value = ''.join(c for c in unicodedata.normalize('NFC', text) if not c.isspace()).translate(_FOLD)
    return ''.join(chr(ord(c)-0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else '-' if c == '−' else c for c in value)


def annotate_body(blocks, chars):
    by_id = {c['id']: c for c in chars}
    for i, block in enumerate(blocks):
        original = [by_id[v] for v in block['character_ids']]
        sizes = [effective_em(c) for c in original if c['text'].strip()]
        if not sizes or min(sizes) >= max(sizes)*.84:
            continue
        layout = table_layout(original)
        scripts = layout.get('scripts', [])
        if not scripts:
            continue
        used = set()
        issues = [issue for s in scripts if (issue := verify_script(s, original, used))]
        if issues or key(layout['text']) != key(block['text']):
            block['script_reviews'] = [{'reason': issue} for issue in issues] or [{'reason': 'body_script_order_requires_review'}]
            continue
        text = block['text']
        positions = [index for index, c in enumerate(text) if key(c)]
        if len(positions) != len(key(text)):
            block['script_reviews'] = [{'reason': 'body_unicode_alignment_requires_review'}]
            continue
        lookup = {s['id']: s for s in scripts}
        changes, cursor = [], 0
        for part in layout['script_parts']:
            if 'text' in part:
                cursor += len(key(part['text']))
                continue
            s = lookup[part['script']]
            length = len(key(script_value(s, lookup)))
            if length:
                changes.append((positions[cursor], positions[cursor+length-1]+1, script_html(s, lookup)))
            cursor += length
        result, start = '', 0
        for lo, hi, replacement in changes:
            result += html.escape(text[start:lo], quote=False)+replacement
            start = hi
        result += html.escape(text[start:], quote=False)
        block.update({'html_text': result, 'scripts': scripts,
                      'body_id': f'b{i}', 'linear_text': layout['linear_text']})
    return blocks


def body_html(block, heading):
    if block.get('toc'):
        from .printed_toc import render_html
        return render_html(block['toc'])
    if not block.get('scripts'):
        return block['text'] if block.get('printed_contents') else heading(block['text'])
    return (f'<!-- BODY_SCRIPT:{block["body_id"]} -->\n'+block['html_text']+
            '\n<!-- /BODY_SCRIPT -->')
