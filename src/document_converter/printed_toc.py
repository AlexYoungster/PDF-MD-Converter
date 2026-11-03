"""Printed contents reconstructed from glyph geometry, never PDF block order.

Leaders are decoration, not title punctuation. Their complete source spelling
is stored in the HTML and JSON while a compact ellipsis is displayed. Titles,
printed page labels and their associations remain independently inspectable.
"""
from __future__ import annotations

from collections import Counter
import html
import re
from statistics import median
from typing import Callable

from .body_script_layout import annotate_body

_LEAD = r"[.…·．](?:[.…·．\s]*[.…·．]){2,}"
_PAGE = r"(?:\d{1,6}(?:[-–—]\d{1,6})?|[IVXLCDMivxlcdm]{1,12}|[Ⅰ-Ⅻⅰ-ⅻ]+|[A-Z]-\d{1,5})"
_PAIR = re.compile(rf"(?P<leader>{_LEAD})\s*(?P<page>{_PAGE})(?![0-9A-Za-z])")
_HEADING = re.compile(r"^(?:目录|目次|contents|tableofcontents)$", re.I)
_ROOT = re.compile(r"^(?:第\s*[\d一二三四五六七八九十百千零〇]+\s*[卷篇章部]|(?:chapter|part|book)\s+[\divxlcdm]+\b)", re.I)
_FRONT = re.compile(r"^(?:前\s*言|序(?:言)?|绪论|引言|后记|参考文献|附录|foreword|preface|references|appendix)$", re.I)
_OUTLINE = re.compile(r"^(\d+(?:\s*[.．]\s*\d+)+)\s*\D")


def _box(chars: list[dict]) -> list[float]:
    return [min(c['bbox'][0] for c in chars), min(c['bbox'][1] for c in chars),
            max(c['bbox'][2] for c in chars), max(c['bbox'][3] for c in chars)]


def _depth(text: str) -> int | None:
    if _ROOT.match(text) or _FRONT.fullmatch(text):
        return 0
    match = _OUTLINE.match(text)
    if match:
        return len(re.findall(r"[.．]", match[1]))
    return None


