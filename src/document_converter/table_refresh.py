"""Rebuild saved table text from the original PDF; recheck stale line roles.

All glyphs and coordinate spaces are reread from the source. Existing table
geometry is an explicit input; no text is patched by book/page-specific rules.
An opt-in grid recheck rerenders pages whose fraction roles have changed.
Markdown, structure, audit, changed images and volumes commit with rollback.
"""
from __future__ import annotations

from collections import Counter
from collections import defaultdict
import json
from pathlib import Path
import re
import tempfile

import pymupdf
from bs4 import BeautifulSoup

from .fidelity import SpatialIndex, canonical, fingerprint, oriented
from .pdf_tables import REVISION, editable_table, extract_tables
from .units import cell_unit
from .fraction_layout import cell_layout, detect_fractions
from .volumes import write_volumes
from .script_witness import verify_script
from .pdf_tables import body_blocks
from .body_script_layout import key as body_key


def refresh_table_text(source: Path, markdown: Path, *, recheck_fraction_grid=False) -> dict:
    source, markdown = source.resolve(), markdown.resolve()
    old_md = markdown.read_text(encoding='utf-8')
    md_hash = fingerprint(markdown)
    audit_path = markdown.with_suffix('.audit.json')
    audit = json.loads(audit_path.read_text(encoding='utf-8'))
    source_hash = fingerprint(source)
    if audit['sha256'] != source_hash:
        raise ValueError('原文指纹与结构记录不匹配，拒绝重建')
    assets = markdown.parent / audit['asset_directory']
    structure_path = assets / 'structure.jsonl'
    records = [json.loads(line) for line in structure_path.read_text(encoding='utf-8').splitlines()]
    tables = {}
    changes = 0
    regridded_pages = []
    body_layouts = {}
    staged_images = tempfile.TemporaryDirectory(prefix='.fraction-images-', dir=markdown.parent)
    with pymupdf.open(source) as doc:
        if [p['pdf_page'] for p in records] != list(range(1, len(doc)+1)):
            raise ValueError('结构记录页码不完整')
        for record in records:
            with oriented(doc, record['pdf_page'] - 1) as (page, chars, _):
                coordinate = record['coordinate_space']
                if abs(page.rect.width - coordinate['width']) > .1 or abs(page.rect.height - coordinate['height']) > .1:
                    raise ValueError('原始 PDF 坐标系与结构记录不一致')
                index = SpatialIndex(chars)
                drawings = page.get_drawings()
                if recheck_fraction_grid:
                    # Revisit only pages whose recorded fraction roles changed.
                    # Full grid detection then reruns from the original PDF;
                    # no page-specific geometry patches or inherited fallback.
                    fractions = detect_fractions([c | {'block':c['line'][0], 'line':c['line'][1]} for c in chars], drawings)
                    signature = lambda f: (tuple(round(v,2) for v in f['bar']),f['numerator'],f['denominator'])
                    stale = any(
                        {signature(f) for c in t['cells'] for f in c.get('fractions',[])} !=
                        {signature(f) for f in fractions if t['bbox'][0]<=f['anchor'][0]<=t['bbox'][2]
                         and t['bbox'][1]<=f['anchor'][1]<=t['bbox'][3]}
                        for t in record['tables'])
                    if stale:
                        adapted = [c | {'block':c['line'][0], 'line':c['line'][1]} for c in chars]
                        rebuilt, issues = extract_tables(page,adapted,Path(staged_images.name),assets.name,record['pdf_page'])
                        identity = lambda ts: {t['id']:tuple(round(v,2) for v in t['bbox']) for t in ts}
                        if identity(rebuilt) != identity(record['tables']):
                            raise ValueError('分数角色变化造成表格来源区域变化，需完整重转，拒绝局部提交')
                        if any(t.get('fallback') and not next(o for o in record['tables'] if o['id']==t['id']).get('fallback') for t in rebuilt):
                            raise ValueError('网格复核引入新的降级，拒绝局部提交')
                        record['tables'] = rebuilt
                        regridded_pages.append(record['pdf_page'])
                claimed = set()
                for table in record['tables']:
                    tables[table['id']] = table
                    for cell in table['cells']:
                        if cell.get('geometry_gap'):
                            continue
                        original = [c for c in index.inside(cell['bbox']) if c['id'] not in claimed]
                        claimed.update(c['id'] for c in original)
                        adapted = [c | {'block': c['line'][0], 'line': c['line'][1]} for c in original]
                        layout = cell_layout(adapted, drawings)
                        used_scripts = set()
                        for script in layout.get('scripts', []):
                            issue = verify_script(script, adapted, used_scripts)
                            if issue:
                                raise ValueError(f'{table["id"]} r{cell["row"]} c{cell["column"]}: {issue}')
                        value = layout['text']
                        if Counter(canonical(layout.get('source_text', value))) != Counter(canonical(''.join(c['text'] for c in original))):
                            raise ValueError(f'{table["id"]}: 重建文字违反字符守恒')
                        changes += cell['text'] != value
                        for key in ('source_text', 'linear_text', 'math_parts', 'fractions', 'scripts', 'script_parts', 'script_reviews'):
                            cell.pop(key, None)
                        cell.update(layout)
                        cell.pop('unit', None)
                        unit = cell_unit(value)
                        if unit:
                            cell['unit'] = unit
                    table['numeric_fraction_count'] = sum(len(c.get('fractions', [])) for c in table['cells'])
                    table['mixed_number_count'] = sum('whole' in f for c in table['cells'] for f in c.get('fractions', []))
                adapted_body = [c | {'block': c['line'][0], 'line': c['line'][1]} for c in chars]
                blocks = body_blocks(adapted_body, claimed, [t['bbox'] for t in record['tables']])
                candidates = [b for b in blocks if b.get('scripts')]
                body_layouts[record['pdf_page']] = candidates
                record['body_scripts'] = []
                record['body_script_reviews'] = [b['script_reviews'] for b in blocks if b.get('script_reviews')]
            if record['pdf_page'] % 100 == 0:
                print(f'table refresh: {record["pdf_page"]}/{len(records)}', flush=True)
    replacements = []
    seen = set()
    for marker in re.finditer(r'(?m)^<!-- TABLE:([^>\s]+) -->[ \t]*$', old_md):
        table_id = marker[1]
        if table_id in seen or table_id not in tables:
            raise ValueError('Markdown 表格标记与结构记录不一致')
        seen.add(table_id)
        start = old_md.index('<summary>', marker.end())
        end = old_md.index('</details>', start)
        table=tables[table_id]
        summary=('展开保留的原表文字（网格不确定，未推断行列）' if table.get('fallback') else
                 f'展开表格文本（{table["rows"]} 行 × {table["columns"]} 列，保留合并单元格）')
        replacements.append((start, end, '<summary>'+summary+'</summary>\n\n'+editable_table(table)+'\n\n'))
        # Old and new source-image references must match regenerated assets.
        close=old_md.index('</details>',start)+len('</details>')
        original_link=re.match(r'\s*\[对照补线前原表\]\([^\n]+\)\s*',old_md[close:])
        if table.get('original_image') and not original_link:
            replacements.append((close,close,'\n\n[对照补线前原表]('+table['original_image']+')\n'))
        elif original_link and not table.get('original_image'):
            replacements.append((close,close+original_link.end(),'\n\n'))
    if seen != set(tables):
        raise ValueError('Markdown 缺少结构记录对应的表格')
    # Insert semantic tags only where existing prose is an exact normalized
    # witness. Never reflow a paragraph merely to make an attachment fit.
    page_markers = list(re.finditer(r'(?m)^<!-- PDF_PAGE:(\d+) -->\s*$', old_md))
    body_changes = 0
    for pi, marker in enumerate(page_markers):
        number = int(marker[1])
        end = page_markers[pi+1].start() if pi+1 < len(page_markers) else len(old_md)
        candidates = body_layouts.get(number, [])
        page_text = old_md[marker.end():end]
        existing = list(re.finditer(r'(?ms)^<!-- BODY_SCRIPT:([^>\s]+) -->\n(.*?)\n<!-- /BODY_SCRIPT -->[ \t]*$', page_text))
        existing_values = {prior.start(): body_key(BeautifulSoup(prior[2], 'html.parser').get_text())
                           for prior in existing}
        lines = []
        for line in re.finditer(r'(?m)^.+$', page_text):
            if any(prior.start() <= line.start() < prior.end() for prior in existing):
                continue
            value = line[0]
            if value.lstrip().startswith(('<', '[', '!', '>')):
                continue
            value = re.sub(r'^#{1,6}\s+', '', value)
            lines.append((line.start(), line.end(), body_key(value)))
        by_key = defaultdict(list)
        for block in candidates:
            by_key[body_key(block['text'])].append(block)
        assigned = set()
        consumed = set()
        for needle, blocks in by_key.items():
            blocks = sorted(blocks, key=lambda block: min(block['character_ids']))
            locations = []
            # An existing marker with the same stable ID is the strongest
            # alignment witness when a refreshed artifact is processed again.
            for block in blocks:
                prior = next((p for p in existing if p[1] == block['body_id']
                              and existing_values[p.start()] == needle), None)
                if prior:
                    locations.append((block, marker.end()+prior.start(), marker.end()+prior.end()))
                    assigned.add(block['body_id'])
                    consumed.add(prior.start())
            remaining = [block for block in blocks if block['body_id'] not in assigned]
            candidates_at_key = [(lo, hi) for lo, hi, value in lines if value == needle and
                                 not any(lo < prior.end() and hi > prior.start() for prior in existing)]
            if len(candidates_at_key) == len(remaining):
                locations.extend((block, marker.end()+lo, marker.end()+hi)
                                 for block, (lo, hi) in zip(remaining, candidates_at_key, strict=True))
                assigned.update(block['body_id'] for block in remaining)
                consumed.update(lo for lo, _ in candidates_at_key)
            for block in remaining:
                if block['body_id'] in assigned:
                    continue
                records[number-1].setdefault('body_script_reviews', []).append([{'reason': 'saved_body_alignment_requires_review',
                                            'text': block['text'], 'character_ids': block['character_ids']}])
            for block, lo, hi in locations:
                if block['body_id'] not in assigned:
                    continue
                rendered = f'<!-- BODY_SCRIPT:{block["body_id"]} -->\n'+block['html_text']+'\n<!-- /BODY_SCRIPT -->'
                replacements.append((lo, hi, rendered))
                records[number-1]['body_scripts'].append({k: block[k] for k in ('body_id', 'character_ids', 'scripts', 'linear_text', 'text')})
                body_changes += old_md[lo:hi] != rendered
        if any(prior.start() not in consumed for prior in existing):
            raise ValueError(f'第 {number} 页已有正文上下标无法唯一重建，拒绝提交不一致的结构记录')
    new_md = old_md
    for start, end, value in sorted(replacements,reverse=True):
        new_md = new_md[:start] + value + new_md[end:]
    if fingerprint(source) != source_hash or fingerprint(markdown) != md_hash:
        raise ValueError('重建期间输入变化')
    audit['extraction_model'] = f'PyMuPDF original glyphs + rectangular cells and spans; {REVISION}'
    audit.setdefault('fraction_reconstruction', {}).update({
        'numeric_fractions': sum(t['numeric_fraction_count'] for t in tables.values()),
        'mixed_numbers': sum(t['mixed_number_count'] for t in tables.values())})
    audit['table_details'] = [
        {k:v for k,v in t.items() if k not in {'cells','x_boundaries','y_boundaries','inferred_rules'}} |
        {'inferred_rule_count':len(t['inferred_rules'])} for t in tables.values()]
    audit['repaired_tables'] = sum(bool(t['inferred_rules']) for t in tables.values())
    audit['merged_cells'] = sum(t['merged_cells'] for t in tables.values())
    audit['issues'] = [issue for issue in audit.get('issues', [])
                       if not ('table retained as source text' in issue
                               and issue.split(':',1)[0] in tables
                               and not tables[issue.split(':',1)[0]].get('fallback'))]
    new_md = re.sub(r'(?m)^- 补绘缺失行线的表格：\d+ 个；原表对照图同时保留$',
                    f'- 补绘缺失行线的表格：{audit["repaired_tables"]} 个；原表对照图同时保留',new_md)
    audit['table_text_refresh'] = {'previous_markdown_sha256': md_hash, 'source_sha256': source_hash,
                                   'changed_cells': changes, 'tables': len(tables),
                                   'fraction_grid_rechecked_pages': regridded_pages,
                                   'method': 'original PDF local baselines, letter/numeric script runs with provenance, vector fractions and conservative unit syntax'}
    audit['script_reconstruction'] = {
        'runs': sum(len(c.get('scripts', [])) for t in tables.values() for c in t['cells']),
        'body_runs': sum(len(b['scripts']) for r in records for b in r.get('body_scripts', [])),
        'changed_body_blocks': body_changes,
        'review_candidates': sum(len(c.get('script_reviews', [])) for t in tables.values() for c in t['cells'])}
    volume_dir = markdown.parent / (markdown.stem+'_volumes')
    with tempfile.TemporaryDirectory(prefix='.table-refresh-', dir=markdown.parent) as name:
        stage = Path(name)
        staged_md = stage / markdown.name
        staged_md.write_text(new_md, encoding='utf-8', newline='\n')
        staged_structure = stage / 'structure.jsonl'
        staged_structure.write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in records), encoding='utf-8')
        staged_volumes = stage / volume_dir.name
        audit['volumes'] = write_volumes(new_md, audit['pdf_pages'], staged_volumes, markdown.name, assets.name)
        staged_audit = stage / audit_path.name
        staged_audit.write_text(json.dumps(audit, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        targets = [(staged_md, markdown), (staged_structure, structure_path), (staged_audit, audit_path)]
        for image in (Path(staged_images.name)/'tables').glob('*.png'):
            targets.append((image,assets/'tables'/image.name))
        if audit['volumes']:
            if {p.name for p in staged_volumes.iterdir()} != {p.name for p in volume_dir.iterdir()}:
                raise ValueError('分卷文件集合变化，拒绝局部重建')
            targets += [(p, volume_dir/p.name) for p in staged_volumes.iterdir()]
        backup = stage / 'backup'
        backup.mkdir()
        committed = []
        saved = []
        try:
            for i, (staged, target) in enumerate(targets):
                original = backup / str(i)
                if target.exists():
                    target.replace(original)
                    saved.append((original, target))
                staged.replace(target)
                committed.append(target)
        except Exception:
            for target in reversed(committed):
                target.unlink(missing_ok=True)
            for original, target in reversed(saved):
                original.replace(target)
            raise
    staged_images.cleanup()
    return {'tables': len(tables), 'changed_cells': changes, 'changed_body_blocks': body_changes, 'fraction_grid_rechecked_pages':regridded_pages,
            'markdown_sha256': fingerprint(markdown)}
