"""Split a completed PDF Markdown without re-extracting or reflowing its content."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from urllib.parse import quote


class VolumeError(ValueError):
    """The available printed headings do not establish safe volume boundaries."""


_PAGE = re.compile(r'(?m)^<a id="pdf-page-(\d+)"></a>\r?\n<!-- PDF_PAGE:\1 -->')
_VOLUME = re.compile(r"^第\s*([0-9零〇一二两三四五六七八九十百千]+)\s*卷\s*(.*?)$")


def _number(value: str) -> int:
    if value.isascii() and value.isdigit():
        return int(value)
    digits = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    total = current = 0
    for char in value:
        if char in digits:
            current = current * 10 + digits[char]
        else:
            total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            current = 0
    return total + current


def _pages(markdown: str, expected_pages: int) -> list[re.Match]:
    pages = list(_PAGE.finditer(markdown))
    if [int(page[1]) for page in pages] != list(range(1, expected_pages + 1)) or not pages:
        raise VolumeError("PDF 页锚必须从 1 开始完整连续，拒绝分卷以避免漏页或重复。")
    return pages


def _opening_lines(section: str) -> list[str]:
    lines = []
    for raw in section.splitlines():
        line = unicodedata.normalize("NFKC", raw).strip()
        if not line or line.startswith(("<!--", "<a ", "![", ">")):
            continue
        if line.startswith(("[原始 PDF 第 ", "来源：", "来源:")):
            continue
        # Never interpret table cells, embedded HTML or code as volume titles.
        if line.startswith(("|", "<", chr(96) * 3, "~~~", "---")):
            break
        lines.append(line)
        if len(lines) == 8:
            break
    return lines


def _title(text: str) -> str:
    text = re.sub(r"^#{1,6}\s+", "", text).strip()
    if (not text or len(text) > 100 or re.search(r"[。；;]|[.…·]{3}", text)
            or re.match(r"^第\s*[\d一二三四五六七八九十百千零〇]+\s*[卷篇章节]", text)
            or re.match(r"^\d+[.、]", text)):
        return ""
    return text


def printed_contents(lines: list[str]) -> bool:
    """Recognize printed TOC continuations even when leaders wrap separately.

    Ignore HTML/table material: numeric table columns alone are not a TOC.
    Require repeated leader/page-number evidence, never the volume sequence.
    """
    if any(line.strip() == '<!-- PRINTED_TOC -->' for line in lines):
        return True
    visible = []
    in_details = False
    for raw in lines:
        line = unicodedata.normalize("NFKC", raw).strip()
        if line.startswith("<details"):
            in_details = True
        if in_details:
            if line.startswith("</details"):
                in_details = False
            continue
        if not line or line.startswith(("<", "|", "![", ">", "[原始 PDF", "来源：", "来源:")):
            continue
        if line == "---":
            break
        visible.append(re.sub(r"^#{1,6}\s+", "", line))
    opening = re.sub(r"\s", "", "".join(visible[:4]))
    if opening.startswith("目录"):
        return True
    leaders = [line for line in visible if re.search(r"[.…·．]{3,}", line)]
    page_numbers = sum(bool(re.fullmatch(r"[0-9]{1,5}", line)) for line in visible)
    inline_entries = sum(bool(re.search(r"[.…·．]{3,}\s*[0-9]{1,5}\s*$", line))
                         for line in leaders)
    return inline_entries >= 4 or len(leaders) >= 4 and page_numbers >= 4


def volume_pages(markdown: str, expected_pages: int) -> list[dict]:
    """Use printed, sequential volume headings; PDF bookmarks are never read.

    Front matter is assigned to the first volume. Ambiguous or incomplete
    sequences are rejected rather than guessed. Documents without volume
    headings produce no additional files.
    """
    pages = _pages(markdown, expected_pages)
    found = []
    for index, page in enumerate(pages):
        end = pages[index + 1].start() if index + 1 < len(pages) else len(markdown)
        section = markdown[page.end():end]
        if printed_contents(section.splitlines()):
            continue
        lines = _opening_lines(section)
        if re.sub(r"[\s#]", "", "".join(lines[:2])).startswith("目录"):
            continue
        for position, line in enumerate(lines[:3]):
            heading = re.match(r"^#{1,6}\s+", line)
            text = line[heading.end():] if heading else line
            match = _VOLUME.fullmatch(text)
            if not match:
                continue
            # Plain inline titles are frequently entries in the printed TOC.
            # Explicit Markdown headings or standalone printed labels are safe.
            if match[2] and not heading:
                continue
            title = _title(match[2]) if match[2] else (
                _title(lines[position + 1]) if position + 1 < len(lines) else "")
            if match[2] and not title:
                continue
            number = _number(match[1])
            if number < 1:
                raise VolumeError("印刷卷号必须为正整数。")
            found.append({"volume": number, "title_page": int(page[1]), "title": title})
            break
    if not found:
        return []
    numbers = [item["volume"] for item in found]
    if numbers != list(range(1, len(found) + 1)):
        raise VolumeError(f"印刷卷号不连续或重复：{numbers}；拒绝猜测分卷位置。")
    for index, item in enumerate(found):
        item["start_page"] = item["title_page"] if index else 1
        item["end_page"] = found[index + 1]["title_page"] - 1 if index + 1 < len(found) else expected_pages
    return found


def write_volumes(markdown: str, expected_pages: int, destination: Path,
                  full_name: str, asset_name: str) -> list[dict]:
    """Write into a fresh staging directory; caller commits the complete bundle.

    Slices cover the exact original text, including each complete page anchor.
    Only a navigation preamble and relative link adjustments are added.
    Shared assets, tables, HTML spans and page content remain unchanged.
    """
    volumes = volume_pages(markdown, expected_pages)
    if not volumes:
        return []
    offsets = {int(page[1]): page.start() for page in _pages(markdown, expected_pages)}
    pieces = []
    for index, volume in enumerate(volumes):
        start = offsets[volume["start_page"]] if index else 0
        end = offsets[volumes[index + 1]["start_page"]] if index + 1 < len(volumes) else len(markdown)
        pieces.append(markdown[start:end])
        volume["file"] = f"{destination.name}/{Path(full_name).stem}_第{volume['volume']}卷.md"
    if "".join(pieces) != markdown:
        raise VolumeError("分卷内容校验失败：原文存在遗漏或重复。")
    destination.mkdir(parents=True, exist_ok=False)
    # All generated page links currently point at the source PDF. Preserve any
    # additional Markdown page links, including cross-volume destinations.
    owners = {page: index for index, volume in enumerate(volumes)
              for page in range(volume["start_page"], volume["end_page"] + 1)}
    for index, (volume, part) in enumerate(zip(volumes, pieces)):
        for prefix in {asset_name, quote(asset_name)}:
            part = part.replace(f"]({prefix}/", f"](../{prefix}/")

        def page_link(match: re.Match) -> str:
            target = owners.get(int(match[1]))
            if target is None or target == index:
                return match[0]
            return f"]({quote(Path(volumes[target]['file']).name)}#pdf-page-{match[1]})"

        part = re.sub(r"\]\(#pdf-page-(\d+)\)", page_link, part)
        title = f"第{volume['volume']}卷" + (f" {volume['title']}" if volume["title"] else "")
        links = f"[完整全文]({quote('../' + full_name)}) · [分卷索引](README.md)"
        if index == 0:
            # Keep the complete source header as the first volume's only H1.
            preamble = (f"{links} · {title}\n\n"
                        f"> PDF 第 {volume['start_page']}–{volume['end_page']} 页。\n\n")
        else:
            preamble = (f"# {title}\n\n{links}\n\n"
                        f"> PDF 第 {volume['start_page']}–{volume['end_page']} 页。\n\n")
        (destination / Path(volume["file"]).name).write_text(preamble + part, encoding="utf-8", newline="\n")
    lines = [f"# {Path(full_name).stem} 分卷索引", "",
             f"[打开完整全文 Markdown]({quote('../' + full_name)})", "",
             "封面、前言及印刷目录等前置页并入第1卷；附件共用上级目录中的原附件文件夹。", ""]
    for volume in volumes:
        title = f"第{volume['volume']}卷" + (f" {volume['title']}" if volume["title"] else "")
        lines.append(f"- [{title}]({quote(Path(volume['file']).name)})：PDF 第 {volume['start_page']}–{volume['end_page']} 页")
    (destination / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return volumes
