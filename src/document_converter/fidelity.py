"""Independent, page/region grounded PDF -> Markdown fidelity inspection.

Never imports the converter's text reconstruction or trusts its conservation
counts. Persisted geometry is a *partition proposal*, rechecked against original
PDF glyphs. OCR is a separate, explicitly uncertain visual witness.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import hashlib
import html
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import quote, unquote, urlsplit
import unicodedata

import pymupdf
from bs4 import BeautifulSoup
from .math_witness import read_cell, verify_fraction
from .script_witness import read_scripts, verify_script, uncovered_candidates

VERSION = "1.2.0"
PAGE = re.compile(r'(?m)^<!-- PDF_PAGE:(\d+) -->[ \t]*$')
TABLE = re.compile(r'(?m)^<!-- TABLE:([^>\s]+) -->[ \t]*$')
IMAGE = re.compile(r'!\[[^\]\n]*\]\(([^\n]+?)\)')
# Only presentation aliases: no traditional/simplified folding, fuzzy glyph
# substitution, case folding, decimal rounding, or dropping punctuation.
SCRIPTS = str.maketrans(dict(zip(
    "⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉⁺₊⁻₋⁼₌⁽₍⁾₎",
    "01234567890123456789++--==(())")))


def fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def compact(value: str) -> str:
    """Whitespace-only equivalence, used for strict order/format checks."""
    return ''.join(c for c in unicodedata.normalize('NFC', value) if not c.isspace())


def canonical(value: str) -> str:
    # Count alias transformations separately; equality here is never labelled
    # exact Unicode equality. Script geometry is checked separately below.
    value = compact(value).translate(SCRIPTS)
    return ''.join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E
                   else '-' if c == '−' else c for c in value)


@dataclass
class Region:
    text: str
    start: int
    end: int
    kind: str = 'body'
    table_id: str | None = None
    row: int | None = None
    column: int | None = None
    rowspan: int = 1
    colspan: int = 1
    display_text: str | None = None
    fractions: list[dict] = field(default_factory=list)
    math_errors: list[str] = field(default_factory=list)
    scripts: list[dict] = field(default_factory=list)
    script_errors: list[str] = field(default_factory=list)
    body_id: str | None = None
    toc_id: str | None = None
    toc_fields: dict = field(default_factory=dict)


@dataclass
class MarkdownPage:
    number: int
    start: int
    end: int
    content: str
    regions: list[Region]
    images: list[str]


def visible(value: str) -> str:
    # Parse visible HTML only, excluding data-value attributes and provenance.
    # Escape non-tags first: raw engineering expressions such as a<b must not
    # disappear as a made-up HTML element.
    value = re.sub(r'<(?!/?(?:a|br|sub|sup|data|p|ol|ul|li|table|thead|tbody|tr|td|th|details|summary|span|div|em|strong)\b|!--)', '&lt;', value, flags=re.I)
    return BeautifulSoup(value, 'html.parser').get_text()


def parse_markdown(text: str) -> tuple[list[MarkdownPage], list[int]]:
    anchors = list(PAGE.finditer(text))
    result = []
    for i, anchor in enumerate(anchors):
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(text)
        raw = text[anchor.end():end]
        # The next page's <a> and final generated validation footer are metadata.
        raw = re.split(r'(?m)^## 转换校验\s*$', raw, maxsplit=1)[0]
        regions: list[Region] = []
        table_spans = []
        for marker in TABLE.finditer(raw):
            close = raw.find('</details>', marker.end())
            if close < 0:
                close = len(raw)
            else:
                close += len('</details>')
            table_spans.append((marker.start(), close))
            fragment = raw[marker.end():close]
            soup = BeautifulSoup(fragment, 'html.parser')
            table = soup.find('table')
            if table is not None:
                occupied: set[tuple[int, int]] = set()
                cursor = marker.end()
                for row_number, tr in enumerate(table.find_all('tr')):
                    column = 0
                    for cell in tr.find_all(['td', 'th'], recursive=False):
                        while (row_number, column) in occupied:
                            column += 1
                        rowspan, colspan = int(cell.get('rowspan', 1)), int(cell.get('colspan', 1))
                        for r in range(row_number, row_number + rowspan):
                            for c in range(column, column + colspan):
                                occupied.add((r, c))
                        hit = re.search(r'<(?:td|th)\b[^>]*>.*?</(?:td|th)>', raw[cursor:close], re.S | re.I)
                        lo, hi = (cursor + hit.start(), cursor + hit.end()) if hit else (marker.end(), close)
                        cursor = hi
                        scripts, script_errors = read_scripts(cell)
                        value, display, fractions, math_errors = read_cell(cell)
                        regions.append(Region(value, anchor.end() + lo, anchor.end() + hi,
                                              'cell', marker[1], row_number, column, rowspan, colspan,
                                              display, fractions, math_errors, scripts, script_errors))
                        column += colspan
            else:
                # Nonrectangular tables are exported as coordinate-labelled li.
                cursor = marker.end()
                for li in soup.find_all('li'):
                    scripts, script_errors = read_scripts(li)
                    value, display, fractions, math_errors = read_cell(li)
                    label = re.match(r'第\s*(\d+)\s*行，第\s*(\d+)\s*列(?:；跨\s*(\d+)\s*行、\s*(\d+)\s*列)?：', value)
                    hit = re.search(r'<li\b[^>]*>.*?</li>', raw[cursor:close], re.S)
                    lo, hi = (cursor + hit.start(), cursor + hit.end()) if hit else (marker.end(), close)
                    cursor = hi
                    if label:
                        regions.append(Region(value[label.end():], anchor.end() + lo, anchor.end() + hi,
                                              'cell', marker[1], int(label[1]) - 1, int(label[2]) - 1,
                                              int(label[3] or 1), int(label[4] or 1),
                                              display[label.end():], fractions, math_errors, scripts, script_errors))
        # Mask only known wrappers; preserve arbitrary source text/ASCII symbols.
        body = list(raw)
        for lo, hi in table_spans:
            body[lo:hi] = ' ' * (hi - lo)
        body = ''.join(body)
        contents = list(re.finditer(r'(?ms)^<!-- PRINTED_TOC -->\n(.*?)\n<!-- /PRINTED_TOC -->[ \t]*$', body))
        for match in contents:
            for token in re.finditer(r'<tr\b[^>]*>.*?</tr>|<p\b[^>]*>.*?</p>', match[1], re.S):
                node = BeautifulSoup(token[0], 'html.parser')
                row = node.find('tr')
                lo = anchor.end() + match.start(1) + token.start()
                hi = anchor.end() + match.start(1) + token.end()
                if row is None or row.get('data-toc-extra') == 'unresolved':
                    regions.append(Region(node.get_text(), lo, hi))
                    continue
                fields = {role: row.find(attrs={'data-toc-role': role}) for role in ('title', 'leader', 'page')}
                title = fields['title'].get_text() if fields['title'] else ''
                page_label = fields['page'].get_text() if fields['page'] else ''
                leader = fields['leader'].get('data-source-text', '') if fields['leader'] else ''
                scripts, errors = read_scripts(fields['title'] or row)
                display_leader = fields['leader'].get_text() if fields['leader'] else ''
                if (not fields['title'] or not fields['page']
                        or canonical(row.get_text()) != canonical(title + display_leader + page_label)
                        or fields['leader'] and display_leader.strip() != '…'):
                    errors.append('invalid contents fields or unexpected visible text')
                regions.append(Region(title + leader + page_label, lo, hi,
                                      display_text=row.get_text(), scripts=scripts, script_errors=errors,
                                      toc_id=row.get('data-toc-id', ''),
                                      toc_fields={'title': title, 'leader': leader, 'page': page_label,
                                                  'level': row.get('data-toc-level'), 'column': row.get('data-toc-column')}))
        for match in reversed(contents):
            body = body[:match.start()]+' '*(match.end()-match.start())+body[match.end():]
        semantic = list(re.finditer(r'(?ms)^<!-- BODY_SCRIPT:([^>\s]+) -->\n(.*?)\n<!-- /BODY_SCRIPT -->[ \t]*$', body))
        for match in semantic:
            node = BeautifulSoup(match[2], 'html.parser')
            scripts, errors = read_scripts(node)
            regions.append(Region(node.get_text(), anchor.end()+match.start(), anchor.end()+match.end(),
                                  scripts=scripts, script_errors=errors, body_id=match[1]))
        for match in reversed(semantic):
            body = body[:match.start()]+' '*(match.end()-match.start())+body[match.end():]
        offset = 0
        for line in body.splitlines(keepends=True):
            stripped = line.strip()
            metadata = (not stripped or stripped == '---' or stripped.startswith('<!--')
                        or re.fullmatch(r'<a id="pdf-page-\d+"></a>', stripped)
                        or re.match(r'^\[(?:原始 PDF 第 \d+ 页|对照补线前原表)\]\(', stripped)
                        or stripped.startswith('![PDF 第 ') or stripped.startswith('![完整表格 ')
                        or stripped.startswith('来源：'))
            if not metadata:
                value = re.sub(r'^#{1,6}\s+', '', line)
                value = visible(value) if '<' in value or '&' in value else value
                regions.append(Region(value, anchor.end() + offset, anchor.end() + offset + len(line)))
            offset += len(line)
        regions.sort(key=lambda r: r.start)
        result.append(MarkdownPage(int(anchor[1]), anchor.start(), end, raw, regions, IMAGE.findall(raw)))
    return result, [int(m[1]) for m in anchors]


def glyphs(page: pymupdf.Page) -> list[dict]:
    result = []
    for bi, block in enumerate(page.get_text('rawdict').get('blocks', [])):
        for li, line in enumerate(block.get('lines', [])):
            for span in line.get('spans', []):
                for char in span.get('chars', []):
                    result.append({'text': char['c'], 'bbox': list(char['bbox']),
                                   'origin': list(char['origin']), 'size': span['size'],
                                   'direction': list(line['dir']), 'line': (bi, li), 'id': len(result)})
    return result


def rotation_for(chars: list[dict]) -> int:
    counts = Counter(tuple(round(v) for v in c['direction']) for c in chars if c['text'].strip())
    if not counts:
        return 0
    direction, count = counts.most_common(1)[0]
    return {(0, -1): 270, (0, 1): 90, (-1, 0): 180}.get(direction, 0) if count > sum(counts.values()) * .65 else 0


@contextmanager
def oriented(doc: pymupdf.Document, index: int):
    original = doc[index]
    chars = glyphs(original)
    angle = rotation_for(chars)
    shadow = None
    try:
        page = original
        if angle or original.rotation:
            shadow = pymupdf.open()
            w, h = original.cropbox.width, original.cropbox.height
            page = shadow.new_page(width=h if angle % 180 else w, height=w if angle % 180 else h)
            saved = original.rotation
            original.set_rotation(0)
            try:
                page.show_pdf_page(page.rect, doc, index, rotate=angle)
            finally:
                original.set_rotation(saved)
            chars = glyphs(page)
        yield page, chars, angle
    finally:
        if shadow is not None:
            shadow.close()


class SpatialIndex:
    def __init__(self, chars: list[dict]):
        self.bins: dict[tuple[int, int], list[dict]] = defaultdict(list)
        for c in chars:
            b = c['bbox']
            self.bins[(int((b[0] + b[2]) / 48), int((b[1] + b[3]) / 48))].append(c)

    def inside(self, box: list[float]) -> list[dict]:
        result = []
        for x in range(int((box[0] - .1) / 24), int((box[2] + .1) / 24) + 1):
            for y in range(int((box[1] - .1) / 24), int((box[3] + .1) / 24) + 1):
                for c in self.bins.get((x, y), []):
                    b = c['bbox']
                    if box[0] - .1 <= (b[0] + b[2]) / 2 <= box[2] + .1 and box[1] - .1 <= (b[1] + b[3]) / 2 <= box[3] + .1:
                        result.append(c)
        return sorted(result, key=lambda c: c['id'])


def bounds(chars: list[dict]) -> list[float] | None:
    if not chars:
        return None
    return [round(min(c['bbox'][0] for c in chars), 3), round(min(c['bbox'][1] for c in chars), 3),
            round(max(c['bbox'][2] for c in chars), 3), round(max(c['bbox'][3] for c in chars), 3)]


def source_lines(chars: list[dict]) -> list[list[dict]]:
    lines: dict[tuple[int, int], list[dict]] = {}
    for c in chars:
        lines.setdefault(c['line'], []).append(c)
    return list(lines.values())


def text_candidates(chars: list[dict]) -> list[str]:
    raw = ''.join(c['text'] for c in chars)
    # Two independent source reading orders; choose an exact witness when one
    # exists, otherwise retain the disagreement as uncertain layout evidence.
    lines = source_lines(chars)
    geometric = ''.join(''.join(c['text'] for c in sorted(line, key=lambda c: (c['bbox'][0], c['id'])))
                        for line in sorted(lines, key=lambda g: (round(g[0]['origin'][1] / 2), min(c['bbox'][0] for c in g))))
    return list(dict.fromkeys([raw, geometric]))


def delta(source: str, target: str) -> dict:
    left, right = Counter(canonical(source)), Counter(canonical(target))
    return {'missing': dict(sorted((left - right).items())), 'extra': dict(sorted((right - left).items()))}


def local_diff(source: str, target: str) -> list[dict]:
    a, b = canonical(source), canonical(target)
    # Hard bound repeated numerical tables: never quadratic on a whole book.
    if len(a) * len(b) > 2_000_000:
        return [{'operation': 'bounded_region', 'source': source, 'converted': target,
                 'reason': '区域过大，保留全文及字符差集；不执行无界编辑距离'}]
    return [{'operation': op, 'source': a[i:j], 'converted': b[k:l],
             'source_range': [i, j], 'converted_range': [k, l],
             'source_context': a[max(0, i - 24):min(len(a), j + 24)],
             'converted_context': b[max(0, k - 24):min(len(b), l + 24)]}
            for op, i, j, k, l in SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op != 'equal']


def number_tokens(text: str) -> list[str]:
    # Preserve decimal/sign/exponent inequality context in displayed evidence.
    return re.findall(r'[+−-]?\d+(?:\.\d+)?(?:[eE][+−-]?\d+)?(?:[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)?', compact(text))


class Inspector:
    def __init__(self, source: Path, markdown: Path, out: Path):
        self.source, self.markdown, self.out = source.resolve(), markdown.resolve(), out.resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.text = self.markdown.read_text(encoding='utf-8-sig')
        self.line_offsets = [0] + [m.end() for m in re.finditer('\n', self.text)]
        self.findings: list[dict] = []
        self.page_metrics: list[dict] = []
        self.checked_assets: set[str] = set()
        self.image_checks = 0
        self.fraction_checks = 0
        self.script_checks = 0
        self.script_review_glyphs = 0
        self.script_cross_cell_candidates = 0
        self.body_script_review_glyphs = 0
        self.body_script_checks = 0
        self.contents_entry_checks = 0
        self.contents_leader_characters = 0
        self.math_syntax_characters = 0
        self.source_hash, self.md_hash = fingerprint(self.source), fingerprint(self.markdown)
        self.algorithm_hash = hashlib.sha256((fingerprint(Path(__file__)) +
            fingerprint(Path(__file__).with_name('math_witness.py')) +
            fingerprint(Path(__file__).with_name('script_witness.py'))).encode()).hexdigest()
        self.audit: dict = {}
        self.structures: dict[int, dict] = {}
        audit_path = markdown.with_suffix('.audit.json')
        if audit_path.exists():
            self.audit = json.loads(audit_path.read_text(encoding='utf-8'))
        structure_path = markdown.parent / (markdown.stem + '_assets') / 'structure.jsonl'
        if structure_path.exists():
            with structure_path.open(encoding='utf-8') as stream:
                for line in stream:
                    record = json.loads(line)
                    if record['pdf_page'] in self.structures:
                        self.add('structure_duplicate_page', 'error', record['pdf_page'], '结构记录页码重复')
                    self.structures[record['pdf_page']] = record

    def add(self, kind: str, severity: str, page: int | None, reason: str, *, region: Region | None = None,
            source: str = '', converted: str = '', bbox: list | None = None, **extra):
        item = {'id': f'F{len(self.findings) + 1:06}', 'kind': kind, 'severity': severity,
                'pdf_page': page, 'reason': reason, 'source_text': source, 'converted_text': converted,
                'bbox': bbox, 'coordinate_space': 'oriented_pdf_points', **extra}
        if region:
            item.update({'md_lines': [bisect_right(self.line_offsets, region.start),
                                      bisect_right(self.line_offsets, max(region.start, region.end - 1))],
                         'md_char_range': [region.start, region.end], 'table_id': region.table_id,
                         'row': region.row, 'column': region.column})
        if (source or converted) and kind in {
                'cell_content_difference', 'cell_order_review', 'body_content_difference',
                'page_character_difference', 'structure_markdown_difference', 'sparse_text_review'}:
            item['comparison_scope'] = 'complete_region'
            item['character_delta'] = delta(source, converted)
            item['diff'] = local_diff(source, converted)
            item['numeric_sensitive'] = number_tokens(source) != number_tokens(converted)
        elif source or converted:
            item['comparison_scope'] = 'partial_context_witness'
        self.findings.append(item)

    def check_cell(self, chars: list[dict], cell: dict, region: Region | None, page: int, drawings=None):
        source = ''.join(c['text'] for c in chars)
        target = region.text if region else ''
        change = delta(source, target)
        if change['missing'] or change['extra']:
            self.add('cell_content_difference', 'error', page, '原 PDF 单元格区域与 Markdown 字符数量/字词不同',
                     region=region, source=source, converted=target, bbox=cell['bbox'],
                     cell_location={'table_id': cell.get('table_id'), 'row': cell['row'], 'column': cell['column']})
        elif compact(source) != compact(target):
            candidates = text_candidates(chars)
            if not any(canonical(s) == canonical(target) for s in candidates):
                # Repeated numbers can conserve characters and still reorder
                # values. This must remain visible even when the bag matches.
                self.add('cell_order_review', 'review', page, '字符守恒但单元格顺序不匹配独立来源顺序，需对照公式/上下标位置',
                         region=region, source=source, converted=target, bbox=cell['bbox'])
            elif compact(source) != compact(target) and canonical(source) == canonical(target):
                self.presentation_pages.add(page)
        # Compare actual MD to archived structured text as a second witness.
        # Agreement here alone can never prove fidelity to the source PDF.
        if region and (compact(cell.get('source_text', cell['text'])) != compact(region.text)
                       or compact(cell['text']) != compact(region.display_text if region.display_text is not None else region.text)):
            self.add('structure_markdown_difference', 'error', page, 'Markdown 单元格与转换时保存的单元格文字不同',
                     region=region, source=cell['text'], converted=region.text, bbox=cell['bbox'],
                     witness='structure.jsonl (secondary evidence)')
        if region:
            script_errors = list(region.script_errors)
            script_used = set()
            for script in region.scripts:
                self.script_checks += 1
                issue = verify_script(script, chars, script_used)
                if issue:
                    script_errors.append(issue)
            if sorted(region.scripts, key=lambda s:s['id']) != sorted(cell.get('scripts', []), key=lambda s:s['id']):
                script_errors.append('HTML and structured script evidence differ')
            if script_errors:
                self.add('script_evidence_difference', 'error', page,
                         '上下标的原始字形、主体绑定、升降方向或HTML表示不一致', region=region,
                         source=source, converted=region.display_text or target, bbox=cell['bbox'], issues=script_errors)
            fraction_ids = {i for f in cell.get('fractions', []) for i in f['character_ids']}
            pending = uncovered_candidates(chars, script_used | fraction_ids)
            if pending or cell.get('script_reviews'):
                self.script_review_glyphs += len(pending)
                self.add('script_attachment_review', 'review', page,
                         '有未绑定的小字形或多义绑定候选，需对照原图；可能为公差、公式或上下标',
                         region=region, source=source, converted=region.display_text or target,
                         bbox=cell['bbox'], candidates=pending, layout_reviews=cell.get('script_reviews', []))
            errors = list(region.math_errors)
            used = set()
            for fraction in region.fractions:
                self.fraction_checks += 1
                self.math_syntax_characters += 4 if 'whole' in fraction else 3
                issue = verify_fraction(fraction, chars, drawings or [], used)
                if issue:
                    errors.append(issue)
            if len(region.fractions) != len(cell.get('fractions', [])):
                errors.append('HTML and structured fraction counts differ')
            if errors:
                self.add('fraction_evidence_difference', 'error', page,
                         '分数表示与原始矢量分数线/数字或生成标记不一致', region=region,
                         source=source, converted=region.display_text or target, bbox=cell['bbox'], issues=errors)

    def check_contents(self, chars: list[dict], regions: list[Region], page: int) -> set[int]:
        """Re-read entry fields from original glyphs; do not trust source counts.

        Page swaps can conserve the whole page's character bag. Field binding,
        original baselines and column order are independent additional checks.
        Only a verified decorative leader may come from an HTML attribute.
        """
        models = self.structures.get(page, {}).get('printed_contents', [])
        expected = {e['id']: e for m in models for e in m['entries']}
        by_id, seen, used = {c['id']: c for c in chars}, set(), set()
        order = []
        for r in regions:
            if r.toc_id is None:
                continue
            saved = expected.get(r.toc_id)
            issues = list(r.script_errors)
            if r.toc_id in seen or saved is None:
                issues.append('unknown or duplicate printed contents entry')
            seen.add(r.toc_id)
            if saved:
                self.contents_entry_checks += 1
                order.append((saved['column'], saved['y']))
                selected = {}
                for name in ('title', 'leader', 'page'):
                    field = saved['fields'][name]
                    ids = field['character_ids']
                    group = [by_id[i] for i in ids if i in by_id]
                    selected[name] = group
                    if len(group) != len(ids):
                        issues.append(f'{name}: source identity unavailable')
                    source = ''.join(c['text'] for c in group)
                    if canonical(source) != canonical(r.toc_fields[name]):
                        issues.append(f'{name}: source glyphs differ from Markdown')
                    box = field['bbox']
                    if box and any(not (box[0]-.1 <= (c['bbox'][0]+c['bbox'][2])/2 <= box[2]+.1
                                         and box[1]-.1 <= (c['bbox'][1]+c['bbox'][3])/2 <= box[3]+.1) for c in group):
                        issues.append(f'{name}: source glyph outside recorded geometry')
                if r.toc_fields['level'] != str(saved['level']) or r.toc_fields['column'] != str(saved['column']):
                    issues.append('contents level or column differs from saved geometry')
                if r.toc_fields['leader'] and not re.fullmatch(r'[.…·．\s]+', r.toc_fields['leader']):
                    issues.append('non-decorative content hidden in leader metadata')
                self.contents_leader_characters += len(compact(r.toc_fields['leader']))
                title, leader, label = (selected[k] for k in ('title', 'leader', 'page'))
                if label and title:
                    from statistics import median
                    y = median(c['origin'][1] for c in label)
                    em = median(c['size'] for c in label)
                    if min(abs(c['origin'][1]-y) for c in title) > max(2, em*.3):
                        issues.append('page label is not on a title baseline')
                    if leader and abs(median(c['origin'][1] for c in leader)-y) > max(2, em*.3):
                        issues.append('page label is not on its leader baseline')
                    same_line = [c for c in title if abs(c['origin'][1]-y) <= max(2, em*.3)]
                    if same_line and max(c['bbox'][2] for c in same_line) > min(c['bbox'][0] for c in label)+.5:
                        issues.append('page label is not to the right of its title')
                if sorted(r.scripts, key=lambda s:s['id']) != sorted(saved.get('scripts', []), key=lambda s:s['id']):
                    issues.append('contents script evidence differs from saved title')
                for script in r.scripts:
                    self.body_script_checks += 1
                    issue = verify_script(script, chars, used)
                    if issue:
                        issues.append(issue)
            if issues:
                self.add('contents_entry_difference', 'error', page,
                         '印刷目录条目的标题、引导线、页码配对或层级与来源不一致', region=r,
                         bbox=saved['bbox'] if saved else None, issues=issues)
        if seen != expected.keys():
            self.add('contents_entry_set_difference', 'error', page, '印刷目录条目缺失、重复或无来源记录')
        if order != sorted(order):
            self.add('contents_order_difference', 'error', page, '印刷目录未按栏内从上到下、栏间从左到右排列')
        for model in models:
            if model.get('reviews'):
                self.add('contents_layout_review', 'review', page, '印刷目录仍有不确定布局，原文已保留', issues=model['reviews'])
        return used

    def check_body(self, chars: list[dict], regions: list[Region], page: int, global_delta: dict):
        source = ''.join(c['text'] for c in chars)
        target = ''.join(r.text for r in regions)
        before = len(self.findings)
        used, errors = self.check_contents(chars, regions, page), []
        contents_verified = (any(r.toc_id is not None for r in regions)
                             and len(self.findings) == before)
        expected = {b['body_id']: b for b in self.structures.get(page, {}).get('body_scripts', [])}
        matched = set()
        for r in regions:
            if r.body_id is None:
                continue
            matched.add(r.body_id)
            errors.extend(r.script_errors)
            saved = expected.get(r.body_id)
            if not saved or sorted(r.scripts, key=lambda s:s['id']) != sorted(saved['scripts'], key=lambda s:s['id']):
                errors.append('body HTML and saved script evidence differ')
            for script in r.scripts:
                self.body_script_checks += 1
                issue = verify_script(script, chars, used)
                if issue:
                    errors.append(issue)
        if matched != expected.keys():
            errors.append('body script block missing from Markdown')
        if errors:
            self.add('body_script_evidence_difference', 'error', page,
                     '正文上下标的来源绑定、HTML语义或结构记录不一致',
                     source=source, converted=target, bbox=bounds(chars), issues=errors)
        pending = uncovered_candidates(chars, used)
        if pending:
            self.body_script_review_glyphs += len(pending)
            by_id = {c['id']: c for c in chars}
            for candidate in pending:
                candidate['possible_base'] = by_id[candidate['possible_base_id']]['text']
                candidate['base_bbox'] = by_id[candidate['possible_base_id']]['bbox']
            self.add('body_script_attachment_review', 'review', page,
                     '正文或图中文字存在偏移小字形，当前无表格上下标结构证据，需对照来源确认',
                     region=Region(target, regions[0].start, regions[-1].end) if regions else None,
                     source=source, converted=target, bbox=bounds(chars), candidates=pending)
        change = delta(source, target)
        if change['missing'] or change['extra']:
            self.add('body_content_difference', 'error', page, '原 PDF 正文区域与 Markdown 正文字词/数量不同',
                     region=Region(target, regions[0].start, regions[-1].end) if regions else None,
                     source=source, converted=target, bbox=bounds(chars))
        elif compact(source) != compact(target) and canonical(source) == canonical(target):
            self.presentation_pages.add(page)
        # Spatial entry/field checks replace native stream order on a verified
        # contents page. PDF producers often append running headers last or
        # interleave columns; that stream is not a reading-order witness.
        if contents_verified:
            return
        # Contiguous line witnesses detect order errors missed by character bags.
        target_value = canonical(target)
        unmatched = []
        anchors = []
        for line in source_lines(chars):
            value = ''.join(c['text'] for c in line)
            needle = canonical(value)
            if len(needle) >= 12 and needle not in target_value:
                unmatched.append({'source': value, 'bbox': bounds(line)})
            elif len(needle) >= 12 and target_value.count(needle) == 1:
                anchors.append({'source': value, 'position': target_value.index(needle), 'bbox': bounds(line)})
        if unmatched:
            self.add('body_order_review', 'review', page, '部分原生文字行在正文中不连续；可能为重排、跨字体/公式插入或真实顺序偏差',
                     region=Region(target, regions[0].start, regions[-1].end) if regions else None,
                     source='\n'.join(x['source'] for x in unmatched), converted=target,
                     bbox=bounds(chars), unmatched_lines=unmatched,
                     character_bag_equal=not change['missing'] and not change['extra'])
        inversions = [(a, b) for a, b in zip(anchors, anchors[1:]) if a['position'] > b['position']]
        if inversions:
            self.add('body_block_order_review', 'review', page,
                     '原生长行均能在 Markdown 唯一找到，但相对顺序逆转；需对照多栏版式确认段落是否错序',
                     region=Region(target, regions[0].start, regions[-1].end) if regions else None,
                     source='\n'.join(a['source'] + '\n' + b['source'] for a, b in inversions),
                     converted=target, bbox=bounds(chars), inverted_anchors=inversions)

    def check_page(self, page: pymupdf.Page, chars: list[dict], md: MarkdownPage, angle: int, verify_images: bool):
        number = md.number
        source = ''.join(c['text'] for c in chars)
        target = ''.join(r.text for r in md.regions)
        diff = delta(source, target)
        self.page_metrics.append({'pdf_page': number, 'source_characters': len(compact(source)),
                                  'converted_characters': len(compact(target)), 'rotation': angle,
                                  'missing_characters': sum(diff['missing'].values()),
                                  'extra_characters': sum(diff['extra'].values()),
                                  'raw_replacement_characters': source.count('�'),
                                  'converted_replacement_characters': target.count('�')})
        if diff['missing'] or diff['extra']:
            self.add('page_character_difference', 'error', number, '独立逐页计数发现原始文字与可检索正文不同',
                     source=source, converted=target)
        if '�' in source or any(unicodedata.category(c) == 'Co' for c in source):
            self.add('source_encoding_review', 'review', number, '原 PDF 文字层含替代字符/私用区，须用原页图确认字形',
                     source=source, converted=target)
        if len(compact(source)) < 40:
            self.add('sparse_text_review', 'review', number, '原 PDF 文字层少于 40 字；页图保留不代表内容已成为可检索文字',
                     source=source, converted=target, native_characters=len(compact(source)))
        structure = self.structures.get(number)
        cell_regions = {(r.table_id, r.row, r.column): r for r in md.regions if r.kind == 'cell'}
        index = SpatialIndex(chars)
        claimed = set()
        matched = set()
        if structure:
            space = structure.get('coordinate_space', {})
            if abs(space.get('width', page.rect.width) - page.rect.width) > .1 or abs(space.get('height', page.rect.height) - page.rect.height) > .1:
                self.add('structure_coordinate_mismatch', 'error', number, '保存的表格坐标系与原 PDF 归一化页面不一致')
                structure = None
        if structure:
            drawings = page.get_drawings() if any(r.fractions or r.math_errors for r in cell_regions.values()) else []
            for table in structure.get('tables', []):
                # A grid edge can isolate a script from its base. Per-cell
                # recognition cannot see that failure; audit the whole table.
                table_chars = index.inside(table['bbox'])
                ownership = {c['id']: (cell['row'], cell['column'])
                             for cell in table['cells'] if not cell.get('geometry_gap')
                             for c in index.inside(cell['bbox'])}
                covered = {i for cell in table['cells'] for s in cell.get('scripts', []) for i in s['character_ids']}
                covered.update(i for cell in table['cells'] for f in cell.get('fractions', []) for i in f['character_ids'])
                crossing = [c for c in uncovered_candidates(table_chars, covered)
                            if ownership.get(c['character_id']) != ownership.get(c['possible_base_id'])]
                if crossing:
                    self.script_cross_cell_candidates += len(crossing)
                    self.add('script_cross_cell_review', 'review', number,
                             '小字形与可能主体分属不同单元格，需复核网格是否切开上下标',
                             bbox=table['bbox'], table_id=table['id'], candidates=crossing)
                if table.get('fallback') or table.get('geometry_gaps'):
                    self.add('table_geometry_review', 'review', number, '转换器已将不确定表格降级或保留非矩形坐标，行列语义仍需复核',
                             bbox=table['bbox'], table_id=table['id'], fallback=table.get('fallback', False),
                             geometry_gaps=table.get('geometry_gaps', 0),
                             source='\n'.join(c['text'] for c in table['cells'] if c['text']))
                for cell in table['cells']:
                    if cell.get('geometry_gap'):
                        continue
                    cell_chars = [c for c in index.inside(cell['bbox']) if c['id'] not in claimed]
                    claimed.update(c['id'] for c in cell_chars)
                    key = (table['id'], cell['row'], cell['column'])
                    region = cell_regions.get(key)
                    matched.add(key)
                    self.check_cell(cell_chars, cell | {'table_id': table['id']}, region, number, drawings)
                    if region and (region.rowspan != cell['rowspan'] or region.colspan != cell['colspan']):
                        self.add('cell_span_difference', 'error', number, 'Markdown 合并行列跨度与保存的原单元格模型不同',
                                 region=region, bbox=cell['bbox'], expected_span=[cell['rowspan'], cell['colspan']],
                                 actual_span=[region.rowspan, region.colspan])
                for image_key in ('image', 'original_image'):
                    link = table.get(image_key)
                    if link and unquote(link) not in {unquote(v) for v in md.images}:
                        # Original-image comparison uses a normal Markdown link.
                        if image_key == 'image' or f']({link})' not in md.content:
                            self.add('table_image_reference_difference', 'error', number,
                                     '表格对应的原图引用被移除或替换', table_id=table['id'], asset=link)
                if verify_images:
                    self.check_table_image(page, table, number)
            for key in cell_regions.keys() - matched:
                self.add('unexpected_cell', 'error', number, 'Markdown 中出现结构记录没有的单元格',
                         region=cell_regions[key], converted=cell_regions[key].text)
            self.check_body([c for c in chars if c['id'] not in claimed],
                            [r for r in md.regions if r.kind == 'body'], number, diff)
        elif cell_regions:
            self.add('missing_structure_review', 'review', number, '缺少可用表格坐标，仅执行独立逐页计数；不能确认单元格归属')
        else:
            self.check_body(chars, [r for r in md.regions if r.kind == 'body'], number, diff)
        for link in md.images:
            path = self.resolve_asset(link)
            if path is None or not path.is_file():
                self.add('missing_image', 'error', number, 'Markdown 图片不存在或引用了非本地资源', asset=link)
            elif verify_images and '/pages/' in link.replace('\\', '/'):
                self.check_page_image(page, path, number)

    def resolve_asset(self, link: str) -> Path | None:
        parts = urlsplit(link)
        if parts.scheme or parts.netloc:
            return None
        return (self.markdown.parent / unquote(parts.path)).resolve()

    def check_table_image(self, page: pymupdf.Page, table: dict, number: int):
        from PIL import Image, ImageChops, ImageDraw
        link = table.get('original_image') or table.get('image')
        path = self.resolve_asset(link) if link else None
        if path is None or not path.exists():
            self.add('missing_table_image', 'error', number, '保存的表格原版式图片缺失', table_id=table['id'], asset=link)
            return
        dpi = table.get('image_dpi', 180)
        clip = (pymupdf.Rect(table['bbox']) + (-2, -2, 2, 2)) & page.rect
        pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
        expected = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
        with Image.open(path) as stored:
            actual = stored.convert('RGB')
        self.image_checks += 1
        if expected.size != actual.size or ImageChops.difference(expected, actual).getbbox():
            self.add('table_image_difference', 'error', number, '表格对照图与源 PDF 重新渲染像素不同',
                     table_id=table['id'], bbox=table['bbox'], asset=link, expected_size=expected.size, actual_size=actual.size)
        # Verify edited image too: accept exactly the documented added rules.
        if table.get('original_image') and table.get('image'):
            edited = self.resolve_asset(table['image'])
            if edited is None or not edited.exists():
                self.add('missing_table_image', 'error', number, '补线后表格图缺失', table_id=table['id'])
                return
            draw = ImageDraw.Draw(expected)
            scale = dpi / 72
            for rule in table.get('inferred_rules', []):
                draw.line([(rule['x0'] * scale - pix.x, rule['y'] * scale - pix.y),
                           (rule['x1'] * scale - pix.x, rule['y'] * scale - pix.y)],
                          fill=(95, 95, 95), width=max(1, round(scale * .3)))
            with Image.open(edited) as stored:
                actual = stored.convert('RGB')
            self.image_checks += 1
            if expected.size != actual.size or ImageChops.difference(expected, actual).getbbox():
                self.add('repaired_image_difference', 'error', number, '补线图中存在已登记规则以外的像素差异',
                         table_id=table['id'], bbox=table['bbox'], asset=table['image'])

    def check_page_image(self, page: pymupdf.Page, path: Path, number: int):
        from PIL import Image, ImageChops, ImageStat
        with Image.open(path) as stored:
            actual = stored.convert('RGB')
        # Infer the original integral DPI from dimensions, independent of audit.
        dpi = round(actual.width * 72 / page.rect.width)
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        expected = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
        self.image_checks += 1
        if expected.size != actual.size:
            self.add('page_image_difference', 'error', number, '原页图尺寸与原 PDF 渲染不符', asset=str(path))
        else:
            error = sum(ImageStat.Stat(ImageChops.difference(expected, actual)).mean) / 3
            if error > 4:
                self.add('page_image_difference', 'error', number, '原页图与原 PDF 存在超出 JPEG 压缩容差的差异', asset=str(path), mean_absolute_error=error)

    def optical_review(self, doc, pages, selected, dpi):
        """Independent raster witness; disagreements never auto-correct the MD."""
        import numpy as np
        try:
            from rapidocr import RapidOCR
            engine = RapidOCR(params={'Global.log_level': 'warning',
                                      'EngineConfig.onnxruntime.intra_op_num_threads': 2,
                                      'EngineConfig.onnxruntime.inter_op_num_threads': 1})
        except Exception as exc:
            self.add('ocr_unavailable', 'review', None, f'独立视觉见证未运行：{type(exc).__name__}: {exc}')
            return []
        cache = self.out / 'ocr_cache'
        cache.mkdir(exist_ok=True)
        results = []
        by_number = {p.number: p for p in pages}
        for number in selected:
            key = f'{self.source_hash[:20]}-p{number:05}-dpi{dpi}-rapidocr-{importlib.metadata.version("rapidocr")}.json'
            cached = cache / key
            try:
                with oriented(doc, number - 1) as (page, chars, angle):
                    if cached.exists():
                        data = json.loads(cached.read_text(encoding='utf-8'))
                        if data.get('source_sha256') != self.source_hash:
                            raise ValueError('OCR cache fingerprint mismatch')
                        tokens = data['tokens']
                    else:
                        pix = page.get_pixmap(dpi=dpi, alpha=False)
                        raster = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
                        output = engine(raster)
                        tokens = []
                        if output.txts is not None and output.boxes is not None:
                            for box, value, score in zip(output.boxes, output.txts, output.scores):
                                xy = np.asarray(box) * (72 / dpi)
                                tokens.append({'text': str(value), 'confidence': float(score),
                                               'bbox': [float(xy[:, 0].min()), float(xy[:, 1].min()),
                                                        float(xy[:, 0].max()), float(xy[:, 1].max())]})
                        atomic_json(cached, {'source_sha256': self.source_hash, 'pdf_page': number,
                                             'dpi': dpi, 'rotation': angle, 'tokens': tokens})
                    target = canonical(''.join(r.text for r in by_number[number].regions))
                    candidates = []
                    for token in tokens:
                        needle = canonical(token['text'])
                        if token['confidence'] < .96 or len(needle) < 5 or needle in target:
                            continue
                        b = token['bbox']
                        local = [c for c in chars if b[0] - 2 <= (c['bbox'][0] + c['bbox'][2]) / 2 <= b[2] + 2
                                 and b[1] - 2 <= (c['bbox'][1] + c['bbox'][3]) / 2 <= b[3] + 2]
                        candidates.append(token | {'native_text_in_region': ''.join(c['text'] for c in local)})
                    results.append({'pdf_page': number, 'tokens': len(tokens), 'candidates': len(candidates),
                                    'cache': str(cached.relative_to(self.out))})
                    if candidates:
                        self.add('visual_text_review', 'review', number,
                                 '独立 OCR 高置信文字在 Markdown 中未连续找到；可能为扫描未提取、OCR 误读或原生编码/顺序偏差，需对照原图',
                                 source='\n'.join(t['text'] for t in candidates),
                                 converted='\n'.join(t['native_text_in_region'] for t in candidates),
                                 optical_candidates=candidates, witness='independent raster OCR; not ground truth')
            except Exception as exc:
                self.add('ocr_page_failed', 'review', number, f'该页 OCR 未完成：{type(exc).__name__}: {exc}')
            print(f'fidelity OCR: {number}; {len(results)}/{len(selected)} pages', file=sys.stderr, flush=True)
        return results

    def evidence(self, doc, limit):
        folder = self.out / 'evidence'
        folder.mkdir(exist_ok=True)
        candidates = [f for f in self.findings if f.get('pdf_page') and
                      f['kind'] in {'cell_content_difference', 'body_content_difference',
                                    'cell_order_review', 'body_order_review', 'body_block_order_review', 'visual_text_review',
                                    'sparse_text_review', 'table_geometry_review'}]
        candidates.sort(key=lambda f: (f['severity'] != 'error', f['kind'] != 'visual_text_review', f['pdf_page'], f['id']))
        rendered = {}
        for item in candidates[:limit]:
            number = item['pdf_page']
            with oriented(doc, number - 1) as (page, _, _):
                bbox = item.get('bbox')
                if not bbox and item.get('optical_candidates'):
                    boxes = [t['bbox'] for t in item['optical_candidates']]
                    bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                            max(b[2] for b in boxes), max(b[3] for b in boxes)]
                rect = ((pymupdf.Rect(bbox) + (-10, -10, 10, 10)) & page.rect) if bbox else page.rect
                key = (number, tuple(round(v, 1) for v in rect))
                if key not in rendered:
                    name = f'p{number:05}-{item["id"]}.png'
                    page.get_pixmap(dpi=150, clip=rect, alpha=False).save(folder / name)
                    rendered[key] = 'evidence/' + name
                item['evidence_image'] = rendered[key]

    def apply_reviews(self, path):
        """Load explicit visual decisions bound to exact input and snippets."""
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('source_sha256') != self.source_hash or data.get('markdown_sha256') != self.md_hash:
            raise ValueError('复核决定与本次 PDF/Markdown 哈希不一致，拒绝沿用')
        for decision in data.get('decisions', []):
            selector = decision['match']
            candidates = [f for f in self.findings if all(f.get(k) == v for k, v in selector.items())]
            if len(candidates) != 1:
                raise ValueError(f'复核定位应唯一命中，实际命中 {len(candidates)} 条：{selector}')
            finding = candidates[0]
            if fingerprint_text(finding['source_text']) != decision['source_text_sha256'] or fingerprint_text(finding['converted_text']) != decision['converted_text_sha256']:
                raise ValueError('复核文段指纹变化，拒绝沿用决定')
            finding['visual_review'] = {k: v for k, v in decision.items() if k not in {'match', 'source_text_sha256', 'converted_text_sha256'}}
            if decision['outcome'] == 'confirmed_difference':
                finding['severity'] = 'error'
            elif decision['outcome'] == 'confirmed_equivalent':
                finding['severity'] = 'info'
            else:
                raise ValueError('复核 outcome 必须为 confirmed_difference 或 confirmed_equivalent')

    def run(self, *, verify_images=True, ocr='risk', ocr_dpi=150,
            ocr_sample=12, evidence_limit=48, page_limit=None, review_file=None):
        start = time.monotonic()
        self.presentation_pages = set()
        pages, numbers = parse_markdown(self.text)
        with pymupdf.open(self.source) as doc:
            expected = list(range(1, len(doc) + 1))
            if numbers != expected:
                self.add('page_anchor_difference', 'error', None, 'PDF 页锚不连续、缺页、重复或顺序与原文件不同',
                         missing_pages=sorted(set(expected) - set(numbers)),
                         duplicate_pages=sorted(n for n, count in Counter(numbers).items() if count > 1),
                         actual_sequence=numbers)
            if self.audit.get('sha256') != self.source_hash:
                self.add('source_fingerprint_mismatch', 'error' if self.audit else 'review', None,
                         '原 PDF 哈希与转换记录不匹配或转换记录缺失', expected=self.audit.get('sha256'), actual=self.source_hash)
            asset = self.audit.get('source_asset')
            if asset:
                path = self.resolve_asset(asset)
                if path is None or not path.exists() or fingerprint(path) != self.source_hash:
                    self.add('source_copy_mismatch', 'error', None, '转换包保存的原 PDF 副本缺失或字节内容不同', asset=asset)
            valid_pages = [p for p in pages if 1 <= p.number <= len(doc)]
            if page_limit:
                valid_pages = valid_pages[:page_limit]
            for i, md in enumerate(valid_pages):
                try:
                    with oriented(doc, md.number - 1) as (page, chars, angle):
                        self.check_page(page, chars, md, angle, verify_images)
                except Exception as exc:
                    self.add('page_check_failed', 'error', md.number, f'该页校验未完成：{type(exc).__name__}: {exc}')
                if (i + 1) % 50 == 0 or i + 1 == len(valid_pages):
                    print(f'fidelity: {i + 1}/{len(valid_pages)} pages; {len(self.findings)} findings; {self.image_checks} image checks',
                          file=sys.stderr, flush=True)
            optical = []
            selected = []
            if ocr != 'none':
                eligible = sorted({p.number for p in valid_pages})
                if ocr == 'all':
                    selected = eligible
                else:
                    selected = sorted({f['pdf_page'] for f in self.findings if f.get('pdf_page') in eligible and
                                       f['kind'] in {'sparse_text_review', 'source_encoding_review'}})
                    selected += [eligible[min(len(eligible) - 1, int((i + .5) * len(eligible) / ocr_sample))]
                                 for i in range(ocr_sample)] if eligible and ocr_sample else []
                    selected += [f['pdf_page'] for f in self.findings if f.get('pdf_page') in eligible
                                 and f['kind'] in {'cell_content_difference', 'body_content_difference'}][:8]
                    selected = sorted(set(selected))
                optical = self.optical_review(doc, valid_pages, selected, ocr_dpi)
            if review_file:
                self.apply_reviews(review_file)
            self.evidence(doc, evidence_limit)
            total_pages = len(doc)
        for path, before in [(self.source, self.source_hash), (self.markdown, self.md_hash)]:
            if fingerprint(path) != before:
                self.add('input_changed_during_check', 'error', None, '检查期间输入文件发生变化，结果不可用于验收', path=str(path))
        summary = {'algorithm': 'region-grounded dual-witness fidelity', 'version': VERSION,
                   'algorithm_sha256': self.algorithm_hash,
                   'source': str(self.source), 'markdown': str(self.markdown),
                   'source_sha256': self.source_hash, 'markdown_sha256': self.md_hash,
                   'pdf_pages': total_pages, 'checked_pages': len(self.page_metrics),
                   'page_anchor_count': len(numbers), 'image_checks': self.image_checks,
                   'fraction_checks': self.fraction_checks,
                   'script_checks': self.script_checks,
                   'script_review_glyphs': self.script_review_glyphs,
                   'script_cross_cell_candidates': self.script_cross_cell_candidates,
                   'body_script_review_glyphs': self.body_script_review_glyphs,
                   'body_script_checks': self.body_script_checks,
                   'contents_entry_checks': self.contents_entry_checks,
                   'contents_leader_source_characters': self.contents_leader_characters,
                   'derived_fraction_syntax_characters': self.math_syntax_characters,
                   'images_verified': verify_images, 'ocr_mode': ocr, 'ocr_requested_pages': selected,
                   'ocr_completed_pages': [p['pdf_page'] for p in optical], 'ocr_results': optical,
                   'presentation_alias_pages': sorted(self.presentation_pages),
                   'finding_counts': dict(Counter(f['kind'] for f in self.findings)),
                   'severity_counts': dict(Counter(f['severity'] for f in self.findings)),
                   'pages_with_character_differences': [p['pdf_page'] for p in self.page_metrics
                                                         if p['missing_characters'] or p['extra_characters']],
                   'source_characters': sum(p['source_characters'] for p in self.page_metrics),
                   'converted_characters': sum(p['converted_characters'] for p in self.page_metrics),
                   'converted_character_scope': 'source_glyphs_including_contents_leader_metadata_excluding_derived_fraction_notation',
                   'seconds': round(time.monotonic() - start, 3),
                   'complete_text_layer_check': len(self.page_metrics) == total_pages and numbers == expected
                                                and not any(f['kind'] == 'page_check_failed' for f in self.findings),
                   'limitations': [
                       '逐页及单元格字符守恒不能单独证明顺序、公式语义或印刷字形正确。',
                       '目录长引导线缩短显示，原字符从装饰来源属性参与独立比对；转换字符数不是屏幕可见字数。',
                       '显式分数的括号和斜杠是派生表示；仅在HTML形状和原PDF矢量线/数字独立核验后从原字符计数排除。',
                       '仅对白空格、全角 ASCII 与上下标表示作明确等价；保留字词、标点、大小写和数值差异。',
                       '表格坐标来自转换记录，但字符、图像均独立读取原 PDF；分区本身的语义需复核。',
                       '正文按原生行及几何顺序提供顺序见证；复杂公式/多栏不作无证据的错误判定。',
                       'OCR 是带置信度的候选见证，不能作原文真值；risk 模式不是全书视觉识别。',
                       '图像校验验证已引用的图表/页图；未独立导出的矢量图形和公式的语义无法据此验收。',
                       'PDF 渲染版本变化可能造成像素差异；报告记录 PyMuPDF 版本。'],
                   'runtime': {'python': sys.version.split()[0], 'pymupdf': pymupdf.VersionBind}}
        summary['confirmed_visual_differences'] = sum(
            f.get('visual_review', {}).get('outcome') == 'confirmed_difference' for f in self.findings)
        summary['review_file'] = str(Path(review_file).resolve()) if review_file else None
        summary['status'] = 'differences_found' if summary['severity_counts'].get('error') else 'review_required' if self.findings else 'passed_text_layer'
        atomic_json(self.out / 'summary.json', summary)
        atomic_text(self.out / 'findings.jsonl', ''.join(json.dumps(f, ensure_ascii=False) + '\n' for f in self.findings))
        atomic_text(self.out / 'pages.jsonl', ''.join(json.dumps(p, ensure_ascii=False) + '\n' for p in self.page_metrics))
        atomic_text(self.out / '检查报告.md', self.report(summary))
        return summary

    def report(self, summary):
        def excerpt(value, limit=400):
            value = value.replace('\r', '').replace('\n', ' ⏎ ')
            return html.escape(value[:limit]) + (' …（完整文段见 findings.jsonl）' if len(value) > limit else '')

        source_link = quote(self.source.as_posix(), safe='/:')
        md_link = quote(self.markdown.as_posix(), safe='/:')
        lines = ['# 原 PDF 与转换 Markdown 内容一致性检查报告', '',
                 f'- 原文件：[{self.source.name}]({source_link})',
                 f'- 转换文件：[{self.markdown.name}]({md_link})',
                 f'- 算法版本：{VERSION}；PyMuPDF {pymupdf.VersionBind}',
                 f'- 原 PDF SHA-256：{self.source_hash}',
                 f'- Markdown SHA-256：{self.md_hash}', '', '## 检查结论', '',
                 f'检查 {summary["checked_pages"]}/{summary["pdf_pages"]} 页，独立读取原始非空白字符 {summary["source_characters"]:,} 个，Markdown 对应原字形字符 {summary["converted_characters"]:,} 个（分数派生表示另行核验、记账）。', '',
                 f'逐页字符差集异常页：{len(summary["pages_with_character_differences"])}；确定性差异/失败记录：{summary["severity_counts"].get("error", 0)}；待复核记录：{summary["severity_counts"].get("review", 0)}。记录可重叠，不能把记录数视为独立错字数。', '',
                 f'图片验证 {summary["image_checks"]:,} 次；独立 OCR 完成 {len(summary["ocr_completed_pages"])} 页。', '',
                 f'分数矢量线与原数字独立核验 {summary["fraction_checks"]:,} 次；线性化括号/斜杠共 {summary["derived_fraction_syntax_characters"]:,} 个单独记账，不算原PDF文字字形。', '',
                 f'上下标及脚注的字形、基线方向和主体绑定独立核验 {summary["script_checks"]:,} 次；未绑定的小字形候选 {summary["script_review_glyphs"]:,} 个，单独列为复核项。', '',
                 f'正文上下标独立核验 {summary["body_script_checks"]:,} 次；正文未绑定候选 {summary["body_script_review_glyphs"]:,} 个；跨单元格候选 {summary["script_cross_cell_candidates"]:,} 个。候选为复核线索，不等同于已确认错误。', '',
                 '状态：' + summary['status'] + '。' + ('文字层全书检查已完成。' if summary['complete_text_layer_check'] else '文字层检查不完整，不能作为全书验收结论。'), '',
                 '**不得将“逐页字符相同”解释为“整本书无偏差”。顺序、扫描页、复杂公式与未编码图形分别列入复核范围。**', '',
                 '## 算法与证据链', '',
                 '1. 对 origin 原文件和 Markdown 计算 SHA-256，验证附件 PDF 的身份，并检查页锚的完整性、重复与顺序。',
                 '2. 独立读取原 PDF 字符及几何；按正文/表格/单元格分区，解析 Markdown 的可见 HTML，剔除来源导航、图片说明和生成摘要。',
                 '3. 逐页与逐单元格比较字符多重集，保留数值、单位和标点；同时用原生字序、几何字序和原生行连续性定位顺序风险。',
                 '4. 对差异区域执行有上界的局部字符序列对齐，输出原文、转换文段、增删替换、PDF 坐标及 Markdown 行号；大区域保留完整证据，避免无界二次复杂度。',
                 '5. 重新渲染已保存的表格原图并逐像素核对；补线图只允许已登记的补线；JPEG 原页图使用明确容差。',
                 '6. 对低文本页/风险页及确定性分层样本执行独立 OCR；候选冲突标记为待复核，绝不自动更改原文。', '',
                 '### 类型统计', '', '| 类型 | 记录数 |', '|---|---:|']
        lines.extend(f'| {kind} | {count} |' for kind, count in sorted(summary['finding_counts'].items()))
        lines += ['', '### 视觉识别覆盖', '',
                  f'OCR 完成 PDF 页：{", ".join(map(str, summary["ocr_completed_pages"])) or "无"}。', '',
                  f'有表示等价（如上下标/全角）但不能视为严格 Unicode 相同的页数：{len(summary["presentation_alias_pages"])}。', '',
                  '## 局部差异与复核文段（完整索引）', '',
                  '下列列表覆盖全部检查记录；较长文段仅在 Markdown 中节选，完整文段及局部 diff 均保存在 findings.jsonl。错误与待复核应分别处理。', '']
        confirmed = [f for f in self.findings if f.get('visual_review', {}).get('outcome') == 'confirmed_difference']
        if confirmed:
            lines += ['### 已对照原图确认的偏差', '', '| 记录 | PDF页 / MD行 | 原图正确表达 | 转换文段 | 来源判断 |',
                      '|---|---|---|---|---|']
            for f in confirmed:
                review = f['visual_review']
                lines += [f'| {f["id"]} | {f["pdf_page"]} / {f.get("md_lines", ["?"])[0]} | {excerpt(review["expected_text"])} | {excerpt(f["converted_text"])} | {excerpt(review["cause"])} |']
            lines += ['', '上述确认结论由 Codex 对照原 PDF 渲染区域图作出，记录绑定本次原文件、Markdown 及局部文段指纹。', '']
        for item in sorted(self.findings, key=lambda f: (f['severity'] != 'error', f['pdf_page'] or 0, f['id'])):
            number = item.get('pdf_page')
            location = f'PDF 第 {number} 页' if number else '文件级'
            if item.get('table_id'):
                location += f'，表 {item["table_id"]}'
            if item.get('row') is not None:
                location += f'，第 {item["row"] + 1} 行第 {item["column"] + 1} 列'
            if item.get('md_lines'):
                location += f'，MD 第 {item["md_lines"][0]}–{item["md_lines"][1]} 行'
            lines += [f'### {item["id"]} · {item["severity"]} · {item["kind"]}', '', location, '', item['reason'], '']
            if number:
                lines += [f'[原 PDF 页]({source_link}#page={number}) · [Markdown 页]({md_link}#pdf-page-{number})', '']
            if item.get('bbox'):
                lines += [f'原文区域（归一化 PDF 点坐标）：{item["bbox"]}。', '']
            if item.get('source_text') or item.get('converted_text'):
                lines += ['原文/来源见证：', '', '> ' + (excerpt(item['source_text']) or '∅'), '',
                          '转换文段/本地文字层见证：', '', '> ' + (excerpt(item['converted_text']) or '∅'), '']
            if item.get('character_delta'):
                lines += ['字符差集：' + html.escape(json.dumps(item['character_delta'], ensure_ascii=False)) + '。', '']
            if item.get('visual_review'):
                review = item['visual_review']
                lines += ['原图复核结论：' + review['outcome'] + '。', '',
                          '原图正确表达：' + html.escape(review['expected_text']), '',
                          '偏差来源：' + review['cause'], '',
                          '复核说明：' + review['notes'], '']
            if item.get('evidence_image'):
                lines += [f'[原文区域图]({item["evidence_image"]})', '']
        lines += ['## 输出与边界', '',
                  '- summary.json：指纹、覆盖范围、运行参数、结论及计数。',
                  '- findings.jsonl：全部局部偏差和待复核记录，含完整文段、局部差异、坐标和行号。',
                  '- pages.jsonl：每页独立字符数量与差集计数。',
                  '- evidence/：优先记录的来源区域图片；PDF 页链接覆盖全部记录。',
                  '- ocr_cache/：按原文 SHA-256、页码、DPI 和引擎版本绑定的独立 OCR 见证。', '']
        lines.extend('- ' + value for value in summary['limitations'])
        return '\n'.join(lines) + '\n'


def atomic_text(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(value, encoding='utf-8', newline='\n')
    temporary.replace(path)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def fingerprint_text(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description='独立比对原 PDF 与带页锚 Markdown，生成可定位的检查报告。')
    parser.add_argument('source', type=Path)
    parser.add_argument('markdown', type=Path)
    parser.add_argument('-o', '--output', type=Path, required=True)
    parser.add_argument('--ocr', choices=['none', 'risk', 'all'], default='risk')
    parser.add_argument('--ocr-dpi', type=int, choices=range(72, 301), default=150, metavar='72..300')
    parser.add_argument('--ocr-sample', type=int, default=12)
    parser.add_argument('--skip-images', action='store_true')
    parser.add_argument('--evidence-limit', type=int, default=48)
    parser.add_argument('--page-limit', type=int, help='开发抽查；报告明确标记未全书覆盖')
    parser.add_argument('--review-file', type=Path, help='绑定原文/MD及局部文段指纹的视觉复核决定 JSON')
    args = parser.parse_args(argv)
    if args.ocr_sample < 0 or args.evidence_limit < 0 or args.page_limit is not None and args.page_limit < 1:
        parser.error('样本数/证据数不能为负，page-limit 必须大于零')
    try:
        result = Inspector(args.source, args.markdown, args.output).run(
            verify_images=not args.skip_images, ocr=args.ocr, ocr_dpi=args.ocr_dpi,
            ocr_sample=args.ocr_sample, evidence_limit=args.evidence_limit, page_limit=args.page_limit,
            review_file=args.review_file)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f'fidelity: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 1 if result['severity_counts'].get('error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
