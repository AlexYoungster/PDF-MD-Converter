# 输出数据与下游读取

完整结构包须一起保存。`*.audit.json` 记录来源 SHA-256、选项、物理页数、表格摘要、字符分配和异常；`*_assets/structure.jsonl` 每物理页一条 JSON，包含坐标系、表格、单元格、脚标及分数证据。

## 页、表和单元格

Markdown 使用 `<a id="pdf-page-N"></a>` 和 `<!-- PDF_PAGE:N -->` 标识物理页。表格由 `<!-- TABLE:p00001-t01 -->` 标识。`row`、`column` 为从 0 开始的索引，`rowspan`、`colspan` 为真实跨度。坐标使用统一方向后的 PDF 点坐标，页面坐标系尺寸和旋转记录在 `coordinate_space`。

单元格 `text` 是可读表达。存在分数时，`source_text` 去除另行记账的派生括号、斜杠和加号，仅用于来源字符比对。`linear_text` 是带显式脚标关系的线性表示。各字段不能相互当成无条件等价的纯文本。

结构记录不是独立来源。最终验收应重新读取相同哈希的原 PDF，并使用独立检查器验证坐标、字形和图像。

## 印刷目录

`<!-- PRINTED_TOC -->` 到 `<!-- /PRINTED_TOC -->` 包围一次目录输出。两列 HTML 中每个 `tr` 有 `data-toc-id`、`data-toc-level`、`data-toc-column`；三者分别是页内条目标识、从 0 开始的层级和栏号。`data-toc-role="title"`、`"leader"`、`"page"` 分别给出标题、装饰引导线和原书印刷页码。标题内的全角空白只用于缩进；脚标仍由 `script-run` 保留。

长引导线在可读层显示 `…`，完整原字符保存在 leader 的 `data-source-text`。这项装饰缩排有明确来源，不应把显示省略号当作原标题内容。独立检查器将装饰来源字符纳入逻辑字符比对，并单独验证属性只能包含引导点/空白。报告中的转换字符数因此包括这些已归档的原始装饰字符，不等于屏幕上的可见字符数。

每页 `printed_contents` 数组记录 `entries`、`extras`、`columns` 和 `reviews`。条目保存 `title`、`leader`、`page`、`level`、`level_evidence`、`column`、坐标、脚标，以及 `fields` 内各字段的字形 ID / 区域框。`extras` 保留页眉、页脚和未配对片段。`audit.json` 中的 `printed_contents_pages`、`printed_contents_entries` 记录印刷目录；原有 `toc_entries=0` 表示未提取 PDF 书签，两者不可混淆。

下游读取目录应按字段提取标题和印刷页码，不必解析长引导点。印刷页码可为罗马数字、带前缀的页码或页码区间，不强制转换为整数，也不直接当作 PDF 物理页索引。

## 给大模型或检索系统的脚标表示

需要先解析语义再去 HTML 标签：

```python
from bs4 import BeautifulSoup
from document_converter.script_witness import linear_cell

# cell_html 是实际输出中的一个单元格 HTML，包含 script-run 来源声明。
node = BeautifulSoup(cell_html, "html.parser")
text, errors = linear_cell(node)
if errors:
    raise ValueError(errors)
print(text)  # 例如 d_{k}、F_{i_{j}}、6.102×10^{4}
```

`linear_cell()` 检查 HTML 声明与结构并输出线性写法，不独自证明原 PDF 上的绑定正确；应先完成独立来源检查。直接用 `get_text()` 会丢失字母脚标角色；不得先去标签再尝试从 `dk` 猜回 `d_{k}`。

科学计数法在可读层保留尾数、底数和指数关系。部分路径会生成 `<data value="6.102e4">`，脚标语义路径则以 `script-run` 保存指数绑定；不能假设每个数都有 `<data>` 属性。需要统一标准数值的消费者必须解析完整数学关系，再执行受控的数值求值，不能把普通相邻数字当指数。

## 分数与单位

`numeric-fraction` 保存分子、分母、分数线及数字框，`math-generated` 标记新增的表达符号。例 `(5/9)`、`(1+1/2)` 保留精确比例，不提前做浮点近似。当前仅有来源证据的简单数字分数自动线性化。

识别成功的单位使用 `unit-expression`，`data-unit` 保存例如 `W/(m*K)` 的 ASCII 写法，`data-unit-ast` 保存乘除及幂次树。结构记录的 `unit` 字段保存同类语法。未知符号或不平衡括号保留原文，不补猜。

## 检查结果

`summary.json` 给出覆盖页数、输入哈希、检查次数、严重级别计数及状态；`findings.jsonl` 是逐条异常。典型状态为 `passed_text_layer`、`review_required`、`differences_found`，不表示任意视觉/语义均已验证。

记录通常含物理页码、表格/单元格位置、区域框、Markdown 行号和来源差异。同一问题可能产生多个记录，不把 finding 数量相加当成错字数量。`--page-limit` 只适合开发抽查，报告会标记覆盖不完整。
