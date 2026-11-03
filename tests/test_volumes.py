from pathlib import Path
import json
import re
from urllib.parse import unquote
from unittest.mock import patch

import pytest

from document_converter.converter import ConversionError, convert_document
from document_converter.volumes import VolumeError, volume_pages, write_volumes


def pages(*bodies: str) -> str:
    return "# Complete document\n\n" + "".join(
        f'<a id="pdf-page-{n}"></a>\n<!-- PDF_PAGE:{n} -->\n'
        f'[原始 PDF 第 {n} 页](book_assets/source.pdf#page={n})\n\n{body}\n\n---\n\n'
        for n, body in enumerate(bodies, 1))


@pytest.mark.parametrize("labels", [("第一卷", "第二卷"), ("第１ 卷", "第２ 卷"),
                                   ("## 第1卷 材料", "## 第2卷 工艺")])
def test_generic_titles_and_variable_volume_counts(labels: tuple[str, str]) -> None:
    md = pages("前言\n本书分为第一卷和第二卷。", labels[0] + "\n\n材料",
               "中间页", labels[1] + "\n\n工艺")
    volumes = volume_pages(md, 4)
    assert [(v["start_page"], v["end_page"], v["title_page"]) for v in volumes] == [(1, 3, 2), (4, 4, 4)]
    assert [v["title"] for v in volumes] == ["材料", "工艺"]


def test_toc_references_table_cells_and_code_are_not_boundaries() -> None:
    md = pages("目录\n\n## 第一卷 材料\n\n## 第二卷 工艺",
               "第一卷 材料 ........................ 10\n第二卷 工艺 ........ 50",
               "本文引用第一卷。\n| 标题 |\n| --- |\n第一卷\n材料",
               chr(96) * 3 + "\n第一卷\n材料\n" + chr(96) * 3,
               "正文\n更多正文\n更多正文\n第一卷\n材料")
    assert volume_pages(md, 5) == []


def test_continued_contents_with_split_leaders_is_not_a_volume_start() -> None:
    contents = ("实用机械手册 第5版\nⅧ\n## 第2卷 工艺技术\n"
                + "章节标题\n…………………………\n693\n" * 5)
    md = pages(contents, "第1卷\n材料", "第2卷\n工艺")
    result = volume_pages(md, 3)
    assert [v["title_page"] for v in result] == [2, 3]


@pytest.mark.parametrize("mutate", [
    lambda md: md.replace('<a id="pdf-page-2"></a>', '<a id="pdf-page-1"></a>'),
    lambda md: md.replace('<!-- PDF_PAGE:2 -->', '<!-- PDF_PAGE:3 -->'),
    lambda md: md + '<a id="pdf-page-2"></a>\n<!-- PDF_PAGE:2 -->\n',
])
def test_missing_duplicate_or_mismatched_page_anchors_are_rejected(mutate) -> None:
    with pytest.raises(VolumeError, match="页锚"):
        volume_pages(mutate(pages("第一卷\n材料", "第二卷\n工艺")), 2)


def test_volume_slices_preserve_tables_pages_and_resolve_links(tmp_path: Path) -> None:
    table = '<!-- TABLE:t1 -->\n![表](book_assets/tables/table.png)\n<details>\n<table><tr><td rowspan="2">A</td></tr><tr></tr></table>\n</details>'
    md = pages("封面", "第一卷\n材料\n" + table + "\n[后页](#pdf-page-3)",
               "第二卷\n工艺\n[同页](#pdf-page-3)\n[前页](#pdf-page-2)")
    destination = tmp_path / "book_volumes"
    volumes = write_volumes(md, 3, destination, "book.md", "book_assets")
    texts = [(tmp_path / v["file"]).read_text(encoding="utf-8") for v in volumes]
    assert [re.findall(r'<a id="pdf-page-(\d+)"></a>', part) for part in texts] == [["1", "2"], ["3"]]
    assert [re.findall(r'<!-- PDF_PAGE:(\d+) -->', part) for part in texts] == [["1", "2"], ["3"]]
    assert texts[0].count("# Complete document") == 1
    assert texts[0].count("# 第1卷 材料") == 0
    assert "# 第2卷 工艺" in texts[1]
    assert table.replace("](book_assets/", "](../book_assets/") in texts[0]
    assert table not in texts[1]
    assert "[后页](book_第2卷.md#pdf-page-3)" in unquote(texts[0])
    assert "[同页](#pdf-page-3)" in texts[1]
    assert "[前页](book_第1卷.md#pdf-page-2)" in unquote(texts[1])
    assert "../book_assets/source.pdf#page=3" in texts[1]
    # Beyond the navigation and link relocation, every original byte survives.
    restored = []
    for text in texts:
        body = text[text.index('<a id="pdf-page-'):]
        body = body.replace("](../book_assets/", "](book_assets/")
        body = re.sub(r'\]\(book_%E7%AC%AC[12]%E5%8D%B7.md#pdf-page-(\d+)\)', r'](#pdf-page-\1)', body)
        restored.append(body)
    original_prefix = md[:md.index('<a id="pdf-page-')]
    assert original_prefix + "".join(restored) == md


