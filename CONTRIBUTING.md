# 开发与验证

需要 Python 3.11+。在项目根目录创建虚拟环境后：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m build
```

测试使用合成 PDF 验证正文段落、网格、跨度、补线、单位、分数、脚标来源、分卷和事务回滚。独立检查器还测试数字篡改、单元格互换、页锚缺失和图片差异。修改算法时应增加能揭示实际错误的结构案例，不通过修改特定书名、页码或句子内容绕过问题。

## 可选的私有来源回归

少数测试来自早期开发手册，使用 `private_source` 标记，默认在缺少文件时跳过。运行普通测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not private_source"
```

如自行持有对应手册，可将文件放在本项目 `origin/`，文件名需匹配 `实用机械加工工艺手册 第5版*.pdf`，再运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -m private_source
```

其中一项单位回归还需要该书原转换结果的 `output/document_converter/实用机械加工工艺手册_全文_assets/structure.jsonl`。缺少此证据时仍跳过，不从网络获取。通用安装、示例和转换功能不依赖这些材料。

## 文档和依赖

根目录 `pyproject.toml` 是安装入口，不能依赖其他项目目录、用户计算机绝对路径或旧虚拟环境。保持 Python 导入名 `document_converter`、三个命令入口与 README 一致。版本号同时保存在包 `__init__.py` 和 `pyproject.toml`。

基础依赖提供原生 PDF 转换和不带 OCR 的检查；OCR、Office、开发工具通过 extras 单独安装。`docs/verification.md` 记录本次实际验证覆盖；依赖范围在 `pyproject.toml` 中声明，不把历史机器的全部包列表当成跨平台锁文件。

发布前构建源码包与 wheel，并从 wheel 在项目外目录验证 `--help`、一次转换和独立检查。保留包构建及 `pip check` 结果。不要将 `.venv`、私有原文、模型缓存和生成结果提交到源码仓库。