def _spatial_rows(lines: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for line in sorted(lines, key=lambda item: (item['y'], item['x'])):
        # Baselines, not font bounding-box tops, align mixed CJK/Latin runs.
        tolerance = max(1.3, min(line['size'], 14) * .18)
        candidates = [r for r in rows[-4:] if abs(r['y'] - line['y']) <= tolerance]
        if candidates:
            row = min(candidates, key=lambda r: abs(r['y'] - line['y']))
            row['chars'].extend(line['chars'])
        else:
            rows.append({'y': line['y'], 'chars': list(line['chars'])})
    for row in rows:
        row['chars'].sort(key=lambda c: (c['bbox'][0], c['id']))
    return rows


def _slices(chars: list[dict], start: int, end: int) -> list[dict]:
    result, offset = [], 0
    for c in chars:
        if start <= offset < end and c['id'] is not None:
            result.append(c)
        offset += len(c['text'])
    return result


def _separated(chars: list[dict], em: float) -> list[dict]:
    """Virtual spaces prevent adjacent columns from forming a single token."""
    result = []
    for c in chars:
        if result and c['bbox'][0] - result[-1]['bbox'][2] > em * .9:
            result.append(c | {'id': None, 'text': ' '})
        result.append(c)
    return result


def _title(rows: list[list[dict]], render: Callable) -> str:
    value = ''
    for row in rows:
        part = render(row)
        space = (value[-1:].isascii() and value[-1:].isalnum()
                 and part[:1].isascii() and part[:1].isalnum())
        value += (' ' if value and part and space else '') + part
    return value


def reconstruct(lines: list[dict], render: Callable[[list[dict]], str]) -> dict | None:
    """Return a conservative spatial model, or leave the ordinary prose path.

    An explicit title needs two aligned entries. Continuations need at least
    four leader/page pairs. Numeric columns or an incidental 'contents' word
    alone cannot switch the page into this mode.
    """
    if not lines:
        return None
    rows = _spatial_rows(lines)
    explicit = any(_HEADING.fullmatch(re.sub(r'\s', '', render(r['chars']))) for r in rows[:6])
    em = median(c['size'] for r in rows for c in r['chars'] if c['text'].strip())
    anchors, leftovers = [], []
    for row in rows:
        cs = _separated(row['chars'], em)
        raw = ''.join(c['text'] for c in cs)
        cursor = 0
        for match in _PAIR.finditer(raw):
            title = _slices(cs, cursor, match.start())
            leader = _slices(cs, match.start('leader'), match.end('leader'))
            page = _slices(cs, match.start('page'), match.end('page'))
            anchors.append({'title_chars': title, 'leader_chars': leader, 'page_chars': page,
                            'title_lines': [title],
                            'y': row['y'], 'right': max(c['bbox'][2] for c in page)})
            cursor = match.end()
        remaining = _slices(cs, cursor, len(raw))
        if any(c['text'].strip() for c in remaining):
            leftovers.append({'chars': remaining, 'y': row['y']})
    # With a printed heading, leaderless entries may still have a clearly
    # separated right-aligned page label. Require a genuine inter-run gap.
    if explicit:
        kept = []
        for row in leftovers:
            cs = row['chars']
            raw = ''.join(c['text'] for c in cs)
            match = re.search(rf'(?P<page>{_PAGE})\s*$', raw)
            before = _slices(cs, 0, match.start()) if match else []
            after = _slices(cs, match.start(), match.end('page')) if match else []
            left = [c for c in before if c['text'].strip()]
            right = [c for c in after if c['text'].strip()]
            if (left and right and any(c['text'].isalpha() for c in left)
                    and min(c['bbox'][0] for c in right) - max(c['bbox'][2] for c in left) > em * 1.5):
                anchors.append({'title_chars': before, 'leader_chars': [], 'page_chars': after,
                                'title_lines': [before],
                                'y': row['y'], 'right': max(c['bbox'][2] for c in after)})
            else:
                kept.append(row)
        leftovers = kept
    if len(anchors) < (2 if explicit else 4):
        return None
    if sum(any(c['text'].isalpha() for c in a['title_chars']) for a in anchors) < 2:
        return None
    # Repeated page-label right edges establish columns. A column's page
    # number belongs to the title on its left, on the same physical row.
    groups: list[list[dict]] = []
    for anchor in sorted(anchors, key=lambda a: a['right']):
        if not groups or anchor['right'] - median(a['right'] for a in groups[-1]) > max(6, em * 1.8):
            groups.append([])
        groups[-1].append(anchor)
    if any(len(group) < 2 for group in groups):
        return None  # an unstable page edge must not manufacture a column
    for column, group in enumerate(groups):
        visible = [c for a in group for c in a['title_chars'] if c['text'].strip()]
        if not visible:
            return None
        left = min(c['bbox'][0] for c in visible)
        for a in group:
            a['column'], a['left_edge'] = column, left
    # Separate unrelated fragments in gutters before handling wrapped titles.
    fragments = []
    for row in leftovers:
        chunks: list[list[dict]] = []
        for c in row['chars']:
            if chunks and c['bbox'][0] - chunks[-1][-1]['bbox'][2] > em * 2.5:
                chunks.append([])
            if not chunks:
                chunks.append([])
            chunks[-1].append(c)
        fragments.extend({'chars': chunk, 'y': row['y']} for chunk in chunks
                         if any(c['text'].strip() for c in chunk))
    extras, reviews = [], []
    for fragment in sorted(fragments, key=lambda f: -f['y']):
        cs = fragment['chars']
        value = render(cs)
        x = min(c['bbox'][0] for c in cs if c['text'].strip())
        possible = [a for a in anchors if .5 < a['y'] - fragment['y'] <= em * 2.15
                    and a['left_edge'] - em * .5 <= x < a['right'] - em * 2]
        next_entry = min(possible, key=lambda a: a['y'] - fragment['y']) if possible else None
        # A new numbered entry cannot swallow an unnumbered front-matter
        # heading. A continuation at a hanging indent can join its first line.
        if (next_entry and not _HEADING.fullmatch(re.sub(r'\s', '', value))
                and not _FRONT.fullmatch(value)
                and _depth(render(next_entry['title_chars'])) is None):
            next_entry['title_chars'] = cs + next_entry['title_chars']
            next_entry['title_lines'].insert(0, cs)
            next_entry['y'] = fragment['y']
            next_entry.setdefault('title_rows', []).insert(0, _box(cs))
            continue
        first_y, last_y = min(a['y'] for a in anchors), max(a['y'] for a in anchors)
        if (_HEADING.fullmatch(re.sub(r'\s', '', value)) or fragment['y'] < first_y - em * 2.2):
            role = 'header'
        elif (fragment['y'] > last_y + em * .5
              and re.fullmatch(r'[·.\s]*(?:\d+|[IVXLCDMivxlcdm]+|[Ⅰ-Ⅻⅰ-ⅻ]+)[·.\s]*', value)):
            role = 'footer'
        elif _FRONT.fullmatch(value):
            # An unpaginated printed entry is retained in its geometric order.
            col = min(range(len(groups)), key=lambda i: abs(x - groups[i][0]['left_edge']))
            anchors.append({'title_chars': cs, 'leader_chars': [], 'page_chars': [],
                            'title_lines': [cs],
                            'column': col, 'left_edge': groups[col][0]['left_edge'],
                            'y': fragment['y'], 'right': max(c['bbox'][2] for c in cs)})
            continue
        else:
            role = 'unresolved'
            reviews.append({'reason': 'unpaired_contents_fragment', 'bbox': _box(cs), 'text': value})
        col = min(range(len(groups)), key=lambda i: abs(x - groups[i][0]['left_edge']))
        extras.append({'role': role, 'text': value, 'bbox': _box(cs), 'y': fragment['y'], 'column': col,
                       'character_ids': [c['id'] for c in cs]})
    entries = []
    for i, a in enumerate(sorted(anchors, key=lambda a: (a['column'], a['y']))):
        title_chars = a['title_chars']
        title = _title(a['title_lines'], render)
        cs = title_chars + a['leader_chars'] + a['page_chars']
        if not title:
            reviews.append({'reason': 'missing_contents_title', 'bbox': _box(cs)})
        entry = {'id': f'e{i:04}', 'title': title,
                 'leader': ''.join(c['text'] for c in a['leader_chars']).strip(),
                 'page': render(a['page_chars']) if a['page_chars'] else '',
                 'column': a['column'], 'x': min((c['bbox'][0] for c in title_chars if c['text'].strip()), default=a['left_edge']),
                 'y': a['y'], 'bbox': _box(cs),
                 'character_ids': [c['id'] for c in cs],
                 'fields': {name: {'character_ids': [c['id'] for c in group],
                                   'bbox': _box(group) if group else None}
                            for name, group in (('title', title_chars), ('leader', a['leader_chars']), ('page', a['page_chars']))}}
        annotated = annotate_body([{'text': title, 'character_ids': [c['id'] for c in title_chars]}], title_chars)[0]
        entry['title_html'] = annotated.get('html_text', html.escape(title, quote=False))
        entry['scripts'] = annotated.get('scripts', [])
        reviews.extend(annotated.get('script_reviews', []))
        entries.append(entry)
    for column in range(len(groups)):
        members = [e for e in entries if e['column'] == column]
        edges: list[list[float]] = []
        for x in sorted(e['x'] for e in members):
            if not edges or x - median(edges[-1]) > em * .42:
                edges.append([])
            edges[-1].append(x)
        indents = [median(g) for g in edges]
        # Missing indentation bands are common on continuation pages. Estimate
        # a physical indent step rather than assigning a rank to each band.
        numbered = [(e['x'], _depth(e['title'])) for e in members if _depth(e['title']) is not None]
        steps = [(x2-x1)/(d2-d1) for x1, d1 in numbered for x2, d2 in numbered
                 if d2 > d1 and x2-x1 > em*.42]
        gaps = [b-a for a, b in zip(indents, indents[1:]) if b-a > em*.42]
        step = median(steps) if steps else min(gaps, default=em)
        origin = median(x-depth*step for x, depth in numbered) if numbered else indents[0]
        for e in members:
            geometric = round((e['x']-origin)/step)
            semantic = _depth(e['title'])
            e['level'] = max(0, semantic if semantic is not None else geometric)
            e['level_evidence'] = 'numbering_and_indentation' if semantic is not None else 'indentation'
    extras.sort(key=lambda e: (e['y'], e['bbox'][0]))
    all_chars = [c for r in rows for c in r['chars']]
    assigned = [i for e in entries + extras for i in e['character_ids']]
    required = Counter(c['id'] for c in all_chars if c['text'].strip())
    actual = Counter(i for i in assigned if i in required)
    if actual != required:
        return None  # keep every original glyph via the ordinary body path
    return {'entries': entries, 'extras': extras, 'columns': len(groups), 'reviews': reviews,
            'character_ids': assigned, 'x': min(c['bbox'][0] for c in all_chars),
            'y': min(r['y'] for r in rows),
            'text': '\n'.join([e['text'] for e in extras if e['role'] == 'header'] +
                              [e['title'] + e['leader'] + e['page'] for e in entries] +
                              [e['text'] for e in extras if e['role'] != 'header'])}


def render_html(model: dict) -> str:
    """Two columns keep page labels attached; whitespace shows outline depth.

    HTML is used inside Markdown so pipes and inline scientific scripts are
    unambiguous. No CSS is required to read this correctly on GitHub.
    """
    result = ['<!-- PRINTED_TOC -->']
    for extra in model['extras']:
        if extra['role'] == 'header':
            result.append('<p>' + html.escape(extra['text'], quote=False) + '</p>')
    result.append('<table class="pdf-toc">')
    content = model['entries'] + [e for e in model['extras'] if e['role'] == 'unresolved']
    for e in sorted(content, key=lambda e: (e['column'], e['y'])):
        if e.get('role') == 'unresolved':
            result.append('<tr data-toc-extra="unresolved"><td colspan="2">' +
                          html.escape(e['text'], quote=False) + '</td></tr>')
            continue
        title = '\u3000' * e['level'] + e['title_html']
        if e['level'] == 0:
            title = '<strong>' + title + '</strong>'
        leader = ('<span data-toc-role="leader" data-source-text="' + html.escape(e['leader'], quote=True) + '"> … </span>') if e['leader'] else ''
        result.append(f'<tr data-toc-id="{e["id"]}" data-toc-level="{e["level"]}" data-toc-column="{e["column"]}">'
                      f'<td><span data-toc-role="title">{title}</span>{leader}</td>'
                      f'<td><span data-toc-role="page">{html.escape(e["page"], quote=False)}</span></td></tr>')
    result.append('</table>')
    for extra in model['extras']:
        if extra['role'] == 'footer':
            result.append('<p>' + html.escape(extra['text'], quote=False) + '</p>')
    return '\n'.join(result + ['<!-- /PRINTED_TOC -->'])
