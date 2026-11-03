from __future__ import annotations

import hashlib
import html
import json
import csv
import io
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import pymupdf

from .formats import convert_docx, convert_html, convert_pptx, convert_xlsx
from .volumes import VolumeError, write_volumes


class ConversionError(RuntimeError):
    """The requested conversion could not be completed safely."""


SUPPORTED = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".txt", ".md", ".markdown", ".csv"}
_OCR_ENGINE = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(value: str) -> str:
    return (re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._") or "document")[:100]


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return html.escape(text).replace("|", "&#124;").replace("\r\n", "\n").replace("\n", "<br>")


def markdown_table(rows: list[list[Any]]) -> str:
    if not rows:
        return ""
    width = max((len(row) for row in rows), default=0)
    normalized = [list(row) + [""] * (width - len(row)) for row in rows]
    head = "| " + " | ".join(_cell(value) for value in normalized[0]) + " |"
    sep = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = ["| " + " | ".join(_cell(value) for value in row) + " |" for row in normalized[1:]]
    return "\n".join([head, sep, *body])


def _ocr(doc: pymupdf.Document, page_number: int, dpi: int) -> tuple[list[dict[str, Any]], str | None]:
    global _OCR_ENGINE
    try:
        import numpy as np
        from rapidocr import RapidOCR
        if _OCR_ENGINE is None:
            _OCR_ENGINE = RapidOCR(params={"Global.log_level": "warning",
                "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1})
        pix = doc[page_number - 1].get_pixmap(dpi=dpi, alpha=False)
        image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        output = _OCR_ENGINE(image)
        tokens = []
        if output.boxes is not None and output.txts is not None:
            for box, text, score in zip(output.boxes, output.txts, output.scores):
                coords = np.asarray(box) * (72 / dpi)
                tokens.append({"text": str(text), "score": float(score),
                    "bbox": [float(coords[:, 0].min()), float(coords[:, 1].min()),
                             float(coords[:, 0].max()), float(coords[:, 1].max())]})
        tokens.sort(key=lambda item: (round((item["bbox"][1] + item["bbox"][3]) / 8), item["bbox"][0]))
        return tokens, None
    except Exception as exc:
        return [], f"page {page_number}: optional OCR failed ({type(exc).__name__}: {exc})"


def _convert_pdf(source: Path, stage: Path, asset_name: str, *, copy_source: bool,
                 render_pages: bool, render_dpi: int, ocr_mode: str, ocr_dpi: int,
                 include_toc: bool) -> tuple[str, dict[str, Any]]:
    from .pdf_document import convert_pdf
    # Retain the old keyword for callers; bookmarks are intentionally ignored.
    return convert_pdf(source, stage, asset_name, copy_source=copy_source,
                       render_pages=render_pages, render_dpi=render_dpi,
                       ocr_mode=ocr_mode, ocr_dpi=ocr_dpi,
                       slug=_slug, sha256=_sha256, ocr=_ocr)


def _convert_text(source: Path) -> tuple[str, dict[str, Any]]:
    raw = source.read_text(encoding="utf-8-sig", errors="replace")
    if source.suffix.lower() in {".md", ".markdown"}:
        text = raw if raw.endswith("\n") else raw + "\n"
    else:
        text = "# " + source.stem + "\n\n" + raw.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"
    return text, {"format": source.suffix.lower().lstrip("."), "characters": len(raw),
                  "issues": [], "extraction_model": "UTF-8 decoding with replacement for invalid byte sequences"}


def _convert(source: Path, stage: Path, asset_name: str, *, render_pages: bool, render_dpi: int,
             ocr_mode: str, ocr_dpi: int, include_toc: bool) -> tuple[str, dict[str, Any]]:
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        return _convert_pdf(source, stage, asset_name, copy_source=True, render_pages=render_pages,
                            render_dpi=render_dpi, ocr_mode=ocr_mode, ocr_dpi=ocr_dpi,
                            include_toc=include_toc)
    if suffix in {".html", ".htm"}:
        text, audit = convert_html(source)
    elif suffix == ".docx":
        text, audit = convert_docx(source)
    elif suffix == ".pptx":
        text, audit = convert_pptx(source)
    elif suffix == ".xlsx":
        text, audit = convert_xlsx(source)
    elif suffix == ".csv":
        raw = source.read_text(encoding="utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(raw)))
        text, audit = markdown_table(rows), {"format": "csv", "rows": len(rows),
            "columns": max((len(row) for row in rows), default=0), "issues": [],
            "extraction_model": "CSV parser with UTF-8 replacement decoding"}
    elif suffix in {".txt", ".md", ".markdown"}:
        text, audit = _convert_text(source)
    else:
        raise ConversionError("不支持的文件类型：" + (suffix or "无扩展名") + "。支持：" + ", ".join(sorted(SUPPORTED)))
    asset_dir = stage / asset_name
    asset_dir.mkdir(parents=True, exist_ok=True)
    copied = asset_dir / ("source_" + _slug(source.name))
    shutil.copy2(source, copied)
    audit["source_asset"] = f"{asset_name}/{copied.name}"
    audit["sha256"] = _sha256(source)
    text = f"> 原始文档：[打开源文件]({audit['source_asset']})\n\n" + text
    return text, audit


def convert_document(source: str | Path, output: str | Path | None = None, *, overwrite: bool = False,
                     render_pages: bool = False, render_dpi: int = 120, ocr: str = "none",
                     ocr_dpi: int = 200, include_toc: bool = False) -> dict[str, Any]:
    """Create an auditable Markdown bundle from a supported document."""
    if ocr not in {"none", "auto", "all"}:
        raise ConversionError("OCR 模式必须是 none、auto 或 all。")
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise ConversionError("输入文件不存在：" + str(source_path))
    if source_path.suffix.lower() not in SUPPORTED:
        raise ConversionError("不支持的文件类型：" + (source_path.suffix or "无扩展名") + "。支持：" + ", ".join(sorted(SUPPORTED)))
    if source_path.stat().st_size == 0:
        raise ConversionError("输入文件为空。")
    if not 36 <= render_dpi <= 600 or not 36 <= ocr_dpi <= 600:
        raise ConversionError("渲染 DPI 必须在 36 到 600 之间。")
    if ocr != "none" and source_path.suffix.lower() == ".pdf":
        try:
            import numpy  # noqa: F401
            from rapidocr import RapidOCR  # noqa: F401
        except ImportError as exc:
            raise ConversionError("启用 OCR 需要安装可选依赖：pip install '.[ocr]'。") from exc
    output_path = Path(output).expanduser().resolve() if output else source_path.with_suffix(".md")
    if output_path.suffix.lower() != ".md":
        output_path = output_path / (_slug(source_path.stem) + ".md")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path == source_path:
        raise ConversionError("输出 Markdown 不能覆盖输入文件。")
    asset_name = _slug(output_path.stem) + "_assets"
    asset_path = output_path.parent / asset_name
    audit_path = output_path.with_name(output_path.stem + ".audit.json")
    volume_dir = output_path.parent / (output_path.stem + "_volumes")
    targets = [output_path, asset_path, audit_path, volume_dir]
    existing = [path for path in targets if path.exists()]
    if output_path.is_dir() or audit_path.is_dir():
        raise ConversionError("Markdown 或审计 JSON 的目标路径当前是目录，不能替换。")
    if volume_dir in existing and not volume_dir.is_dir():
        raise ConversionError("分卷输出目标已存在且不是文件夹，不能安全替换：" + str(volume_dir))
    if existing and not overwrite:
        raise ConversionError("输出文件已存在；请明确设置 --overwrite：" + ", ".join(str(path) for path in existing))

    stage = Path(tempfile.mkdtemp(prefix="." + output_path.stem + ".stage-", dir=output_path.parent))
    backup = stage / "backup"
    backup.mkdir()
    started = time.time()
    try:
        markdown, audit = _convert(source_path, stage, asset_name, render_pages=render_pages,
                                   render_dpi=render_dpi, ocr_mode=ocr, ocr_dpi=ocr_dpi,
                                   include_toc=include_toc)
        volume_targets: list[tuple[Path, Path]] = []
        if audit.get("format") == "pdf":
            staged_volume_dir = stage / volume_dir.name
            try:
                audit["volumes"] = write_volumes(markdown, audit["pdf_pages"], staged_volume_dir,
                                                  output_path.name, asset_name)
            except VolumeError as exc:
                raise ConversionError(str(exc)) from exc
            if audit["volumes"]:
                volume_targets.append((staged_volume_dir, volume_dir))
        audit.update({"schema_version": 2, "source_path": str(source_path), "output": output_path.name,
                      "asset_directory": asset_name, "conversion_seconds": round(time.time() - started, 3),
                      "options": {"render_pages": render_pages, "render_dpi": render_dpi, "ocr": ocr,
                                  "ocr_dpi": ocr_dpi, "include_toc": False}})
        staged_md = stage / output_path.name
        staged_audit = stage / audit_path.name
        staged_md.write_text(markdown, encoding="utf-8", newline="\n")
        staged_audit.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        backed_up: list[tuple[Path, Path]] = []
        committed: list[Path] = []
        locked_volume = False
        try:
            for target in existing:
                saved = backup / (str(len(backed_up)) + "-" + target.name)
                try:
                    target.rename(saved)
                    backed_up.append((target, saved))
                except OSError as exc:
                    if target != volume_dir or getattr(exc, "winerror", None) not in {5, 32}:
                        raise
                    # Windows can hold an open directory handle even when its
                    # individual Markdown files are replaceable. Back up the
                    # flat generated volume files in the same transaction.
                    children = list(target.iterdir())
                    if any(not child.is_file() or child.is_symlink() for child in children):
                        raise
                    saved.mkdir()
                    for child in children:
                        child_saved = saved / child.name
                        child.rename(child_saved)
                        backed_up.append((child, child_saved))
                    locked_volume = True
            for staged, target in ((staged_md, output_path), (stage / asset_name, asset_path), (staged_audit, audit_path), *volume_targets):
                if target == volume_dir and locked_volume:
                    for child in staged.iterdir():
                        final = target / child.name
                        child.rename(final)
                        committed.append(final)
                else:
                    staged.rename(target)
                    committed.append(target)
        except Exception:
            for target in reversed(committed):
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            for target, saved in reversed(backed_up):
                if saved.exists():
                    saved.rename(target)
            raise
        result = {"markdown": str(output_path), "audit": str(audit_path), "assets": str(asset_path),
                  "source_sha256": audit["sha256"], "format": audit["format"]}
        if volume_targets:
            result["volumes"] = str(volume_dir)
        return result
    finally:
        shutil.rmtree(stage, ignore_errors=True)
