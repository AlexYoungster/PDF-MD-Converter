"""Geometry-preserving PDF tables, with conservative repair of missing row rules.

The visual representation uses the original PDF glyphs and graphics. Only row
rules supported by both drawing endpoints and text alignment are added. The
editable representation retains the actual rectangular cells and their spans.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import html
from pathlib import Path
import re
import unicodedata
from statistics import median
from typing import Any
from .units import cell_unit, unit_html
from .fraction_layout import detect_fractions, atomic_glyphs, cell_layout, fraction_html

import pymupdf


REVISION = "geometry-grid-10-spatial-printed-contents"

_SUPERSCRIPT = str.maketrans({"0": "⁰", "1": "¹", "2": "²", "3": "³",
                              "4": "⁴", "5": "⁵", "6": "⁶",
                              "7": "⁷", "8": "⁸", "9": "⁹",
                              "+": "⁺", "-": "⁻", "−": "⁻",
                              "=": "⁼", "(": "⁽", ")": "⁾"})
_SUBSCRIPT = str.maketrans({"0": "₀", "1": "₁", "2": "₂", "3": "₃",
                            "4": "₄", "5": "₅", "6": "₆",
                            "7": "₇", "8": "₈", "9": "₉",
                            "+": "₊", "-": "₋", "−": "₋",
                            "=": "₌", "(": "₍", ")": "₎"})
_SUPERSCRIPT_VALUE = str.maketrans({"⁰": "0", "¹": "1", "²": "2", "³": "3",
                                   "⁴": "4", "⁵": "5", "⁶": "6",
                                   "⁷": "7", "⁸": "8", "⁹": "9",
                                   "⁺": "+", "⁻": "-"})
_SCIENTIFIC = re.compile(
    r"(?<![\w.])((?:\d+(?:\.\d*)?|\.\d+))\s*[×xX]\s*10([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)(?![\w])")


def characters(page: pymupdf.Page) -> list[dict[str, Any]]:
    result = []
    for bi, block in enumerate(page.get_text("rawdict").get("blocks", [])):
        for li, line in enumerate(block.get("lines", [])):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    result.append({"id": len(result), "text": char["c"], "bbox": list(char["bbox"]),
                                   "origin": list(char["origin"]), "size": span["size"],
                                   "direction": tuple(line["dir"]), "block": bi, "line": li})
    return result


def inside(char: dict, box: list | tuple) -> bool:
    x0, y0, x1, y1 = char["bbox"]
    return box[0] - .1 <= (x0 + x1) / 2 <= box[2] + .1 and box[1] - .1 <= (y0 + y1) / 2 <= box[3] + .1


def cluster(values: list[float], tolerance: float = .65) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if not groups or value - groups[-1][0] > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def text_lines(chars: list[dict]) -> list[str]:
    from .glyph_layout import table_text_lines
    return table_text_lines(chars)


def _html_cell(text: str) -> str:
    """Render searchable cell text and expose scientific values to parsers."""
    output, position = [], 0
    for match in _SCIENTIFIC.finditer(text.replace("\x00", "�")):
        output.append(html.escape(text[position:match.start()].replace("\x00", "�"), quote=False))
        mantissa = match.group(1)
        exponent = match.group(2).translate(_SUPERSCRIPT_VALUE)
        sign = "-" if exponent.startswith("-") else "+" if exponent.startswith("+") else ""
        digits = exponent[1:] if sign else exponent
        canonical = mantissa + "e" + sign + str(int(digits or "0"))
        visible = html.escape(match.group(0), quote=False)
        output.append(f'<data value="{html.escape(canonical, quote=True)}">{visible}</data>')
        position = match.end()
    output.append(html.escape(text[position:].replace("\x00", "�"), quote=False))
    return "".join(output)


def _vertical_endpoints(drawings: list[dict], box: tuple) -> list[tuple[float, float]]:
    endpoints = []
    for path in drawings:
        for item in path.get("items", []):
            if item[0] == "l":
                a, b = item[1:3]
                if abs(a.x - b.x) <= .7 and abs(a.y - b.y) >= 4:
                    x, lo, hi = (a.x + b.x) / 2, min(a.y, b.y), max(a.y, b.y)
                else:
                    continue
            elif item[0] == "re":
                rect = item[1]
                if rect.width > 3 or rect.height < 4:
                    continue
                x, lo, hi = (rect.x0 + rect.x1) / 2, rect.y0, rect.y1
            else:
                continue
            if box[0] - 1 <= x <= box[2] + 1:
                endpoints.extend((x, y) for y in (lo, hi) if box[1] + 2 < y < box[3] - 2)
    return endpoints


def _repair_rows(boxes: list[tuple], chars: list[dict], drawings: list[dict], bbox: tuple) -> tuple[list[tuple], list[dict]]:
    """Split only coextensive cells, never a lone multirow label.

    A candidate boundary needs independent vertical tracks ending there, text
    above and below in two cells, and no baseline at the rule.
    This leaves wrapped text and genuinely merged cells intact.
    """
    fractions = detect_fractions(chars, drawings)
    atoms = atomic_glyphs(chars, fractions)
    endpoints = _vertical_endpoints(drawings, bbox)
    ys = cluster([y for _, y in endpoints])
    supported = []
    for y in ys:
        tracks = cluster([x for x, ey in endpoints if abs(ey - y) <= .7], 3)
        if len(tracks) >= 2:
            supported.append(y)
    bands: dict[tuple, list[tuple]] = defaultdict(list)
    for box in boxes:
        bands[(round(box[1], 1), round(box[3], 1))].append(box)
    native_ys = cluster([v for box in boxes for v in (box[1], box[3])])
    typical_row_height = median(b - a for a, b in zip(native_ys, native_ys[1:]))
    replacements, repairs = {}, []
    for band in bands.values():
        if len(band) < 2:
            continue
        top, bottom = band[0][1], band[0][3]
        if bottom - top < 23:
            continue
        # Existing subdivisions elsewhere identify true rowspan cells. Also
        # avoid splitting ordinary wrapped rows in an already ruled table:
        # PDF producers often split vertical strokes at *every text line*.
        if any(top + 1 < y < bottom - 1 for y in native_ys):
            continue
        if len(native_ys) > 3 and bottom - top < typical_row_height * 2.5:
            continue
        contents = [[c for c in atoms if c["text"].strip() and inside(c, box)] for box in band]
        cuts = []
        for y in supported:
            if not top + 5 < y < bottom - 5:
                continue
            # A short fraction bar is content. Neither it nor any row cut may
            # separate its numerator and denominator, even across columns.
            if any(f['baseline_extent'][0] < y < f['baseline_extent'][1]
                   and bbox[0] <= f['anchor'][0] <= bbox[2] for f in fractions):
                continue
            # Cross-column agreement is required; an endpoint in a drawing or
            # a wrapped paragraph in just one cell cannot create a row.
            agreement = sum(any(c["origin"][1] < y - 2 for c in group)
                            and any(c["origin"][1] > y + 2 for c in group) for group in contents)
            if agreement < 2:
                continue
            if any(abs(c["origin"][1] - y) < 2
                   for group in contents for c in group):
                continue
            # A nearby real edge is already represented in the native grid.
            if cuts and y - cuts[-1] < 5:
                continue
            cuts.append(y)
        if not cuts:
            continue
        for box in band:
            boundaries = [box[1], *cuts, box[3]]
            replacements[box] = [(box[0], a, box[2], b) for a, b in zip(boundaries, boundaries[1:])]
            for y in cuts:
                repairs.append({"x0": box[0], "x1": box[2], "y": y,
                                "evidence": "segmented_vertical_rules_and_cross_column_atomic_text",
                                "fraction_atoms": len(fractions),
                                "fraction_cut_veto": True})
    return [part for box in boxes for part in replacements.get(box, [box])], repairs


def _model(native: Any, chars: list[dict], drawings: list[dict], table_id: str) -> dict:
    boxes = list(dict.fromkeys(tuple(round(v, 3) for v in box) for row in native.rows for box in row.cells if box))
    original_rows = native.row_count
    boxes, repairs = _repair_rows(boxes, chars, drawings, tuple(native.bbox))
    xs = cluster([v for box in boxes for v in (box[0], box[2])])
    ys = cluster([v for box in boxes for v in (box[1], box[3])])
    nearest = lambda values, value: min(range(len(values)), key=lambda i: abs(values[i] - value))
    cells, occupied, assigned = [], set(), set()
    for box in sorted(boxes, key=lambda b: (b[1], b[0])):
        c0, c1 = nearest(xs, box[0]), nearest(xs, box[2])
        r0, r1 = nearest(ys, box[1]), nearest(ys, box[3])
        slots = {(r, c) for r in range(r0, r1) for c in range(c0, c1)}
        if not slots or occupied.intersection(slots):
            raise ValueError("overlapping or degenerate native cell geometry")
        occupied.update(slots)
        contained = [c for c in chars if c["id"] not in assigned and inside(c, box)]
        assigned.update(c["id"] for c in contained)
        layout = cell_layout(contained, drawings)
        bag = lambda s: Counter(''.join(unicodedata.normalize('NFKC', s).split()).replace('−', '-'))
        if bag(layout.get('source_text', layout['text'])) != bag(''.join(c['text'] for c in contained)):
            raise ValueError('table layout did not conserve source glyphs')
        cells.append({"row": r0, "column": c0, "rowspan": r1 - r0, "colspan": c1 - c0,
                      "bbox": list(box), **layout,
                      "character_ids": [c["id"] for c in contained]})
    # Geometric holes must not shift the following cells when HTML is rendered.
    holes = 0
    for r in range(len(ys) - 1):
        for c in range(len(xs) - 1):
            if (r, c) not in occupied:
                holes += 1
                cells.append({"row": r, "column": c, "rowspan": 1, "colspan": 1,
                              "bbox": [xs[c], ys[r], xs[c + 1], ys[r + 1]], "text": "",
                              "character_ids": [], "geometry_gap": True})
    return {"id": table_id, "bbox": list(native.bbox), "rows": len(ys) - 1, "columns": len(xs) - 1,
            "native_rows": original_rows, "x_boundaries": xs, "y_boundaries": ys,
            "cells": cells, "inferred_rules": repairs, "geometry_gaps": holes,
            "merged_cells": sum(c["rowspan"] > 1 or c["colspan"] > 1 for c in cells)}


def editable_table(table: dict) -> str:
    if table["geometry_gaps"]:
        # Do not invent blank cells in a non-rectangular native grid. The exact
        # rendered table remains directly above this audit view in the Markdown.
        entries = []
        for cell in sorted(table["cells"], key=lambda c: (c["row"], c["column"])):
            if cell.get("geometry_gap") or not cell["text"]:
                continue
            span = f"；跨 {cell['rowspan']} 行、{cell['colspan']} 列" if cell["rowspan"] > 1 or cell["colspan"] > 1 else ""
            value = fraction_html(cell, lambda t: unit_html(t, _html_cell)).replace('\n', '<br>')
            entries.append(f'<li>第 {cell["row"] + 1} 行，第 {cell["column"] + 1} 列{span}：{value}</li>')
        return ('<p>该表含非矩形分区，以下保留原单元格坐标；上方原版表格图为完整视觉结构。</p>\n'
                '<ol>\n' + '\n'.join(entries) + '\n</ol>')
    lines = ['<table border="1" cellspacing="0" cellpadding="4">']
    for row in range(table["rows"]):
        lines.append("<tr>")
        for cell in sorted((c for c in table["cells"] if c["row"] == row), key=lambda c: c["column"]):
            attrs = f' rowspan="{cell["rowspan"]}"' if cell["rowspan"] > 1 else ""
            attrs += f' colspan="{cell["colspan"]}"' if cell["colspan"] > 1 else ""
            value = fraction_html(cell, lambda t: unit_html(t, _html_cell)).replace("\n", "<br>")
            lines.append(f"<td{attrs}>{value}</td>")
        lines.append("</tr>")
    lines.append("</table>")
    return "\n".join(lines)


def render_table(page: pymupdf.Page, table: dict, folder: Path, asset_name: str, dpi: int = 180) -> None:
    from PIL import Image, ImageDraw

    folder.mkdir(parents=True, exist_ok=True)
    clip = (pymupdf.Rect(table["bbox"]) + (-2, -2, 2, 2)) & page.rect
    pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    name = table["id"] + ".png"
    if table["inferred_rules"]:
        original = table["id"] + "-original.png"
        image.save(folder / original)
        table["original_image"] = f"{asset_name}/tables/{original}"
        draw = ImageDraw.Draw(image)
        scale = dpi / 72
        for rule in table["inferred_rules"]:
            # pix.x/y reflect the integer pixel clip origin, avoiding subpixel
            # rounding shifts between the PDF and the supplementary grid.
            draw.line([(rule["x0"] * scale - pix.x, rule["y"] * scale - pix.y),
                       (rule["x1"] * scale - pix.x, rule["y"] * scale - pix.y)],
                      fill=(95, 95, 95), width=max(1, round(scale * .3)))
    image.save(folder / name)
    table["image"] = f"{asset_name}/tables/{name}"
    table["image_dpi"] = dpi


def extract_tables(page: pymupdf.Page, chars: list[dict], assets: Path, asset_name: str,
                   page_number: int) -> tuple[list[dict], list[str]]:
    issues, result = [], []
    try:
        drawings = page.get_drawings()
        fractions = detect_fractions(chars, drawings)
        content_strokes = {tuple(item) for f in fractions for item in f['path_items']}
        # Remove only positively identified fraction items from grid detection;
        # keep every unrelated item, including those in the same PDF path.
        grid_paths = [path | {'items': [item for ii, item in enumerate(path['items'])
                                     if (pi, ii) not in content_strokes]}
                      for pi, path in enumerate(drawings)]
        found = page.find_tables(paths=[p for p in grid_paths if p['items']])
    except Exception as exc:
        return [], [f"page {page_number}: table detection failed ({type(exc).__name__}: {exc})"]
    claimed: set[int] = set()
    for index, native in enumerate(found.tables, 1):
        table_id = f"p{page_number:05}-t{index:02}"
        try:
            table = _model(native, [c for c in chars if inside(c, native.bbox)], drawings, table_id)
            ids = {i for cell in table["cells"] for i in cell["character_ids"]}
            if ids.intersection(claimed):
                raise ValueError("overlapping detected tables")
            for cell in table['cells']:
                unit = cell_unit(cell['text'])
                if unit:
                    cell['unit'] = unit
            table['numeric_fraction_count'] = sum(len(c.get('fractions', [])) for c in table['cells'])
            render_table(page, table, assets / "tables", asset_name)
            if table["geometry_gaps"]:
                issues.append(f"{table_id}: {table['geometry_gaps']} geometric gaps retained as empty cells")
            claimed.update(ids)
            result.append(table)
        except Exception as exc:
            # Preserve a visual crop and every still-unclaimed glyph. The
            # fallback makes no row/column claim, so ambiguous geometry cannot
            # silently become a misleading structured table.
            issues.append(f"{table_id}: table retained as source text ({type(exc).__name__}: {exc})")
            try:
                bbox = tuple(native.bbox)
                fallback_chars = [c for c in chars if c["id"] not in claimed and inside(c, bbox)]
                ids = {c["id"] for c in fallback_chars}
                fallback = {"id": table_id, "bbox": list(bbox), "rows": 1, "columns": 1,
                            "native_rows": native.row_count, "cells": [{"row": 0, "column": 0,
                            "rowspan": 1, "colspan": 1, "bbox": list(bbox),
                            **cell_layout(fallback_chars, drawings), "character_ids": sorted(ids)}],
                            "inferred_rules": [], "geometry_gaps": 0, "merged_cells": 0,
                            "fallback": True, "fallback_reason": f"{type(exc).__name__}: {exc}"}
                unit = cell_unit(fallback['cells'][0]['text'])
                if unit:
                    fallback['cells'][0]['unit'] = unit
                render_table(page, fallback, assets / "tables", asset_name)
                claimed.update(ids)
                result.append(fallback)
            except Exception as render_exc:
                issues.append(f"{table_id}: fallback table image failed ({type(render_exc).__name__})")
    return result, issues


def body_blocks(chars: list[dict], excluded: set[int],
                barriers: list[list[float]] | None = None) -> list[dict]:
    """Rebuild prose from native glyph order, physical lines and paragraph edges.

    Table geometry must not drive prose grouping: CJK and Latin fonts can
    report very different sizes while sharing the same printed baseline.
    """
    def render_source_line(group: list[dict]) -> str:
        ordered = group
        raw = "".join(c["text"] for c in ordered)
        sizes = [c["size"] for c in ordered if c["text"].strip()]
        bands: list[list[float]] = []
        for size in sorted(sizes):
            if not bands or size - bands[-1][0] > max(.2, size * .035):
                bands.append([size])
            else:
                bands[-1].append(size)
        if not bands:
            return raw.strip()
        scripts: dict[int, str] = {}
        protected = [m.span() for pattern in (r"[（(]\s*\d+\s*[）)]",
                                               r"\d+(?:[-－]\d+){2,}",
                                               r"\d+\.\s*\d+")
                     for m in re.finditer(pattern, raw)]
        raw_positions = {}
        offset = 0
        for char in ordered:
            raw_positions[char["id"]] = offset
            offset += len(char["text"])
        for char in ordered:
            if (not char["text"].isascii() and char["text"] != "−") or not char["text"].strip():
                continue
            position = raw_positions[char["id"]]
            if any(start <= position < end for start, end in protected):
                continue
            candidates = []
            # Use a nearby glyph's own size as evidence. A dominant CJK font
            # can be twice the Latin size, even on the same baseline.
            for anchor in ordered:
                if anchor["id"] == char["id"] or not anchor["text"].strip():
                    continue
                if char["size"] >= anchor["size"] * .84:
                    continue
                if anchor["bbox"][0] > char["bbox"][0] + .5:
                    continue
                anchor_size = anchor["size"]
                delta = char["origin"][1] - anchor["origin"][1]
                if not max(.65, anchor_size * .10) <= abs(delta) <= anchor_size * .62:
                    continue
                gap = max(0.0, char["bbox"][0] - anchor["bbox"][2])
                if gap <= max(3, anchor_size * .55):
                    candidates.append((gap, abs(delta), delta))
            if candidates:
                delta = min(candidates)[2]
                scripts[char["id"]] = "sup" if delta < 0 else "sub"
        value = "".join(char["text"].translate(
            _SUPERSCRIPT if scripts.get(char["id"]) == "sup" else _SUBSCRIPT)
                       if char["id"] in scripts else char["text"] for char in ordered)
        return re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", value).strip()

    source_lines: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for char in chars:
        if char["id"] not in excluded:
            source_lines[(char["block"], char["line"])].append(char)

    physical_lines = []
    for (block, source_line), group in source_lines.items():
        ordered = sorted(group, key=lambda c: c["id"])
        runs: list[list[dict]] = []
        for char in ordered:
            if runs:
                previous = runs[-1][-1]
                baseline = median(c["origin"][1] for c in runs[-1][-12:])
                shift = abs(char["origin"][1] - baseline)
                size = min(previous["size"], char["size"])
                # A baseline change plus a return to the left establishes a
                # physical wrap; a small raised glyph moving right does not.
                wraps = (char["bbox"][0] < previous["bbox"][0] - max(3, size * .4)
                         and shift > max(3, size * .35))
                if wraps or shift > max(previous["size"], char["size"]) * .85:
                    runs.append([])
            if not runs:
                runs.append([])
            runs[-1].append(char)
        physical_lines.extend((block, source_line, run) for run in runs)

    lines = []
    for sequence, (block, source_line, ordered) in enumerate(physical_lines):
        value = render_source_line(ordered)
        if not value:
            continue
        lines.append({"block": block, "line": sequence, "source_line": source_line,
                      "chars": ordered, "text": value,
                      "raw": "".join(c["text"] for c in ordered),
                      "ids": [c["id"] for c in ordered],
                      "x": min(c["bbox"][0] for c in ordered if c["text"].strip()),
                      "right": max(c["bbox"][2] for c in ordered),
                      "y": median(c["origin"][1] for c in ordered),
                      "size": median(c["size"] for c in ordered)})

    # Adjacent runs of one printed heading can be separate PDF lines. Reunite
    # only same-block runs on a common baseline and with a positive small gap.
    reunited = []
    for line in lines:
        previous = reunited[-1] if reunited else None
        if (previous and previous["block"] == line["block"]
                and abs(previous["y"] - line["y"]) <= max(1.2, min(previous["size"], line["size"]) * .12)
                and -.5 <= line["x"] - previous["right"] <= max(8, line["size"])):
            previous["chars"].extend(line["chars"])
            previous["ids"].extend(line["ids"])
            previous["right"] = line["right"]
            previous["text"] = render_source_line(previous["chars"])
            previous["raw"] += line["raw"]
        else:
            reunited.append(line)
    lines = reunited

    # Reunite isolated script runs using the same glyph evidence as tables.
    # Ordinary mixed-font source lines retain their native order and baseline.
    consumed: set[int] = set()
    from .glyph_layout import effective_em
    for index, candidate in enumerate(lines):
        if index in consumed or len(candidate["chars"]) > 32:
            continue
        token = candidate["raw"].strip()
        if not token:
            continue
        best = None
        for other_index, base in enumerate(lines):
            if other_index == index or other_index in consumed:
                continue
            candidate_em = median(effective_em(c) for c in candidate['chars'])
            base_sizes = [effective_em(c) for c in base["chars"] if effective_em(c) >= candidate_em]
            if not base_sizes:
                continue
            base_size = median(base_sizes)
            if candidate_em >= base_size * .84:
                continue
            delta = candidate["y"] - base["y"]
            if not max(.65, base_size * .10) <= abs(delta) <= base_size * .62:
                continue
            horizontal_gap = min(max(0.0, c["bbox"][0] - d["bbox"][2],
                                     d["bbox"][0] - c["bbox"][2])
                                 for c in candidate["chars"] for d in base["chars"])
            if horizontal_gap > max(4.5, base_size * .85):
                continue
            from .glyph_layout import table_layout
            proposed = table_layout(base['chars']+candidate['chars'])
            attached = {v for s in proposed.get('scripts', []) for v in s['character_ids']}
            if not {c['id'] for c in candidate['chars'] if c['text'].strip()} <= attached:
                continue
            choice = (abs(delta), horizontal_gap, other_index)
            if best is None or choice < best:
                best = choice
        if best is not None:
            base_index = best[2]
            base = lines[base_index]
            base["chars"].extend(candidate["chars"])
            base["chars"].sort(key=lambda c: (c["bbox"][0], c["id"]))
            base["text"] = render_source_line(base["chars"])
            base["raw"] = "".join(c["text"] for c in base["chars"])
            base["ids"].extend(candidate["ids"])
            consumed.add(index)

    lines = [line for index, line in enumerate(lines) if index not in consumed]
    from .printed_toc import reconstruct
    contents = reconstruct(lines, render_source_line)
    if contents is not None:
        return [{"x": contents["x"], "y": contents["y"], "text": contents["text"],
                 "character_ids": contents["character_ids"],
                 "printed_contents": True, "toc": contents}]
    lines.sort(key=lambda line: (round(line["y"], 2), line["x"]))
    left_edge = min((line["x"] for line in lines), default=0.0)
    common_size = median(line["size"] for line in lines) if lines else 12.0
    caption_re = re.compile(r"^\s*图\s*([\d]+(?:[-－][\d]+)+)\s+\S")
    top_y = min((line["y"] for line in lines), default=0.0)
    top_band = [line for line in lines if abs(line["y"] - top_y) <= max(2.0, common_size * .12)]
    header_titles = [line for line in top_band if len(line["text"].strip()) > 8
                     and line["x"] > left_edge + common_size * 4]
    header_lines = []
    if header_titles:
        title = max(header_titles, key=lambda line: len(line["text"]))
        header_lines = [line for line in top_band
                        if line is title or line["text"].strip().isdigit()]
        lines = [line for line in lines if line not in header_lines]
    caption_starts = [line for line in lines if caption_re.match(line["text"])]
    caption_continuations: set[tuple[int, int]] = set()
    captions = []
    for start in caption_starts:
        match = caption_re.match(start["text"])
        caption = dict(start)
        caption["figure_id"] = match[1].replace("－", "-")
        caption["ids"] = list(start["ids"])
        caption["_end_y"] = start["y"]
        for continuation in lines:
            key = (continuation["block"], continuation["line"])
            if key == (start["block"], start["line"]) or key in caption_continuations:
                continue
            if (continuation["block"] == start["block"]
                    and start["line"] < continuation["line"] <= start["line"] + 2
                    and continuation["y"] - start["y"] <= common_size * 2.2
                    and abs(continuation["x"] - start["x"]) <= common_size):
                if caption_re.match(continuation["text"]):
                    continue
                left, right = caption["text"].rstrip(), continuation["text"].lstrip()
                caption["text"] = left + (" " if left[-1:].isascii() and left[-1:].isalnum()
                                          and right[:1].isascii() and right[:1].isalnum() else "") + right
                caption["ids"].extend(continuation["ids"])
                caption["_end_y"] = continuation["y"]
                caption_continuations.add(key)
        captions.append(caption)
    flow = [line for line in lines
            if (line["block"], line["line"]) not in caption_continuations
            and not caption_re.match(line["text"])]
    left_edge = min((line["x"] for line in flow), default=left_edge)
    common_size = median(line["size"] for line in flow) if flow else common_size
    heading_re = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.\s*[\u4e00-\u9fff]|\d+(?:\.\d+)+\s+\S|第\s*[一二三四五六七八九十百千零〇\d]+\s*[卷篇章节])")
    item_re = re.compile(r"(?:[（(]\s*\d+\s*[）)]|\d+\s*[）)]|[A-Za-z][.．、]\s+|[①-⑳㉑-㉟㊱-㊿]|(?:注[:：]\s*)?\d+[.．、]\s+(?=\D)|方法\s*\d+\s*[:：])")

    def join_text(left: str, right: str) -> str:
        left, right = left.rstrip(), right.lstrip()
        if left and right and left[-1].isascii() and left[-1].isalnum() \
                and right[0].isascii() and right[0].isalnum():
            return left + " " + right
        return left + right

    def split_items(line: dict) -> list[tuple[str, list[int]]]:
        text = line["text"]
        cuts = []
        for match in item_re.finditer(text):
            if match.start() and text[match.start() - 1] in "。；！？":
                cuts.append(match.start())
        boundaries = [0, *cuts, len(text)]
        return [(text[start:end].strip(), line["ids"] if index == 0 else [])
                for index, (start, end) in enumerate(zip(boundaries, boundaries[1:]))
                if text[start:end].strip()]

    paragraphs, current = [], None
    previous_y = None
    previous_line = None

    def flush() -> None:
        nonlocal current
        if current and current["text"]:
            current["text"] = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", current["text"])
            current["text"] = re.sub(r"^([（(]\s*\d+\s*[）)])\s*", r"\1 ", current["text"])
            paragraphs.append(current)
        current = None

    for line in flow:
        segments = split_items(line)
        for segment_index, (value, ids) in enumerate(segments):
            value = re.sub(r"[ \t]{2,}", " ", value)
            is_heading = bool(heading_re.match(value)) and len(value) < 100 and not re.search(
                r"[。；;，,:：]|[.…·]{3}", value)
            is_item = bool(item_re.match(value))
            displayed_formula = ("=" in value and len(value) < 100
                                 and not re.search(r"[。；;，,:：]", value)
                                 and line["x"] > left_edge + 10)
            gap = line["y"] - previous_y if previous_y is not None else 0.0
            local_size = max(line["size"], current.get("_size", line["size"]) if current else line["size"])
            hanging_item = (current is not None and item_re.match(current["text"])
                            and line["block"] == current.get("_block")
                            and 0 <= line["x"] - current["x"] <= local_size * 1.2)
            starts_indented = (not hanging_item
                               and line["x"] > left_edge + max(10.0, common_size * .65)
                               and (previous_line is None
                                    or abs(line["x"] - previous_line["x"]) > local_size * .5))
            continues_split_line = len(segments) > 1 and segment_index == 0 and current is not None
            jumps_left = current is not None and line["x"] < current["x"] - max(35.0, common_size * 2)
            crosses_table = previous_y is not None and any(
                previous_y < box[1] < line["y"] and line["x"] < box[2]
                and line["right"] > box[0] for box in barriers or [])
            if current and (is_heading or is_item or displayed_formula or starts_indented or jumps_left
                            or crosses_table or gap > local_size * 2.2 and not continues_split_line):
                flush()
            if current is None:
                current = {"y": line["y"], "x": line["x"], "text": value,
                           "character_ids": list(ids), "_last_y": line["y"],
                           "_end_y": line["y"], "_block": line["block"], "_size": line["size"]}
            else:
                current["text"] = join_text(current["text"], value)
                current["character_ids"].extend(ids)
                current["_last_y"] = line["y"]
                current["_end_y"] = line["y"]
            if segment_index < len(segments) - 1:
                flush()
            if is_heading or displayed_formula:
                flush()
            previous_y = line["y"]
            previous_line = line
        if not segments:
            previous_y = line["y"]
    flush()

    # Place captions beside the nearest complete paragraph, so a caption in a
    # figure column cannot interrupt a wrapped sentence in the prose column.
    for caption in captions:
        content_positions = [i for i, paragraph in enumerate(paragraphs)
                              if not paragraph.get("_caption")]
        if content_positions:
            nearest = min(content_positions, key=lambda i: max(
                paragraphs[i]["y"] - caption["y"],
                caption["y"] - paragraphs[i]["_end_y"], 0.0))
            position = nearest + 1 if caption["y"] >= paragraphs[nearest]["y"] else nearest
        else:
            position = 0
        if position:
            caption_y = paragraphs[position - 1]["y"]
            if position < len(paragraphs):
                caption_y = (caption_y + paragraphs[position]["y"]) / 2
        else:
            caption_y = caption["y"]
        paragraphs.insert(position, {"y": caption_y, "x": caption["x"],
                                     "text": caption["text"],
                                     "character_ids": caption["ids"],
                                     "_end_y": caption["_end_y"], "_caption": True})

    if header_lines:
        header_lines.sort(key=lambda line: line["x"])
        paragraphs.insert(0, {"y": top_y, "x": min(line["x"] for line in header_lines),
                              "text": " ".join(line["text"].strip() for line in header_lines),
                              "character_ids": [ident for line in header_lines for ident in line["ids"]],
                              "_end_y": top_y})

    from .body_script_layout import annotate_body
    return annotate_body([{key: value for key, value in paragraph.items() if not key.startswith("_")}
                          for paragraph in paragraphs], chars)


def content_rotation(chars: list[dict]) -> int:
    counts = Counter(tuple(round(v) for v in c["direction"]) for c in chars if c["text"].strip())
    if not counts:
        return 0
    direction, count = counts.most_common(1)[0]
    return {(0, -1): 270, (0, 1): 90, (-1, 0): 180}.get(direction, 0) if count > sum(counts.values()) * .65 else 0