def test_no_volumes_or_incomplete_sequence_does_not_create_partial_directory(tmp_path: Path) -> None:
    destination = tmp_path / "volumes"
    assert write_volumes(pages("正文"), 1, destination, "book.md", "book_assets") == []
    assert not destination.exists()
    with pytest.raises(VolumeError, match="卷号"):
        write_volumes(pages("第一卷\n材料", "第三卷\n工艺"), 2, destination, "book.md", "book_assets")
    assert not destination.exists()


def test_split_error_or_commit_failure_preserves_previous_bundle(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"mock PDF")
    output = tmp_path / "book.md"

    def convert_stub(source, stage, asset_name, **kwargs):
        (stage / asset_name).mkdir()
        (stage / asset_name / "source.pdf").write_bytes(b"source")
        return pages("第一卷\n材料", "第二卷\n工艺"), {"format": "pdf", "pdf_pages": 2, "sha256": "mock"}

    with patch("document_converter.converter._convert", side_effect=convert_stub):
        result = convert_document(source, output)
        before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        with pytest.raises(ConversionError, match="overwrite"):
            convert_document(source, output)
        with patch("document_converter.converter.write_volumes", side_effect=VolumeError("bad boundary")):
            with pytest.raises(ConversionError, match="bad boundary"):
                convert_document(source, output, overwrite=True)

        original_rename = Path.rename

        def fail_last_commit(path, target):
            if path.name == "book_volumes" and path.parent.name.startswith(".book.stage-"):
                raise OSError("commit interrupted")
            return original_rename(path, target)

        with patch.object(Path, "rename", fail_last_commit):
            with pytest.raises(OSError, match="commit interrupted"):
                convert_document(source, output, overwrite=True)
        assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert Path(result["volumes"]).is_dir()
    assert len(json.loads(Path(result["audit"]).read_text(encoding="utf-8"))["volumes"]) == 2

    # Replacing a multi-volume bundle with an ordinary document removes stale splits.
    plain_source = tmp_path / "plain.txt"
    plain_source.write_text("ordinary document", encoding="utf-8")
    convert_document(plain_source, output, overwrite=True)
    assert not Path(result["volumes"]).exists()


def test_windows_open_volume_directory_can_commit_and_rollback(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"mock PDF")
    output = tmp_path / "book.md"

    def convert_stub(source, stage, asset_name, **kwargs):
        (stage / asset_name).mkdir()
        (stage / asset_name / "source.pdf").write_bytes(b"source")
        return pages("第一卷\n材料", "第二卷\n工艺"), {"format": "pdf", "pdf_pages": 2, "sha256": "mock"}

    original_rename = Path.rename

    def locked_folder(path, target):
        if path == tmp_path / "book_volumes":
            exc = PermissionError("Directory held open by Windows")
            exc.winerror = 32
            raise exc
        return original_rename(path, target)

    with patch("document_converter.converter._convert", side_effect=convert_stub):
        convert_document(source, output)
        before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        with patch.object(Path, "rename", locked_folder):
            convert_document(source, output, overwrite=True)
        after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        assert before.keys() == after.keys()
        for path in before:
            if path.suffix != ".json":
                assert before[path] == after[path]

        def fail_second_volume(path, target):
            if path.parent.name == "book_volumes" and path.name == "book_第2卷.md" and path.parent.parent.name.startswith(".book.stage-"):
                raise OSError("Commit interrupted")
            return locked_folder(path, target)

        with patch.object(Path, "rename", fail_second_volume):
            with pytest.raises(OSError, match="interrupted"):
                convert_document(source, output, overwrite=True)
        restored = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        assert restored == after
