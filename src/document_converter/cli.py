from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .converter import ConversionError, convert_document


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pdf-md-converter",
        description="PDF-MD-Converter：将 PDF 等文档转换为保留结构、来源文件和审计报告的 Markdown 包。",
        epilog="支持 PDF、DOCX、PPTX、XLSX、CSV、HTML、TXT、MD；Office 格式需安装 office 可选依赖。",
    )
    parser.add_argument("input", type=Path, help="输入文档")
    parser.add_argument("-o", "--output", type=Path, help="输出 .md 文件，或输出文件夹")
    parser.add_argument("--overwrite", action="store_true", help="原子替换同名 Markdown、分卷、附件包及审计文件")
    parser.add_argument("--render-pages", action="store_true", help="PDF 每页额外保存一张原页图（大型文件会明显增加输出体积）")
    parser.add_argument("--render-dpi", type=int, default=120, help="PDF 原页图分辨率，默认 120 DPI")
    parser.add_argument("--ocr", choices=["none", "auto", "all"], default="none",
                        help="OCR 模式；auto 处理文本少于 80 字的 PDF 页，OCR 输出明确标记为候选文本")
    parser.add_argument("--ocr-dpi", type=int, default=200, help="OCR 渲染分辨率，默认 200 DPI")
    parser.add_argument("--no-toc", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.render_dpi < 36 or args.render_dpi > 600 or args.ocr_dpi < 36 or args.ocr_dpi > 600:
        parser.error("DPI 必须在 36 到 600 之间。")
    try:
        result = convert_document(args.input, args.output, overwrite=args.overwrite,
                                  render_pages=args.render_pages, render_dpi=args.render_dpi,
                                  ocr=args.ocr, ocr_dpi=args.ocr_dpi, include_toc=False)
    except (ConversionError, RuntimeError, OSError, ValueError) as exc:
        print(f"doc2md: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
