"""Create an original, dependency-light PDF fixture; no third-party text."""
from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf


def label(page, point, text, size=11, color=(0.14, 0.2, 0.28)):
    chinese = any(ord(c) > 127 for c in text)
    page.insert_text(point, text, fontsize=size,
                     fontname="china-s" if chinese else "helv", color=color)


def create_demo(destination: Path, *, overwrite: bool = False) -> Path:
    destination = destination.resolve()
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite {destination}; use --overwrite")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as doc:
        p = doc.new_page(width=595, height=700)
        label(p, (42, 52), "第1卷 基础示例", 20)
        label(p, (42, 90), "1. 段落与表格", 15)
        label(p, (60, 125), "这是一份原创格式示例，用于检查正文段落、合并单元格与数值关系。")
        label(p, (42, 145), "同一段落的物理换行应恢复为连续文字，表格则保留明确的结构边界。")
        label(p, (42, 185), "表1-1 示例参数（仅用于格式验证）", 12)
        # A merged first-column header and a merged second/third-column banner.
        for x in (42, 210, 552):
            p.draw_line((x, 210), (x, 430), color=(0.3, 0.4, 0.5))
        p.draw_line((370, 245), (370, 430), color=(0.3, 0.4, 0.5))
        for y in (210, 280, 330, 380, 430):
            p.draw_line((42, y), (552, y), color=(0.3, 0.4, 0.5))
        p.draw_line((210, 245), (552, 245), color=(0.3, 0.4, 0.5))
        label(p, (60, 251), "项目")
        label(p, (315, 232), "表达示例")
        label(p, (228, 267), "数值或符号")
        label(p, (390, 267), "说明")
        label(p, (60, 310), "科学计数法")
        label(p, (228, 310), "6.102 x 10", 12)
        x = 228 + pymupdf.get_text_length("6.102 x 10", fontname="helv", fontsize=12)
        label(p, (x + 1, 305), "4", 8)
        label(p, (390, 310), "指数为4")
        label(p, (60, 360), "数字分数")
        label(p, (252, 352), "5", 10)
        label(p, (252, 367), "9", 10)
        p.draw_line((248, 355), (264, 355))
        label(p, (390, 360), "保留精确比例")
        label(p, (60, 410), "字母下标")
        label(p, (245, 410), "d", 12)
        label(p, (253, 414), "k", 8)
        label(p, (390, 410), "主体与脚标绑定")
        label(p, (42, 475), "（1） 表格后的条目应保持独立，不能接入上方单元格文字。")
        label(p, (42, 497), "（2） 这些数值仅用于格式演示，不是生产或工程参数。")
        label(p, (42, 660), "PDF-MD-Converter / original demo / 1", 9)

        p = doc.new_page(width=595, height=700)
        label(p, (42, 52), "2. 单位与来源", 18)
        label(p, (60, 100), "单位中的分母括号、乘号和幂次表达都应保留，不自动进行单位换算。")
        label(p, (42, 125), "每个页面都可以通过生成的物理页锚和原文件链接回查。")
        label(p, (42, 180), "来源说明：本示例由项目脚本生成，没有使用第三方书籍内容。")
        label(p, (42, 660), "PDF-MD-Converter / original demo / 2", 9)

        p = doc.new_page(width=595, height=700)
        label(p, (42, 52), "第2卷 结构检查", 20)
        label(p, (42, 90), "1. 完整全文与分卷", 15)
        label(p, (60, 130), "完整全文始终保留；明确识别到印刷卷标题后，再生成每卷一个文件。")
        label(p, (42, 150), "分卷按原始页面切片，不重新提取文字，也不重新生成表格。")
        label(p, (42, 195), "请同时查看审计报告与独立检查报告中的待复核项目。")
        label(p, (42, 660), "PDF-MD-Converter / original demo / 3", 9)
        doc.set_toc([[1, "BOOKMARK_ONLY_NOT_EXTRACTED", 1]])
        doc.set_metadata({"title": "PDF-MD-Converter original demo",
                          "author": "PDF-MD-Converter example generator"})
        doc.save(destination, garbage=4, deflate=True)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path,
                        default=Path(__file__).resolve().parents[1] / "origin/demo.pdf")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        print(create_demo(args.output, overwrite=args.overwrite))
    except (OSError, ValueError) as exc:
        parser.exit(2, f"demo: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
