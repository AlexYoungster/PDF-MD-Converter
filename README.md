# PDF-MD-Converter

将 PDF 转换为保留段落、章节和表格组织关系的 Markdown，并同时保存来源文件、结构记录和审计报告。

项目以 PDF 为主要输入：根据原生字形、基线、单元格和矢量线条恢复结构，保守处理上下标、科学计数法、复合单位和数字分数。复杂表格在 Markdown 中同时提供原版式图与可展开的 HTML 结构。可选 OCR 用于补充候选文字，不覆盖原生文字，也不承诺任意文档都能完全自动还原。

这是可独立安装、运行和测试的项目，不依赖早期的机械加工规则提取系统。发行包名称为 `pdf-md-converter`，Python 导入名仍为 `document_converter`。

项目仓库：[AlexYoungster/PDF-MD-Converter](https://github.com/AlexYoungster/PDF-MD-Converter)。问题反馈请使用 [Issues](https://github.com/AlexYoungster/PDF-MD-Converter/issues)。

## 主要功能

- 按实际印刷行恢复连续正文段落，保留明确标题、编号条目、图注和表格边界。
- 忽略 PDF 书签树；按坐标配对印刷目录的标题、引导线和页码，按栏排序并结合缩进、章节编号恢复层级；支持换行条目和有明确目录标题的无引导线条目。
- 恢复表格行列和 `rowspan` / `colspan`；仅在几何证据相互支持时补绘缺失横线，并保存补线前原图。
- 记录上下标的主体绑定、完整脚标串、位置和来源证据；保留数值、符号及单位运算关系。
- 对有原始矢量分数线依据的数字分数生成显式 `(5/9)` 等表示；带分数使用 `(1+1/2)`。
- 先生成完整全文；存在明确、连续的印刷卷标题时，再额外保存每卷一个 Markdown，全文继续保留。
- 提供逐页字符分配审计，以及独立读取源 PDF 和落盘 Markdown 的检查器。
- 输出包先暂存再提交，支持覆盖保护及可捕获写入失败的回滚。

印刷目录在 Markdown 内使用两列 HTML 表格展示：标题保留层级缩进，右列保存原书页码。长引导点显示为紧凑省略号，完整引导字符保存在 `data-source-text` 和结构记录中。原书页码与 PDF 物理页码分别记录，不猜测二者的偏移关系。不确定的目录片段保留原文并标记复核；扫描目录需要另行识别，不能仅凭图像自动建立可靠的文字目录。

## 从零开始：Windows / PowerShell

### 1. 准备项目和 Python

安装 **Python 3.11 或更新版本**，并确认命令可用：

```powershell
py --version
```

安装 Git 后，可以从公开仓库获取项目：

```powershell
git clone https://github.com/AlexYoungster/PDF-MD-Converter.git
Set-Location PDF-MD-Converter
```

也可以在 GitHub 页面选择 **Code → Download ZIP**，解压后进入含有 `pyproject.toml` 的项目根目录，例如：

```powershell
Set-Location F:\PDF-MD-Converter
```

如果电脑只有 `python` 命令，将下面创建环境时的 `py` 换成 `python`。

### 2. 新建虚拟环境并安装

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m document_converter --help
```

这些命令直接使用环境内的 Python，**不需要激活环境或修改 PowerShell 执行策略**。安装依赖需要网络。基础 PDF 转换在本地 CPU 上运行，不需要 GPU、API 密钥或模型服务。

### 3. 把 PDF 放进 origin

将要转换的文件放到项目根目录的 `origin/`，例如：

```text
PDF-MD-Converter/
  origin/
    我的文档.pdf
  output/
  pyproject.toml
```

也可以使用 PowerShell 复制：

```powershell
New-Item -ItemType Directory -Force origin, output | Out-Null
Copy-Item -LiteralPath "C:\Users\你的用户名\Documents\我的文档.pdf" -Destination ".\origin\我的文档.pdf"
```

`origin/` 是约定的输入位置，不会自动扫描或转换其中的所有文件；每次命令明确指定一个输入。也可以直接指定项目外部 PDF 的绝对路径。带空格或中文的路径请使用引号。

### 4. 执行转换

```powershell
.\.venv\Scripts\python.exe -m document_converter ".\origin\我的文档.pdf" --output ".\output\我的文档.md"
```

等价的命令入口：

```powershell
.\.venv\Scripts\pdf-md-converter.exe ".\origin\我的文档.pdf" -o ".\output\我的文档.md"
.\.venv\Scripts\doc2md.exe --help
```

只需选择一种转换命令。大文件会在终端打印页数进度，结束时返回输出位置。已有同名结果时默认拒绝覆盖；确认需要更新时追加 `--overwrite`。

### 5. 查看输出

```text
output/
  我的文档.md                  # 原始完整全文
  我的文档.audit.json          # 来源哈希、页数、字符分配及异常
  我的文档_assets/
    source_我的文档.pdf        # 原文件副本
    structure.jsonl           # 逐页网格、单元格、脚标和分数证据
    tables/                   # 表格图；发生补线时另存补线前图
    pages/                    # 低文本页原图或按选项生成的页图
  我的文档_volumes/            # 仅识别到明确卷标题时存在
    README.md                 # 分卷索引
    我的文档_第1卷.md
    我的文档_第2卷.md
```

使用支持内嵌 HTML、`<details>` 和相对图片路径的 Markdown 阅读器预览。复杂表格不只使用竖线式 Markdown：正文展示表格图，展开区域提供可检索的 HTML 单元格。不同阅读器的 HTML 支持可能不同。

移动或交付转换结果时，请同时带上全文、审计、附件目录及已有分卷目录；仅复制一个 `.md` 会丢失图片和原文链接。普通 PDF 没有卷标题时只生成全文，不会人为拆成卷。

### 6. 独立检查结果

基础安装即可运行不带 OCR 的全文检查：

```powershell
.\.venv\Scripts\python.exe -m document_converter.fidelity ".\origin\我的文档.pdf" ".\output\我的文档.md" --output ".\output\我的文档_check" --ocr none
```

输出 `检查报告.md`、`summary.json`、`findings.jsonl`、`pages.jsonl` 和来源区域图。检查会重新读取原 PDF 的字符与已引用图像，定位缺页、字符差异、单元格内容差异、数学结构和图像问题。

- `error`：确定性差异或未能完成的检查。
- `review`：阅读顺序、脚标候选、网格、扫描内容等仍需对照原图判断。
- 检查退出码 `0` 仅表示未发现 `error`，仍可能是 `review_required`；`1` 表示有 `error`；`2` 表示无法启动或完成检查。
- 转换命令退出码 `0` 表示已生成结果包，`2` 表示转换失败；生成成功不等于所有语义都已确认。

不要仅凭“字符无遗漏”判定数值关系、阅读顺序或公式语义全部正确。

## 没有 PDF 时：运行自带示例

示例脚本使用基础依赖生成原创的三页 PDF，演示段落、合并表格、科学计数法、脚标、数字分数和分卷，不需要下载手册：

```powershell
.\.venv\Scripts\python.exe examples\create_demo.py
.\.venv\Scripts\python.exe -m document_converter origin\demo.pdf -o output\demo.md
.\.venv\Scripts\python.exe -m document_converter.fidelity origin\demo.pdf output\demo.md -o output\demo_check --ocr none
```

示例生成器默认不覆盖已有 `origin/demo.pdf`；需要重新生成时追加 `--overwrite`。其内容用于展示格式，不构成工程参数建议。

## macOS / Linux

在项目根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
.venv/bin/python examples/create_demo.py
.venv/bin/python -m document_converter "origin/demo.pdf" -o "output/demo.md"
.venv/bin/python -m document_converter.fidelity "origin/demo.pdf" "output/demo.md" -o "output/demo_check" --ocr none
```

本次交付在 Windows 环境验证；其他平台请运行测试确认本机依赖与渲染行为。

## 可选 OCR、页图与 Office 支持

扫描 PDF 可安装 OCR 附加依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install ".[ocr]"
.\.venv\Scripts\python.exe -m document_converter origin\scan.pdf -o output\scan.md --ocr auto
```

`--ocr auto` 对少于 80 个原生非空白字符的页面补充 OCR；`--ocr all` 对全部页面识别；默认 `none`。OCR 输出位于明确标注的候选区域，不覆盖原生正文，也不等同于扫描表格的完整行列重建。RapidOCR 的默认模型通常随依赖包提供；使用自定义模型或依赖行为变化时，按相应配置准备资源。

独立检查器的 OCR 参数不同：`none` / `risk` / `all`，默认 `risk`。使用基础安装时按上文显式传入 `--ocr none`；安装 OCR 依赖后可用 `risk` 对风险页及默认 12 个分层样本识别，也可用 `all`。抽样检查不是全书视觉验收。

需要每页原图时：

```powershell
.\.venv\Scripts\python.exe -m document_converter origin\demo.pdf -o output\demo.md --render-pages --render-dpi 150 --overwrite
```

即使不指定该选项，少于 40 个原生非空白字符的页也会保存原页图。转换渲染 DPI 和 OCR DPI 范围为 36–600；独立检查器 OCR DPI 范围为 72–300。页图和原文件副本会增加磁盘占用。

项目还保留 HTML、CSV、TXT、MD 的基础转换，以及可选的 DOCX、PPTX、XLSX 解析器：

```powershell
.\.venv\Scripts\python.exe -m pip install ".[office]"
.\.venv\Scripts\python.exe -m document_converter origin\input.docx -o output\input.md
```

Office 解析不具备 PDF 表格的全部几何恢复能力；原文件副本用于核对浮动对象、图表和复杂版式。详见[能力边界](docs/limitations.md)。

## 在 Python 中调用

```python
from document_converter import convert_document

result = convert_document(
    "origin/demo.pdf",
    "output/demo.md",
    overwrite=False,
    ocr="none",
)
print(result["markdown"])
print(result["audit"])
```

输入与输出路径相对当前工作目录解析。输出参数以 `.md` 结尾时作为文件路径，否则作为输出目录；省略输出参数时写到输入文件旁。

给大模型或检索系统提供纯文本时，应先解析脚标结构再去掉 HTML。`script_witness.linear_cell()` 可生成 `d_{k}` 等表示；`get_text()` 会将字母脚标展平。示例见[数据接口](docs/data-contract.md)。

## 开发、测试与打包

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m build
```

`dist/` 中生成 wheel 和源码包，可在其他机器使用 `python -m pip install 路径\pdf_md_converter-0.1.0-py3-none-any.whl` 安装。这些包不附带用户原文、历史输出或旧规则提取系统。

通用测试自行创建合成 PDF，不要求原手册。需要本地特定手册的回归测试标记为 `private_source`，缺少来源时跳过；运行方式见[贡献与验证说明](CONTRIBUTING.md)。

`requirements-tested.txt` 记录本次 Windows / Python 3.12 的已测直接依赖版本，可用作安装约束，例如 `python -m pip install -c requirements-tested.txt ".[dev]"`。它不包含全部可选依赖，也不是跨平台锁文件。实际验证结果见[验证记录](docs/verification.md)。

项目结构：

```text
PDF-MD-Converter/
  src/document_converter/    # 全部转换、结构重建、分卷和独立校验代码
  tests/                    # 通用测试与可选私有来源回归
  examples/create_demo.py    # 可复现的原创 PDF 示例生成器
  docs/                     # 架构、数据接口、边界与交付说明
  origin/                   # 用户输入，默认不纳入 Git
  output/                   # 转换结果，默认不纳入 Git
  pyproject.toml            # 安装依赖、命令入口和测试配置
```

算法数据流与模块对应见[架构说明](docs/architecture.md)，迁移和后续 GitHub 交付说明见[交付说明](docs/delivery.md)。

## 常见问题

- **提示找不到模块**：确认在项目根目录完成安装，且执行的是同一个 `.venv` 内的 Python；`pip install -e .` 适合修改代码后继续开发。
- **结果已存在**：检查目标路径，明确需要替换时添加 `--overwrite`。同一输出包请串行处理。
- **无法打开 PDF**：确认文件非空、未损坏；需要口令的加密 PDF 应先提供可访问副本。
- **表格显示成图片或坐标列表**：这是复杂网格的可追溯输出方式；原版式图保留视觉结构，不确定的行列关系会进入审计。
- **扫描页没有成为完整正文**：页图保留不等于 OCR 完成；启用 OCR 也只生成候选文本，需复核。
- **安装环境迁移**：移动项目后重新创建虚拟环境，不要复制旧项目的 `.venv`。

项目当前未指定代码许可证。公开发布或授权分发前，由维护者明确许可证并更新项目元数据；本次整理没有代为选择授权条款。
